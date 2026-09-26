from __future__ import annotations

import asyncio
import time
from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from scilab.api.inputs import InputUploadService
from scilab.identity import Identity
from scilab.runs import worker_main
from scilab.runs.model import Run, RunState


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
        "SCILAB_MODEL_PRICES_THB": '{"sci-pi-frontier": {"in": 10, "out": 20}}',
        "SCILAB_ARTIFACT_BUCKET": "artifacts",
        "SCILAB_HERMES_IMAGE": "registry.internal/hermes:16",
        "SCILAB_SKILLS_IMAGE": "registry.internal/skills:16",
        "SCILAB_HERMES_CONFIG_SHA256": "a" * 64,
        "SCILAB_SANDBOX_IMAGE": "registry.internal/sandbox:16",
    }


@pytest.mark.parametrize(
    "name",
    (
        "SCILAB_MINIO_URL",
        "SCILAB_MINIO_ACCESS_KEY",
        "SCILAB_MINIO_SECRET_KEY",
        "SCILAB_INPUT_BUCKET",
        "SCILAB_ARTIFACT_BUCKET",
        "SCILAB_HERMES_IMAGE",
        "SCILAB_SKILLS_IMAGE",
        "SCILAB_HERMES_CONFIG_SHA256",
        "SCILAB_SANDBOX_IMAGE",
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


def test_worker_settings_require_model_prices() -> None:
    environment = _environment()
    environment.pop("SCILAB_MODEL_PRICES_THB")
    with pytest.raises(ValueError, match="SCILAB_MODEL_PRICES_THB is required"):
        worker_main.WorkerSettings.from_environment(environment)


def test_worker_settings_reject_invalid_model_prices_json() -> None:
    environment = _environment()
    environment["SCILAB_MODEL_PRICES_THB"] = "not json"
    with pytest.raises(ValueError, match="model prices must be valid JSON"):
        worker_main.WorkerSettings.from_environment(environment)


def test_worker_settings_parses_model_prices() -> None:
    settings = worker_main.WorkerSettings.from_environment(_environment())
    assert settings.model_prices == {"sci-pi-frontier": {"in": 10.0, "out": 20.0}}


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


def _active_run(
    run_id: str = "run-1",
    *,
    state: RunState,
    queued_at: datetime,
    running_since: datetime | None = None,
    last_heartbeat_at: datetime | None = None,
    max_minutes: int = 120,
    retry_count: int = 0,
    hermes_run_id: str | None = None,
) -> Run:
    return Run(
        id=run_id,
        lab_id="lab-a",
        idempotency_key=f"key-{run_id}",
        state=state,
        reason=None,
        retry_count=retry_count,
        max_minutes=max_minutes,
        hermes_run_id=hermes_run_id,
        created_at=queued_at,
        updated_at=queued_at,
        queued_at=queued_at,
        running_since=running_since,
        runtime_used=timedelta(0),
        last_heartbeat_at=last_heartbeat_at,
        approval_expires_at=None,
    )


class _FakeRuns:
    def __init__(
        self, runs: list[Run], leases: dict[str, datetime | None] | None = None
    ) -> None:
        self._runs = {run.id: run for run in runs}
        self._leases = leases or {}
        self.transitions: list[tuple[str, RunState, str]] = []
        self.retried: list[str] = []
        self.lease_queries: list[str] = []

    def list_active(self, identity: Identity) -> list[Run]:
        assert identity.lab_id == "lab-a" and "runs:read" in identity.scopes
        return list(self._runs.values())

    def lease_expires_at(self, identity: Identity, run_id: str) -> datetime | None:
        assert identity.lab_id == "lab-a" and "runs:read" in identity.scopes
        self.lease_queries.append(run_id)
        return self._leases.get(run_id)

    def transition(self, identity: Identity, run_id: str, target: RunState, *, reason: str) -> Run:
        self.transitions.append((run_id, target, reason))
        updated = replace(self._runs[run_id], state=target, reason=reason)
        self._runs[run_id] = updated
        return updated

    def retry(self, identity: Identity, run_id: str) -> Run:
        self.retried.append(run_id)
        current = self._runs[run_id]
        updated = replace(
            current, state=RunState.QUEUED, reason=None, retry_count=current.retry_count + 1
        )
        self._runs[run_id] = updated
        return updated


class _FakeStopClient:
    def __init__(self) -> None:
        self.stopped: list[tuple[str, str]] = []

    async def stop(self, lab_id: str, run_id: str) -> None:
        self.stopped.append((lab_id, run_id))


class _RecordEvents:
    def __init__(self) -> None:
        self.published: list[tuple[Any, ...]] = []

    async def publish_event(self, *args: Any) -> None:
        self.published.append(args)


def _run_reconcile_once(runs: _FakeRuns, events: _RecordEvents, client: _FakeStopClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def once(_: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(worker_main.asyncio, "sleep", once)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker_main.reconcile_runs_forever(runs, events, client, _IDENTITY))


def test_reconcile_runs_forever_fails_queued_run_on_queue_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    run = _active_run(state=RunState.QUEUED, queued_at=now - timedelta(minutes=31))
    runs = _FakeRuns([run])
    events = _RecordEvents()
    client = _FakeStopClient()

    _run_reconcile_once(runs, events, client, monkeypatch)

    assert runs.transitions == [("run-1", RunState.FAILED, "queue_timeout")]
    assert runs.retried == []
    assert client.stopped == []
    assert [event[2] for event in events.published] == ["run.state"]
    assert events.published[0][3] == {"from": "queued", "to": "failed", "reason": "queue_timeout"}


def test_reconcile_runs_forever_retries_heartbeat_loss_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    run = _active_run(
        state=RunState.RUNNING,
        queued_at=now - timedelta(hours=1),
        running_since=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(seconds=200),
        hermes_run_id="hermes-1",
        retry_count=0,
    )
    runs = _FakeRuns([run], leases={"run-1": now - timedelta(seconds=5)})
    events = _RecordEvents()
    client = _FakeStopClient()

    _run_reconcile_once(runs, events, client, monkeypatch)

    assert runs.transitions == [("run-1", RunState.FAILED, "heartbeat_loss")]
    assert runs.retried == ["run-1"]
    assert client.stopped == [("lab-a", "hermes-1")]
    assert [event[2] for event in events.published] == ["run.state", "run.state"]
    assert events.published[1][3] == {"from": "failed", "to": "queued", "reason": "heartbeat_loss"}


def test_reconcile_runs_forever_skips_unclaimed_stale_heartbeat(monkeypatch: pytest.MonkeyPatch) -> None:
    """Q25: a RUNNING Run no worker has claimed (e.g. resumed after approval,
    waiting for the single per-Lab worker) must keep waiting rather than being
    failed, even though its stale last_heartbeat_at alone satisfies the 90s
    check in due_transition."""
    now = datetime.now(UTC)
    run = _active_run(
        state=RunState.RUNNING,
        queued_at=now - timedelta(hours=1),
        running_since=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(seconds=200),
        hermes_run_id="hermes-1",
    )
    runs = _FakeRuns([run])
    events = _RecordEvents()
    client = _FakeStopClient()

    _run_reconcile_once(runs, events, client, monkeypatch)

    assert runs.transitions == []
    assert runs.retried == []
    assert client.stopped == []
    assert events.published == []
    assert runs.lease_queries == ["run-1"]


def test_reconcile_runs_forever_skips_claimed_run_with_live_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worker still holding a live claim lease is not crashed; its Run must
    not be failed even with a stale last_heartbeat_at (e.g. mid a long
    synchronous completion step that has not renewed the lease yet)."""
    now = datetime.now(UTC)
    run = _active_run(
        state=RunState.RUNNING,
        queued_at=now - timedelta(hours=1),
        running_since=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(seconds=200),
        hermes_run_id="hermes-1",
    )
    runs = _FakeRuns([run], leases={"run-1": now + timedelta(seconds=30)})
    events = _RecordEvents()
    client = _FakeStopClient()

    _run_reconcile_once(runs, events, client, monkeypatch)

    assert runs.transitions == []
    assert runs.retried == []
    assert client.stopped == []
    assert events.published == []


def test_reconcile_runs_forever_never_retries_run_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    run = _active_run(
        state=RunState.RUNNING,
        queued_at=now - timedelta(hours=1),
        running_since=now - timedelta(minutes=2),
        last_heartbeat_at=now,
        max_minutes=1,
        hermes_run_id="hermes-1",
    )
    runs = _FakeRuns([run])
    events = _RecordEvents()
    client = _FakeStopClient()

    _run_reconcile_once(runs, events, client, monkeypatch)

    assert runs.transitions == [("run-1", RunState.FAILED, "run_timeout")]
    assert runs.retried == []
    assert client.stopped == [("lab-a", "hermes-1")]


def test_reconcile_runs_forever_stops_retrying_after_two_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    run = _active_run(
        state=RunState.RUNNING,
        queued_at=now - timedelta(hours=1),
        running_since=now - timedelta(minutes=5),
        last_heartbeat_at=now - timedelta(seconds=200),
        hermes_run_id="hermes-1",
        retry_count=2,
    )
    runs = _FakeRuns([run], leases={"run-1": now - timedelta(seconds=5)})
    events = _RecordEvents()
    client = _FakeStopClient()

    _run_reconcile_once(runs, events, client, monkeypatch)

    assert runs.transitions == [("run-1", RunState.FAILED, "heartbeat_loss")]
    assert runs.retried == []
    assert client.stopped == [("lab-a", "hermes-1")]


def test_reconcile_runs_forever_skips_races_and_keeps_looping(monkeypatch: pytest.MonkeyPatch) -> None:
    from scilab.runs.state import RunStateError

    now = datetime.now(UTC)
    run = _active_run(state=RunState.QUEUED, queued_at=now - timedelta(minutes=31))

    class RacyRuns(_FakeRuns):
        def transition(self, identity: Identity, run_id: str, target: RunState, *, reason: str) -> Run:
            super().transition(identity, run_id, target, reason=reason)
            raise RunStateError("already transitioned by another worker")

    runs = RacyRuns([run])
    events = _RecordEvents()
    client = _FakeStopClient()

    _run_reconcile_once(runs, events, client, monkeypatch)

    assert runs.transitions == [("run-1", RunState.FAILED, "queue_timeout")]
    assert events.published == []
    assert client.stopped == []


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
    reconcile_connection = _Connection([])
    connections = iter((run_connection, cleanup_connection, approval_connection, reconcile_connection))
    storage = _Storage()
    minio_calls: list[tuple[Any, ...]] = []

    def connect_db(_url: str, *, autocommit: bool) -> _Connection:
        assert autocommit is True
        return next(connections)

    def create_minio(endpoint: str, *, access_key: str, secret_key: str, secure: bool) -> _Storage:
        minio_calls.append((endpoint, access_key, secret_key, secure))
        return storage

    class _S3:
        def __init__(self) -> None:
            self.head_bucket_calls: list[str] = []

        def head_bucket(self, *, Bucket: str) -> None:
            self.head_bucket_calls.append(Bucket)

    s3 = _S3()
    s3_calls: list[dict[str, Any]] = []

    def create_s3(service: str, **kwargs: Any) -> _S3:
        assert service == "s3"
        s3_calls.append(kwargs)
        return s3

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
    reconcile_calls: list[Any] = []

    async def poll_until_cleanup(_executor: Any, worker_identity: Identity) -> None:
        nonlocal poll_started
        poll_started = True
        assert worker_identity.lab_id == "lab-a"
        assert _executor.approvals.connection is run_connection
        assert _executor.approvals.opa.endpoint == "http://opa.internal:8181"
        assert _executor.metering.connection is run_connection
        assert _executor.metering.run_service.connection is run_connection
        assert _executor.metering.audit_service.connection is run_connection
        assert _executor.metering.prices == {"sci-pi-frontier": {"in": 10.0, "out": 20.0}}
        assert _executor.artifacts.connection is run_connection
        assert _executor.artifacts.storage.client is s3
        assert _executor.artifacts.storage.bucket == "artifacts"
        assert _executor.manifests.connection is run_connection
        assert _executor.manifests.artifacts is _executor.artifacts
        assert _executor.hermes_image == "registry.internal/hermes:16"
        assert _executor.hermes_config_sha256 == "a" * 64
        assert _executor.skills_image == "registry.internal/skills:16"
        assert _executor.sandbox_image == "registry.internal/sandbox:16"
        while not storage.remove_calls:
            await asyncio.sleep(0.001)
        raise asyncio.CancelledError

    async def observe_expiry(approvals: Any, runs: Any, _client: Any, events: Any, _identity: Identity) -> None:
        expiry_connections.extend((approvals.connection, runs.connection, events.connection))
        await asyncio.Event().wait()

    async def observe_reconcile(runs: Any, events: Any, _client: Any, worker_identity: Identity) -> None:
        assert worker_identity.lab_id == "lab-a"
        reconcile_calls.extend((runs.connection, events.connection))
        await asyncio.Event().wait()

    monkeypatch.setattr(worker_main.psycopg, "connect", connect_db)
    monkeypatch.setattr(worker_main.nats, "connect", connect_nats)
    monkeypatch.setattr(worker_main.httpx, "AsyncClient", lambda **_: _Transport())
    monkeypatch.setattr(worker_main, "Minio", create_minio, raising=False)
    monkeypatch.setattr(worker_main.boto3, "client", create_s3)
    monkeypatch.setattr(worker_main, "poll_forever", poll_until_cleanup)
    monkeypatch.setattr(worker_main, "reconcile_approvals_forever", observe_expiry)
    monkeypatch.setattr(worker_main, "reconcile_runs_forever", observe_reconcile)

    settings = worker_main.WorkerSettings.from_environment(_environment())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asyncio.wait_for(worker_main.run_worker(settings), timeout=1))

    assert poll_started
    assert minio_calls == [("storage.example:9000", "access", "secret", True)]
    assert s3_calls == [
        {
            "endpoint_url": "https://storage.example:9000",
            "aws_access_key_id": "access",
            "aws_secret_access_key": "secret",
            "region_name": "us-east-1",
        }
    ]
    assert s3.head_bucket_calls == ["artifacts"]
    assert storage.remove_calls == [("inputs", "labs/lab-a/inputs/old-a")]
    assert cleanup_connection.rows == []
    assert run_connection is not cleanup_connection
    assert expiry_connections == [approval_connection, approval_connection, approval_connection]
    assert reconcile_calls == [reconcile_connection, reconcile_connection]


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
