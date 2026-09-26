from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from scilab.artifacts import ArtifactNotFound
from scilab.identity import Identity
from scilab.runs.model import Run, RunState
from scilab.runs.state import apply_transition
from scilab.runs.worker import LEASE_SECONDS, RunClaim
from scilab.tenancy import AuthorizationError

_WORKER_MAIN_ENV_EXTRA = {
    "SCILAB_ARTIFACT_BUCKET": "artifacts",
    "SCILAB_HERMES_IMAGE": "registry.internal/hermes:16",
    "SCILAB_SKILLS_IMAGE": "registry.internal/skills:16",
    "SCILAB_HERMES_CONFIG_SHA256": "a" * 64,
    "SCILAB_SANDBOX_IMAGE": "registry.internal/sandbox:16",
}


def completed_output(text: str = "Report body.") -> str:
    """A PI report ending in a fenced JSON block valid ResearchResult."""
    return (
        f"{text}\n\n"
        "```json\n"
        '{"claims": [{"text": "finding one", "evidence": ["doi:10.1/xyz"], "confidence": 0.9}], '
        '"artifacts": [], "caveats": [], "next_steps": []}\n'
        "```"
    )

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


def identity(lab_id: str = "lab-a") -> Identity:
    return Identity(lab_id, "service:run-worker", frozenset({"runs:read", "runs:write"}))


def make_run(
    *,
    lab_id: str = "lab-a",
    state: RunState = RunState.QUEUED,
    hermes_run_id: str | None = None,
    running_since: datetime | None = None,
    max_minutes: int = 120,
    retry_count: int = 0,
    context_id: str | None = None,
) -> Run:
    return Run(
        id="run-1",
        lab_id=lab_id,
        idempotency_key="request-key-1",
        state=state,
        reason=None,
        retry_count=retry_count,
        max_minutes=max_minutes,
        hermes_run_id=hermes_run_id,
        created_at=NOW,
        updated_at=NOW,
        queued_at=NOW,
        running_since=running_since,
        runtime_used=timedelta(0),
        last_heartbeat_at=running_since,
        approval_expires_at=None,
        context_id=context_id,
        budget_thb=None,
    )


class FakeWorker:
    def __init__(self, run: Run, payload: dict[str, Any], *, actor: str | None = None) -> None:
        self.run = run
        self.claim = RunClaim(
            run,
            payload,
            uuid4(),
            NOW + timedelta(seconds=LEASE_SECONDS),
            actor,
        )
        self.transitions: list[tuple[RunState, dict[str, Any]]] = []
        self.released = False
        self.renew_calls = 0
        self.fail_renew_after: int | None = None

    def claim_next(self, _identity: Identity) -> RunClaim | None:
        # Mirrors the production claim query: only queued/running Runs are
        # claimable, so a terminal Run is never re-claimed on the next poll.
        if self.run.state in (RunState.FAILED, RunState.COMPLETED, RunState.CANCELLED):
            return None
        return self.claim

    def renew(self, _identity: Identity, _claim: RunClaim) -> datetime | None:
        self.renew_calls += 1
        if self.fail_renew_after is not None and self.renew_calls > self.fail_renew_after:
            return None
        return NOW + timedelta(seconds=LEASE_SECONDS)

    def current(self, _identity: Identity, _claim: RunClaim) -> Run:
        return self.run

    def transition(
        self,
        _identity: Identity,
        _claim: RunClaim,
        target: RunState,
        **kwargs: Any,
    ) -> Run:
        self.transitions.append((target, kwargs))
        self.run = apply_transition(self.run, target, NOW, **kwargs)
        return self.run

    def release(self, _identity: Identity, _claim: RunClaim) -> bool:
        self.released = True
        return True


class FakeEvents:
    def __init__(self) -> None:
        self.published: list[tuple[Any, ...]] = []
        self.replay: list[Any] = []

    async def publish_event(self, *args: Any) -> None:
        self.published.append(args)

    def replay_events(self, _identity: Identity, _run_id: str) -> list[Any]:
        return self.replay


class FakeArtifacts:
    """Stands in for ArtifactService: hashes real bytes, rejects out-of-Lab URIs."""

    def __init__(self) -> None:
        self.registered: list[dict[str, Any]] = []
        self._by_id: dict[str, Any] = {}

    def register(
        self, identity: Identity, run_id: str, *, kind: str, uri: str,
        content: bytes, produced_by_step: int, metadata: dict[str, Any] | None = None,
    ) -> Any:
        if not uri.startswith(f"s3://{identity.lab_id}/{run_id}/"):
            raise ValueError("artifact URI is outside the authenticated Lab and run")
        sha256 = hashlib.sha256(content).hexdigest()
        artifact = SimpleNamespace(
            id=f"artifact-{len(self.registered) + 1}", uri=uri, sha256=sha256,
            kind=kind, bytes=len(content),
        )
        self.registered.append({
            "identity": identity, "run_id": run_id, "kind": kind, "uri": uri,
            "content": content, "sha256": sha256,
        })
        self._by_id[artifact.id] = artifact
        return artifact

    def get(self, _identity: Identity, artifact_id: str) -> Any:
        try:
            return self._by_id[artifact_id]
        except KeyError:
            raise ArtifactNotFound(artifact_id) from None


class FakeManifests:
    """Stands in for ManifestService.seal; can be told to raise like a lineage failure."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.seal_calls: list[tuple[Any, str, dict[str, Any]]] = []
        self.error = error

    def seal(self, identity: Identity, run_id: str, draft: dict[str, Any]) -> Any:
        self.seal_calls.append((identity, run_id, draft))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(artifact=SimpleNamespace(id="manifest-1"))


def manifest_kwargs(**overrides: Any) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "artifacts": FakeArtifacts(),
        "manifests": FakeManifests(),
        "hermes_image": "registry.internal/hermes:16",
        "hermes_config_sha256": "a" * 64,
        "skills_image": "registry.internal/skills:16",
        "sandbox_image": "registry.internal/sandbox:16",
    }
    defaults.update(overrides)
    return defaults


def _fill_terminal_run_id(message: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Fixtures that don't care about run_id binding get the real one filled in.

    Fixtures that set run_id explicitly (correct or deliberately wrong, for
    negative tests) are left untouched.
    """
    if message.get("event") not in ("run.completed", "run.failed"):
        return message
    data = message.get("data")
    if isinstance(data, dict):
        if "run_id" in data:
            return message
        return {**message, "data": {**data, "run_id": run_id}}
    if "run_id" in message:
        return message
    return {**message, "run_id": run_id}


class FakeHermes:
    def __init__(self) -> None:
        self.messages = [{
            "id": "evt-1", "event": "run.completed",
            "data": {"output": completed_output(), "runtime": {"provider": "vendor", "model": "sci-pi-frontier"}},
        }]
        self.stopped: list[tuple[str, str]] = []
        self.event_calls: list[tuple[str, str]] = []

    async def events(self, _lab_id: str, _run_id: str):
        self.event_calls.append((_lab_id, _run_id))
        for message in self.messages:
            yield _fill_terminal_run_id(message, _run_id)

    async def stop(self, lab_id: str, run_id: str) -> None:
        self.stopped.append((lab_id, run_id))


class FakeCycle:
    lab_id = "lab-a"

    def __init__(self) -> None:
        self.client = FakeHermes()
        self.calls: list[tuple[Any, ...]] = []

    async def run(self, goal: str, **kwargs: Any) -> str:
        self.calls.append((goal, kwargs))
        return "hermes-run-1"


def test_executor_starts_with_stable_key_and_normalizes_terminal_event() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(),
        {
            "goal": "study the evidence",
            "inputs": ["input-1"],
            "skill_packs": ["general-research"],
            "budget": {"thb": 10, "max_minutes": 120},
            "options": {},
        },
    )
    events = FakeEvents()
    cycle = FakeCycle()
    executor = RunExecutor(worker, events, lambda lab_id: cycle, clock=lambda: NOW, **manifest_kwargs())

    result = asyncio.run(executor.execute_next(identity()))

    assert cycle.calls == [
        (
            "study the evidence",
            {
                "idempotency_key": "request-key-1",
                "inputs": ["input-1"],
                "skill_packs": ["general-research"],
            },
        )
    ]
    assert result is not None and result.state is RunState.COMPLETED
    assert worker.transitions == [
        (RunState.RUNNING, {"hermes_run_id": "hermes-run-1"}),
        (RunState.COMPLETED, {"completion_ready": True}),
    ]
    assert [call[2] for call in events.published] == ["run.state", "run.completed", "run.state"]
    assert worker.released


def test_executor_persists_run_failed_terminal_event_for_sse() -> None:
    """run.failed still passes the raw Hermes payload through unchanged.

    run.completed no longer does this (F1): it is rebuilt via completed_payload
    once artifacts are registered and the manifest is sealed, covered by
    test_executor_registers_artifacts_and_seals_manifest_before_completing.
    """
    from scilab.runs.executor import RunExecutor

    payload = {"reason": "research_error"}
    worker = FakeWorker(make_run(), {"goal": "study", "options": {}})
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [{"event": "run.failed", "data": payload}]

    result = asyncio.run(RunExecutor(worker, events, lambda _: cycle, clock=lambda: NOW).execute_next(identity()))

    assert result is not None and result.state is RunState.FAILED
    assert [event[2] for event in events.published] == ["run.state", "run.failed", "run.state"]
    assert events.published[1][3] == payload
    assert events.published[1][4] == "hermes"


def test_executor_pauses_for_valid_hermes_approval_and_reclaims_same_run() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(make_run(), {"goal": "study", "options": {}})
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [{
        "event": "approval.request", "run_id": "hermes-run-1",
        "request_id": "vendor-request-1", "command": "run --flag",
        "description": "review the plan", "pattern_key": "dangerous_shell:run",
        "api_key": "supersecret",
    }]

    class Approvals:
        calls: list[tuple[Any, ...]] = []

        async def request_hermes_approval(
            self, actor: Identity, run_id: str, request_id: str, preview: dict[str, Any],
            *, effect: str,
        ):
            self.calls.append((actor.lab_id, run_id, request_id, preview, effect))
            worker.run = apply_transition(worker.run, RunState.AWAITING_APPROVAL, NOW)
            return object(), worker.run

    approvals = Approvals()
    executor = RunExecutor(worker, events, lambda _: cycle, approvals=approvals, clock=lambda: NOW)
    paused = asyncio.run(executor.execute_next(identity()))
    assert paused is not None and paused.state is RunState.AWAITING_APPROVAL
    assert worker.released
    assert approvals.calls == [("lab-a", "run-1", "vendor-request-1", {
        "command": "run --flag",
        "description": "review the plan",
        "pattern_keys": ["dangerous_shell:run"],
    }, "execute")]
    assert "supersecret" not in repr(approvals.calls) + repr(events.published)
    worker.run = apply_transition(worker.run, RunState.RUNNING, NOW)
    worker.released = False
    cycle.client.messages = [{"event": "run.failed", "data": {"reason": "error"}}]
    resumed = asyncio.run(executor.execute_next(identity()))
    assert resumed is not None and resumed.state is RunState.FAILED
    assert len(cycle.calls) == 1
    assert cycle.client.event_calls[-1] == ("lab-a", "hermes-run-1")


@pytest.mark.parametrize(
    ("message_extra", "expected_effect"),
    [
        ({"pattern_key": "dangerous_shell:rm"}, "execute"),
        ({"pattern_keys": ["execute_code"]}, "execute"),
        ({"pattern_keys": ["plugin_rule:computer_use:abcd"]}, "unknown"),
        ({"pattern_key": "dangerous_shell:rm", "pattern_keys": ["plugin_rule:ssh:1"]}, "unknown"),
        ({}, "unknown"),
    ],
)
def test_executor_maps_hermes_pattern_keys_to_opa_effect(
    message_extra: dict[str, Any], expected_effect: str
) -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(make_run(), {"goal": "study", "options": {}})
    cycle = FakeCycle()
    cycle.client.messages = [{
        "event": "approval.request", "run_id": "hermes-run-1",
        "request_id": "req-1", **message_extra,
    }]

    class Approvals:
        def __init__(self) -> None:
            self.effects: list[str] = []

        async def request_hermes_approval(self, *_: Any, effect: str):
            self.effects.append(effect)
            worker.run = apply_transition(worker.run, RunState.AWAITING_APPROVAL, NOW)
            return object(), worker.run

    approvals = Approvals()
    asyncio.run(
        RunExecutor(worker, FakeEvents(), lambda _: cycle, approvals=approvals, clock=lambda: NOW)
        .execute_next(identity())
    )
    assert approvals.effects == [expected_effect]


def test_executor_redacts_sensitive_keys_in_hermes_approval_preview() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(make_run(), {"goal": "study", "options": {}})
    cycle = FakeCycle()
    cycle.client.messages = [{
        "event": "approval.request", "run_id": "hermes-run-1", "request_id": "req-1",
        "command": {"cmd": "curl", "api_key": "leaked-secret"},
        "description": "call api",
    }]

    class Approvals:
        def __init__(self) -> None:
            self.previews: list[dict[str, Any]] = []

        async def request_hermes_approval(self, _actor, _run_id, _request_id, preview, *, effect: str):
            self.previews.append(preview)
            worker.run = apply_transition(worker.run, RunState.AWAITING_APPROVAL, NOW)
            return object(), worker.run

    approvals = Approvals()
    asyncio.run(
        RunExecutor(worker, FakeEvents(), lambda _: cycle, approvals=approvals, clock=lambda: NOW)
        .execute_next(identity())
    )
    assert approvals.previews == [{
        "command": {"cmd": "curl", "api_key": "[REDACTED]"},
        "description": "call api",
        "pattern_keys": [],
    }]
    assert "leaked-secret" not in repr(approvals.previews)


@pytest.mark.parametrize("vendor_event", [
    {"event": "approval.request", "run_id": "other", "request_id": "req"},
    {"event": "approval.request", "run_id": "hermes-run-1", "request_id": ""},
    {"event": "approval.request", "run_id": "hermes-run-1", "request_id": " req "},
])
def test_executor_rejects_invalid_approval_request(vendor_event: dict[str, str]) -> None:
    """RR-S1: a binding mismatch fails the Run instead of raising and re-looping."""
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(make_run(state=RunState.RUNNING, hermes_run_id="hermes-run-1", running_since=NOW), {"goal": "study", "options": {}})
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [vendor_event]

    result = asyncio.run(
        RunExecutor(worker, events, lambda _: cycle, approvals=object(), clock=lambda: NOW).execute_next(identity())
    )

    assert result is not None and result.state is RunState.FAILED
    assert result.reason == "error"
    assert worker.run.state is RunState.FAILED
    assert worker.released
    assert cycle.client.stopped == [("lab-a", "hermes-run-1")]
    assert [call[2] for call in events.published] == ["run.failed", "run.state"]

    # Not re-claimed: the terminal Run is no longer claimable on the next poll.
    second = asyncio.run(
        RunExecutor(worker, events, lambda _: cycle, approvals=object(), clock=lambda: NOW).execute_next(identity())
    )
    assert second is None


def test_executor_registers_artifacts_and_seals_manifest_before_completing() -> None:
    """T3b happy path: report + tool-log registered, manifest sealed before COMPLETED."""
    from scilab.contracts import RunEvent
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}}, actor="user:alice",
    )
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [{"event": "run.completed", "run_id": "hermes-existing", "output": completed_output()}]
    artifacts = FakeArtifacts()
    manifests = FakeManifests()
    original_seal = manifests.seal

    def seal_before_completed(*args: Any, **kwargs: Any) -> Any:
        assert worker.transitions == [], "manifest must seal before the COMPLETED transition"
        return original_seal(*args, **kwargs)

    manifests.seal = seal_before_completed  # type: ignore[method-assign]

    result = asyncio.run(asyncio.wait_for(
        RunExecutor(
            worker, events, lambda _: cycle, clock=lambda: NOW,
            **manifest_kwargs(artifacts=artifacts, manifests=manifests),
        ).execute_next(identity()),
        timeout=0.2,
    ))

    assert result is not None and result.state is RunState.COMPLETED
    assert [entry["kind"] for entry in artifacts.registered] == ["report", "tool-log"]
    report_entry, log_entry = artifacts.registered
    assert report_entry["sha256"] == hashlib.sha256(completed_output().encode("utf-8")).hexdigest()
    assert report_entry["uri"] == "s3://lab-a/run-1/attempt-0/report.md"
    assert log_entry["uri"] == "s3://lab-a/run-1/attempt-0/tool-log.jsonl"

    # seal happens before the COMPLETED transition (F10)
    assert len(manifests.seal_calls) == 1
    seal_identity, seal_run_id, draft = manifests.seal_calls[0]
    assert seal_run_id == "run-1"
    assert seal_identity.principal == "user:alice"
    assert draft["claims"][0]["evidence"] == ["doi:10.1/xyz"]
    assert draft["steps"][0]["outputs"] == ["artifact-1"]
    assert draft["steps"][0]["commands_log"] == "artifact-2"

    published_types = [call[2] for call in events.published]
    assert published_types == ["run.completed", "run.state"]
    completed_call = events.published[0]
    assert completed_call[3] == {
        "summary": "Report body.",
        "report_artifact_id": "artifact-1",
        "manifest_artifact_id": "manifest-1",
        "claims_count": 1,
    }
    assert completed_call[4] == "run-service"  # S5: not "hermes" -- this is a platform-built event
    event = RunEvent(
        event_id="evt-1", run_id="run-1", lab_id="lab-a", ts=NOW, type="run.completed",
        seq=1, payload=completed_call[3], source="hermes",
    )
    assert event.payload.manifest_artifact_id == "manifest-1"


def test_executor_fails_run_on_terminal_event_run_id_mismatch_in_data_wrapper() -> None:
    """S1/RR-S1: a data-wrapped terminal event bound to the wrong run_id fails the

    Run (instead of raising and being re-claimed forever, up to run_timeout) so
    other queued Runs in the Lab are not starved.
    """
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [{
        "event": "run.completed",
        "data": {"run_id": "some-other-run", "output": completed_output()},
    }]
    executor = RunExecutor(worker, events, lambda _: cycle, clock=lambda: NOW)

    result = asyncio.run(executor.execute_next(identity()))

    assert result is not None and result.state is RunState.FAILED
    assert result.reason == "error"
    assert cycle.client.stopped == [("lab-a", "hermes-existing")]
    assert [call[2] for call in events.published] == ["run.failed", "run.state"]

    # The claim is released in execute_next's finally, but the Run is terminal
    # so the fake (mirroring the production claim query) refuses to re-claim it.
    assert worker.released
    second = asyncio.run(executor.execute_next(identity()))
    assert second is None


def test_executor_reraises_transient_network_error_from_event_stream() -> None:
    """RR-S1: only non-transient ValueError/ValidationError are converted to FAILED.

    A transient httpx/network failure while reading the Hermes event stream must
    keep raising, so the claim is released and the Run is retried later.
    """
    import httpx

    from scilab.runs.executor import RunExecutor

    class FlakyHermes(FakeHermes):
        async def events(self, lab_id: str, run_id: str):
            self.event_calls.append((lab_id, run_id))
            raise httpx.ConnectError("connection reset")
            yield {}  # pragma: no cover - unreachable, keeps this an async generator

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    cycle = FakeCycle()
    cycle.client = FlakyHermes()

    with pytest.raises(httpx.ConnectError):
        asyncio.run(
            RunExecutor(worker, FakeEvents(), lambda _: cycle, clock=lambda: NOW).execute_next(identity())
        )

    assert worker.run.state is RunState.RUNNING
    assert worker.released


def test_executor_drops_hermes_sourced_run_state_event() -> None:
    """S1: a Hermes "run.state" frame is neither terminal nor allowlisted passthrough."""
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [
        {"event": "run.state", "run_id": "hermes-existing", "data": {"from": "RUNNING", "to": "COMPLETED"}},
        {"event": "run.failed", "data": {"reason": "error"}},
    ]

    result = asyncio.run(
        RunExecutor(worker, events, lambda _: cycle, clock=lambda: NOW).execute_next(identity())
    )

    assert result is not None and result.state is RunState.FAILED
    assert [call[2] for call in events.published] == ["run.failed", "run.state"]


def test_executor_includes_retry_attempt_in_artifact_uris() -> None:
    """S4: artifact URIs are scoped by attempt so retries don't clobber earlier ones."""
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(
            state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW,
            retry_count=1,
        ),
        {"goal": "study", "options": {}},
    )
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [{"event": "run.completed", "run_id": "hermes-existing", "output": completed_output()}]
    artifacts = FakeArtifacts()
    manifests = FakeManifests()

    result = asyncio.run(
        RunExecutor(
            worker, events, lambda _: cycle, clock=lambda: NOW,
            **manifest_kwargs(artifacts=artifacts, manifests=manifests),
        ).execute_next(identity())
    )

    assert result is not None and result.state is RunState.COMPLETED
    report_entry, log_entry = artifacts.registered
    assert report_entry["uri"] == "s3://lab-a/run-1/attempt-1/report.md"
    assert log_entry["uri"] == "s3://lab-a/run-1/attempt-1/tool-log.jsonl"


def test_executor_records_unpriced_models_in_manifest_cost() -> None:
    """S3: models metering couldn't price surface as cost.unpriced_models on the manifest."""
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    events = FakeEvents()
    events.replay = [{"type": "cost.updated", "payload": {"warning": "unpriced_model:vendor-new-model"}}]
    cycle = FakeCycle()
    cycle.client.messages = [{"event": "run.completed", "run_id": "hermes-existing", "output": completed_output()}]
    manifests = FakeManifests()

    result = asyncio.run(
        RunExecutor(
            worker, events, lambda _: cycle, clock=lambda: NOW,
            **manifest_kwargs(manifests=manifests),
        ).execute_next(identity())
    )

    assert result is not None and result.state is RunState.COMPLETED
    draft = manifests.seal_calls[0][2]
    assert draft["cost"]["unpriced_models"] == ["vendor-new-model"]


def test_executor_renews_lease_during_completion_steps() -> None:
    """R: a live worker's lease must be renewed across the slow completion steps."""
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    cycle = FakeCycle()
    cycle.client.messages = [{"event": "run.completed", "run_id": "hermes-existing", "output": completed_output()}]

    result = asyncio.run(
        RunExecutor(
            worker, FakeEvents(), lambda _: cycle, clock=lambda: NOW, **manifest_kwargs()
        ).execute_next(identity())
    )

    assert result is not None and result.state is RunState.COMPLETED
    assert worker.renew_calls >= 2


def test_executor_renews_lease_immediately_after_repair_completes() -> None:
    """Problem 2: the repair chat call isn't itself lease-renewed; renew right

    after it returns too, and abort via the existing lost-lease path if that
    renewal fails.
    """
    from scilab.runs.executor import RunExecutor

    class RepairHermes(FakeHermes):
        async def chat_completion(
            self, _lab_id: str, *, model: str, messages: list[dict[str, str]]
        ) -> str:
            return completed_output("Repaired report.")

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    worker.fail_renew_after = 2  # the 3rd renew() is the one immediately after repair
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client = RepairHermes()
    cycle.client.messages = [{
        "event": "run.completed", "run_id": "hermes-existing", "output": "no json here at all",
    }]
    artifacts = FakeArtifacts()
    manifests = FakeManifests()

    result = asyncio.run(
        RunExecutor(
            worker, events, lambda _: cycle, clock=lambda: NOW,
            **manifest_kwargs(artifacts=artifacts, manifests=manifests),
        ).execute_next(identity())
    )

    assert worker.renew_calls == 3
    assert result is not None and result.state is RunState.RUNNING
    assert artifacts.registered == []
    assert manifests.seal_calls == []
    assert events.published == []


def test_executor_aborts_completion_when_lease_is_lost_mid_seal() -> None:
    """R: a lost lease during completion must abort without completing the Run."""
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    worker.fail_renew_after = 1
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [{"event": "run.completed", "run_id": "hermes-existing", "output": completed_output()}]
    artifacts = FakeArtifacts()
    manifests = FakeManifests()

    result = asyncio.run(
        RunExecutor(
            worker, events, lambda _: cycle, clock=lambda: NOW,
            **manifest_kwargs(artifacts=artifacts, manifests=manifests),
        ).execute_next(identity())
    )

    assert result is not None and result.state is RunState.RUNNING
    assert worker.transitions == []
    assert manifests.seal_calls == []
    assert events.published == []


def test_executor_skips_replayed_confirmed_approval_request() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW), {"goal": "study", "options": {}})
    cycle = FakeCycle()
    cycle.client.messages = [
        {"event": "approval.request", "run_id": "hermes-existing", "request_id": "req-1"},
        {"event": "run.failed", "data": {"reason": "error"}},
    ]

    class Approvals:
        async def request_hermes_approval(self, *_: Any, effect: str):
            return object(), worker.run

    result = asyncio.run(RunExecutor(worker, FakeEvents(), lambda _: cycle, approvals=Approvals(), clock=lambda: NOW).execute_next(identity()))
    assert result is not None and result.state is RunState.FAILED


def test_executor_recovers_running_claim_without_starting_another_hermes_run() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    cycle = FakeCycle()
    executor = RunExecutor(worker, FakeEvents(), lambda _lab_id: cycle, clock=lambda: NOW, **manifest_kwargs())

    result = asyncio.run(executor.execute_next(identity()))

    assert cycle.calls == []
    assert cycle.client.event_calls == [("lab-a", "hermes-existing")]
    assert result is not None and result.state is RunState.COMPLETED


def test_executor_denies_a_claim_for_another_lab() -> None:
    from scilab.runs.executor import RunExecutor

    cycle = FakeCycle()
    worker = FakeWorker(make_run(lab_id="lab-b"), {"goal": "study", "options": {}})
    executor = RunExecutor(worker, FakeEvents(), lambda _lab_id: cycle, clock=lambda: NOW)

    with pytest.raises(AuthorizationError):
        asyncio.run(executor.execute_next(identity("lab-a")))

    assert cycle.calls == []


def test_executor_stops_hermes_when_run_is_cancelled_during_start() -> None:
    from scilab.runs.executor import RunExecutor

    class CancellingWorker(FakeWorker):
        def transition(self, _identity: Identity, _claim: RunClaim, target: RunState, **kwargs: Any):
            if target is RunState.RUNNING:
                self.run = apply_transition(self.run, RunState.CANCELLED, NOW, reason="stopped")
                return None
            return super().transition(_identity, _claim, target, **kwargs)

    worker = CancellingWorker(make_run(), {"goal": "study", "options": {}})
    cycle = FakeCycle()
    executor = RunExecutor(worker, FakeEvents(), lambda _lab_id: cycle, clock=lambda: NOW)

    asyncio.run(executor.execute_next(identity()))

    assert cycle.client.stopped == [("lab-a", "hermes-run-1")]


def test_executor_times_out_recovered_run_and_stops_hermes() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(
            state=RunState.RUNNING,
            hermes_run_id="hermes-existing",
            running_since=NOW - timedelta(minutes=2),
            max_minutes=1,
        ),
        {"goal": "study", "options": {}},
    )
    cycle = FakeCycle()
    cycle.client.messages = []
    executor = RunExecutor(worker, FakeEvents(), lambda _lab_id: cycle, clock=lambda: NOW)

    result = asyncio.run(executor.execute_next(identity()))

    assert result is not None
    assert result.state is RunState.FAILED
    assert result.reason == "run_timeout"
    assert cycle.client.stopped == [("lab-a", "hermes-existing")]


def test_executor_times_out_while_hermes_event_stream_is_idle() -> None:
    from scilab.runs.executor import RunExecutor

    class IdleHermes(FakeHermes):
        async def events(self, lab_id: str, run_id: str):
            self.event_calls.append((lab_id, run_id))
            await asyncio.Event().wait()
            yield {}

    worker = FakeWorker(
        make_run(
            state=RunState.RUNNING,
            hermes_run_id="hermes-existing",
            running_since=NOW - timedelta(minutes=1) + timedelta(milliseconds=10),
            max_minutes=1,
        ),
        {"goal": "study", "options": {}},
    )
    cycle = FakeCycle()
    cycle.client = IdleHermes()
    executor = RunExecutor(worker, FakeEvents(), lambda _lab_id: cycle, clock=lambda: NOW)

    result = asyncio.run(asyncio.wait_for(executor.execute_next(identity()), timeout=0.1))

    assert result is not None and result.state is RunState.FAILED
    assert result.reason == "run_timeout"
    assert cycle.client.stopped == [("lab-a", "hermes-existing")]


def test_executor_observes_cancellation_while_hermes_event_stream_is_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scilab.runs import executor as executor_module

    monkeypatch.setattr(executor_module, "_RENEW_INTERVAL", 0.01)

    class IdleHermes(FakeHermes):
        async def events(self, lab_id: str, run_id: str):
            self.event_calls.append((lab_id, run_id))
            await asyncio.Event().wait()
            yield {}

    class CancellingWorker(FakeWorker):
        def renew(self, _identity: Identity, _claim: RunClaim) -> None:
            self.run = apply_transition(self.run, RunState.CANCELLED, NOW, reason="stopped")
            return None

    worker = CancellingWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    cycle = FakeCycle()
    cycle.client = IdleHermes()
    executor = executor_module.RunExecutor(
        worker, FakeEvents(), lambda _lab_id: cycle, clock=lambda: NOW
    )

    result = asyncio.run(asyncio.wait_for(executor.execute_next(identity()), timeout=0.2))

    assert result is not None and result.state is RunState.CANCELLED
    assert cycle.client.stopped == [("lab-a", "hermes-existing")]


def test_executor_retries_transient_start_with_the_same_idempotency_key() -> None:
    from scilab.runs.executor import RunExecutor

    class RetryCycle(FakeCycle):
        async def run(self, goal: str, **kwargs: Any) -> str:
            self.calls.append((goal, kwargs))
            if len(self.calls) == 1:
                raise ConnectionError("response lost after Hermes accepted request")
            return "hermes-run-1"

    worker = FakeWorker(make_run(), {"goal": "study", "options": {}})
    cycle = RetryCycle()
    executor = RunExecutor(worker, FakeEvents(), lambda _lab_id: cycle, clock=lambda: NOW, **manifest_kwargs())

    with pytest.raises(ConnectionError):
        asyncio.run(executor.execute_next(identity()))
    result = asyncio.run(executor.execute_next(identity()))

    assert [call[1]["idempotency_key"] for call in cycle.calls] == [
        "request-key-1",
        "request-key-1",
    ]
    assert result is not None and result.state is RunState.COMPLETED
    assert worker.released


def test_executor_suffixes_idempotency_key_and_passes_session_key_on_retry() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(retry_count=1, context_id="ctx-1"),
        {"goal": "study", "options": {}},
    )
    cycle = FakeCycle()
    executor = RunExecutor(worker, FakeEvents(), lambda _: cycle, clock=lambda: NOW, **manifest_kwargs())

    result = asyncio.run(executor.execute_next(identity()))

    assert cycle.calls == [
        (
            "study",
            {
                "idempotency_key": "request-key-1:retry-1",
                "inputs": [],
                "skill_packs": [],
                "session_key": "ctx-1",
            },
        )
    ]
    assert result is not None and result.state is RunState.COMPLETED


def test_executor_omits_session_key_when_run_has_no_context_id() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(make_run(), {"goal": "study", "options": {}})
    cycle = FakeCycle()
    executor = RunExecutor(worker, FakeEvents(), lambda _: cycle, clock=lambda: NOW)

    asyncio.run(executor.execute_next(identity()))

    assert cycle.calls[0][1] == {
        "idempotency_key": "request-key-1",
        "inputs": [],
        "skill_packs": [],
    }


class FakeMetering:
    def __init__(self, *, totals: tuple[int, int] = (0, 0), cancel_run: Run | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.totals = totals
        self.cancel_run = cancel_run

    async def record_model_usage_async(
        self, identity: Identity, run_id: str, *, actor: str, source: str, model: str,
        tokens_in: int, tokens_out: int, metadata: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append({
            "run_id": run_id, "actor": actor, "source": source, "model": model,
            "tokens_in": tokens_in, "tokens_out": tokens_out, "metadata": metadata,
        })
        if self.cancel_run is not None:
            self.cancel_run()

    def run_token_totals(self, _identity: Identity, _run_id: str) -> tuple[int, int]:
        return self.totals

    def aggregate_usage(self, _identity: Identity, *, run_id: str) -> Any:
        return SimpleNamespace(tokens_in=0, tokens_out=0, llm_cost_thb=0.0, compute_cost_thb=0.0)


def test_executor_publishes_allowlisted_tool_and_delegation_events_and_records_usage() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}, "channel": "a2a"},
        actor="user:alice",
    )
    events = FakeEvents()
    metering = FakeMetering()
    cycle = FakeCycle()
    cycle.client.messages = [
        {
            "event": "tool.started", "run_id": "hermes-existing",
            "data": {"tool": "search", "args_redacted": {}, "duration_ms": 0, "ok": True},
        },
        {
            "event": "tool.finished", "run_id": "hermes-existing",
            "data": {"tool": "search", "args_redacted": {}, "duration_ms": 120, "ok": True},
        },
        {
            "event": "delegation.started", "run_id": "hermes-existing",
            "data": {"delegation_id": "d-1", "role": "subagent", "goal": "search", "child_count": 1},
        },
        {
            "event": "delegation.finished", "run_id": "hermes-existing",
            "data": {"delegation_id": "d-1", "role": "subagent", "goal": "search", "child_count": 1},
            "usage": {"model": "sci-pi-frontier", "input_tokens": 100, "output_tokens": 50, "cost_usd": 0.01},
        },
        {"event": "run.failed", "data": {"reason": "error"}},
    ]

    result = asyncio.run(
        RunExecutor(worker, events, lambda _: cycle, metering=metering, clock=lambda: NOW).execute_next(identity())
    )

    assert result is not None and result.state is RunState.FAILED
    published_types = [call[2] for call in events.published]
    assert published_types == [
        "tool.started", "tool.finished", "delegation.started", "delegation.finished",
        "run.failed", "run.state",
    ]
    assert metering.calls == [{
        "run_id": "run-1", "actor": "user:alice", "source": "a2a", "model": "sci-pi-frontier",
        "tokens_in": 100, "tokens_out": 50,
        "metadata": {"cost_usd": 0.01, "delegation_id": "d-1"},
    }]


def test_executor_drops_spoofed_named_events_that_are_not_allowlisted() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [
        {"event": "cost.updated", "run_id": "hermes-existing", "data": {"tokens_in": 1}},
        {"event": "artifact.registered", "run_id": "hermes-existing", "data": {"artifact_id": "a-1"}},
        {"event": "approval.required", "run_id": "hermes-existing", "data": {"approval_id": "x"}},
        {"event": "run.failed", "data": {"reason": "error"}},
    ]

    result = asyncio.run(
        RunExecutor(worker, events, lambda _: cycle, clock=lambda: NOW).execute_next(identity())
    )

    assert result is not None and result.state is RunState.FAILED
    assert [call[2] for call in events.published] == ["run.failed", "run.state"]


def test_executor_records_completion_usage_delta_before_completing() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}, "channel": "mcp"},
    )
    events = FakeEvents()
    metering = FakeMetering(totals=(40, 10))
    cycle = FakeCycle()
    cycle.client.messages = [{
        "event": "run.completed", "run_id": "hermes-existing", "output": completed_output(),
        "usage": {"input_tokens": 150, "output_tokens": 60},
        "runtime": {"provider": "vendor", "model": "sci-pi-frontier"},
    }]

    result = asyncio.run(
        RunExecutor(
            worker, events, lambda _: cycle, metering=metering, clock=lambda: NOW, **manifest_kwargs()
        ).execute_next(identity())
    )

    assert result is not None and result.state is RunState.COMPLETED
    assert metering.calls == [{
        "run_id": "run-1", "actor": "service:run-worker", "source": "mcp", "model": "sci-pi-frontier",
        "tokens_in": 110, "tokens_out": 50, "metadata": None,
    }]


def test_executor_stops_hermes_and_returns_current_run_when_metering_cancels_on_completion() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )

    def cancel() -> None:
        worker.run = apply_transition(worker.run, RunState.CANCELLED, NOW, reason="budget_exhausted")

    metering = FakeMetering(cancel_run=cancel)
    cycle = FakeCycle()
    cycle.client.messages = [{
        "event": "run.completed", "run_id": "hermes-existing", "output": "done",
        "usage": {"input_tokens": 10, "output_tokens": 5},
        "runtime": {"provider": "vendor", "model": "sci-pi-frontier"},
    }]

    result = asyncio.run(
        RunExecutor(worker, FakeEvents(), lambda _: cycle, metering=metering, clock=lambda: NOW).execute_next(identity())
    )

    assert result is not None
    assert result.state is RunState.CANCELLED
    assert result.reason == "budget_exhausted"
    assert cycle.client.stopped == [("lab-a", "hermes-existing")]
    assert worker.transitions == []


def test_executor_fails_manifest_seal_when_claims_block_is_unrecoverable() -> None:
    """extract_result fails, repair is asked once, still fails -> manifest_seal_failed."""
    from scilab.runs.executor import RunExecutor

    class RepairHermes(FakeHermes):
        def __init__(self) -> None:
            super().__init__()
            self.chat_calls: list[tuple[str, list[dict[str, str]]]] = []

        async def chat_completion(
            self, _lab_id: str, *, model: str, messages: list[dict[str, str]]
        ) -> str:
            self.chat_calls.append((model, messages))
            return "still no fenced json block"

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client = RepairHermes()
    cycle.client.messages = [{
        "event": "run.completed", "run_id": "hermes-existing", "output": "no json here at all",
    }]
    artifacts = FakeArtifacts()
    manifests = FakeManifests()

    result = asyncio.run(
        RunExecutor(
            worker, events, lambda _: cycle, clock=lambda: NOW,
            **manifest_kwargs(artifacts=artifacts, manifests=manifests),
        ).execute_next(identity())
    )

    assert result is not None and result.state is RunState.FAILED
    assert result.reason == "manifest_seal_failed"
    assert len(cycle.client.chat_calls) == 1
    assert artifacts.registered == []
    assert manifests.seal_calls == []
    assert [call[2] for call in events.published] == ["run.failed", "run.state"]
    assert events.published[0][3] == {"reason": "manifest_seal_failed"}


def test_executor_fails_manifest_seal_when_seal_raises() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [{
        "event": "run.completed", "run_id": "hermes-existing", "output": completed_output(),
    }]
    artifacts = FakeArtifacts()
    manifests = FakeManifests(error=ValueError("evidence must be a registered artifact or citation identifier"))

    result = asyncio.run(
        RunExecutor(
            worker, events, lambda _: cycle, clock=lambda: NOW,
            **manifest_kwargs(artifacts=artifacts, manifests=manifests),
        ).execute_next(identity())
    )

    assert result is not None and result.state is RunState.FAILED
    assert result.reason == "manifest_seal_failed"
    assert len(manifests.seal_calls) == 1
    assert [entry["kind"] for entry in artifacts.registered] == ["report", "tool-log"]
    assert [call[2] for call in events.published] == ["run.failed", "run.state"]


def test_executor_fails_manifest_seal_when_artifact_registration_rejects_uri() -> None:
    """register() rejecting an out-of-Lab URI must fail the Run, not raise out of it."""
    from scilab.runs.executor import RunExecutor

    class RejectingArtifacts(FakeArtifacts):
        def register(self, *args: Any, **kwargs: Any) -> Any:
            raise ValueError("artifact URI is outside the authenticated Lab and run")

    worker = FakeWorker(
        make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW),
        {"goal": "study", "options": {}},
    )
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [{
        "event": "run.completed", "run_id": "hermes-existing", "output": completed_output(),
    }]
    artifacts = RejectingArtifacts()
    manifests = FakeManifests()

    result = asyncio.run(
        RunExecutor(
            worker, events, lambda _: cycle, clock=lambda: NOW,
            **manifest_kwargs(artifacts=artifacts, manifests=manifests),
        ).execute_next(identity())
    )

    assert result is not None and result.state is RunState.FAILED
    assert result.reason == "manifest_seal_failed"
    assert manifests.seal_calls == []
    assert [call[2] for call in events.published] == ["run.failed", "run.state"]


def test_worker_main_is_executable_and_reports_missing_required_environment() -> None:
    repo_root = Path(__file__).parents[4]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo_root / "services/control-plane/src")
    for name in (
        "SCILAB_LAB_ID",
        "SCILAB_DATABASE_URL",
        "SCILAB_HERMES_API_KEY",
        "POD_NAMESPACE",
        "SCILAB_NATS_URL",
        "SCILAB_PI_PROVIDER",
        "SCILAB_REVIEWER_PROVIDER",
        "SCILAB_MINIO_URL",
        "SCILAB_MINIO_ACCESS_KEY",
        "SCILAB_MINIO_SECRET_KEY",
        "SCILAB_INPUT_BUCKET",
    ):
        env.pop(name, None)

    completed = subprocess.run(
        [sys.executable, "-m", "scilab.runs.worker_main"],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 2
    assert "SCILAB_LAB_ID is required" in completed.stderr


def test_worker_settings_derive_hermes_endpoint_from_lab_and_namespace() -> None:
    from scilab.runs.worker_main import WorkerSettings

    settings = WorkerSettings.from_environment(
        {
            "SCILAB_LAB_ID": "lab-a",
            "SCILAB_DATABASE_URL": "postgresql://worker@db/scilab",
            "SCILAB_HERMES_API_KEY": "per-lab-key",
            "POD_NAMESPACE": "scilab",
            "SCILAB_NATS_URL": "nats://nats:4222",
            "SCILAB_PI_PROVIDER": "pi-provider",
            "SCILAB_REVIEWER_PROVIDER": "reviewer-provider",
            "SCILAB_MINIO_URL": "https://storage.example:9000",
            "SCILAB_MINIO_ACCESS_KEY": "access",
            "SCILAB_MINIO_SECRET_KEY": "secret",
            "SCILAB_INPUT_BUCKET": "inputs",
            "SCILAB_OPA_URL": "http://opa.internal:8181",
            "SCILAB_MODEL_PRICES_THB": "{}",
            **_WORKER_MAIN_ENV_EXTRA,
        }
    )

    assert settings.lab_id == "lab-a"
    assert settings.hermes_endpoint == (
        "http://scilab-lab-a-hermes.scilab.svc.cluster.local:8642"
    )


def test_worker_polls_again_after_idle_database_result(monkeypatch: pytest.MonkeyPatch) -> None:
    from scilab.runs import worker_main

    class PollExecutor:
        calls = 0

        async def execute_next(self, _identity: Identity) -> None:
            self.calls += 1
            if self.calls == 1:
                return None
            raise asyncio.CancelledError

    sleeps: list[float] = []

    async def stop_after_poll(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(worker_main.asyncio, "sleep", stop_after_poll)
    executor = PollExecutor()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(worker_main.poll_forever(executor, identity(), poll_interval=0.25))

    assert executor.calls == 2
    assert sleeps == [0.25]


def test_worker_builds_lab_specific_hermes_clients_and_credentials() -> None:
    from scilab.runs.worker_main import WorkerSettings, create_lab_cycle

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, str]:
            return {"run_id": "hermes-run"}

    class Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        async def request(self, method: str, url: str, **kwargs: Any) -> Response:
            self.calls.append((method, url, dict(kwargs["headers"])))
            return Response()

    transport = Transport()
    for lab_id, key in (("lab-a", "key-a"), ("lab-b", "key-b")):
        settings = WorkerSettings.from_environment(
            {
                "SCILAB_LAB_ID": lab_id,
                "SCILAB_DATABASE_URL": "postgresql://worker@db/scilab",
                "SCILAB_HERMES_API_KEY": key,
                "POD_NAMESPACE": "scilab",
                "SCILAB_NATS_URL": "nats://nats:4222",
                "SCILAB_PI_PROVIDER": "pi-provider",
                "SCILAB_REVIEWER_PROVIDER": "reviewer-provider",
                "SCILAB_MINIO_URL": "https://storage.example:9000",
                "SCILAB_MINIO_ACCESS_KEY": "access",
                "SCILAB_MINIO_SECRET_KEY": "secret",
                "SCILAB_INPUT_BUCKET": "inputs",
                "SCILAB_OPA_URL": "http://opa.internal:8181",
                "SCILAB_MODEL_PRICES_THB": "{}",
                **_WORKER_MAIN_ENV_EXTRA,
            }
        )
        cycle = create_lab_cycle(settings, transport)
        assert cycle.lab_id == lab_id
        assert asyncio.run(
            cycle.client.start_run(
                lab_id,
                {"input": "study"},
                idempotency_key=f"run-{lab_id}",
            )
        ) == "hermes-run"

    assert [
        (url, headers["Authorization"])
        for _method, url, headers in transport.calls
    ] == [
        (
            "http://scilab-lab-a-hermes.scilab.svc.cluster.local:8642/v1/runs",
            "Bearer key-a",
        ),
        (
            "http://scilab-lab-b-hermes.scilab.svc.cluster.local:8642/v1/runs",
            "Bearer key-b",
        ),
    ]
