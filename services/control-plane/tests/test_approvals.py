from __future__ import annotations

import asyncio
import json
import os
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from scilab.identity import Identity
from scilab.runs.model import RunState
from scilab.tenancy import AuthorizationError


NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[3]
APPROVAL_COLUMNS = (
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


def identity(lab_id: str = "lab-a", *scopes: str) -> Identity:
    return Identity(
        lab_id,
        f"user:{lab_id}",
        frozenset(scopes or {"runs:read", "runs:write"}),
    )


class Cursor:
    def __init__(self, database: "Database") -> None:
        self.database = database
        self.result: list[dict[str, object]] = []
        self.hermes_select: str | None = None

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        compact = " ".join(sql.split()).lower()
        self.hermes_select = None
        if compact.startswith("select set_config"):
            self.database.current_lab_id = str(params[1])
            return
        if "/* approval_run */" in compact:
            lab_id, run_id = params
            run = self.database.runs.get((str(lab_id), str(run_id)))
            self.result = [] if run is None else [run]
            return
        if compact.startswith("insert into approvals"):
            row = dict(zip(APPROVAL_COLUMNS, params))
            if len(params) == 16:
                row["hermes_request_id"] = params[-1]
                row["hermes_decision"] = None
                row["hermes_decision_actor"] = None
                row["hermes_decided_at"] = None
                row["hermes_decision_note"] = None
            existing = next(
                (
                    item
                    for item in self.database.approvals
                    if item["lab_id"] == row["lab_id"]
                    and item["run_id"] == row["run_id"]
                    and item["action_fingerprint"] == row["action_fingerprint"]
                    and item["status"] in {"pending", "approved"}
                ),
                None,
            )
            if existing is None:
                self.database.approvals.append(row)
            return
        if "/* approval_by_fingerprint */" in compact:
            lab_id, run_id, fingerprint = params
            self.result = [
                row
                for row in self.database.approvals
                if row["lab_id"] == lab_id
                and row["run_id"] == run_id
                and row["action_fingerprint"] == fingerprint
                and row["status"] in {"pending", "approved"}
            ]
            return
        if "/* hermes_by_request */" in compact:
            lab_id, run_id, request_id = params
            self.result = [row for row in self.database.approvals
                           if row["lab_id"] == lab_id and row["run_id"] == run_id
                           and row.get("hermes_request_id") == request_id]
            return
        if "/* hermes_by_id */" in compact or "/* hermes_id_lookup */" in compact:
            self.hermes_select = "id" if "/* hermes_by_id */" in compact else "lookup"
            lab_id, approval_id, run_id = params
            self.result = [row for row in self.database.approvals
                           if row["lab_id"] == lab_id and row["id"] == approval_id
                           and row["run_id"] == run_id]
            return
        if "/* approval_by_id */" in compact:
            lab_id, approval_id, *run_id = params
            self.result = [
                row
                for row in self.database.approvals
                if row["lab_id"] == lab_id
                and row["id"] == approval_id
                and (not run_id or row["run_id"] == run_id[0])
            ]
            return
        if "/* due_approvals */" in compact:
            lab_id, now = params
            self.result = [
                row
                for row in self.database.approvals
                if row["lab_id"] == lab_id
                and row["status"] in {"pending", "approved"}
                and row["expires_at"] <= now
            ]
            return
        if compact.startswith("update approvals"):
            if "/* hermes_stage */" in compact:
                decision, actor, decided_at, note, lab_id, approval_id = params
                row = next(row for row in self.database.approvals
                           if row["lab_id"] == lab_id and row["id"] == approval_id)
                row.update(hermes_decision=decision, hermes_decision_actor=actor,
                           hermes_decided_at=decided_at, hermes_decision_note=note)
                return
            status, decided_at, actor, note, lab_id, approval_id = params
            row = next(
                item
                for item in self.database.approvals
                if item["lab_id"] == lab_id and item["id"] == approval_id
            )
            row.update(status=status, decided_at=decided_at, actor=actor, note=note)
            return
        if compact.startswith("insert into audit_events"):
            audit_id, run_id, lab_id, actor, source, action, details, created_at = params
            self.database.audit_events.append(
                {
                    "audit_id": audit_id,
                    "run_id": run_id,
                    "lab_id": lab_id,
                    "actor": actor,
                    "source": source,
                    "action": action,
                    "details": json.loads(details) if isinstance(details, str) else details,
                    "created_at": created_at,
                }
            )
            return
        if compact.startswith("update runs"):
            state, reason, updated_at, running_since, runtime_used, heartbeat, expires, lab_id, run_id = params
            self.database.runs[(str(lab_id), str(run_id))].update(
                state=state,
                reason=reason,
                updated_at=updated_at,
                running_since=running_since,
                runtime_used=runtime_used,
                last_heartbeat_at=heartbeat,
                approval_expires_at=expires,
            )
            return
        raise AssertionError(f"unexpected SQL: {compact}")

    def fetchone(self) -> dict[str, object] | None:
        if not self.result:
            return None
        row = self.result[0]
        if self.database.tuple_rows and self.hermes_select == "id":
            return tuple(row.get(column) for column in (*APPROVAL_COLUMNS, "hermes_request_id",
                          "hermes_decision", "hermes_decision_actor", "hermes_decided_at",
                          "hermes_decision_note"))
        if self.database.tuple_rows and self.hermes_select == "lookup":
            return (row.get("hermes_request_id"),)
        return row

    def fetchall(self) -> list[dict[str, object]]:
        return list(self.result)


class Database:
    def __init__(self) -> None:
        self.runs: dict[tuple[str, str], dict[str, object]] = {}
        self.approvals: list[dict[str, object]] = []
        self.audit_events: list[dict[str, object]] = []
        self.current_lab_id: str | None = None
        self.tuple_rows = False

    def transaction(self):
        return nullcontext()

    def cursor(self) -> Cursor:
        return Cursor(self)

    def add_running_run(self, run_id: str = "run-1", lab_id: str = "lab-a") -> None:
        self.runs[(lab_id, run_id)] = {
            "id": run_id,
            "lab_id": lab_id,
            "idempotency_key": "request-1",
            "state": "running",
            "reason": None,
            "retry_count": 0,
            "max_minutes": 120,
            "hermes_run_id": "hermes-1",
            "created_at": NOW,
            "updated_at": NOW,
            "queued_at": NOW,
            "running_since": NOW,
            "runtime_used": timedelta(0),
            "last_heartbeat_at": NOW,
            "approval_expires_at": None,
            "context_id": None,
            "budget_thb": None,
        }


class OPA:
    def __init__(self, *, broken: object | None = None) -> None:
        self.broken = broken
        self.inputs: list[dict[str, object]] = []

    def evaluate(self, policy_input: dict[str, object]) -> object:
        self.inputs.append(policy_input)
        if isinstance(self.broken, Exception):
            raise self.broken
        if self.broken is not None:
            return self.broken
        effect = policy_input.get("effect")
        requires = effect != "read"
        return {
            "allow": not requires,
            "requires_approval": requires,
            "policy_rule": "read-only" if not requires else "human-review",
            "reason": "read-only action" if not requires else "side effect or unknown action",
        }


class Events:
    def __init__(self) -> None:
        self.published: list[tuple[Identity, str, str, dict[str, object], str]] = []
        self.recorded: list[SimpleNamespace] = []
        self.fanned_out: list[SimpleNamespace] = []

    async def publish_event(
        self,
        actor: Identity,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        source: str,
    ) -> None:
        self.published.append((actor, run_id, event_type, payload, source))

    def record_with_cursor(
        self,
        cursor: object,
        actor: Identity,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        source: str,
    ) -> SimpleNamespace:
        event = SimpleNamespace(
            event_id="approval-event",
            lab_id=actor.lab_id,
            run_id=run_id,
            type=event_type,
            payload=payload,
            source=source,
        )
        self.recorded.append(event)
        return event

    async def fanout_recorded(self, event: SimpleNamespace) -> None:
        self.fanned_out.append(event)


def service(database: Database, opa: OPA | None = None, events: Events | None = None):
    from scilab.approvals import ApprovalService

    return ApprovalService(
        database,
        opa or OPA(),
        events or Events(),
        clock=lambda: NOW,
        id_factory=lambda: f"approval-{len(database.approvals) + 1}",
    )


def test_policy_allows_only_read_and_fails_closed_without_leaking_args() -> None:
    from scilab.approvals import APPROVAL_EFFECTS, ActionGate
    from scilab.runs.service import RunNotFound

    database = Database()
    approval_service = service(database)
    assert approval_service.evaluate_action("search", "read", {"query": "cells"}).allow is True
    for effect in APPROVAL_EFFECTS - {"read"}:
        assert approval_service.evaluate_action("tool", effect, {}).requires_approval is True
    assert approval_service.evaluate_action("tool", None, {}).requires_approval is True

    decision = approval_service.evaluate_action(
        "publish",
        "publish",
        {"target": "report", "password": "must-not-leak"},
    )
    assert decision.requires_approval is True
    assert approval_service.opa.inputs[-1]["args_redacted"]["password"] == "[REDACTED]"
    assert "must-not-leak" not in repr(approval_service.opa.inputs)

    assert service(database, OPA(broken=RuntimeError("offline"))).evaluate_action(
        "search", "read", {}
    ).requires_approval
    assert service(database, OPA(broken={"allow": True})).evaluate_action(
        "search", "read", {}
    ).requires_approval

    database.add_running_run()
    with pytest.raises(RunNotFound):
        asyncio.run(
            ActionGate(approval_service).execute(
                identity("lab-b"),
                "run-1",
                action="search",
                effect="read",
                args_redacted={"query": "cells"},
                executor=lambda: pytest.fail("cross-Lab action must not execute"),
            )
        )


def test_gate_blocks_side_effect_and_creates_idempotent_24_hour_request() -> None:
    from scilab.approvals import ActionGate, ApprovalRequired
    from scilab.contracts import ApprovalPayload

    database = Database()
    database.add_running_run()
    events = Events()
    approval_service = service(database, events=events)
    gate = ActionGate(approval_service)
    executed: list[str] = []

    with pytest.raises(ApprovalRequired) as first:
        asyncio.run(
            gate.execute(
                identity(),
                "run-1",
                action="publish",
                effect="publish",
                args_redacted={"target": "report-1", "api_key": "credential-one"},
                executor=lambda: executed.append("published"),
            )
        )
    with pytest.raises(ApprovalRequired) as retried:
        asyncio.run(
            gate.execute(
                identity(),
                "run-1",
                action="publish",
                effect="publish",
                args_redacted={"target": "report-1", "api_key": "credential-one"},
                executor=lambda: executed.append("published"),
            )
        )

    approval = first.value.approval
    assert retried.value.approval.id == approval.id
    assert executed == []
    assert len(database.approvals) == 1
    assert approval.expires_at == NOW + timedelta(hours=24)
    assert database.runs[("lab-a", "run-1")]["state"] == RunState.AWAITING_APPROVAL.value
    assert approval.event_payload() == {
        "approval_id": approval.id,
        "action": "publish",
        "reason": "side effect or unknown action",
        "policy_rule": "human-review",
        "expires_at": NOW + timedelta(hours=24),
        "preview": {"target": "report-1", "api_key": "[REDACTED]"},
    }
    ApprovalPayload.model_validate(approval.event_payload())
    assert events.published == [
        (identity(), "run-1", "approval.required", approval.event_payload(), "policy")
    ]
    assert approval_service.active_approval(
        identity(),
        "run-1",
        "publish",
        "publish",
        {"target": "report-1", "api_key": "credential-two"},
    ) is None
    assert "credential-one" not in repr(database.approvals)


def test_approve_requires_scope_records_actor_and_authorizes_exact_action() -> None:
    from scilab.approvals import ActionGate, ApprovalRequired, ApprovalStateError

    database = Database()
    database.add_running_run()
    approval_service = service(database)
    gate = ActionGate(approval_service)
    kwargs = {
        "action": "external.write",
        "effect": "external_write",
        "args_redacted": {"target": "dataset-1"},
    }
    with pytest.raises(ApprovalRequired) as blocked:
        asyncio.run(gate.execute(identity(), "run-1", executor=lambda: None, **kwargs))

    with pytest.raises(AuthorizationError):
        approval_service.decide_approval(identity(), blocked.value.approval.id, "approve")
    approved = approval_service.decide_approval(
        identity("lab-a", "runs:approve"),
        blocked.value.approval.id,
        "approve",
        note="Reviewed",
    )
    assert approved.status == "approved"
    assert approved.actor == "user:lab-a"
    assert approved.note == "Reviewed"
    assert database.runs[("lab-a", "run-1")]["state"] == RunState.RUNNING.value
    with pytest.raises(ApprovalStateError):
        approval_service.decide_approval(
            identity("lab-a", "runs:approve"), blocked.value.approval.id, "reject"
        )

    assert approval_service.active_approval(
        identity(),
        "run-1",
        kwargs["action"],
        kwargs["effect"],
        {"target": "dataset-2"},
    ) is None

    assert asyncio.run(gate.execute(identity(), "run-1", executor=lambda: "done", **kwargs)) == "done"
    assert database.approvals[0]["status"] == "consumed"
    with pytest.raises(ApprovalRequired):
        asyncio.run(
            gate.execute(
                identity(),
                "run-1",
                executor=lambda: pytest.fail("approval must be one-use"),
                **kwargs,
            )
        )


def test_decide_approval_writes_audit_row_on_the_same_cursor_as_the_update() -> None:
    from scilab.approvals import ActionGate, ApprovalRequired, ApprovalStateError

    database = Database()
    database.add_running_run()
    approval_service = service(database)
    gate = ActionGate(approval_service)
    with pytest.raises(ApprovalRequired) as blocked:
        asyncio.run(
            gate.execute(
                identity(),
                "run-1",
                action="publish",
                effect="publish",
                args_redacted={"target": "report", "api_key": "leak-me"},
                executor=lambda: None,
            )
        )
    approver = identity("lab-a", "runs:approve")
    approval = approval_service.decide_approval(
        approver, blocked.value.approval.id, "approve", note="Reviewed"
    )

    assert [row["action"] for row in database.audit_events] == ["approval.approved"]
    audit_row = database.audit_events[0]
    assert audit_row["run_id"] == "run-1"
    assert audit_row["actor"] == approver.principal
    assert audit_row["source"] == "approval"
    assert audit_row["details"] == {
        "approval_id": approval.id,
        "effect": approval.effect,
        "policy_rule": approval.policy_rule,
        "note": "Reviewed",
    }
    assert "leak-me" not in repr(database.audit_events)

    with pytest.raises(ApprovalStateError):
        approval_service.decide_approval(approver, approval.id, "approve")
    assert len(database.audit_events) == 1


def test_reject_and_expiry_cancel_run_with_canonical_reason() -> None:
    from scilab.approvals import ActionGate, ApprovalRequired

    rejected_db = Database()
    rejected_db.add_running_run()
    rejected_service = service(rejected_db)
    with pytest.raises(ApprovalRequired) as blocked:
        asyncio.run(
            ActionGate(rejected_service).execute(
                identity(),
                "run-1",
                action="delete",
                effect="delete",
                args_redacted={"target": "artifact-1"},
                executor=lambda: pytest.fail("must stay blocked"),
            )
        )
    rejected = rejected_service.decide_approval(
        identity("lab-a", "runs:approve"), blocked.value.approval.id, "reject", note="Unsafe"
    )
    assert rejected.status == "rejected"
    assert rejected_db.runs[("lab-a", "run-1")]["reason"] == "approval_rejected"
    assert [row["action"] for row in rejected_db.audit_events] == ["approval.rejected"]

    expired_db = Database()
    expired_db.add_running_run()
    expired_service = service(expired_db)
    with pytest.raises(ApprovalRequired):
        asyncio.run(
            ActionGate(expired_service).execute(
                identity(),
                "run-1",
                action="network.change",
                effect="network_change",
                args_redacted={"host": "example.org"},
                executor=lambda: pytest.fail("must stay blocked"),
            )
        )
    assert expired_service.expire_approvals(identity(), now=NOW + timedelta(hours=24) - timedelta(microseconds=1)) == []
    assert expired_db.audit_events == []
    expired = expired_service.expire_approvals(identity(), now=NOW + timedelta(hours=24))
    assert [item.status for item in expired] == ["expired"]
    assert expired_db.runs[("lab-a", "run-1")]["reason"] == "approval_expired"
    assert [row["action"] for row in expired_db.audit_events] == ["approval.expired"]

    stopped_db = Database()
    stopped_db.add_running_run()
    stopped_service = service(stopped_db)
    with pytest.raises(ApprovalRequired):
        asyncio.run(
            ActionGate(stopped_service).execute(
                identity(),
                "run-1",
                action="publish",
                effect="publish",
                args_redacted={"target": "report"},
                executor=lambda: pytest.fail("must stay blocked"),
            )
        )
    stopped_db.runs[("lab-a", "run-1")].update(
        state="cancelled", reason="stopped", approval_expires_at=None
    )
    stopped = stopped_service.expire_approvals(identity(), now=NOW + timedelta(hours=24))
    assert [item.status for item in stopped] == ["expired"]
    assert stopped_db.runs[("lab-a", "run-1")]["reason"] == "stopped"
    assert [row["action"] for row in stopped_db.audit_events] == ["approval.expired"]

    approved_db = Database()
    approved_db.add_running_run()
    approved_service = service(approved_db)
    with pytest.raises(ApprovalRequired) as approved_block:
        asyncio.run(
            ActionGate(approved_service).execute(
                identity(),
                "run-1",
                action="publish",
                effect="publish",
                args_redacted={"target": "report"},
                executor=lambda: pytest.fail("must stay blocked"),
            )
        )
    approved_service.decide_approval(
        identity("lab-a", "runs:approve"),
        approved_block.value.approval.id,
        "approve",
        note="approved earlier",
    )
    approved_expired = approved_service.expire_approvals(
        identity(), now=NOW + timedelta(hours=24)
    )
    assert [item.status for item in approved_expired] == ["expired"]
    assert approved_expired[0].actor == "user:lab-a"
    assert approved_expired[0].note == "approved earlier"
    assert approved_expired[0].decided_at == NOW
    assert [row["action"] for row in approved_db.audit_events] == [
        "approval.approved", "approval.expired",
    ]
    with pytest.raises(ApprovalRequired):
        asyncio.run(
            ActionGate(approved_service).execute(
                identity(),
                "run-1",
                action="publish",
                effect="publish",
                args_redacted={"target": "report"},
                executor=lambda: pytest.fail("expired approval must not execute"),
            )
        )
    assert len(approved_db.approvals) == 2


def test_approval_decision_is_bound_to_the_run_in_the_public_path() -> None:
    from scilab.approvals import ActionGate, ApprovalNotFound, ApprovalRequired

    database = Database()
    database.add_running_run()
    approval_service = service(database)
    with pytest.raises(ApprovalRequired) as blocked:
        asyncio.run(
            ActionGate(approval_service).execute(
                identity(),
                "run-1",
                action="external.write",
                effect="external_write",
                args_redacted={"target": "dataset-1"},
                executor=lambda: None,
            )
        )

    with pytest.raises(ApprovalNotFound):
        approval_service.decide_approval(
            identity("lab-a", "runs:approve"),
            blocked.value.approval.id,
            "approve",
            run_id="run-other",
        )
    assert database.approvals[0]["status"] == "pending"
    assert database.audit_events == []


def test_policy_and_migration_are_fail_safe_tenant_scoped_and_additive() -> None:
    policy = (ROOT / "deploy/policies/approval.rego").read_text().lower()
    migration = (ROOT / "services/control-plane/migrations/005_approvals.sql").read_text().lower()

    assert "package scilab.approval" in policy
    assert "default allow := false" in policy
    assert "effect == \"read\"" in policy
    assert "requires_approval" in policy
    for effect in ("publish", "external_write", "delete", "credential_use", "network_change"):
        assert effect in policy

    assert "create table if not exists approvals" in migration
    assert "foreign key (run_id, lab_id) references runs(id, lab_id)" in migration
    assert "status in ('pending', 'approved', 'consumed', 'rejected', 'expired')" in migration
    assert "enable row level security" in migration
    assert "force row level security" in migration
    assert "current_setting('scilab.current_lab_id', true)" in migration


def test_hermes_request_approval_passes_and_stores_execute_effect() -> None:
    from scilab.approvals import APPROVAL_EFFECTS

    assert "execute" in APPROVAL_EFFECTS
    database = Database()
    database.add_running_run()
    opa = OPA()
    approval_service = service(database, opa=opa)
    approval, _ = asyncio.run(
        approval_service.request_hermes_approval(
            identity(), "run-1", "req-execute",
            {"command": "rm -rf /tmp/x", "description": "delete a file", "pattern_keys": []},
            effect="execute",
        )
    )
    assert approval.effect == "execute"
    assert opa.inputs[-1]["effect"] == "execute"
    assert database.approvals[0]["effect"] == "execute"


def test_hermes_request_approval_defaults_unknown_effect_for_bad_value() -> None:
    database = Database()
    database.add_running_run()
    opa = OPA()
    approval_service = service(database, opa=opa)
    approval, _ = asyncio.run(
        approval_service.request_hermes_approval(
            identity(), "run-1", "req-bad-effect", {}, effect="not-a-real-effect",
        )
    )
    assert approval.effect == "unknown"
    assert opa.inputs[-1]["effect"] == "unknown"


def test_hermes_request_stage_retry_and_confirm() -> None:
    from scilab.approvals import ApprovalNotFound, ApprovalStateError

    database = Database()
    database.add_running_run()
    events = Events()
    approval_service = service(database, events=events)
    approval, awaiting = asyncio.run(approval_service.request_hermes_approval(
        identity(), "run-1", "req-1", {"command": "redacted", "api_key": "secret"}
    ))
    again, _ = asyncio.run(approval_service.request_hermes_approval(
        identity(), "run-1", "req-1", {"command": "redacted"}
    ))
    assert again.id == approval.id
    assert awaiting.state is RunState.AWAITING_APPROVAL
    assert awaiting.runtime_used == timedelta(0)
    assert approval.preview["api_key"] == "[REDACTED]"
    assert [event[2] for event in events.published] == ["approval.required", "run.state"]
    assert events.published[1][3] == {
        "from": "running", "to": "awaiting_approval", "reason": "approval_required"
    }
    assert approval_service.hermes_request_id(identity("lab-a", "runs:approve"), approval.id, "run-1") == "req-1"
    assert approval_service.hermes_run_id(identity("lab-a", "runs:approve"), approval.id, "run-1") == "hermes-1"
    approver = identity("lab-a", "runs:approve")
    assert approval_service.stage_hermes_decision(
        approver, approval.id, "approve", note="reviewed", run_id="run-1"
    ) == "req-1"
    assert database.runs[("lab-a", "run-1")]["state"] == "awaiting_approval"
    retrying_actor = Identity("lab-a", "user:second", frozenset({"runs:approve"}))
    assert approval_service.stage_hermes_decision(
        retrying_actor, approval.id, "approve", note="retry", run_id="run-1"
    ) == "req-1"
    with pytest.raises(ApprovalStateError):
        approval_service.stage_hermes_decision(approver, approval.id, "reject", run_id="run-1")
    assert approval_service.final_hermes_approval(
        approver, approval.id, "approve", run_id="run-1"
    ) is None
    confirmed = asyncio.run(
        approval_service.confirm_hermes_decision(retrying_actor, approval.id, run_id="run-1")
    )
    assert confirmed.status == "approved"
    assert confirmed.note == "reviewed"
    assert confirmed.actor == approver.principal
    assert confirmed.decided_at == NOW
    assert database.runs[("lab-a", "run-1")]["state"] == "running"
    assert [row["action"] for row in database.audit_events] == ["approval.approved"]
    assert database.audit_events[0]["actor"] == approver.principal
    assert database.audit_events[0]["details"]["hermes_request_id"] == "req-1"
    assert database.audit_events[0]["details"]["note"] == "reviewed"
    assert asyncio.run(
        approval_service.confirm_hermes_decision(approver, approval.id, run_id="run-1")
    ) == confirmed
    assert len(database.audit_events) == 1
    assert approval_service.final_hermes_approval(
        retrying_actor, approval.id, "approve", run_id="run-1"
    ) == confirmed
    with pytest.raises(ApprovalStateError):
        approval_service.final_hermes_approval(
            retrying_actor, approval.id, "reject", run_id="run-1"
        )
    with pytest.raises(ApprovalNotFound):
        approval_service.final_hermes_approval(
            identity("lab-b", "runs:approve"), approval.id, "approve", run_id="run-1"
        )
    with pytest.raises(ApprovalNotFound):
        approval_service.final_hermes_approval(
            retrying_actor, approval.id, "approve", run_id="other-run"
        )
    assert [event.type for event in events.fanned_out] == ["run.state"]
    assert events.recorded == events.fanned_out
    assert events.recorded[0].payload == {
        "from": "awaiting_approval", "to": "running", "reason": "approval_approved"
    }


def test_hermes_reject_is_bound_and_cannot_use_platform_decision_path() -> None:
    from scilab.approvals import ApprovalNotFound, ApprovalStateError

    database = Database()
    database.add_running_run()
    approval_service = service(database)
    approval, _ = asyncio.run(approval_service.request_hermes_approval(
        identity(), "run-1", "req-1", {"token": "sensitive"}
    ))
    approver = identity("lab-a", "runs:approve")
    assert approval_service.hermes_request_id(
        identity("lab-b", "runs:approve"), approval.id, "run-1"
    ) is None
    with pytest.raises(ApprovalNotFound):
        approval_service.stage_hermes_decision(approver, approval.id, "reject", run_id="other")
    with pytest.raises(ApprovalStateError):
        approval_service.decide_approval(approver, approval.id, "approve", run_id="run-1")
    approval_service.stage_hermes_decision(approver, approval.id, "reject", run_id="run-1")
    assert database.runs[("lab-a", "run-1")]["state"] == "awaiting_approval"
    rejected = asyncio.run(
        approval_service.confirm_hermes_decision(approver, approval.id, run_id="run-1")
    )
    assert rejected.status == "rejected"
    assert database.runs[("lab-a", "run-1")]["reason"] == "approval_rejected"
    assert [row["action"] for row in database.audit_events] == ["approval.rejected"]
    assert database.audit_events[0]["actor"] == approver.principal


def test_hermes_request_id_validation_and_24_hour_boundary() -> None:
    from scilab.approvals import ApprovalStateError

    database = Database()
    database.add_running_run()
    approval_service = service(database)
    for request_id in ("", " " * 2, "x" * 257):
        with pytest.raises(ValueError):
            asyncio.run(approval_service.request_hermes_approval(identity(), "run-1", request_id, {}))
    approval, _ = asyncio.run(approval_service.request_hermes_approval(identity(), "run-1", "req-1", {}))
    assert approval.expires_at == NOW + timedelta(hours=24)
    expired = approval_service.expire_approvals(identity(), now=approval.expires_at)
    assert [item.status for item in expired] == ["expired"]
    assert database.runs[("lab-a", "run-1")]["runtime_used"] == timedelta(0)
    with pytest.raises(ApprovalStateError):
        approval_service.stage_hermes_decision(
            identity("lab-a", "runs:approve"), approval.id, "approve", run_id="run-1"
        )


def test_hermes_decision_supports_tuple_database_rows() -> None:
    database = Database()
    database.tuple_rows = True
    database.add_running_run()
    approval_service = service(database)
    approval, _ = asyncio.run(approval_service.request_hermes_approval(
        identity(), "run-1", "req-1", {}
    ))
    approver = identity("lab-a", "runs:approve")
    assert approval_service.hermes_request_id(approver, approval.id, "run-1") == "req-1"
    assert approval_service.stage_hermes_decision(
        approver, approval.id, "approve", run_id="run-1"
    ) == "req-1"
    assert asyncio.run(
        approval_service.confirm_hermes_decision(approver, approval.id, run_id="run-1")
    ).status == "approved"


@pytest.mark.skipif(not os.getenv("SCILAB_TEST_POSTGRES_DSN"), reason="disposable PostgreSQL DSN unavailable")
def test_postgres_decide_approval_audit_insert_is_transactional_and_append_only() -> None:
    import psycopg

    from scilab.approvals import ApprovalService
    from scilab.events import EventService

    class Policy:
        def evaluate(self, policy_input: dict[str, object]) -> dict[str, object]:
            return {
                "allow": False,
                "requires_approval": True,
                "reason": "review",
                "policy_rule": "human-review",
            }

    class Bus:
        async def publish(self, subject: str, data: bytes) -> None:
            return None

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with psycopg.connect(os.environ["SCILAB_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        for path in sorted(migrations.glob("*.sql")):
            conn.execute(path.read_text())
        with conn.transaction():
            conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
            conn.execute("INSERT INTO labs (id, name) VALUES ('lab-a', 'A')")
            conn.execute(
                "INSERT INTO runs (id, lab_id, idempotency_key, state, hermes_run_id, "
                "created_at, updated_at, queued_at, running_since) "
                "VALUES ('run-1', 'lab-a', 'key-1', 'running', 'vendor-1', "
                "now() - interval '1 minute', now() - interval '1 minute', "
                "now() - interval '1 minute', now() - interval '1 minute')"
            )

        actor = Identity("lab-a", "user:a", frozenset({"runs:write", "runs:approve"}))
        approval_events = EventService(conn, Bus())
        approval_service = ApprovalService(
            conn, Policy(), approval_events,
            clock=lambda: datetime.now(timezone.utc),
        )
        approval = asyncio.run(
            approval_service.request_approval(actor, "run-1", "publish", "publish", {"target": "report"})
        )
        decided = approval_service.decide_approval(actor, approval.id, "approve", note="ok")

        with conn.transaction():
            conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
            audit_rows = conn.execute(
                "SELECT action, actor, source, details FROM audit_events "
                "WHERE lab_id = 'lab-a' AND run_id = 'run-1'"
            ).fetchall()
        assert [row[0] for row in audit_rows] == ["approval.approved"]
        assert audit_rows[0][1] == actor.principal
        assert audit_rows[0][2] == "approval"
        assert audit_rows[0][3]["approval_id"] == decided.id

        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with conn.transaction():
                conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
                conn.execute("UPDATE audit_events SET action = 'tampered' WHERE run_id = 'run-1'")

        second = asyncio.run(
            approval_service.request_approval(actor, "run-1", "delete", "delete", {"target": "x"})
        )
        conn.execute(
            "CREATE OR REPLACE FUNCTION fail_audit_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'audit insert failed'; END; $$"
        )
        conn.execute(
            "CREATE TRIGGER fail_audit_insert BEFORE INSERT ON audit_events "
            "FOR EACH ROW EXECUTE FUNCTION fail_audit_insert()"
        )
        with pytest.raises(psycopg.errors.RaiseException, match="audit insert failed"):
            approval_service.decide_approval(actor, second.id, "approve")
        conn.execute("DROP TRIGGER fail_audit_insert ON audit_events")
        conn.execute("DROP FUNCTION fail_audit_insert()")

        with conn.transaction():
            conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
            status = conn.execute(
                "SELECT status FROM approvals WHERE id = %s", (second.id,)
            ).fetchone()[0]
            run_state = conn.execute("SELECT state FROM runs WHERE id = 'run-1'").fetchone()[0]
        assert status == "pending"
        assert run_state == "awaiting_approval"
