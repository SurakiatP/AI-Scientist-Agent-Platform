from __future__ import annotations

import hashlib
import inspect
import json
import logging
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.request import Request, urlopen
from uuid import uuid4

from scilab.audit import AuditService
from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.events import EventFanoutError
from scilab.identity import Identity
from scilab.redaction import redact as _redact
from scilab.runs.model import Run, RunState
from scilab.runs.service import RunNotFound, RunService
from scilab.runs.state import apply_transition
from scilab.tenancy import require_scope


APPROVAL_EFFECTS = frozenset(
    {"read", "publish", "external_write", "delete", "credential_use", "network_change", "execute", "unknown"}
)
_LOG = logging.getLogger(__name__)


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
        audit: Any = None,
    ) -> None:
        self.connection = connection
        self.opa = opa
        self.event_service = event_service
        self.clock = clock
        self.id_factory = id_factory
        self.audit = audit if audit is not None else AuditService(connection)

    @contextmanager
    def _access(self, identity: Identity, scope: str):
        require_scope(identity, scope)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                yield cursor

    @classmethod
    def _approval(cls, row: Mapping[str, Any] | tuple[Any, ...]) -> Approval:
        values = ({column: row[column] for column in cls._approval_columns}
                  if isinstance(row, Mapping) else dict(zip(cls._approval_columns, row[:len(cls._approval_columns)], strict=True)))
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

    async def request_hermes_approval(
        self, identity: Identity, run_id: str, hermes_request_id: str,
        preview: Mapping[str, Any], *, effect: str = "unknown",
    ) -> tuple[Approval, Run]:
        request_id = _text(hermes_request_id, "hermes_request_id")
        if len(request_id) > 256:
            raise ValueError("hermes_request_id exceeds 256 characters")
        if not isinstance(preview, Mapping):
            raise ValueError("preview must be a mapping")
        safe_preview = _redact(dict(preview))
        normalized_effect = effect if effect in APPROVAL_EFFECTS else "unknown"
        policy = self.evaluate_action("hermes.tool", normalized_effect, safe_preview)
        fingerprint = hashlib.sha256(
            json.dumps([identity.lab_id, run_id, request_id], separators=(",", ":")).encode()
        ).hexdigest()
        now = self.clock()
        created = False
        with self._access(identity, "runs:write") as cursor:
            run = self._load_run(cursor, identity, run_id)
            cursor.execute(
                f"SELECT {self._approval_select} FROM approvals "
                "WHERE lab_id = %s AND run_id = %s AND hermes_request_id = %s "
                "/* hermes_by_request */",
                (identity.lab_id, run_id, request_id),
            )
            row = cursor.fetchone()
            if row is not None:
                return self._approval(row), run
            if run.state is not RunState.RUNNING:
                raise ApprovalStateError("Hermes approval requires a running Run")
            updated = apply_transition(run, RunState.AWAITING_APPROVAL, now)
            candidate = Approval(
                self.id_factory(), run_id, identity.lab_id, "hermes.tool", normalized_effect,
                fingerprint, "pending", policy.reason, policy.policy_rule, safe_preview,
                now, now + timedelta(hours=24), None, None, None,
            )
            cursor.execute(
                "INSERT INTO approvals (id, run_id, lab_id, action, effect, "
                "action_fingerprint, status, reason, policy_rule, preview, requested_at, "
                "expires_at, decided_at, actor, note, hermes_request_id) "
                "VALUES (" + ", ".join(["%s"] * 16) + ") ON CONFLICT DO NOTHING",
                (candidate.id, candidate.run_id, candidate.lab_id, candidate.action,
                 candidate.effect, candidate.action_fingerprint, candidate.status,
                 candidate.reason, candidate.policy_rule, json.dumps(candidate.preview),
                 candidate.requested_at, candidate.expires_at, None, None, None, request_id),
            )
            self._save_run(cursor, updated)
            approval = candidate
            created = True
        if created:
            await self.event_service.publish_event(
                identity, run_id, "approval.required", approval.event_payload(), "policy"
            )
            await self.event_service.publish_event(
                identity, run_id, "run.state",
                {"from": "running", "to": "awaiting_approval", "reason": "approval_required"},
                "run-service",
            )
        return approval, updated

    def _hermes_record(self, cursor: Any, identity: Identity, approval_id: str, run_id: str) -> Mapping[str, Any]:
        cursor.execute(
            f"SELECT {self._approval_select}, hermes_request_id, hermes_decision, "
            "hermes_decision_actor, hermes_decided_at, hermes_decision_note FROM approvals "
            "WHERE lab_id = %s AND id = %s AND run_id = %s FOR UPDATE /* hermes_by_id */",
            (identity.lab_id, approval_id, _text(run_id, "run_id")),
        )
        row = cursor.fetchone()
        if row is not None and not isinstance(row, Mapping):
            row = dict(zip(
                (*self._approval_columns, "hermes_request_id", "hermes_decision",
                 "hermes_decision_actor", "hermes_decided_at", "hermes_decision_note"),
                row, strict=True,
            ))
        if row is None or row["hermes_request_id"] is None:
            raise ApprovalNotFound("Hermes approval not found")
        return row

    def hermes_request_id(self, identity: Identity, approval_id: str, run_id: str) -> str | None:
        with self._access(identity, "runs:approve") as cursor:
            cursor.execute(
                "SELECT hermes_request_id FROM approvals WHERE lab_id = %s AND id = %s "
                "AND run_id = %s /* hermes_id_lookup */",
                (identity.lab_id, approval_id, _text(run_id, "run_id")),
            )
            row = cursor.fetchone()
            return None if row is None else (row["hermes_request_id"] if isinstance(row, Mapping) else row[0])

    def hermes_run_id(self, identity: Identity, approval_id: str, run_id: str) -> str:
        with self._access(identity, "runs:approve") as cursor:
            self._hermes_record(cursor, identity, approval_id, run_id)
            vendor_id = self._load_run(cursor, identity, run_id).hermes_run_id
            if not vendor_id:
                raise ApprovalStateError("Hermes Run ID unavailable")
            return vendor_id

    def stage_hermes_decision(
        self, identity: Identity, approval_id: str, decision: str,
        *, note: str | None = None, run_id: str,
    ) -> str:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        now = self.clock()
        with self._access(identity, "runs:approve") as cursor:
            row = self._hermes_record(cursor, identity, approval_id, run_id)
            approval = self._approval(row)
            if approval.status != "pending" or now >= approval.expires_at:
                raise ApprovalStateError("approval is not pending")
            run = self._load_run(cursor, identity, run_id)
            if run.state is not RunState.AWAITING_APPROVAL:
                raise ApprovalStateError("Run is not awaiting approval")
            if row["hermes_decision"] is not None:
                if row["hermes_decision"] != decision:
                    raise ApprovalStateError("conflicting Hermes decision")
                return row["hermes_request_id"]
            cursor.execute(
                "UPDATE approvals SET hermes_decision = %s, hermes_decision_actor = %s, "
                "hermes_decided_at = %s, hermes_decision_note = %s "
                "WHERE lab_id = %s AND id = %s /* hermes_stage */",
                (decision, identity.principal, now, note, identity.lab_id, approval_id),
            )
            return row["hermes_request_id"]

    async def confirm_hermes_decision(
        self, identity: Identity, approval_id: str, *, run_id: str,
    ) -> Approval:
        with self._access(identity, "runs:approve") as cursor:
            row = self._hermes_record(cursor, identity, approval_id, run_id)
            approval = self._approval(row)
            if approval.status in {"approved", "rejected"}:
                return approval
            if approval.status != "pending" or row["hermes_decision"] is None:
                raise ApprovalStateError("Hermes decision was not staged")
            now = self.clock()
            if now >= approval.expires_at:
                raise ApprovalStateError("approval expired")
            run = self._load_run(cursor, identity, run_id)
            if run.state is not RunState.AWAITING_APPROVAL:
                raise ApprovalStateError("Run is not awaiting approval")
            approved = row["hermes_decision"] == "approve"
            status = "approved" if approved else "rejected"
            updated = apply_transition(
                run, RunState.RUNNING if approved else RunState.CANCELLED,
                now, reason=None if approved else "approval_rejected",
            )
            cursor.execute(
                "UPDATE approvals SET status = %s, decided_at = %s, actor = %s, note = %s "
                "WHERE lab_id = %s AND id = %s",
                (status, row["hermes_decided_at"], row["hermes_decision_actor"],
                 row["hermes_decision_note"], identity.lab_id, approval_id),
            )
            self._save_run(cursor, updated)
            details: dict[str, Any] = {
                "approval_id": approval.id,
                "effect": approval.effect,
                "policy_rule": approval.policy_rule,
                "hermes_request_id": row["hermes_request_id"],
            }
            if row["hermes_decision_note"] is not None:
                details["note"] = row["hermes_decision_note"]
            self.audit.append_with_cursor(
                cursor,
                identity,
                run_id,
                row["hermes_decision_actor"],
                "approval",
                "approval.approved" if approved else "approval.rejected",
                details,
            )
            event = self.event_service.record_with_cursor(
                cursor,
                identity,
                run_id,
                "run.state",
                {
                    "from": run.state.value,
                    "to": updated.state.value,
                    "reason": updated.reason or "approval_approved",
                },
                "run-service",
            )
            confirmed = Approval(
                **{
                    **approval.__dict__,
                    "status": status,
                    "decided_at": row["hermes_decided_at"],
                    "actor": row["hermes_decision_actor"],
                    "note": row["hermes_decision_note"],
                }
            )
        try:
            await self.event_service.fanout_recorded(event)
        except EventFanoutError:
            _LOG.warning("Hermes approval confirmed; persisted run-state event fanout failed")
        return confirmed

    def final_hermes_approval(
        self,
        identity: Identity,
        approval_id: str,
        decision: str,
        *,
        run_id: str,
    ) -> Approval | None:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        with self._access(identity, "runs:approve") as cursor:
            row = self._hermes_record(cursor, identity, approval_id, run_id)
            approval = self._approval(row)
            if approval.status not in {"approved", "rejected"}:
                return None
            if row["hermes_decision"] != decision:
                raise ApprovalStateError("conflicting Hermes decision")
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
        run_id: str | None = None,
    ) -> Approval:
        if decision not in {"approve", "reject"}:
            raise ValueError("decision must be approve or reject")
        now = self.clock()
        with self._access(identity, "runs:approve") as cursor:
            run_filter = " AND run_id = %s" if run_id is not None else ""
            params = (
                (identity.lab_id, approval_id, _text(run_id, "run_id"))
                if run_id is not None
                else (identity.lab_id, approval_id)
            )
            cursor.execute(
                f"SELECT {self._approval_select}, hermes_request_id FROM approvals "
                f"WHERE lab_id = %s AND id = %s{run_filter} "
                "FOR UPDATE /* approval_by_id */",
                params,
            )
            row = cursor.fetchone()
            if row is None:
                raise ApprovalNotFound("approval not found")
            approval = self._approval(row)
            if (row.get("hermes_request_id") if isinstance(row, Mapping) else row[-1]) is not None:
                raise ApprovalStateError("Hermes approval requires staged vendor confirmation")
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
            details: dict[str, Any] = {
                "approval_id": approval.id,
                "effect": approval.effect,
                "policy_rule": approval.policy_rule,
            }
            if note is not None:
                details["note"] = note
            self.audit.append_with_cursor(
                cursor,
                identity,
                approval.run_id,
                identity.principal,
                "approval",
                "approval.approved" if status == "approved" else "approval.rejected",
                details,
            )
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
                details: dict[str, Any] = {
                    "approval_id": approval.id,
                    "effect": approval.effect,
                    "policy_rule": approval.policy_rule,
                }
                if approval.note is not None:
                    details["note"] = approval.note
                self.audit.append_with_cursor(
                    cursor,
                    identity,
                    approval.run_id,
                    identity.principal,
                    "approval",
                    "approval.expired",
                    details,
                )
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
