from __future__ import annotations

import asyncio
import time
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scilab.api.inputs import InputUploadService
from scilab.identity import Identity
from scilab.runs import worker_main


_IDENTITY = Identity(
    "lab-a", "service:run-worker", frozenset({"runs:read", "runs:write", "lab:admin"})
)


def _environment() -> dict[str, str]:
    return {
        "SCILAB_LAB_ID": "lab-a",
        "SCILAB_DATABASE_URL": "postgresql://worker@db/scilab",
        "SCILAB_HERMES_API_KEY": "lab-key",
        "POD_NAMESPACE": "scilab",
        "SCILAB_NATS_URL": "nats://nats:4222",
        "SCILAB_PI_PROVIDER": "pi-provider",
        "SCILAB_REVIEWER_PROVIDER": "reviewer-provider",
        "SCILAB_MINIO_URL": "https://storage.example:9000",
        "SCILAB_MINIO_ACCESS_KEY": "access",
        "SCILAB_MINIO_SECRET_KEY": "secret",
        "SCILAB_INPUT_BUCKET": "inputs",
        "SCILAB_OPA_URL": "http://opa.internal:8181",
    }


@pytest.mark.parametrize(
    "name",
    (
        "SCILAB_MINIO_URL",
        "SCILAB_MINIO_ACCESS_KEY",
        "SCILAB_MINIO_SECRET_KEY",
        "SCILAB_INPUT_BUCKET",
    ),
)
def test_worker_settings_require_minio_inputs(name: str) -> None:
    environment = _environment()
    environment.pop(name)

    with pytest.raises(ValueError, match=f"{name} is required"):
        worker_main.WorkerSettings.from_environment(environment)


def test_worker_settings_require_opa_url() -> None:
    environment = _environment()
    environment.pop("SCILAB_OPA_URL")
    with pytest.raises(ValueError, match="SCILAB_OPA_URL is required"):
        worker_main.WorkerSettings.from_environment(environment)


def test_approval_expiry_stops_same_vendor_run_without_blocking_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    state_events: list[tuple[Any, ...]] = []

    class Approvals:
        def expire_approvals(self, identity: Identity):
            time.sleep(0.1)
            assert identity.lab_id == "lab-a" and "runs:read" in identity.scopes
            return [type("Approval", (), {"run_id": "run-1"})()]

    class Runs:
        def get(self, identity: Identity, run_id: str):
            assert identity.lab_id == "lab-a" and run_id == "run-1"
            return type("Run", (), {"id": run_id, "hermes_run_id": "hermes-1", "state": worker_main.RunState.CANCELLED, "reason": "approval_expired"})()

    class Client:
        async def stop(self, lab_id: str, run_id: str):
            calls.append((lab_id, run_id))

    class Events:
        async def publish_event(self, *args: Any):
            state_events.append(args)

    async def once(_: float):
        raise asyncio.CancelledError

    original_sleep = asyncio.sleep
    monkeypatch.setattr(worker_main.asyncio, "sleep", once)
    async def probe():
        loop = asyncio.get_running_loop()
        start = loop.time()
        ticker = asyncio.create_task(original_sleep(0.01))
        with pytest.raises(asyncio.CancelledError):
            await worker_main.reconcile_approvals_forever(Approvals(), Runs(), Client(), Events(), _IDENTITY)
        return loop.time() - start, ticker.done()

    elapsed, ticked = asyncio.run(probe())
    assert ticked and elapsed >= 0.1
    assert calls == [("lab-a", "hermes-1")]
    assert state_events[0][2:5] == ("run.state", {"from": "awaiting_approval", "to": "cancelled", "reason": "approval_expired"}, "run-service")


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        statement = " ".join(sql.lower().split())
        if statement == "select 1":
            return
        if "set_config" in statement:
            return
        if statement.startswith("select id, bucket_name, object_key from uploaded_inputs"):
            lab_id, before, limit = params
            self.connection.cleanup_limit = limit
            self.rows = [
                {key: row[key] for key in ("id", "bucket_name", "object_key")}
                for row in self.connection.rows
                if row["lab_id"] == lab_id
                and row["status"] == "pending"
                and row["created_at"] < before
            ][:limit]
            return
        if statement.startswith("delete from uploaded_inputs"):
            lab_id, input_id = params
            self.connection.rows[:] = [
                row
                for row in self.connection.rows
                if not (
                    row["lab_id"] == lab_id
                    and row["id"] == input_id
                    and row["status"] == "pending"
                )
            ]
            return
        raise AssertionError(f"unexpected SQL: {statement}")

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class _Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.cleanup_limit: int | None = None

    def transaction(self):
        return nullcontext()

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def close(self) -> None:
        return None


class _Storage:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.remove_calls: list[tuple[str, str]] = []

    def remove_object(self, bucket: str, object_key: str) -> None:
        self.remove_calls.append((bucket, object_key))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("MinIO unavailable")


def _pending(input_id: str, lab_id: str, created_at: datetime) -> dict[str, Any]:
    return {
        "id": input_id,
        "lab_id": lab_id,
        "status": "pending",
        "bucket_name": "inputs",
        "object_key": f"labs/{lab_id}/inputs/{input_id}",
        "created_at": created_at,
    }


def test_reconciliation_keeps_recent_and_other_lab_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    connection = _Connection(
        [
            _pending("old-a", "lab-a", now - timedelta(minutes=6)),
            _pending("recent-a", "lab-a", now - timedelta(minutes=4)),
            _pending("old-b", "lab-b", now - timedelta(minutes=6)),
        ]
    )
    storage = _Storage()
    service = InputUploadService(connection, storage, bucket="inputs")

    async def stop_after_first_interval(_delay: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker_main.asyncio, "sleep", stop_after_first_interval)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker_main.reconcile_pending_forever(service, _IDENTITY))

    assert storage.remove_calls == [("inputs", "labs/lab-a/inputs/old-a")]
    assert {row["id"] for row in connection.rows} == {"recent-a", "old-b"}
    assert connection.cleanup_limit == 100


def test_reconciliation_retries_storage_failure_without_stopping(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC) - timedelta(minutes=6)
    connection = _Connection([_pending("old-a", "lab-a", now)])
    storage = _Storage(failures=1)
    service = InputUploadService(connection, storage, bucket="inputs")
    intervals: list[float] = []

    async def stop_after_retry(delay: float) -> None:
        intervals.append(delay)
        if len(intervals) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker_main.asyncio, "sleep", stop_after_retry)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker_main.reconcile_pending_forever(service, _IDENTITY))

    assert storage.remove_calls == [
        ("inputs", "labs/lab-a/inputs/old-a"),
        ("inputs", "labs/lab-a/inputs/old-a"),
    ]
    assert connection.rows == []
    assert intervals == [60.0, 60.0]


def test_run_worker_wires_minio_cleanup_and_starts_run_polling(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC) - timedelta(minutes=6)
    run_connection = _Connection([])
    cleanup_connection = _Connection([_pending("old-a", "lab-a", now)])
    approval_connection = _Connection([])
    connections = iter((run_connection, cleanup_connection, approval_connection))
    storage = _Storage()
    minio_calls: list[tuple[Any, ...]] = []

    def connect_db(_url: str, *, autocommit: bool) -> _Connection:
        assert autocommit is True
        return next(connections)

    def create_minio(endpoint: str, *, access_key: str, secret_key: str, secure: bool) -> _Storage:
        minio_calls.append((endpoint, access_key, secret_key, secure))
        return storage

    class _Nats:
        async def close(self) -> None:
            return None

    async def connect_nats(_url: str) -> _Nats:
        return _Nats()

    class _Transport:
        async def __aenter__(self) -> _Transport:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

    poll_started = False
    expiry_connections: list[Any] = []

    async def poll_until_cleanup(_executor: Any, worker_identity: Identity) -> None:
        nonlocal poll_started
        poll_started = True
        assert worker_identity.lab_id == "lab-a"
        assert _executor.approvals.connection is run_connection
        assert _executor.approvals.opa.endpoint == "http://opa.internal:8181"
        while not storage.remove_calls:
            await asyncio.sleep(0.001)
        raise asyncio.CancelledError

    async def observe_expiry(approvals: Any, runs: Any, _client: Any, events: Any, _identity: Identity) -> None:
        expiry_connections.extend((approvals.connection, runs.connection, events.connection))
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_main.psycopg, "connect", connect_db)
    monkeypatch.setattr(worker_main.nats, "connect", connect_nats)
    monkeypatch.setattr(worker_main.httpx, "AsyncClient", lambda **_: _Transport())
    monkeypatch.setattr(worker_main, "Minio", create_minio, raising=False)
    monkeypatch.setattr(worker_main, "poll_forever", poll_until_cleanup)
    monkeypatch.setattr(worker_main, "reconcile_approvals_forever", observe_expiry)

    settings = worker_main.WorkerSettings.from_environment(_environment())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asyncio.wait_for(worker_main.run_worker(settings), timeout=1))

    assert poll_started
    assert minio_calls == [("storage.example:9000", "access", "secret", True)]
    assert storage.remove_calls == [("inputs", "labs/lab-a/inputs/old-a")]
    assert cleanup_connection.rows == []
    assert run_connection is not cleanup_connection
    assert expiry_connections == [approval_connection, approval_connection, approval_connection]


def test_pending_cleanup_does_not_block_run_lease_heartbeat() -> None:
    class SlowInputs:
        def reconcile_pending(self, *_args: object, **_kwargs: object) -> int:
            time.sleep(0.15)
            return 0

    async def probe() -> float:
        loop = asyncio.get_running_loop()
        start = loop.time()
        task = asyncio.create_task(worker_main.reconcile_pending_forever(SlowInputs(), _IDENTITY))
        await asyncio.sleep(0.01)
        elapsed = loop.time() - start
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return elapsed

    assert asyncio.run(probe()) < 0.08
