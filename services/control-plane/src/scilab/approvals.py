from __future__ import annotations

import hashlib
import inspect
import json
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.request import Request, urlopen
from uuid import uuid4

from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.runs.model import Run, RunState
from scilab.runs.service import RunNotFound, RunService
from scilab.runs.state import apply_transition
from scilab.tenancy import require_scope


APPROVAL_EFFECTS = frozenset(
    {"read", "publish", "external_write", "delete", "credential_use", "network_change", "unknown"}
)
_SENSITIVE_KEYS = frozenset(
    {"password", "passwd", "secret", "token", "api_key", "authorization", "credential"}
)


class ApprovalNotFound(LookupError):
    pass


class ApprovalStateError(ValueError):
    pass


@dataclass(frozen=True)
class PolicyDecision:
    allow: bool
    requires_approval: bool
    policy_rule: str
    reason: str


@dataclass(frozen=True)
class Approval:
    id: str
    run_id: str
    lab_id: str
    action: str
    effect: str
    action_fingerprint: str
    status: str
    reason: str
    policy_rule: str
    preview: dict[str, Any]
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    actor: str | None
    note: str | None

    def event_payload(self) -> dict[str, Any]:
        return {
            "approval_id": self.id,
            "action": self.action,
            "reason": self.reason,
            "policy_rule": self.policy_rule,
            "expires_at": self.expires_at,
            "preview": self.preview,
        }


class ApprovalRequired(RuntimeError):
    def __init__(self, approval: Approval) -> None:
        super().__init__(f"approval required: {approval.id}")
        self.approval = approval


class OPAClient:
    def __init__(self, endpoint: str, *, timeout: float = 2.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def evaluate(self, policy_input: dict[str, object]) -> object:
        request = Request(
            f"{self.endpoint}/v1/data/scilab/approval/decision",
            data=json.dumps({"input": policy_input}, separators=(",", ":")).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - configured internal OPA
            return json.loads(response.read()).get("result")


def _is_sensitive(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return lowered in _SENSITIVE_KEYS or lowered.endswith("_token") or lowered.endswith("_secret")


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: "[REDACTED]" if _is_sensitive(key) else _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, tuple):
        return [_redact(item) for item in value]
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be non-blank")
    return value


def _fingerprint(action: str, effect: str, preview: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"action": action, "effect": effect, "preview": preview},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class ApprovalService:
    _approval_columns = (
        "id",
        "run_id",
        "lab_id",
        "action",
        "effect",
        "action_fingerprint",
        "status",
        "reason",
        "policy_rule",
        "preview",
        "requested_at",
        "expires_at",
        "decided_at",
        "actor",
        "note",
    )
    _approval_select = ", ".join(_approval_columns)
    _run_columns = RunService._columns
    _run_select = ", ".join(_run_columns)

    def __init__(
        self,
        connection: Any,
        opa: Any,
        event_service: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.connection = connection
        self.opa = opa
        self.event_service = event_service
        self.clock = clock
        self.id_factory = id_factory

    @contextmanager
    def _access(self, identity: Identity, scope: str):
        require_scope(identity, scope)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                yield cursor

    @classmethod
    def _approval(cls, row: Mapping[str, Any] | tuple[Any, ...]) -> Approval:
        values = dict(row) if isinstance(row, Mapping) else dict(zip(cls._approval_columns, row, strict=True))
        if isinstance(values["preview"], (str, bytes, bytearray)):
            values["preview"] = json.loads(values["preview"])
        return Approval(**values)

    @classmethod
    def _run(cls, row: Mapping[str, Any] | tuple[Any, ...]) -> Run:
        values = dict(row) if isinstance(row, Mapping) else dict(zip(cls._run_columns, row, strict=True))
        return Run(**{column: values[column] for column in cls._run_columns})

    def _load_run(self, cursor: Any, identity: Identity, run_id: str) -> Run:
        cursor.execute(
            f"SELECT {self._run_select} FROM runs "
            "WHERE lab_id = %s AND id = %s FOR UPDATE /* approval_run */",
            (identity.lab_id, run_id),
        )
        row = cursor.fetchone()
        if row is None:
            raise RunNotFound("run not found")
        return self._run(row)

    @staticmethod
    def _save_run(cursor: Any, run: Run) -> None:
        cursor.execute(
            """
            UPDATE runs SET state = %s, reason = %s, updated_at = %s,
                running_since = %s, runtime_used = %s, last_heartbeat_at = %s,
                approval_expires_at = %s
            WHERE lab_id = %s AND id = %s
            """,
            (
                run.state.value,
                run.reason,
                run.updated_at,
                run.running_since,
                run.runtime_used,
                run.last_heartbeat_at,
                run.approval_expires_at,
                run.lab_id,
                run.id,
            ),
        )

    def evaluate_action(
        self,
        action: str,
        effect: str | None,
        args_redacted: Mapping[str, Any],
    ) -> PolicyDecision:
        action = _text(action, "action")
        normalized_effect = effect if effect in APPROVAL_EFFECTS else "unknown"
        policy_input = {
            "action": action,
            "effect": normalized_effect,
            "args_redacted": _redact(dict(args_redacted)),
        }
        try:
            result = self.opa.evaluate(policy_input)
            if not isinstance(result, Mapping):
                raise ValueError("OPA result must be an object")
            decision = PolicyDecision(
                allow=result["allow"],
                requires_approval=result["requires_approval"],
                policy_rule=_text(result["policy_rule"], "policy_rule"),
                reason=_text(result["reason"], "reason"),
            )
            if not isinstance(decision.allow, bool) or not isinstance(decision.requires_approval, bool):
                raise TypeError("OPA flags must be boolean")
            if decision.allow == decision.requires_approval:
                raise ValueError("OPA decision must choose allow or approval")
            return decision
        except Exception:
            return PolicyDecision(False, True, "opa-fail-closed", "policy unavailable or invalid")

    def _find_active(
        self,
        cursor: Any,
        identity: Identity,
        run_id: str,
        fingerprint: str,
    ) -> Approval | None:
        cursor.execute(
            f"SELECT {self._approval_select} FROM approvals "
            "WHERE lab_id = %s AND run_id = %s AND action_fingerprint = %s "
            "AND status IN ('pending', 'approved') ORDER BY requested_at DESC LIMIT 1 "
            "/* approval_by_fingerprint */",
            (identity.lab_id, run_id, fingerprint),
        )
        row = cursor.fetchone()
        return None if row is None else self._approval(row)

    def active_approval(
        self,
        identity: Identity,
        run_id: str,
        action: str,
        effect: str | None,
        args_redacted: Mapping[str, Any],
    ) -> Approval | None:
        normalized_effect = effect if effect in APPROVAL_EFFECTS else "unknown"
        fingerprint = _fingerprint(_text(action, "action"), normalized_effect, dict(args_redacted))
        with self._access(identity, "runs:write") as cursor:
            self._load_run(cursor, identity, run_id)
            return self._find_active(cursor, identity, run_id, fingerprint)

    def assert_runnable(self, identity: Identity, run_id: str) -> None:
        with self._access(identity, "runs:write") as cursor:
            run = self._load_run(cursor, identity, run_id)
            if run.state is not RunState.RUNNING:
                raise ApprovalStateError("action requires a running Run")

    async def request_approval(
        self,
        identity: Identity,
        run_id: str,
        action: str,
        effect: str | None,
        args_redacted: Mapping[str, Any],
    ) -> Approval:
        decision = self.evaluate_action(action, effect, args_redacted)
        if decision.allow:
            raise ApprovalStateError("action does not require approval")
        normalized_effect = effect if effect in APPROVAL_EFFECTS else "unknown"
        preview = _redact(dict(args_redacted))
        fingerprint = _fingerprint(action, normalized_effect, dict(args_redacted))
        now = self.clock()
        created = False
        with self._access(identity, "runs:write") as cursor:
            run = self._load_run(cursor, identity, run_id)
            existing = self._find_active(cursor, identity, run_id, fingerprint)
            if existing is not None:
                return existing
            if run.state is not RunState.RUNNING:
                raise ApprovalStateError("approval can only be requested for a running Run")
            updated = apply_transition(run, RunState.AWAITING_APPROVAL, now)
            candidate = Approval(
                id=self.id_factory(),
                run_id=run_id,
                lab_id=identity.lab_id,
                action=action,
                effect=normalized_effect,
                action_fingerprint=fingerprint,
                status="pending",
                reason=decision.reason,
                policy_rule=decision.policy_rule,
                preview=preview,
                requested_at=now,
                expires_at=now + timedelta(hours=24),
                decided_at=None,
                actor=None,
                note=None,
            )
            cursor.execute(
                """
                INSERT INTO approvals
                    (id, run_id, lab_id, action, effect, action_fingerprint,
                     status, reason, policy_rule, preview, requested_at,
                     expires_at, decided_at, actor, note)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    candidate.id,
                    candidate.run_id,
                    candidate.lab_id,
                    candidate.action,
                    candidate.effect,
                    candidate.action_fingerprint,
                    candidate.status,
                    candidate.reason,
                    candidate.policy_rule,
                    json.dumps(candidate.preview, sort_keys=True, separators=(",", ":")),
                    candidate.requested_at,
                    candidate.expires_at,
                    candidate.decided_at,
                    candidate.actor,
                    candidate.note,
                ),
            )
            self._save_run(cursor, updated)
            approval = self._find_active(cursor, identity, run_id, fingerprint) or candidate
            created = True
        if created:
            await self.event_service.publish_event(
                identity,
                run_id,
                "approval.required",
                approval.event_payload(),
                "policy",
            )
        return approval

    def consume_approval(self, identity: Identity, approval_id: str) -> Approval:
        now = self.clock()
        with self._access(identity, "runs:write") as cursor:
            cursor.execute(
                f"SELECT {self._approval_select} FROM approvals "
                "WHERE lab_id = %s AND id = %s FOR UPDATE /* approval_by_id */",
                (identity.lab_id, approval_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ApprovalNotFound("approval not found")
            approval = self._approval(row)
            if approval.status != "approved":
                raise ApprovalStateError("approval is not executable")
            if now >= approval.expires_at:
                raise ApprovalStateError("approval expired before execution")
            run = self._load_run(cursor, identity, approval.run_id)
            if run.state is not RunState.RUNNING:
                raise ApprovalStateError("approved action requires a running Run")
            cursor.execute(
                "UPDATE approvals SET status = %s, decided_at = %s, actor = %s, note = %s "
                "WHERE lab_id = %s AND id = %s",
                (
                    "consumed",
                    approval.decided_at,
                    approval.actor,
                    approval.note,
                    identity.lab_id,
                    approval.id,
                ),
            )
            return Approval(**{**approval.__dict__, "status": "consumed"})

    def decide_approval(
        self,
        identity: Identity,
        approval_id: str,
        decision: str,
        *,
        note: str | None = None,
    ) -> Approval:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        now = self.clock()
        with self._access(identity, "runs:approve") as cursor:
            cursor.execute(
                f"SELECT {self._approval_select} FROM approvals "
                "WHERE lab_id = %s AND id = %s FOR UPDATE /* approval_by_id */",
                (identity.lab_id, approval_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ApprovalNotFound("approval not found")
            approval = self._approval(row)
            if approval.status != "pending":
                raise ApprovalStateError("approval already decided")
            if now >= approval.expires_at:
                raise ApprovalStateError("approval expired")
            run = self._load_run(cursor, identity, approval.run_id)
            status = "approved" if decision == "approve" else "rejected"
            target = RunState.RUNNING if decision == "approve" else RunState.CANCELLED
            reason = None if decision == "approve" else "approval_rejected"
            updated_run = apply_transition(run, target, now, reason=reason)
            cursor.execute(
                "UPDATE approvals SET status = %s, decided_at = %s, actor = %s, note = %s "
                "WHERE lab_id = %s AND id = %s",
                (status, now, identity.principal, note, identity.lab_id, approval.id),
            )
            self._save_run(cursor, updated_run)
            return Approval(
                **{
                    **approval.__dict__,
                    "status": status,
                    "decided_at": now,
                    "actor": identity.principal,
                    "note": note,
                }
            )

    def expire_approvals(self, identity: Identity, *, now: datetime | None = None) -> list[Approval]:
        at = self.clock() if now is None else now
        expired: list[Approval] = []
        with self._access(identity, "runs:write") as cursor:
            cursor.execute(
                f"SELECT {self._approval_select} FROM approvals "
                "WHERE lab_id = %s AND status IN ('pending', 'approved') AND expires_at <= %s "
                "FOR UPDATE /* due_approvals */",
                (identity.lab_id, at),
            )
            for row in cursor.fetchall():
                approval = self._approval(row)
                run = self._load_run(cursor, identity, approval.run_id)
                updated_run = None
                if run.state is RunState.AWAITING_APPROVAL:
                    updated_run = apply_transition(
                        run, RunState.CANCELLED, at, reason="approval_expired"
                    )
                decided_at = approval.decided_at or at
                cursor.execute(
                    "UPDATE approvals SET status = %s, decided_at = %s, actor = %s, note = %s "
                    "WHERE lab_id = %s AND id = %s",
                    (
                        "expired",
                        decided_at,
                        approval.actor,
                        approval.note,
                        identity.lab_id,
                        approval.id,
                    ),
                )
                if updated_run is not None:
                    self._save_run(cursor, updated_run)
                expired.append(
                    Approval(
                        **{
                            **approval.__dict__,
                            "status": "expired",
                            "decided_at": decided_at,
                        }
                    )
                )
        return expired


class ActionGate:
    def __init__(self, approvals: ApprovalService) -> None:
        self.approvals = approvals

    async def execute(
        self,
        identity: Identity,
        run_id: str,
        *,
        action: str,
        effect: str | None,
        args_redacted: Mapping[str, Any],
        executor: Callable[[], Any],
    ) -> Any:
        decision = self.approvals.evaluate_action(action, effect, args_redacted)
        if decision.allow:
            self.approvals.assert_runnable(identity, run_id)
            result = executor()
            return await result if inspect.isawaitable(result) else result
        existing = self.approvals.active_approval(
            identity, run_id, action, effect, args_redacted
        )
        if existing is not None and existing.status == "approved":
            self.approvals.consume_approval(identity, existing.id)
            result = executor()
            return await result if inspect.isawaitable(result) else result
        approval = existing or await self.approvals.request_approval(
            identity, run_id, action, effect, args_redacted
        )
        raise ApprovalRequired(approval)
