from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scilab.identity import Identity
from scilab.runs.model import RunState
from scilab.runs.service import RunNotFound, RunService
from scilab.tenancy import AuthorizationError


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def identity(lab_id: str = "lab-a", *scopes: str) -> Identity:
    return Identity(lab_id, f"user:{lab_id}", frozenset(scopes or {"runs:read", "runs:write"}))


class Transaction:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        self.calls.append(("transaction.enter",))
        return self

    def __exit__(self, *_):
        self.calls.append(("transaction.exit",))


class Cursor:
    def __init__(self, database):
        self.database = database
        self.result = None

    def __enter__(self):
        self.database.calls.append(("cursor.enter",))
        return self

    def __exit__(self, *_):
        self.database.calls.append(("cursor.exit",))

    def execute(self, sql, params=()):
        self.database.calls.append(("execute", sql, params))
        compact = " ".join(sql.split()).lower()
        if compact.startswith("select set_config"):
            self.database.current_lab_id = params[1]
        elif compact.startswith("insert into runs"):
            self.result = self.database.insert(params)
        elif compact.startswith("select request_payload, actor from runs"):
            row = next((row for row in self.database.rows if row["lab_id"] == params[0] and row["id"] == params[1]), None)
            self.result = None if row is None else (row.get("request_payload"), row.get("actor"))
        elif compact.startswith("select") and "for update" in compact:
            self.result = self.database.find(params[0], params[1])
        elif compact.startswith("select") and "order by" in compact:
            self.result = self.database.list_lab(params[0])
        elif compact.startswith("select") and (
            "where lab_id = %s and idempotency_key = %s" in compact
        ):
            self.result = self.database.find_key(params[0], params[1])
        elif compact.startswith("select"):
            self.result = self.database.find(params[0], params[1])
        elif compact.startswith("update runs set request_payload"):
            payload, actor, lab_id, run_id = params
            row = next(row for row in self.database.rows if row["lab_id"] == lab_id and row["id"] == run_id)
            row["request_payload"] = payload
            row["actor"] = actor
            self.result = (run_id,)
        elif compact.startswith("update runs"):
            self.database.update(params)

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result or []


class Database:
    columns = (
        "id",
        "lab_id",
        "idempotency_key",
        "state",
        "reason",
        "retry_count",
        "max_minutes",
        "hermes_run_id",
        "created_at",
        "updated_at",
        "queued_at",
        "running_since",
        "runtime_used",
        "last_heartbeat_at",
        "approval_expires_at",
        "context_id",
        "budget_thb",
    )

    def __init__(self):
        self.rows = []
        self.calls = []
        self.current_lab_id = None

    def transaction(self):
        self.calls.append(("transaction",))
        return Transaction(self.calls)

    def cursor(self):
        self.calls.append(("cursor",))
        return Cursor(self)

    def insert(self, params):
        row = dict(zip(self.columns, params))
        row.setdefault("context_id", None)
        if not any(
            existing["lab_id"] == row["lab_id"]
            and existing["idempotency_key"] == row["idempotency_key"]
            for existing in self.rows
        ):
            self.rows.append(row)
            return (row["id"],)
        return None

    def find(self, lab_id, run_id):
        return next(
            (tuple(row[column] for column in self.columns) for row in self.rows
             if row["lab_id"] == lab_id and row["id"] == run_id),
            None,
        )

    def find_key(self, lab_id, key):
        return next(
            (tuple(row[column] for column in self.columns) for row in self.rows
             if row["lab_id"] == lab_id and row["idempotency_key"] == key),
            None,
        )

    def list_lab(self, lab_id):
        rows = [row for row in self.rows if row["lab_id"] == lab_id]
        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        return [tuple(row[column] for column in self.columns) for row in rows]

    def update(self, params):
        values = dict(zip(self.columns[3:], params[:-2]))
        lab_id, run_id = params[-2:]
        row = next(row for row in self.rows if row["lab_id"] == lab_id and row["id"] == run_id)
        row.update(values)


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


def test_duplicate_key_returns_original_and_does_not_change_budget():
    database = Database()
    clock = Clock()
    service = RunService(database, clock=clock)

    original = service.create(identity(), "same-key", max_minutes=10)
    duplicate = service.create(identity(), "same-key", max_minutes=99)

    assert duplicate == original
    assert len(database.rows) == 1
    assert database.rows[0]["max_minutes"] == 10


def test_run_budget_persists_across_duplicate_create_and_service_restart():
    database = Database()
    service = RunService(database, clock=Clock())

    original = service.create(identity(), "budget-key", budget_thb=25)
    duplicate = service.create(identity(), "budget-key", budget_thb=50)
    restarted = RunService(database, clock=Clock())

    assert duplicate == original
    assert duplicate.budget_thb == 25
    assert restarted.get(identity("lab-a", "runs:read"), original.id).budget_thb == 25


def test_run_without_budget_is_unbounded():
    run = RunService(Database(), clock=Clock()).create(identity(), "unbounded-key")

    assert run.budget_thb is None


def test_create_rejects_negative_run_budget_before_database_access():
    database = Database()

    with pytest.raises(ValueError, match="budget_thb"):
        RunService(database, clock=Clock()).create(identity(), "invalid-budget", budget_thb=-1)

    assert database.calls == []


def test_run_budget_migration_adds_nullable_nonnegative_column():
    migration_path = (
        Path(__file__).parents[4]
        / "services/control-plane/migrations/009_run_budget.sql"
    )
    sql = "\n".join(
        line.split("--", 1)[0] for line in migration_path.read_text().splitlines()
    )

    assert " ".join(sql.split()).lower() == (
        "alter table runs add column if not exists budget_thb numeric "
        "check (budget_thb >= 0);"
    )


def test_same_key_is_allowed_in_another_lab_and_list_is_newest_first():
    database = Database()
    clock = Clock()
    service = RunService(database, clock=clock)
    first = service.create(identity("lab-a"), "same-key")
    clock.value = NOW + timedelta(seconds=1)
    second = service.create(identity("lab-b"), "same-key")

    assert second.id != first.id
    assert [run.id for run in service.list(identity("lab-a", "runs:read"))] == [first.id]


def test_list_tie_breaks_by_id_descending_after_created_at():
    database = Database()
    service = RunService(database, clock=Clock())
    first = service.create(identity(), "first")
    second = service.create(identity(), "second")

    listed = service.list(identity("lab-a", "runs:read"))

    assert [run.id for run in listed] == sorted([first.id, second.id], reverse=True)


def test_stop_sets_cancelled_state_and_stopped_reason():
    database = Database()
    service = RunService(database, clock=Clock())
    run = service.create(identity(), "key")

    stopped = service.stop(identity(), run.id)

    assert stopped.state is RunState.CANCELLED
    assert stopped.reason == "stopped"


def test_every_transaction_sets_tenant_context_before_query():
    database = Database()
    RunService(database, clock=Clock()).create(identity(), "key")

    statements = [call for call in database.calls if call[0] == "execute"]
    assert statements[0][1].lower().startswith("select set_config")
    assert statements[0][2] == ("scilab.current_lab_id", "lab-a")


def test_mutations_lock_tenant_row_and_retry_same_run():
    database = Database()
    service = RunService(database, clock=Clock())
    run = service.create(identity(), "key")
    service.transition(identity(), run.id, RunState.RUNNING, hermes_run_id="hermes-1")
    service.transition(identity(), run.id, RunState.FAILED, reason="error")
    retried = service.retry(identity(), run.id)

    assert retried.id == run.id
    assert retried.retry_count == 1
    assert any("for update" in call[1].lower() for call in database.calls if call[0] == "execute")
    assert len(database.rows) == 1


def test_missing_and_cross_lab_runs_are_indistinguishable():
    database = Database()
    service = RunService(database, clock=Clock())
    run = service.create(identity("lab-a"), "key")

    with pytest.raises(RunNotFound) as missing:
        service.get(identity("lab-a", "runs:read"), "not-found")
    with pytest.raises(RunNotFound) as cross_lab:
        service.get(identity("lab-b", "runs:read"), run.id)

    assert str(missing.value) == str(cross_lab.value)


def test_scope_is_checked_before_database_access():
    database = Database()
    service = RunService(database, clock=Clock())

    with pytest.raises(AuthorizationError):
        service.create(identity("lab-a", "runs:read"), "key")
    assert database.calls == []


def test_a2a_context_id_round_trips_without_changing_idempotency():
    database = Database()
    service = RunService(database, clock=Clock())

    original = service.create(identity(), "a2a-key", context_id="context-1")
    duplicate = service.create(identity(), "a2a-key", context_id="context-2")

    assert duplicate == original
    restarted = RunService(database, clock=Clock())
    assert restarted.get(identity(), original.id).context_id == "context-1"
    assert restarted.list(identity("lab-a", "runs:read"))[0].context_id == "context-1"
    assert restarted.stop(identity(), original.id).context_id == "context-1"


def test_a2a_context_migration_is_nullable_and_additive():
    migration_path = Path("services/control-plane/migrations/007_a2a_context.sql")
    assert migration_path.exists(), "Wave13 context migration is missing"
    migration = migration_path.read_text().lower()
    assert "alter table runs add column if not exists context_id text" in migration
    assert "context_id text not null" not in migration
    assert "create table" not in migration
    assert "alter table runs drop column context_id" in migration


def test_migration_declares_run_constraints_index_and_rls():
    migration = Path("services/control-plane/migrations/002_runs.sql").read_text().lower()
    compact = " ".join(migration.split())

    for state in ("queued", "running", "awaiting_approval", "completed", "failed", "cancelled"):
        assert state in compact
    assert "references labs(id) on delete cascade" in compact
    assert "btrim(id) <> ''" in compact
    assert "btrim(lab_id) <> ''" in compact
    assert "btrim(idempotency_key) <> ''" in compact
    assert "unique (lab_id, idempotency_key)" in compact
    assert "retry_count between 0 and 2" in compact
    assert "max_minutes > 0" in compact
    assert "runtime_used >= interval '0 seconds'" in compact
    assert "create index" in compact and "created_at desc" in compact
    assert "alter table runs enable row level security" in compact
    assert "alter table runs force row level security" in compact
    assert "using (lab_id = current_setting('scilab.current_lab_id', true))" in compact
    assert "with check (lab_id = current_setting('scilab.current_lab_id', true))" in compact
