import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scilab.approvals import Approval
from scilab.artifacts import Artifact, ArtifactNotFound
from scilab.identity import Identity
from scilab.metering import UsageSummary
from scilab.runs.model import Run, RunState
from scilab.tenancy import AuthorizationError, require_scope
from scilab.api.mcp_services import MCPServices


NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


def _run(
    state: RunState = RunState.QUEUED, *, hermes_run_id: str | None = None
) -> Run:
    return Run(
        id="run-1",
        lab_id="lab-a",
        idempotency_key="request-1",
        state=state,
        reason=None,
        retry_count=0,
        max_minutes=120,
        hermes_run_id=hermes_run_id,
        created_at=NOW,
        updated_at=NOW,
        queued_at=NOW,
        running_since=NOW if state is not RunState.QUEUED else None,
        runtime_used=timedelta(0),
        last_heartbeat_at=None,
        approval_expires_at=(NOW + timedelta(hours=1))
        if state is RunState.AWAITING_APPROVAL
        else None,
    )


class _Harness:
    def __init__(
        self,
        *,
        cycle_lab: str = "lab-a",
        current_run: Run | None = None,
        created_run: Run | None = None,
        transitioned_run: Run | None = None,
        event_rows: list[object] | None = None,
        artifact_rows: list[Artifact] | None = None,
        manifest_verified: bool = True,
    ) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.current_run = current_run or _run()
        self.created_run = created_run or _run()
        self.transitioned_run = transitioned_run or _run(
            RunState.RUNNING, hermes_run_id="hermes-1"
        )
        self.event_rows = event_rows or []
        self.artifact_rows = artifact_rows or []
        self.manifest_verified = manifest_verified

        def create(identity: Identity, key: str, **kwargs: object) -> Run:
            self.calls.append(("runs.create", identity, key, kwargs))
            return self.created_run

        def transition(
            identity: Identity, run_id: str, target: str, **kwargs: object
        ) -> Run:
            self.calls.append(("runs.transition", identity, run_id, target, kwargs))
            return self.transitioned_run

        def get(identity: Identity, run_id: str) -> Run:
            self.calls.append(("runs.get", identity, run_id))
            return self.current_run

        async def publish_event(*args: object) -> None:
            self.calls.append(("events.publish_event", *args))

        def replay_events(
            identity: Identity, run_id: str, from_seq: int = 0
        ) -> list[object]:
            self.calls.append(("events.replay_events", identity, run_id, from_seq))
            return self.event_rows

        def check(identity: Identity, payload: dict[str, object], key: str) -> None:
            self.calls.append(("admission.check", identity, payload, key))

        def aggregate_usage(identity: Identity, **kwargs: object) -> UsageSummary:
            self.calls.append(("metering.aggregate_usage", identity, kwargs))
            return UsageSummary(10, 20, 0.3, 0.2, 0.3, 4.0, 3.5)

        def decide_approval(
            identity: Identity,
            approval_id: str,
            decision: str,
            *,
            note: str | None = None,
            run_id: str | None = None,
        ) -> Approval:
            self.calls.append(
                ("approvals.decide_approval", identity, approval_id, decision, note, run_id)
            )
            return Approval(
                id=approval_id,
                run_id=run_id or "run-1",
                lab_id=identity.lab_id,
                action="write_file",
                effect="file.write",
                action_fingerprint="fingerprint",
                status="approved" if decision == "approve" else "rejected",
                reason="human decision",
                policy_rule="test-rule",
                preview={"path": "result.txt"},
                requested_at=NOW,
                expires_at=NOW + timedelta(hours=1),
                decided_at=NOW,
                actor=identity.principal,
                note=note,
            )

        def list_for_run(identity: Identity, run_id: str) -> list[Artifact]:
            self.calls.append(("artifacts.list_for_run", identity, run_id))
            return self.artifact_rows

        def get_artifact(identity: Identity, artifact_id: str) -> Artifact:
            self.calls.append(("artifacts.get", identity, artifact_id))
            require_scope(identity, "artifacts:read")
            for row in self.artifact_rows:
                if row.id == artifact_id and row.lab_id == identity.lab_id:
                    return row
            raise ArtifactNotFound("artifact not found")

        def presign(identity: Identity, artifact_id: str) -> str:
            self.calls.append(("artifacts.presign", identity, artifact_id))
            return f"https://objects.example/{artifact_id}"

        def verify(identity: Identity, run_id: str) -> bool:
            self.calls.append(("manifests.verify", identity, run_id))
            return self.manifest_verified

        async def run_cycle(goal: str, **kwargs: object) -> str:
            self.calls.append(("cycle.run", goal, kwargs))
            return "hermes-1"

        cycle = SimpleNamespace(lab_id=cycle_lab, run=run_cycle)

        def cycle_for_lab(lab_id: str) -> SimpleNamespace:
            self.calls.append(("cycle_for_lab", lab_id))
            return cycle

        async def ask_knowledge(identity: Identity, question: str) -> dict[str, object]:
            self.calls.append(("knowledge.ask", identity, question))
            return {"answer": "The Lab report says so.", "sources": ["artifact-1"]}

        self.services = MCPServices(
            runs=SimpleNamespace(create=create, transition=transition, get=get),
            events=SimpleNamespace(
                publish_event=publish_event, replay_events=replay_events
            ),
            metering=SimpleNamespace(aggregate_usage=aggregate_usage),
            approvals=SimpleNamespace(decide_approval=decide_approval),
            artifacts=SimpleNamespace(list_for_run=list_for_run, presign=presign, get=get_artifact),
            manifests=SimpleNamespace(verify=verify),
            admission=SimpleNamespace(check=check),
            cycle_for_lab=cycle_for_lab,
            knowledge=SimpleNamespace(ask=ask_knowledge),
        )


def _identity(*scopes: str) -> Identity:
    return Identity("lab-a", "user:alice", frozenset(scopes))


def _artifact(artifact_id: str, kind: str, created_at: datetime = NOW) -> Artifact:
    return Artifact(
        id=artifact_id,
        run_id="run-1",
        lab_id="lab-a",
        kind=kind,
        uri=f"s3://lab-a/run-1/{artifact_id}",
        sha256="a" * 64,
        bytes=32,
        produced_by_step=1,
        metadata={},
        created_at=created_at,
    )


def test_start_research_checks_admission_then_runs_idempotently_and_publishes_state() -> None:
    harness = _Harness(
        created_run=_run(),
        transitioned_run=_run(RunState.RUNNING, hermes_run_id="hermes-1"),
    )
    identity = _identity("runs:write")
    payload = {"goal": "Compare the two methods", "budget_thb": 8.0, "max_minutes": 25}

    result = asyncio.run(
        harness.services.start_research(identity, payload, "request-42")
    )

    assert result == {"run_id": "run-1", "state": "running"}
    assert [call[0] for call in harness.calls] == [
        "cycle_for_lab",
        "admission.check",
        "runs.create",
        "cycle.run",
        "runs.transition",
        "events.publish_event",
    ]
    assert harness.calls[1] == ("admission.check", identity, payload, "request-42")
    assert harness.calls[2] == (
        "runs.create", identity, "request-42", {"max_minutes": 25, "budget_thb": 8.0}
    )
    assert harness.calls[3] == (
        "cycle.run",
        "Compare the two methods",
        {"idempotency_key": "request-42"},
    )
    assert harness.calls[5] == (
        "events.publish_event",
        identity,
        "run-1",
        "run.state",
        {"from": "queued", "to": "running", "reason": ""},
        "run-service",
    )


def test_start_research_leaves_optional_budget_and_max_minutes_unspecified() -> None:
    harness = _Harness()
    identity = _identity("runs:write")

    asyncio.run(
        harness.services.start_research(identity, {"goal": "Find evidence"}, "request-43")
    )

    assert harness.calls[1] == (
        "admission.check",
        identity,
        {"goal": "Find evidence"},
        "request-43",
    )
    assert harness.calls[2] == ("runs.create", identity, "request-43", {})


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"goal": "Research", "lab_id": "lab-b"}, ValueError),
    ],
)
def test_start_research_rejects_untrusted_or_unsupported_fields(
    payload: dict[str, object], error: type[Exception]
) -> None:
    harness = _Harness()

    with pytest.raises(error):
        asyncio.run(
            harness.services.start_research(_identity("runs:write"), payload, "request-44")
        )

    assert harness.calls == []


def test_start_research_rejects_a_cycle_for_another_lab_before_admission() -> None:
    harness = _Harness(cycle_lab="lab-b")

    with pytest.raises(AuthorizationError):
        asyncio.run(
            harness.services.start_research(
                _identity("runs:write"), {"goal": "Research"}, "request-45"
            )
        )

    assert [call[0] for call in harness.calls] == ["cycle_for_lab"]


def test_get_run_projects_actual_usage_and_pending_approval_event() -> None:
    pending = {
        "approval_id": "approval-1",
        "action": "write_file",
        "reason": "policy requires approval",
        "preview": {"path": "result.txt"},
    }
    harness = _Harness(
        current_run=_run(RunState.AWAITING_APPROVAL),
        event_rows=[SimpleNamespace(type="approval.required", payload=pending)],
    )

    result = harness.services.get_run(_identity("runs:read"), "run-1")

    assert result == {
        "run_id": "run-1",
        "state": "awaiting_approval",
        "progress": None,
        "partial_findings": None,
        "cost": 0.5,
        "budget_thb": None,
        "pending_approval": pending,
    }
    assert harness.calls[0] == ("runs.get", _identity("runs:read"), "run-1")
    assert harness.calls[1] == (
        "metering.aggregate_usage",
        _identity("runs:read"),
        {"run_id": "run-1"},
    )
    assert harness.calls[2][0] == "events.replay_events"


def test_get_run_marks_missing_pending_approval_event_unavailable() -> None:
    harness = _Harness(current_run=_run(RunState.AWAITING_APPROVAL))

    result = harness.services.get_run(_identity("runs:read"), "run-1")

    assert result["pending_approval"] == {"status": "unavailable"}


def test_get_events_and_usage_use_lab_scoped_run_filters() -> None:
    identity = _identity("runs:read")
    rows = [{"event_id": "event-1", "run_id": "run-1", "seq": 1}]
    harness = _Harness(event_rows=rows)

    assert harness.services.get_events(identity, "run-1", from_seq=1) == rows
    usage = harness.services.get_usage(identity, "run-1")

    assert usage["tokens_in"] == 10
    assert usage["tokens_out"] == 20
    assert usage["cost_thb"] == 0.5
    assert harness.calls[0] == ("events.replay_events", identity, "run-1", 1)
    assert harness.calls[1] == (
        "metering.aggregate_usage",
        identity,
        {"run_id": "run-1"},
    )


def test_approve_binds_the_decision_to_the_requested_run() -> None:
    harness = _Harness()
    identity = _identity("runs:approve")

    result = harness.services.approve(
        identity, "run-1", "approval-1", "approve", note="Reviewed"
    )

    assert result == {"ok": True}
    assert harness.calls == [
        ("approvals.decide_approval", identity, "approval-1", "approve", "Reviewed", "run-1")
    ]


def test_report_and_manifest_are_explicitly_unavailable_when_absent() -> None:
    harness = _Harness()
    identity = _identity("artifacts:read")

    assert harness.services.get_report(identity, "run-1") == {"status": "unavailable"}
    assert harness.services.get_manifest(identity, "run-1") == {"status": "unavailable"}
    assert not any(call[0] == "manifests.verify" for call in harness.calls)


def test_report_and_manifest_return_the_lab_scoped_artifact_links() -> None:
    older = _artifact("report-old", "report", NOW - timedelta(days=1))
    latest = _artifact("report-new", "report")
    manifest = _artifact("manifest-1", "manifest")
    harness = _Harness(artifact_rows=[older, latest, manifest])
    identity = _identity("artifacts:read")

    report = harness.services.get_report(identity, "run-1")
    result_manifest = harness.services.get_manifest(identity, "run-1")

    assert report["artifact_id"] == "report-new"
    assert report["url"] == "https://objects.example/report-new"
    assert report["content_type"] == "text/markdown"
    assert result_manifest["artifact_id"] == "manifest-1"
    assert result_manifest["url"] == "https://objects.example/manifest-1"
    assert result_manifest["verified"] is True
    assert result_manifest["content_type"] == "application/json"


def test_mcp_resources_read_actual_report_and_manifest_content() -> None:
    report = _artifact("report-1", "report")
    manifest = _artifact("manifest-1", "manifest")
    harness = _Harness(artifact_rows=[report, manifest])
    identity = _identity("artifacts:read")
    contents = {
        "report-1": b"# Result\nVerified findings.",
        "manifest-1": b'{"run_id":"run-1","lab_id":"lab-a"}',
    }
    harness.services.artifacts.read_bytes = lambda actor, artifact_id: contents[artifact_id]

    assert harness.services.read_report(identity, "run-1") == "# Result\nVerified findings."
    assert harness.services.read_manifest(identity, "run-1") == {
        "run_id": "run-1", "lab_id": "lab-a"
    }


def test_wait_run_reports_only_observed_events_then_returns_terminal_state() -> None:
    import asyncio

    harness = _Harness()
    identity = _identity("runs:read")
    states = iter([{"state": "running"}, {"state": "completed"}])
    harness.services.get_run = lambda actor, run_id: next(states)
    harness.services.get_events = lambda actor, run_id, from_seq=0: (
        [{"seq": 1, "type": "tool.finished"}] if from_seq == 0 else []
    )
    observed = []

    async def progress(count: float, message: str) -> None:
        observed.append((count, message))

    result = asyncio.run(
        harness.services.wait_run(identity, "run-1", timeout_s=2, progress=progress)
    )
    assert result == {"state": "completed"}
    assert observed == [(1.0, "tool.finished")]


def test_get_artifact_returns_actual_metadata_and_presigned_url() -> None:
    artifact = _artifact("report-1", "report")
    harness = _Harness()
    identity = _identity("artifacts:read")
    harness.services.artifacts.get = lambda actor, artifact_id: artifact

    result = harness.services.get_artifact(identity, artifact.id)
    assert result["id"] == artifact.id
    assert result["sha256"] == artifact.sha256
    assert result["url"] == "https://objects.example/report-1"
def test_start_research_wires_lab_inputs_pack_and_persisted_budget() -> None:
    harness = _Harness(artifact_rows=[_artifact("artifact-1", "document")])
    identity = _identity("runs:write", "artifacts:read")
    asyncio.run(
        harness.services.start_research(
            identity,
            {
                "goal": "Research",
                "inputs": ["artifact-1", "https://example.org/paper"],
                "skill_packs": ["general-research"],
                "budget_thb": 80.0,
            },
            "request-1",
        )
    )
    assert ("artifacts.get", identity, "artifact-1") in harness.calls
    assert ("runs.create", identity, "request-1", {"budget_thb": 80.0}) in harness.calls
    cycle = next(call for call in harness.calls if call[0] == "cycle.run")
    assert cycle[2]["inputs"] == ["artifact-1", "https://example.org/paper"]
    assert cycle[2]["skill_packs"] == ["general-research"]


def test_start_research_artifact_input_requires_read_scope() -> None:
    harness = _Harness(artifact_rows=[_artifact("artifact-1", "document")])
    with pytest.raises(AuthorizationError, match="artifacts:read"):
        asyncio.run(
            harness.services.start_research(
                _identity("runs:write"),
                {"goal": "Research", "inputs": ["artifact-1"]},
                "request-1",
            )
        )
    assert not any(call[0] == "runs.create" for call in harness.calls)


@pytest.mark.parametrize(
    "payload",
    [
        {"inputs": ["other-lab-artifact"]},
        {"skill_packs": ["unapproved-pack"]},
        {"inputs": ["file:///etc/passwd"]},
    ],
)
def test_start_research_rejects_unauthorized_research_inputs(payload: dict[str, object]) -> None:
    harness = _Harness()
    with pytest.raises((ValueError, ArtifactNotFound)):
        asyncio.run(
            harness.services.start_research(
                _identity("runs:write", "artifacts:read"),
                {"goal": "Research", **payload}, "request-1"
            )
        )
    assert not any(call[0] == "runs.create" for call in harness.calls)


def test_list_skills_reports_only_approved_pack_and_filters_names() -> None:
    harness = _Harness()
    identity = _identity("runs:read")
    all_skills = harness.services.list_skills(identity)
    assert len(all_skills) == 1
    assert all_skills[0]["pack"] == "general-research"
    assert len(all_skills[0]["skills"]) == 7
    filtered = harness.services.list_skills(identity, filter="paper")
    assert filtered == [{"pack": "general-research", "skills": ["paper-lookup"]}]


def test_ask_lab_uses_knowledge_without_run_or_sandbox() -> None:
    harness = _Harness()
    identity = _identity("artifacts:read")
    result = asyncio.run(harness.services.ask_lab(identity, question="What did we find?"))
    assert result == {"answer": "The Lab report says so.", "sources": ["artifact-1"]}
    assert harness.calls == [("knowledge.ask", identity, "What did we find?")]


def test_ask_lab_denies_without_artifact_read_scope() -> None:
    harness = _Harness()
    with pytest.raises(AuthorizationError):
        asyncio.run(harness.services.ask_lab(_identity("runs:write"), question="What did we find?"))
    assert harness.calls == []


def test_get_run_returns_persisted_budget() -> None:
    harness = _Harness(current_run=replace(_run(), budget_thb=80.0))
    assert harness.services.get_run(_identity("runs:read"), "run-1")["budget_thb"] == 80.0
