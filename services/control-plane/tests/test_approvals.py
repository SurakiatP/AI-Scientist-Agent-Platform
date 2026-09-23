from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

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

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        compact = " ".join(sql.split()).lower()
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
            status, decided_at, actor, note, lab_id, approval_id = params
            row = next(
                item
                for item in self.database.approvals
                if item["lab_id"] == lab_id and item["id"] == approval_id
            )
            row.update(status=status, decided_at=decided_at, actor=actor, note=note)
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
        return self.result[0] if self.result else None

    def fetchall(self) -> list[dict[str, object]]:
        return list(self.result)


class Database:
    def __init__(self) -> None:
        self.runs: dict[tuple[str, str], dict[str, object]] = {}
        self.approvals: list[dict[str, object]] = []
        self.current_lab_id: str | None = None

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

    async def publish_event(
        self,
        actor: Identity,
        run_id: str,
        event_type: str,
        payload: dict[str, object],
        source: str,
    ) -> None:
        self.published.append((actor, run_id, event_type, payload, source))


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
    expired = expired_service.expire_approvals(identity(), now=NOW + timedelta(hours=24))
    assert [item.status for item in expired] == ["expired"]
    assert expired_db.runs[("lab-a", "run-1")]["reason"] == "approval_expired"

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
