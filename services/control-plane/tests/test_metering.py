from __future__ import annotations

import asyncio
import json
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scilab.audit import AuditService, redact_sensitive
from scilab.contracts import RunEvent
from scilab.identity import Identity
from scilab.metering import MeteringService, parse_model_prices
from scilab.runs.state import RunStateError
from scilab.telemetry import (
    correlation_context,
    current_correlation,
    extracted_correlation,
    inject_correlation,
)


ROOT = Path(__file__).parents[3]
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def identity(lab_id: str = "lab-a", *scopes: str) -> Identity:
    return Identity(
        lab_id,
        "user:alice",
        frozenset(scopes or {"runs:read", "runs:write", "lab:admin"}),
    )


class Cursor:
    def __init__(self, database: "Database") -> None:
        self.database = database
        self.result: list[Any] = []
        self.rowcount = -1

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        compact = " ".join(sql.split()).lower()
        self.database.calls.append((compact, params))
        self.result = []
        self.rowcount = -1

        if compact.startswith("select set_config"):
            self.database.current_lab_id = params[1]
        elif compact.startswith("select id, lab_id, state, reason, budget_thb from runs"):
            run = self.database.runs.get((params[0], params[1]))
            self.result = [run] if run else []
        elif compact.startswith("select id, lab_id, idempotency_key, state"):
            run = self.database.runs.get((params[0], params[1]))
            self.result = [run] if run else []
        elif compact.startswith("update runs set"):
            columns = (
                "state", "reason", "retry_count", "max_minutes", "hermes_run_id",
                "created_at", "updated_at", "queued_at", "running_since",
                "runtime_used", "last_heartbeat_at", "approval_expires_at", "context_id",
                "budget_thb",
            )
            values = dict(zip(columns, params[:14], strict=True))
            run = self.database.runs[(params[14], params[15])]
            run.update(values)
            self.rowcount = 1
        elif compact.startswith("insert into lab_budgets"):
            lab_id, budget_thb, configured_at = params
            self.database.budgets[lab_id] = (budget_thb, configured_at)
            self.rowcount = 1
        elif compact.startswith("select budget_thb from lab_budgets"):
            budget = self.database.budgets.get(params[0])
            self.result = [(budget[0],)] if budget else []
        elif compact.startswith("insert into metering_usage"):
            columns = (
                "usage_id", "run_id", "lab_id", "actor", "source", "tokens_in",
                "tokens_out", "compute", "llm_cost_thb", "compute_cost_thb", "model",
                "metadata", "recorded_at",
            )
            row = dict(zip(columns, params, strict=True))
            row["metadata"] = json.loads(row["metadata"])
            self.database.usage.append(row)
            self.rowcount = 1
        elif compact.startswith(
            "select usage_id, run_id, lab_id, actor, source, tokens_in, tokens_out"
        ):
            rows = self._usage_rows(compact, params)
            columns = (
                "usage_id", "run_id", "lab_id", "actor", "source", "tokens_in",
                "tokens_out", "compute", "llm_cost_thb", "compute_cost_thb", "recorded_at",
            )
            self.result = [tuple(row[column] for column in columns) for row in rows]
        elif compact.startswith("select coalesce(sum(tokens_in)"):
            rows = self._usage_rows(compact, params)
            priced_llm_cost = sum(
                row["llm_cost_thb"] for row in rows if row["llm_cost_thb"] is not None
            )
            if "sum(compute)" in compact:
                self.result = [(
                    sum(row["tokens_in"] for row in rows),
                    sum(row["tokens_out"] for row in rows),
                    sum(row["compute"] for row in rows),
                    priced_llm_cost,
                    sum(row["compute_cost_thb"] for row in rows),
                )]
            elif "sum(llm_cost_thb)" in compact:
                self.result = [(
                    sum(row["tokens_in"] for row in rows),
                    sum(row["tokens_out"] for row in rows),
                    priced_llm_cost,
                    sum(row["compute_cost_thb"] for row in rows),
                )]
            else:
                self.result = [(
                    sum(row["tokens_in"] for row in rows),
                    sum(row["tokens_out"] for row in rows),
                )]
        elif compact.startswith(
            "select coalesce(sum(llm_cost_thb + compute_cost_thb)"
        ):
            rows = [row for row in self.database.usage if row["lab_id"] == params[0]]
            self.result = [(
                sum(
                    row["llm_cost_thb"] + row["compute_cost_thb"]
                    for row in rows
                    if row["llm_cost_thb"] is not None
                ),
            )]
        elif compact.startswith("insert into audit_events"):
            columns = (
                "audit_id", "run_id", "lab_id", "actor", "source", "action",
                "details", "created_at",
            )
            row = dict(zip(columns, params, strict=True))
            row["details"] = json.loads(row["details"])
            self.database.audit.append(row)
            self.rowcount = 1
        elif compact.startswith("insert into metering_event_outbox"):
            columns = (
                "usage_id", "run_id", "lab_id", "payload", "created_at",
            )
            row = dict(zip(columns, params, strict=True))
            row["payload"] = json.loads(row["payload"])
            row["delivered_at"] = None
            self.database.outbox[row["usage_id"]] = row
            self.rowcount = 1
        elif compact.startswith("select run_id, payload from metering_event_outbox"):
            row = self.database.outbox.get(params[1])
            if row and row["lab_id"] == params[0] and row["delivered_at"] is None:
                self.result = [(row["run_id"], row["payload"])]
        elif compact.startswith("select usage_id from metering_event_outbox"):
            self.result = [
                (row["usage_id"],)
                for row in self.database.outbox.values()
                if row["lab_id"] == params[0] and row["delivered_at"] is None
            ]
        elif compact.startswith("update metering_event_outbox set delivered_at"):
            delivered_at, lab_id, usage_id = params
            row = self.database.outbox.get(usage_id)
            if row and row["lab_id"] == lab_id and row["delivered_at"] is None:
                row["delivered_at"] = delivered_at
                self.rowcount = 1
        elif compact.startswith(
            "select audit_id, run_id, lab_id, actor, source, action, details, created_at"
        ):
            columns = ["lab_id"]
            columns.extend(
                name
                for name in ("run_id", "actor", "source")
                if f"{name} = %s" in compact
            )
            rows = [
                row
                for row in self.database.audit
                if all(
                    row[column] == value
                    for column, value in zip(columns, params, strict=True)
                )
            ]
            self.result = [
                (
                    row["audit_id"], row["run_id"], row["lab_id"], row["actor"],
                    row["source"], row["action"], json.dumps(row["details"]), row["created_at"],
                )
                for row in rows
            ]

    def _usage_rows(self, sql: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        columns = ["lab_id"]
        columns.extend(
            name for name in ("run_id", "actor", "source") if f"{name} = %s" in sql
        )
        return [
            row
            for row in self.database.usage
            if all(
                row[column] == value
                for column, value in zip(columns, params, strict=True)
            )
        ]

    def fetchone(self) -> Any:
        return self.result[0] if self.result else None

    def fetchall(self) -> list[Any]:
        return list(self.result)


class Database:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.current_lab_id: str | None = None
        self.lock = threading.RLock()
        self.runs = {
            ("lab-a", "run-1"): {
                "id": "run-1", "lab_id": "lab-a", "idempotency_key": "key-a",
                "state": "running", "reason": None, "retry_count": 0,
                "max_minutes": 120, "hermes_run_id": "hermes-a",
                "created_at": NOW, "updated_at": NOW, "queued_at": NOW,
                "running_since": NOW, "runtime_used": timedelta(0),
                "last_heartbeat_at": NOW, "approval_expires_at": None,
                "context_id": None, "budget_thb": None,
            },
            ("lab-b", "run-2"): {
                "id": "run-2", "lab_id": "lab-b", "idempotency_key": "key-b",
                "state": "running", "reason": None, "retry_count": 0,
                "max_minutes": 120, "hermes_run_id": "hermes-b",
                "created_at": NOW, "updated_at": NOW, "queued_at": NOW,
                "running_since": NOW, "runtime_used": timedelta(0),
                "last_heartbeat_at": NOW, "approval_expires_at": None,
                "context_id": None, "budget_thb": None,
            },
        }
        self.usage: list[dict[str, Any]] = []
        self.budgets: dict[str, tuple[float, datetime]] = {}
        self.audit: list[dict[str, Any]] = []
        self.outbox: dict[str, dict[str, Any]] = {}

    @contextmanager
    def transaction(self):
        with self.lock:
            yield

    @contextmanager
    def cursor(self):
        yield Cursor(self)


class EventSink:
    def __init__(self, database: Database | None = None) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.database = database
        self.states_at_publish: list[str] = []

    def publish_event(
        self,
        identity: Identity,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        source: str,
    ) -> None:
        if self.database is not None:
            self.states_at_publish.append(
                self.database.runs[(identity.lab_id, run_id)]["state"]
            )
        self.events.append((identity, run_id, event_type, payload, source))


class FailingEventSink:
    def publish_event(self, *_: object) -> None:
        raise RuntimeError("event unavailable")


def record(
    meter: MeteringService,
    who: Identity,
    run_id: str,
    *,
    actor: str = "user:alice",
    source: str = "litellm",
    tokens: object = 100,
    compute: float = 1.0,
    llm_cost_thb: float = 1.0,
    compute_cost_thb: float = 0.0,
):
    return meter.record_usage(
        who,
        run_id,
        actor,
        source,
        tokens,
        compute,
        llm_cost_thb,
        compute_cost_thb,
    )


def test_usage_is_append_only_tenant_scoped_and_queryable_by_dimensions() -> None:
    database = Database()
    meter = MeteringService(database, clock=lambda: NOW)

    record(meter, identity(), "run-1", llm_cost_thb=2.0)
    record(
        meter,
        identity(),
        "run-1",
        actor="provider:model",
        source="compute",
        tokens=20,
        compute=0.5,
        llm_cost_thb=0,
        compute_cost_thb=1,
    )
    record(meter, identity("lab-b"), "run-2", llm_cost_thb=9)

    rows = meter.query_usage(
        identity(), run_id="run-1", actor="user:alice", source="litellm"
    )
    assert len(rows) == 1
    assert rows[0].llm_cost_thb == 2
    assert rows[0].compute_cost_thb == 0
    assert len(database.usage) == 3
    assert database.usage[-1]["lab_id"] == "lab-b"


def test_explicit_cost_components_aggregate_without_default_rate_or_currency() -> None:
    database = Database()
    meter = MeteringService(database, clock=lambda: NOW)

    record(meter, identity(), "run-1", tokens={"tokens_in": 80, "tokens_out": 20}, llm_cost_thb=2)
    record(
        meter,
        identity(),
        "run-1",
        tokens={"tokens_in": 40, "tokens_out": 10},
        compute=0.5,
        llm_cost_thb=2,
        compute_cost_thb=1.25,
    )

    total = meter.aggregate_usage(identity(), run_id="run-1")
    assert (total.tokens_in, total.tokens_out, total.tokens) == (120, 30, 150)
    assert total.compute == 1.5
    assert (total.llm_cost_thb, total.compute_cost_thb, total.cost_thb) == (4, 1.25, 5.25)
    assert total.budget_thb is None
    assert all(
        row["payload"]["budget_remaining_thb"] is None
        for row in database.outbox.values()
    )


def test_budget_exhaustion_is_atomic_and_event_is_delivered_after_transition() -> None:
    database = Database()
    events = EventSink(database)
    meter = MeteringService(database, event_service=events, clock=lambda: NOW)
    meter.set_lab_budget(identity(), 5)

    record(meter, identity(), "run-1", llm_cost_thb=4)
    record(meter, identity(), "run-1", llm_cost_thb=1, compute_cost_thb=1)

    assert database.runs[("lab-a", "run-1")]["state"] == "cancelled"
    assert database.runs[("lab-a", "run-1")]["reason"] == "budget_exhausted"
    assert events.states_at_publish[-1] == "cancelled"
    assert len(events.events) == 2
    assert events.events[-1][2] == "cost.updated"
    assert events.events[-1][3] == {
        "tokens_in": 200,
        "tokens_out": 0,
        "llm_cost_thb": 5.0,
        "compute_cost_thb": 1.0,
        "budget_remaining_thb": 0.0,
    }
    with pytest.raises(RunStateError):
        record(meter, identity(), "run-1")


def test_run_budget_records_one_overshoot_then_denies_later_usage() -> None:
    database = Database()
    database.runs[("lab-a", "run-1")]["budget_thb"] = 5
    events = EventSink(database)
    meter = MeteringService(database, event_service=events, clock=lambda: NOW)

    record(meter, identity(), "run-1", llm_cost_thb=4)
    crossing_charge = record(meter, identity(), "run-1", llm_cost_thb=2)

    assert crossing_charge.cost_thb == 2
    assert sum(row["llm_cost_thb"] for row in database.usage) == 6
    assert database.runs[("lab-a", "run-1")]["state"] == "cancelled"
    assert database.runs[("lab-a", "run-1")]["reason"] == "budget_exhausted"
    assert events.states_at_publish == ["running", "cancelled"]

    with pytest.raises(RunStateError):
        record(meter, identity(), "run-1", llm_cost_thb=0.25)

    assert len(database.usage) == len(database.audit) == len(database.outbox) == 2


def test_event_failure_keeps_committed_cancellation_and_retryable_outbox() -> None:
    database = Database()
    meter = MeteringService(
        database, event_service=FailingEventSink(), clock=lambda: NOW
    )
    meter.set_lab_budget(identity(), 0)

    with pytest.raises(RuntimeError, match="event unavailable"):
        record(meter, identity(), "run-1")

    assert database.runs[("lab-a", "run-1")]["state"] == "cancelled"
    usage_id = database.usage[0]["usage_id"]
    assert database.outbox[usage_id]["delivered_at"] is None

    delivered = EventSink(database)
    meter.event_service = delivered
    assert meter.deliver_pending_events(identity()) == 1
    assert database.outbox[usage_id]["delivered_at"] == NOW
    assert len(delivered.events) == 1


def test_missing_event_service_leaves_retryable_outbox_after_cancellation() -> None:
    database = Database()
    meter = MeteringService(database, clock=lambda: NOW)
    meter.set_lab_budget(identity(), 0)

    usage = record(meter, identity(), "run-1")

    assert database.runs[("lab-a", "run-1")]["state"] == "cancelled"
    assert database.outbox[usage.usage_id]["delivered_at"] is None


def test_budget_check_locks_budget_row_and_serializes_concurrent_usage() -> None:
    database = Database()
    events = EventSink(database)
    meter = MeteringService(database, event_service=events, clock=lambda: NOW)
    meter.set_lab_budget(identity(), 5)

    failures: list[BaseException] = []

    def write_usage() -> None:
        try:
            record(meter, identity(), "run-1", llm_cost_thb=3)
        except BaseException as exc:  # pragma: no cover - assertion reports the cause
            failures.append(exc)

    threads = [threading.Thread(target=write_usage) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert database.runs[("lab-a", "run-1")]["reason"] == "budget_exhausted"
    assert sum(row["llm_cost_thb"] for row in database.usage) == 6
    assert any(
        sql.startswith("select budget_thb from lab_budgets")
        and sql.endswith("for update")
        for sql, _ in database.calls
    )


def test_record_model_usage_priced_computes_thb_and_publishes_run_service_source() -> None:
    database = Database()
    events = EventSink(database)
    meter = MeteringService(
        database, event_service=events, clock=lambda: NOW,
        prices={"gpt-x": {"in": 10.0, "out": 30.0}},
    )

    record = asyncio.run(meter.record_model_usage_async(
        identity(), "run-1", actor="user:alice", source="hermes", model="gpt-x",
        tokens_in=1_000_000, tokens_out=500_000, metadata={"cost_usd": 0.5},
    ))

    assert record.llm_cost_thb == 25.0
    assert record.compute_cost_thb == 0.0
    assert record.compute == 0.0
    assert database.usage[-1]["model"] == "gpt-x"
    assert database.usage[-1]["metadata"] == {"cost_usd": 0.5}
    assert database.runs[("lab-a", "run-1")]["state"] == "running"

    assert len(events.events) == 1
    identity_arg, run_id, event_type, payload, source = events.events[0]
    assert (run_id, event_type, source) == ("run-1", "cost.updated", "run-service")
    assert "warning" not in payload
    assert meter.run_token_totals(identity(), "run-1") == (1_000_000, 500_000)


def test_record_model_usage_unpriced_model_warns_and_does_not_cancel_run() -> None:
    database = Database()
    events = EventSink(database)
    meter = MeteringService(database, event_service=events, clock=lambda: NOW, prices={})

    record = asyncio.run(meter.record_model_usage_async(
        identity(), "run-1", actor="user:alice", source="hermes", model="mystery-model",
        tokens_in=100, tokens_out=50,
    ))

    assert record.llm_cost_thb is None
    assert record.cost_thb == 0.0
    assert database.runs[("lab-a", "run-1")]["state"] == "running"
    payload = events.events[-1][3]
    assert payload["warning"] == "unpriced_model:mystery-model"


def test_cost_updated_payload_from_unpriced_usage_validates_as_run_event() -> None:
    database = Database()
    events = EventSink(database)
    meter = MeteringService(database, event_service=events, clock=lambda: NOW, prices={})

    asyncio.run(meter.record_model_usage_async(
        identity(), "run-1", actor="user:alice", source="hermes", model="mystery-model",
        tokens_in=100, tokens_out=50,
    ))

    _, run_id, event_type, payload, source = events.events[-1]
    event = RunEvent.model_validate({
        "event_id": "01ARZ3NDEKTSV4RRFFQ69G5FAV",
        "run_id": run_id,
        "lab_id": "lab-a",
        "ts": NOW,
        "type": event_type,
        "seq": 1,
        "payload": payload,
        "source": source,
    })
    assert event.payload.warning == "unpriced_model:mystery-model"
    assert event.payload.llm_cost_thb == 0.0


def test_budget_exhaustion_counts_only_priced_usage() -> None:
    database = Database()
    events = EventSink(database)
    meter = MeteringService(
        database, event_service=events, clock=lambda: NOW,
        prices={"m": {"in": 10.0, "out": 0.0}},
    )
    meter.set_lab_budget(identity(), 5)

    asyncio.run(meter.record_model_usage_async(
        identity(), "run-1", actor="user:alice", source="hermes", model="m",
        tokens_in=400_000, tokens_out=0,
    ))
    assert database.runs[("lab-a", "run-1")]["state"] == "running"

    asyncio.run(meter.record_model_usage_async(
        identity(), "run-1", actor="user:alice", source="hermes", model="unknown",
        tokens_in=10_000_000, tokens_out=0,
    ))
    assert database.runs[("lab-a", "run-1")]["state"] == "running"

    asyncio.run(meter.record_model_usage_async(
        identity(), "run-1", actor="user:alice", source="hermes", model="m",
        tokens_in=100_000, tokens_out=0,
    ))
    assert database.runs[("lab-a", "run-1")]["state"] == "cancelled"
    assert database.runs[("lab-a", "run-1")]["reason"] == "budget_exhausted"


def test_parse_model_prices_accepts_empty_object_and_rejects_invalid_input() -> None:
    assert parse_model_prices("{}") == {}
    assert parse_model_prices('{"m": {"in": 1, "out": 2.5}}') == {
        "m": {"in": 1.0, "out": 2.5}
    }

    for bad in ("not json", "[]", '{"m": 1}', '{"m": {"in": -1, "out": 0}}',
                '{"m": {"in": 1}}', '{"": {"in": 1, "out": 2}}'):
        with pytest.raises(ValueError):
            parse_model_prices(bad)


def test_audit_is_tenant_scoped_and_redacts_sensitive_keys_and_raw_args() -> None:
    database = Database()
    audit = AuditService(database, clock=lambda: NOW)
    details = {
        "args_redacted": {
            "query": "safe",
            "api_key": "secret-value",
            "nested": {"password": "pw"},
        },
        "safe": "kept",
    }

    audit.append(
        identity(), "run-1", "user:alice", "litellm", "model.request", details
    )
    rows = audit.query(
        identity(), run_id="run-1", actor="user:alice", source="litellm"
    )

    assert len(rows) == 1
    assert "secret-value" not in repr(rows)
    assert "pw" not in repr(rows)
    assert rows[0].details["args_redacted"]["api_key"] == "[REDACTED]"
    assert redact_sensitive({"authorization": "bearer secret", "value": "ok"}) == {
        "authorization": "[REDACTED]",
        "value": "ok",
    }
    with pytest.raises(ValueError, match="raw tool args"):
        audit.append(
            identity(), "run-1", "user:alice", "litellm", "bad", {"args": {"x": 1}}
        )


def test_telemetry_round_trips_run_and_lab_through_w3c_baggage() -> None:
    carrier: dict[str, str] = {}

    with correlation_context(run_id="run-1", lab_id="lab-a"):
        inject_correlation(carrier)
    assert current_correlation() == {}
    assert "baggage" in carrier

    with extracted_correlation(
        carrier, expected_run_id="run-1", expected_lab_id="lab-a"
    ) as extracted:
        assert extracted == {"run_id": "run-1", "lab_id": "lab-a"}
        assert current_correlation() == extracted
    assert current_correlation() == {}

    with pytest.raises(ValueError, match="authenticated context"):
        with extracted_correlation(
            carrier, expected_run_id="run-1", expected_lab_id="lab-b"
        ):
            pass


def test_metering_migration_is_additive_rls_and_immutable() -> None:
    migration = (
        ROOT / "services/control-plane/migrations/006_metering_audit.sql"
    ).read_text().lower()
    compact = " ".join(migration.split())

    assert "create table if not exists metering_usage" in compact
    assert "create table if not exists audit_events" in compact
    assert "create table if not exists lab_budgets" in compact
    assert "create table if not exists metering_event_outbox" in compact
    assert "alter table metering_usage enable row level security" in compact
    assert "alter table audit_events enable row level security" in compact
    assert "create policy metering_usage_tenant" in compact
    assert "create policy audit_events_tenant" in compact
    assert "create policy metering_event_outbox_tenant" in compact
    assert "before update or delete on metering_usage" in compact
    assert "before update or delete on audit_events" in compact
    assert "drop table" not in compact


def test_observability_config_declares_correlation_and_no_budget_default() -> None:
    config = (
        ROOT / "deploy/helm/scilab/templates/observability.yaml"
    ).read_text()

    for required in (
        "Langfuse", "Prometheus", "Grafana", "Loki", "LiteLLM", "run_id", "lab_id"
    ):
        assert required.lower() in config.lower()
    assert "requireVirtualKey: true" in config
    assert "defaultBudgetTHB: null" in config
