"""REST Run adapters backed by the durable Run service."""

from __future__ import annotations

import base64
import binascii
import json
import math
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from scilab.identity import Identity
from scilab.runs.model import RunState
from scilab.runs.service import RunIdempotencyConflict, RunService
from scilab.skill_catalog import get_skill_pack
from scilab.tenancy import require_scope

InputResolver = Callable[[Identity, Sequence[str]], Sequence[str]]
AdmissionPolicy = Callable[[Identity, Mapping[str, Any], str], bool]


class RunAdmissionAdapter:
    """Delegate quota/rate admission to the configured policy; fail closed on bad output."""

    def __init__(self, policy: AdmissionPolicy) -> None:
        if not callable(policy):
            raise TypeError("an admission policy is required")
        self.policy = policy

    def check(
        self, identity: Identity, payload: Mapping[str, Any], idempotency_key: str
    ) -> bool:
        require_scope(identity, "runs:write")
        if not isinstance(payload, Mapping):
            raise HTTPException(status_code=422, detail="invalid Run request")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise HTTPException(status_code=422, detail="Idempotency-Key is required")
        allowed = self.policy(identity, payload, idempotency_key)
        if not isinstance(allowed, bool):
            raise RuntimeError("admission policy must return a boolean")
        if not allowed:
            raise HTTPException(status_code=429, detail="Run admission denied")
        return allowed


class RunSubmissionAdapter:
    """Validate uploaded refs and persist the REST request with its Run atomically."""

    def __init__(self, runs: RunService, *, input_resolver: InputResolver) -> None:
        self.runs = runs
        self.input_resolver = input_resolver

    def create(
        self, identity: Identity, idempotency_key: str, payload: Mapping[str, Any]
    ) -> dict[str, str]:
        require_scope(identity, "runs:write")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise HTTPException(status_code=422, detail="Idempotency-Key is required")
        request = self._request(identity, payload)
        request["channel"] = "web" if identity.principal.startswith("user:") else "rest"
        try:
            run = self.runs.create(
                identity,
                idempotency_key,
                max_minutes=request["budget"]["max_minutes"],
                budget_thb=request["budget"]["thb"],
                request_payload=request,
                actor=identity.principal,
            )
        except RunIdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": run.id, "state": str(run.state)}

    def _request(self, identity: Identity, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise HTTPException(status_code=422, detail="invalid Run request")
        if set(payload) - {"goal", "inputs", "skill_packs", "budget", "options"}:
            raise HTTPException(status_code=422, detail="unsupported Run request field")
        goal = payload.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise HTTPException(status_code=422, detail="goal must be non-blank")
        inputs = self._string_list(payload.get("inputs"), "inputs")
        packs = self._string_list(payload.get("skill_packs"), "skill_packs")
        budget = payload.get("budget")
        if not isinstance(budget, Mapping):
            raise HTTPException(status_code=422, detail="budget is required")
        amount = budget.get("thb")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise HTTPException(status_code=422, detail="budget.thb must be non-negative")
        amount = float(amount)
        minutes = budget.get("max_minutes")
        if not math.isfinite(amount) or amount < 0:
            raise HTTPException(status_code=422, detail="budget.thb must be non-negative")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            raise HTTPException(status_code=422, detail="budget.max_minutes must be positive")
        options = payload.get("options", {})
        if not isinstance(options, Mapping):
            raise HTTPException(status_code=422, detail="options must be an object")
        if options:
            raise HTTPException(status_code=422, detail="execution options are not supported")

        try:
            for pack_id in packs:
                get_skill_pack(pack_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            resolved_inputs = self.input_resolver(identity, inputs)
        except LookupError:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if (
            isinstance(resolved_inputs, (str, bytes))
            or not isinstance(resolved_inputs, Sequence)
            or len(resolved_inputs) != len(inputs)
            or any(not isinstance(value, str) or not value.strip() for value in resolved_inputs)
        ):
            raise RuntimeError("input resolver must return one validated ID per input")

        request = {
            "goal": goal,
            "inputs": list(resolved_inputs),
            "skill_packs": packs,
            "budget": {"thb": amount, "max_minutes": minutes},
            "options": dict(options),
        }
        try:
            json.dumps(request, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="options must contain JSON values") from exc
        return request

    @staticmethod
    def _string_list(value: Any, field: str) -> list[str]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise HTTPException(status_code=422, detail=f"{field} must be an array")
        result = list(value)
        if any(not isinstance(item, str) or not item.strip() for item in result):
            raise HTTPException(status_code=422, detail=f"{field} must contain non-blank strings")
        return result


class RunSearchAdapter:
    _page_size = 100

    def __init__(self, runs: RunService) -> None:
        self.runs = runs

    def list(
        self,
        identity: Identity,
        *,
        state: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        require_scope(identity, "runs:read")
        if state is not None:
            try:
                state = RunState(state).value
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="unknown Run state") from exc
        if actor is not None and not actor.strip():
            raise HTTPException(status_code=422, detail="actor must be non-blank")
        since_at = self._parse_time(since) if since is not None else None
        after = self._decode_cursor(cursor) if cursor is not None else None
        records = self.runs.search_submissions(
            identity,
            state=state,
            actor=actor,
            since=since_at,
            after=after,
            limit=self._page_size + 1,
        )
        has_more = len(records) > self._page_size
        page = records[: self._page_size]
        items = [self._item(record) for record in page]
        next_cursor = self._encode_cursor(page[-1]) if has_more else None
        return {"items": items, "next_cursor": next_cursor}

    @staticmethod
    def _item(record: Mapping[str, Any]) -> dict[str, Any]:
        item = {key: record[key] for key in RunService._columns}
        payload = record.get("request_payload")
        if isinstance(payload, (str, bytes, bytearray)):
            payload = json.loads(payload)
        if payload is not None:
            if not isinstance(payload, Mapping):
                raise RuntimeError("stored Run request payload is not an object")
            item.update(payload)
            item.pop("channel", None)
        item["actor"] = record.get("actor")
        return item

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="since must be an ISO date-time") from exc
        if result.tzinfo is None or result.utcoffset() is None:
            raise HTTPException(status_code=422, detail="since must include a timezone")
        return result.astimezone(timezone.utc)

    @classmethod
    def _encode_cursor(cls, record: Mapping[str, Any]) -> str:
        raw = json.dumps(
            [record["created_at"].isoformat(), record["id"]], separators=(",", ":")
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(value: str) -> tuple[datetime, str]:
        if not value or len(value) > 4096:
            raise HTTPException(status_code=422, detail="invalid cursor")
        try:
            encoded = value.encode("ascii")
            raw = base64.b64decode(
                encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
            parts = json.loads(raw)
            if (
                not isinstance(parts, list)
                or len(parts) != 2
                or not isinstance(parts[0], str)
                or not isinstance(parts[1], str)
                or not parts[1].strip()
            ):
                raise ValueError
            created_at = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
            if created_at.tzinfo is None or created_at.utcoffset() is None:
                raise ValueError
            return created_at.astimezone(timezone.utc), parts[1]
        except (ValueError, UnicodeEncodeError, binascii.Error, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=422, detail="invalid cursor") from exc
