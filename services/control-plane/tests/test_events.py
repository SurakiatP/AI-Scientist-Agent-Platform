from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scilab.contracts import EventSource, EventType
from scilab.identity import Identity


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def identity(lab_id: str = "lab-a", *scopes: str) -> Identity:
    return Identity(lab_id, f"user:{lab_id}", frozenset(scopes or {"runs:read", "runs:write"}))


class Transaction:
    def __init__(self, database: "Database") -> None:
        self.database = database

    def __enter__(self) -> "Transaction":
        self.database.calls.append(("transaction.enter",))
        return self

    def __exit__(self, *_: object) -> None:
        self.database.committed = True
        self.database.calls.append(("transaction.exit",))


class Cursor:
    def __init__(self, database: "Database") -> None:
        self.database = database
        self.result: list[dict[str, object]] = []

    def __enter__(self) -> "Cursor":
        self.database.calls.append(("cursor.enter",))
        return self

    def __exit__(self, *_: object) -> None:
        self.database.calls.append(("cursor.exit",))

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        compact = " ".join(sql.split()).lower()
        self.database.calls.append(("execute", sql, params))
        if compact.startswith("select set_config"):
            self.database.current_lab_id = params[1]
        elif compact.startswith("select id, lab_id from runs"):
            lab_id, run_id = params
            self.result = [
                {"id": run_id, "lab_id": lab_id}
                for row in self.database.runs
                if row["lab_id"] == lab_id and row["id"] == run_id
            ]
        elif compact.startswith("select event_id, seq from run_events"):
            lab_id, run_id = params
            rows = [
                row
                for row in self.database.events
                if row["lab_id"] == lab_id and row["run_id"] == run_id
            ]
            self.result = sorted(rows, key=lambda row: row["seq"], reverse=True)[:1]
        elif compact.startswith("select event_id, run_id"):
            lab_id, run_id, from_seq = params
            self.result = sorted(
                [
                    row
                    for row in self.database.events
                    if row["lab_id"] == lab_id
                    and row["run_id"] == run_id
                    and row["seq"] > from_seq
                ],
                key=lambda row: row["seq"],
            )
        elif compact.startswith("insert into run_events"):
            event_id, run_id, lab_id, ts, event_type, seq, payload, source = params
            self.database.events.append(
                {
                    "event_id": event_id,
                    "run_id": run_id,
                    "lab_id": lab_id,
                    "ts": ts,
                    "type": event_type,
                    "seq": seq,
                    "payload": json.loads(payload) if isinstance(payload, str) else payload,
                    "source": source,
                }
            )

    def fetchone(self) -> dict[str, object] | None:
        return self.result[0] if self.result else None

    def fetchall(self) -> list[dict[str, object]]:
        return list(self.result)


class Database:
    def __init__(self) -> None:
        self.runs: list[dict[str, object]] = []
        self.events: list[dict[str, object]] = []
        self.calls: list[tuple[object, ...]] = []
        self.current_lab_id: object = None
        self.committed = False

    def transaction(self) -> Transaction:
        self.calls.append(("transaction",))
        self.committed = False
        return Transaction(self)

    def cursor(self) -> Cursor:
        self.calls.append(("cursor",))
        return Cursor(self)

    def add_run(self, run_id: str = "run-1", lab_id: str = "lab-a") -> None:
        self.runs.append({"id": run_id, "lab_id": lab_id})


class Bus:
    def __init__(self, database: Database, *, fail: bool = False) -> None:
        self.database = database
        self.fail = fail
        self.published: list[tuple[str, bytes]] = []
        self.subscriptions: list[str] = []

    async def publish(self, subject: str, data: bytes) -> None:
        assert self.database.committed
        self.published.append((subject, data))
        if self.fail:
            raise RuntimeError("bus unavailable")

    @asynccontextmanager
    async def subscribe(self, subject: str):
        self.subscriptions.append(subject)
        yield anext_never()


async def anext_never():
    await asyncio.Future()


def event_payload() -> dict[str, object]:
    return {"from": "queued", "to": "running", "reason": "started"}


def test_event_service_is_importable() -> None:
    from scilab.events import EventService

    assert EventService


def test_persisted_run_state_event_calls_a2a_push_after_bus_publish() -> None:
    from scilab.events import EventService

    database = Database()
    database.add_run("run-1")
    bus = Bus(database)
    observed: list[object] = []

    async def push(event: object) -> None:
        assert database.committed
        assert len(bus.published) == 1
        observed.append(event)

    service = EventService(database, bus, clock=lambda: NOW, push_notification=push)
    event = asyncio.run(
        service.publish_event(identity(), "run-1", "run.state", event_payload(), "run-service")
    )
    assert observed == [event]


def test_publish_allocates_monotonic_ulids_and_sequences_per_run() -> None:
    from scilab.events import EventService

    database = Database()
    database.add_run("run-1")
    database.add_run("run-2")
    clock_values = iter([NOW, NOW, NOW - timedelta(seconds=1)])
    service = EventService(database, Bus(database), clock=lambda: next(clock_values), entropy=lambda _: b"\x01" * 10)

    first = asyncio.run(service.publish_event(identity(), "run-1", "run.state", event_payload(), "run-service"))
    second = asyncio.run(service.publish_event(identity(), "run-1", "run.state", event_payload(), "run-service"))
    other = asyncio.run(service.publish_event(identity(), "run-2", "run.state", event_payload(), "run-service"))

    assert (first.seq, second.seq, other.seq) == (1, 2, 1)
    assert first.event_id < second.event_id
    assert len(first.event_id) == len(second.event_id) == 26


def test_publish_locks_parent_after_tenant_context_and_replays_exclusively() -> None:
    from scilab.events import EventService

    database = Database()
    database.add_run()
    service = EventService(database, Bus(database), clock=lambda: NOW)
    asyncio.run(service.publish_event(identity(), "run-1", "run.state", event_payload(), "run-service"))
    asyncio.run(service.publish_event(identity(), "run-1", "run.state", event_payload(), "run-service"))

    replayed = service.replay_events(identity("lab-a", "runs:read"), "run-1", from_seq=1)
    assert [event.seq for event in replayed] == [2]
    statements = [call for call in database.calls if call[0] == "execute"]
    assert statements[0][1].lower().startswith("select set_config")
    assert any("for update" in call[1].lower() for call in statements)
    assert database.calls.index(statements[0]) < database.calls.index(next(call for call in statements if "for update" in call[1].lower()))


@pytest.mark.parametrize("run_id", ["", "bad.token", "bad*token", "bad>token", "bad token"])
def test_subject_tokens_are_strict(run_id: str) -> None:
    from scilab.events import EventService

    database = Database()
    database.add_run(run_id or "placeholder")
    service = EventService(database, Bus(database))
    with pytest.raises(ValueError):
        asyncio.run(service.publish_event(identity(), run_id, "run.state", event_payload(), "run-service"))


def test_missing_and_cross_lab_runs_are_indistinguishable() -> None:
    from scilab.events import EventService, RunNotFound

    database = Database()
    database.add_run("run-a", "lab-a")
    service = EventService(database, Bus(database))
    with pytest.raises(RunNotFound) as missing:
        asyncio.run(service.publish_event(identity(), "missing", "run.state", event_payload(), "run-service"))
    with pytest.raises(RunNotFound) as cross_lab:
        asyncio.run(service.publish_event(identity("lab-b"), "run-a", "run.state", event_payload(), "run-service"))
    assert type(missing.value) is type(cross_lab.value)


def test_authorization_requires_write_and_read_scopes() -> None:
    from scilab.events import EventService
    from scilab.tenancy import AuthorizationError

    database = Database()
    database.add_run()
    service = EventService(database, Bus(database))
    with pytest.raises(AuthorizationError):
        asyncio.run(service.publish_event(identity("lab-a", "runs:read"), "run-1", "run.state", event_payload(), "run-service"))
    with pytest.raises(AuthorizationError):
        service.replay_events(identity("lab-a", "runs:write"), "run-1")


def test_tool_secret_redaction_happens_before_sql_and_does_not_mutate_input() -> None:
    from scilab.events import EventService

    database = Database()
    database.add_run()
    bus = Bus(database)
    service = EventService(database, bus, clock=lambda: NOW)
    payload = {
        "tool": "search",
        "args_redacted": {"nested": {"password": "do-not-leak", "safe": "keep"}, "api_key": "do-not-leak"},
        "duration_ms": 1,
        "ok": True,
        "error": None,
    }
    original = json.loads(json.dumps(payload))
    event = asyncio.run(service.publish_event(identity(), "run-1", "tool.started", payload, "sandbox"))

    assert payload == original
    assert event.model_dump(mode="json", by_alias=True)["payload"]["args_redacted"]["nested"]["password"] == "[REDACTED]"
    assert all("do-not-leak" not in repr(call) for call in database.calls)
    assert all(b"do-not-leak" not in data for _, data in bus.published)


def test_raw_tool_args_are_rejected() -> None:
    from scilab.events import EventService

    database = Database()
    database.add_run()
    service = EventService(database, Bus(database))
    payload = {"tool": "search", "args": {"password": "secret"}, "duration_ms": 1, "ok": True, "error": None}
    with pytest.raises(ValueError):
        asyncio.run(service.publish_event(identity(), "run-1", "tool.started", payload, "sandbox"))


def test_database_commit_precedes_bus_and_failure_keeps_event() -> None:
    from scilab.events import EventFanoutError, EventService

    database = Database()
    database.add_run()
    bus = Bus(database, fail=True)
    service = EventService(database, bus, clock=lambda: NOW)
    with pytest.raises(EventFanoutError) as error:
        asyncio.run(service.publish_event(identity(), "run-1", "run.state", event_payload(), "run-service"))
    assert error.value.event.event_id == database.events[0]["event_id"]
    assert len(database.events) == 1
    assert bus.published


def test_migration_contains_composite_fk_constraints_and_forced_rls() -> None:
    migration = Path("services/control-plane/migrations/003_run_events.sql").read_text().lower()
    compact = " ".join(migration.split())
    assert "create unique index if not exists runs_id_lab_uidx on runs (id, lab_id)" in compact
    assert "primary key" in compact
    assert "foreign key (run_id, lab_id) references runs (id, lab_id) on delete cascade" in compact
    assert "unique (lab_id, run_id, seq)" in compact
    assert "jsonb_typeof(payload) = 'object'" in compact
    assert "alter table run_events enable row level security" in compact
    assert "alter table run_events force row level security" in compact
    assert "using (lab_id = current_setting('scilab.current_lab_id', true))" in compact
    assert "with check (lab_id = current_setting('scilab.current_lab_id', true))" in compact
