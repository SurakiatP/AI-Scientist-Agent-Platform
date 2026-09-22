from __future__ import annotations

import asyncio
import inspect
import json
import math
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from scilab.audit import AuditService
from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.runs.model import RunState
from scilab.runs.service import RunNotFound, RunService
from scilab.runs.state import RunStateError
from scilab.tenancy import require_scope


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    return value


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{field} must be a non-negative finite number")
    return number


def _tokens(value: object) -> tuple[int, int]:
    if isinstance(value, bool):
        raise ValueError("tokens must be a non-negative integer or token mapping")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("tokens must be non-negative")
        return value, 0
    if isinstance(value, Mapping):
        tokens_in = value.get("tokens_in", value.get("input"))
        tokens_out = value.get("tokens_out", value.get("output"))
        if not isinstance(tokens_in, int) or isinstance(tokens_in, bool) or tokens_in < 0:
            raise ValueError("tokens_in must be a non-negative integer")
        if not isinstance(tokens_out, int) or isinstance(tokens_out, bool) or tokens_out < 0:
            raise ValueError("tokens_out must be a non-negative integer")
        return tokens_in, tokens_out
    raise ValueError("tokens must be a non-negative integer or token mapping")


@dataclass(frozen=True, slots=True)
class UsageRecord:
    usage_id: str
    run_id: str
    lab_id: str
    actor: str
    source: str
    tokens_in: int
    tokens_out: int
    compute: float
    llm_cost_thb: float
    compute_cost_thb: float
    recorded_at: datetime

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def cost_thb(self) -> float:
        return self.llm_cost_thb + self.compute_cost_thb


@dataclass(frozen=True, slots=True)
class UsageSummary:
    tokens_in: int
    tokens_out: int
    compute: float
    llm_cost_thb: float
    compute_cost_thb: float
    budget_thb: float | None
    budget_remaining_thb: float | None

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def cost_thb(self) -> float:
        return self.llm_cost_thb + self.compute_cost_thb


class MeteringService:
    _COLUMNS = (
        "usage_id", "run_id", "lab_id", "actor", "source", "tokens_in",
        "tokens_out", "compute", "llm_cost_thb", "compute_cost_thb", "recorded_at",
    )

    def __init__(
        self,
        connection: Any,
        *,
        event_service: Any | None = None,
        run_service: Any | None = None,
        audit_service: AuditService | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.connection = connection
        self.event_service = event_service
        self.run_service = run_service or RunService(connection, clock=clock)
        self.audit_service = audit_service or AuditService(connection, clock=clock)
        self.clock = clock

    @contextmanager
    def _access(self, identity: Identity, scope: str):
        require_scope(identity, scope)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                yield cursor

    def set_lab_budget(self, identity: Identity, budget_thb: float) -> None:
        budget = _number(budget_thb, "budget_thb")
        with self._access(identity, "lab:admin") as cursor:
            cursor.execute(
                "INSERT INTO lab_budgets (lab_id, budget_thb, configured_at) VALUES (%s, %s, %s) "
                "ON CONFLICT (lab_id) DO UPDATE SET budget_thb = EXCLUDED.budget_thb, "
                "configured_at = EXCLUDED.configured_at",
                (identity.lab_id, budget, self.clock()),
            )

    def record_usage(
        self, identity: Identity, run_id: str, actor: str, source: str,
        tokens: object, compute: float, llm_cost_thb: float, compute_cost_thb: float,
    ) -> UsageRecord:
        record, has_event = self._record_usage(
            identity, run_id, actor, source, tokens, compute, llm_cost_thb, compute_cost_thb
        )
        if has_event:
            self.deliver_usage_event(identity, record.usage_id)
        return record

    async def record_usage_async(
        self, identity: Identity, run_id: str, actor: str, source: str,
        tokens: object, compute: float, llm_cost_thb: float, compute_cost_thb: float,
    ) -> UsageRecord:
        record, has_event = self._record_usage(
            identity, run_id, actor, source, tokens, compute, llm_cost_thb, compute_cost_thb
        )
        if has_event:
            await self.deliver_usage_event_async(identity, record.usage_id)
        return record

    def _record_usage(
        self, identity: Identity, run_id: str, actor: str, source: str,
        tokens: object, compute: float, llm_cost_thb: float, compute_cost_thb: float,
    ) -> tuple[UsageRecord, bool]:
        run_id = _text(run_id, "run_id")
        actor = _text(actor, "actor")
        source = _text(source, "source")
        tokens_in, tokens_out = _tokens(tokens)
        compute_value = _number(compute, "compute")
        llm_cost = _number(llm_cost_thb, "llm_cost_thb")
        compute_cost = _number(compute_cost_thb, "compute_cost_thb")
        usage_id = uuid4().hex
        recorded_at = self.clock()

        with self._access(identity, "runs:write") as cursor:
            cursor.execute(
                "SELECT id, lab_id, state, reason FROM runs "
                "WHERE lab_id = %s AND id = %s FOR UPDATE",
                (identity.lab_id, run_id),
            )
            run = cursor.fetchone()
            if run is None:
                raise RunNotFound("run not found")
            state = run["state"] if isinstance(run, Mapping) else run[2]
            if state != RunState.RUNNING.value:
                raise RunStateError("usage can only be recorded for a running Run")
            cursor.execute(
                "SELECT budget_thb FROM lab_budgets WHERE lab_id = %s FOR UPDATE",
                (identity.lab_id,),
            )
            budget_row = cursor.fetchone()
            cursor.execute(
                "INSERT INTO metering_usage "
                "(usage_id, run_id, lab_id, actor, source, tokens_in, tokens_out, compute, "
                "llm_cost_thb, compute_cost_thb, recorded_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    usage_id, run_id, identity.lab_id, actor, source, tokens_in, tokens_out,
                    compute_value, llm_cost, compute_cost, recorded_at,
                ),
            )
            cursor.execute(
                "SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0), "
                "COALESCE(SUM(llm_cost_thb), 0), COALESCE(SUM(compute_cost_thb), 0) "
                "FROM metering_usage WHERE lab_id = %s AND run_id = %s",
                (identity.lab_id, run_id),
            )
            run_totals = cursor.fetchone() or (0, 0, 0, 0)
            cursor.execute(
                "SELECT COALESCE(SUM(llm_cost_thb + compute_cost_thb), 0) "
                "FROM metering_usage WHERE lab_id = %s",
                (identity.lab_id,),
            )
            lab_total = float((cursor.fetchone() or (0,))[0])
            budget = None if budget_row is None else float(budget_row[0])
            remaining = None if budget is None else max(0.0, budget - lab_total)
            self.audit_service.append_with_cursor(
                cursor,
                identity,
                run_id,
                actor,
                source,
                "usage.recorded",
                {
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                    "compute": compute_value,
                    "llm_cost_thb": llm_cost,
                    "compute_cost_thb": compute_cost,
                },
            )
            payload = {
                "tokens_in": int(run_totals[0]),
                "tokens_out": int(run_totals[1]),
                "llm_cost_thb": float(run_totals[2]),
                "compute_cost_thb": float(run_totals[3]),
                "budget_remaining_thb": remaining,
            }
            cursor.execute(
                "INSERT INTO metering_event_outbox "
                "(usage_id, run_id, lab_id, payload, created_at, delivered_at) "
                "VALUES (%s, %s, %s, %s, %s, NULL)",
                (
                    usage_id,
                    run_id,
                    identity.lab_id,
                    json.dumps(payload, separators=(",", ":")),
                    recorded_at,
                ),
            )
            if budget is not None and lab_total >= budget:
                self.run_service.transition_with_cursor(
                    cursor,
                    identity,
                    run_id,
                    RunState.CANCELLED,
                    reason="budget_exhausted",
                )

        return (
            UsageRecord(
                usage_id,
                run_id,
                identity.lab_id,
                actor,
                source,
                tokens_in,
                tokens_out,
                compute_value,
                llm_cost,
                compute_cost,
                recorded_at,
            ),
            True,
        )

    def deliver_usage_event(self, identity: Identity, usage_id: str) -> bool:
        pending = self._pending_event(identity, usage_id)
        if pending is None or self.event_service is None:
            return False
        run_id, payload = pending
        self._publish_event(identity, run_id, payload)
        self._mark_delivered(identity, usage_id)
        return True

    def deliver_pending_events(self, identity: Identity) -> int:
        with self._access(identity, "runs:write") as cursor:
            cursor.execute(
                "SELECT usage_id FROM metering_event_outbox "
                "WHERE lab_id = %s AND delivered_at IS NULL ORDER BY created_at, usage_id",
                (identity.lab_id,),
            )
            usage_ids = [
                row["usage_id"] if isinstance(row, Mapping) else row[0]
                for row in cursor.fetchall()
            ]
        return sum(self.deliver_usage_event(identity, usage_id) for usage_id in usage_ids)

    async def deliver_usage_event_async(self, identity: Identity, usage_id: str) -> bool:
        pending = self._pending_event(identity, usage_id)
        if pending is None or self.event_service is None:
            return False
        run_id, payload = pending
        result = self._event_call(identity, run_id, payload)
        if inspect.isawaitable(result):
            await result
        self._mark_delivered(identity, usage_id)
        return True

    def _pending_event(
        self, identity: Identity, usage_id: str
    ) -> tuple[str, dict[str, Any]] | None:
        with self._access(identity, "runs:write") as cursor:
            cursor.execute(
                "SELECT run_id, payload FROM metering_event_outbox "
                "WHERE lab_id = %s AND usage_id = %s AND delivered_at IS NULL",
                (identity.lab_id, _text(usage_id, "usage_id")),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        payload = row["payload"] if isinstance(row, Mapping) else row[1]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return (row["run_id"] if isinstance(row, Mapping) else row[0], dict(payload))

    def _mark_delivered(self, identity: Identity, usage_id: str) -> None:
        with self._access(identity, "runs:write") as cursor:
            cursor.execute(
                "UPDATE metering_event_outbox SET delivered_at = %s "
                "WHERE lab_id = %s AND usage_id = %s AND delivered_at IS NULL",
                (self.clock(), identity.lab_id, usage_id),
            )

    def query_usage(
        self, identity: Identity, *, run_id: str | None = None,
        actor: str | None = None, source: str | None = None,
    ) -> list[UsageRecord]:
        filters = ["lab_id = %s"]
        params: list[Any] = [identity.lab_id]
        for column, value in (("run_id", run_id), ("actor", actor), ("source", source)):
            if value is not None:
                filters.append(f"{column} = %s")
                params.append(_text(value, column))
        with self._access(identity, "runs:read") as cursor:
            cursor.execute(
                "SELECT usage_id, run_id, lab_id, actor, source, tokens_in, tokens_out, "
                "compute, llm_cost_thb, compute_cost_thb, recorded_at FROM metering_usage WHERE "
                + " AND ".join(filters) + " ORDER BY recorded_at ASC, usage_id ASC",
                tuple(params),
            )
            return [self._from_row(row) for row in cursor.fetchall()]

    def aggregate_usage(
        self, identity: Identity, *, run_id: str | None = None,
        actor: str | None = None, source: str | None = None,
    ) -> UsageSummary:
        filters = ["lab_id = %s"]
        params: list[Any] = [identity.lab_id]
        for column, value in (("run_id", run_id), ("actor", actor), ("source", source)):
            if value is not None:
                filters.append(f"{column} = %s")
                params.append(_text(value, column))
        with self._access(identity, "runs:read") as cursor:
            cursor.execute(
                "SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0), "
                "COALESCE(SUM(compute), 0), COALESCE(SUM(llm_cost_thb), 0), "
                "COALESCE(SUM(compute_cost_thb), 0) FROM metering_usage WHERE "
                + " AND ".join(filters),
                tuple(params),
            )
            totals = cursor.fetchone() or (0, 0, 0, 0, 0)
            cursor.execute(
                "SELECT budget_thb FROM lab_budgets WHERE lab_id = %s",
                (identity.lab_id,),
            )
            budget_row = cursor.fetchone()
        budget = None if budget_row is None else float(budget_row[0])
        cost = float(totals[3]) + float(totals[4])
        return UsageSummary(
            int(totals[0]), int(totals[1]), float(totals[2]), float(totals[3]),
            float(totals[4]), budget, None if budget is None else max(0.0, budget - cost),
        )

    def _event_call(self, identity: Identity, run_id: str, payload: dict[str, Any]) -> Any:
        if self.event_service is None:
            raise RuntimeError("cost.updated publication requires an event service")
        return self.event_service.publish_event(
            identity, run_id, "cost.updated", payload, "metering"
        )

    def _publish_event(self, identity: Identity, run_id: str, payload: dict[str, Any]) -> None:
        result = self._event_call(identity, run_id, payload)
        if not inspect.isawaitable(result):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(result)
            return
        if inspect.iscoroutine(result):
            result.close()
        raise RuntimeError("use record_usage_async inside an event loop")

    @classmethod
    def _from_row(cls, row: Mapping[str, Any] | tuple[Any, ...]) -> UsageRecord:
        values = row if isinstance(row, Mapping) else dict(zip(cls._COLUMNS, row))
        return UsageRecord(**{column: values[column] for column in cls._COLUMNS})
