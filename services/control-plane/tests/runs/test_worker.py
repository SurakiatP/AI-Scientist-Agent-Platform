from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from scilab.identity import Identity
from scilab.runs.model import RunState
from scilab.runs.service import RunService
from scilab.tenancy import AuthorizationError

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)
ROOT = Path(__file__).parents[4]


def worker_api() -> Any:
    try:
        from scilab.runs import worker
    except ImportError as exc:
        pytest.fail(f"Run worker module is missing: {exc}", pytrace=False)
    return worker


def worker_identity(lab_id: str = "lab-a", *scopes: str) -> Identity:
    return Identity(lab_id, "service:run-worker", frozenset(scopes or {"runs:write"}))


def queued_run(
    run_id: str = "run-a",
    lab_id: str = "lab-a",
    *,
    queued_at: datetime = NOW,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "lab_id": lab_id,
        "idempotency_key": f"key-{run_id}",
        "state": "queued",
        "reason": None,
        "retry_count": 0,
        "max_minutes": 120,
        "hermes_run_id": None,
        "created_at": queued_at,
        "updated_at": queued_at,
        "queued_at": queued_at,
        "running_since": None,
        "runtime_used": timedelta(0),
        "last_heartbeat_at": None,
        "approval_expires_at": None,
        "context_id": None,
        "budget_thb": None,
        "request_payload": payload or {"goal": "study", "options": {}},
        "actor": "user:researcher",
        "worker_claim_token": None,
        "worker_lease_expires_at": None,
    }


class Transaction:
    def __init__(self, database: Database) -> None:
        self.database = database

    def __enter__(self) -> Transaction:
        self.rows = deepcopy(self.database.rows)
        self.tenant = self.database.current_lab_id
        return self

    def __exit__(self, exc_type: object, *_: object) -> None:
        if exc_type is not None:
            self.database.rows = self.rows
        self.database.current_lab_id = self.tenant


class Cursor:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.result: dict[str, Any] | None = None

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        compact = " ".join(sql.split()).lower()
        self.database.calls.append((compact, params))
        if compact.startswith("select set_config"):
            self.database.current_lab_id = params[1]
            self.result = None
            return

        if compact.startswith("select ") and "worker_claim_token = %s" in compact:
            lab_id, run_id, token = params
            row = next(
                (
                    candidate
                    for candidate in self.database.rows
                    if candidate["lab_id"] == lab_id
                    and candidate["id"] == run_id
                    and candidate["lab_id"] == self.database.current_lab_id
                    and candidate["worker_claim_token"] == token
                    and candidate["worker_lease_expires_at"] > self.database.now
                ),
                None,
            )
            self.result = dict(row) if row is not None else None
            return

        if compact.startswith("select ") and "from runs" in compact and "for update" in compact:
            lab_id, run_id = params
            row = next(
                (
                    candidate
                    for candidate in self.database.rows
                    if candidate["lab_id"] == lab_id
                    and candidate["id"] == run_id
                    and candidate["lab_id"] == self.database.current_lab_id
                ),
                None,
            )
            self.result = dict(row) if row is not None else None
            return

        if compact.startswith("with candidate as"):
            assert "for update skip locked" in compact
            lab_id, token, lease_seconds = params
            candidates = [
                row
                for row in self.database.rows
                if row["lab_id"] == lab_id
                and row["lab_id"] == self.database.current_lab_id
                        and (
                            (row["state"] == "queued" and row["request_payload"] is not None)
                            or (row["state"] == "running" and row["hermes_run_id"] is not None)
                        )
                        and (
                            row["state"] == "running"
                            or row["request_payload"].get("options", {}) == {}
                        )
                        and (
                            row["worker_lease_expires_at"] is None
                            or row["worker_lease_expires_at"] <= self.database.now
                )
            ]
            candidates.sort(key=lambda row: (row["queued_at"], row["id"]))
            if not candidates:
                self.result = None
                return
            row = candidates[0]
            row["worker_claim_token"] = token
            row["worker_lease_expires_at"] = self.database.now + timedelta(
                seconds=lease_seconds
            )
            self.result = dict(row)
            return

        if compact.startswith("update runs"):
            if "set state =" in compact:
                fields = RunService._columns[3:]
                values = params[: len(fields)]
                lab_id, run_id = params[-2:]
                row = next(
                    (
                        candidate
                        for candidate in self.database.rows
                        if candidate["lab_id"] == lab_id and candidate["id"] == run_id
                    ),
                    None,
                )
                if row is not None:
                    row.update(zip(fields, values, strict=True))
                self.result = None
                return
            if "set worker_lease_expires_at" in compact:
                lease_seconds, lab_id, run_id, token = params
            else:
                lab_id, run_id, token = params
                lease_seconds = None
            row = next(
                (
                    candidate
                    for candidate in self.database.rows
                    if candidate["lab_id"] == lab_id
                    and candidate["id"] == run_id
                    and candidate["lab_id"] == self.database.current_lab_id
                    and candidate["worker_claim_token"] == token
                    and candidate["worker_lease_expires_at"] > self.database.now
                    and (
                        "and state in ('queued', 'running')" not in compact
                        or candidate["state"] in {"queued", "running"}
                    )
                ),
                None,
            )
            if row is None:
                self.result = None
            elif lease_seconds is not None:
                row["worker_lease_expires_at"] = self.database.now + timedelta(
                    seconds=lease_seconds
                )
                row["last_heartbeat_at"] = self.database.now
                self.result = {"worker_lease_expires_at": row["worker_lease_expires_at"]}
            else:
                row["worker_claim_token"] = None
                row["worker_lease_expires_at"] = None
                self.result = {"id": run_id}
            return

        raise AssertionError(f"unexpected SQL: {compact}")

    def fetchone(self) -> dict[str, Any] | None:
        return self.result


class Database:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.current_lab_id: str | None = None
        self.now = NOW

    def transaction(self) -> Transaction:
        return Transaction(self)

    def cursor(self) -> Cursor:
        return Cursor(self)


def test_claim_is_tenant_scoped_and_sets_a_durable_lease() -> None:
    other_lab_run = queued_run("run-b", "lab-b")
    database = Database([queued_run(), other_lab_run])
    api = worker_api()
    claim = api.RunWorker(database).claim_next(worker_identity())

    assert claim is not None
    assert claim.run.id == "run-a"
    assert claim.request_payload["options"] == {}
    assert claim.actor == "user:researcher"
    assert claim.lease_expires_at == NOW + timedelta(seconds=api.LEASE_SECONDS)
    assert database.rows[0]["worker_claim_token"] == claim.token
    assert database.calls[0][1] == ("scilab.current_lab_id", "lab-a")
    claim_sql = next(sql for sql, _ in database.calls if sql.startswith("with candidate"))
    assert "lab_id = %s" in claim_sql
    assert "for update skip locked" in claim_sql
    assert other_lab_run["worker_claim_token"] is None


def test_active_lease_cannot_be_claimed_by_another_worker() -> None:
    database = Database([queued_run()])
    worker = worker_api().RunWorker(database)
    first = worker.claim_next(worker_identity())

    second = worker.claim_next(worker_identity())

    assert first is not None
    assert second is None
    assert database.rows[0]["worker_claim_token"] == first.token
    claim_sql = next(sql for sql, _ in database.calls if sql.startswith("with candidate"))
    assert "and ( (state = 'queued' and request_payload is not null)" in claim_sql
    assert "or (state = 'running' and hermes_run_id is not null) ) and (" in claim_sql
    assert "worker_lease_expires_at is null or worker_lease_expires_at <=" in claim_sql


def test_cancelled_runs_are_not_claimed() -> None:
    run = queued_run()
    run["state"] = "cancelled"
    database = Database([run])

    assert worker_api().RunWorker(database).claim_next(worker_identity()) is None
    assert run["worker_claim_token"] is None


def test_unsupported_stored_options_are_not_reclaimed() -> None:
    run = queued_run(payload={"goal": "study", "options": {"seed": 7}})
    database = Database([run])

    claim = worker_api().RunWorker(database).claim_next(worker_identity())

    assert claim is None
    assert run["state"] == "queued"
    assert run["worker_claim_token"] is None
    claim_sql = next(sql for sql, _ in database.calls if sql.startswith("with candidate"))
    assert "coalesce(request_payload -> 'options', '{}'::jsonb) = '{}'::jsonb" in claim_sql


def test_expired_lease_is_reclaimed_with_a_new_fencing_token() -> None:
    database = Database([queued_run()])
    api = worker_api()
    worker = api.RunWorker(database)
    first = worker.claim_next(worker_identity())
    assert first is not None
    database.now = first.lease_expires_at

    recovered = worker.claim_next(worker_identity())

    assert recovered is not None
    assert recovered.run.id == first.run.id
    assert recovered.token != first.token
    assert recovered.lease_expires_at == database.now + timedelta(seconds=api.LEASE_SECONDS)


def test_running_claim_with_null_payload_returns_empty_mapping_instead_of_raising() -> None:
    run = queued_run()
    run["state"] = "running"
    run["hermes_run_id"] = "hermes-1"
    run["request_payload"] = None
    database = Database([run])

    claim = worker_api().RunWorker(database).claim_next(worker_identity())

    assert claim is not None
    assert claim.request_payload == {}
    assert claim.actor == "user:researcher"


def test_expired_running_lease_recovers_the_existing_hermes_run() -> None:
    run = queued_run()
    run["state"] = "running"
    run["hermes_run_id"] = "hermes-1"
    run["running_since"] = NOW
    run["worker_claim_token"] = uuid4()
    run["worker_lease_expires_at"] = NOW
    previous_token = run["worker_claim_token"]
    database = Database([run])
    worker = worker_api().RunWorker(database)

    claim = worker.claim_next(worker_identity())

    assert claim is not None
    assert claim.run.state.value == "running"
    assert claim.run.hermes_run_id == "hermes-1"
    assert claim.token != previous_token


def test_stale_claim_cannot_transition_after_running_claim_recovery() -> None:
    database = Database([queued_run()])
    worker = worker_api().RunWorker(database)
    stale = worker.claim_next(worker_identity())
    assert stale is not None
    database.now = stale.lease_expires_at
    current = worker.claim_next(worker_identity())
    assert current is not None

    updated = worker.transition(
        worker_identity(), stale, RunState.RUNNING, hermes_run_id="late-hermes-run"
    )

    assert updated is None
    assert database.rows[0]["state"] == "queued"
    assert database.rows[0]["worker_claim_token"] == current.token


def test_current_claim_transitions_run_through_run_service() -> None:
    database = Database([queued_run()])
    worker = worker_api().RunWorker(database)
    claim = worker.claim_next(worker_identity())
    assert claim is not None

    updated = worker.transition(
        worker_identity(), claim, RunState.RUNNING, hermes_run_id="hermes-1"
    )

    assert updated is not None
    assert updated.state is RunState.RUNNING
    assert updated.hermes_run_id == "hermes-1"
    assert database.rows[0]["state"] == "running"
    assert database.rows[0]["worker_claim_token"] == claim.token


def test_stale_claim_cannot_renew_or_release_recovered_lease() -> None:
    database = Database([queued_run()])
    worker = worker_api().RunWorker(database)
    stale = worker.claim_next(worker_identity())
    assert stale is not None
    database.now = stale.lease_expires_at
    current = worker.claim_next(worker_identity())
    assert current is not None

    assert worker.renew(worker_identity(), stale) is None
    assert worker.release(worker_identity(), stale) is False
    assert database.rows[0]["worker_claim_token"] == current.token


def test_current_claim_can_renew_and_release() -> None:
    database = Database([queued_run()])
    worker = worker_api().RunWorker(database)
    claim = worker.claim_next(worker_identity())
    assert claim is not None
    database.now += timedelta(seconds=10)

    renewed = worker.renew(worker_identity(), claim)

    assert renewed is not None
    assert renewed == claim.lease_expires_at + timedelta(seconds=10)
    assert worker.release(worker_identity(), claim)
    assert database.rows[0]["worker_claim_token"] is None
    assert database.rows[0]["worker_lease_expires_at"] is None


def test_claim_requires_write_scope_before_touching_database() -> None:
    database = Database([queued_run()])

    with pytest.raises(AuthorizationError, match="runs:write"):
        worker_api().RunWorker(database).claim_next(
            worker_identity("lab-a", "runs:read")
        )

    assert database.calls == []


def test_worker_lease_migration_is_additive_and_idempotent() -> None:
    migration_path = ROOT / "services/control-plane/migrations/013_run_worker_lease.sql"
    assert migration_path.is_file()
    migration = migration_path.read_text()
    compact = " ".join(migration.lower().split())

    assert "alter table runs add column if not exists worker_claim_token uuid" in compact
    assert "add column if not exists worker_lease_expires_at timestamptz" in compact
    assert "create index if not exists runs_worker_claim_idx" in compact
    assert "where state = 'queued' and request_payload is not null" in compact
    assert "drop " not in compact
