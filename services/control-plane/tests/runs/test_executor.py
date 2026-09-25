from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from typing import Any
from uuid import uuid4

import pytest

from scilab.identity import Identity
from scilab.runs.model import Run, RunState
from scilab.runs.state import apply_transition
from scilab.runs.worker import LEASE_SECONDS, RunClaim
from scilab.tenancy import AuthorizationError

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
) -> Run:
    return Run(
        id="run-1",
        lab_id=lab_id,
        idempotency_key="request-key-1",
        state=state,
        reason=None,
        retry_count=0,
        max_minutes=max_minutes,
        hermes_run_id=hermes_run_id,
        created_at=NOW,
        updated_at=NOW,
        queued_at=NOW,
        running_since=running_since,
        runtime_used=timedelta(0),
        last_heartbeat_at=running_since,
        approval_expires_at=None,
        context_id=None,
        budget_thb=None,
    )


class FakeWorker:
    def __init__(self, run: Run, payload: dict[str, Any]) -> None:
        self.run = run
        self.claim = RunClaim(
            run,
            payload,
            uuid4(),
            NOW + timedelta(seconds=LEASE_SECONDS),
        )
        self.transitions: list[tuple[RunState, dict[str, Any]]] = []
        self.released = False

    def claim_next(self, _identity: Identity) -> RunClaim:
        return self.claim

    def renew(self, _identity: Identity, _claim: RunClaim) -> datetime:
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

    async def publish_event(self, *args: Any) -> None:
        self.published.append(args)


class FakeHermes:
    def __init__(self) -> None:
        self.messages = [{"id": "evt-1", "event": "run.state", "data": {"state": "completed"}}]
        self.stopped: list[tuple[str, str]] = []
        self.event_calls: list[tuple[str, str]] = []

    async def events(self, _lab_id: str, _run_id: str):
        self.event_calls.append((_lab_id, _run_id))
        for message in self.messages:
            yield message

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
    executor = RunExecutor(worker, events, lambda lab_id: cycle, clock=lambda: NOW)

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
    assert [call[2] for call in events.published] == ["run.state", "run.state"]
    assert worker.released


@pytest.mark.parametrize(
    ("event_type", "payload", "expected_state"),
    [
        (
            "run.completed",
            {
                "summary": "Research complete",
                "report_artifact_id": "report-1",
                "manifest_artifact_id": "manifest-1",
                "claims_count": 2,
            },
            RunState.COMPLETED,
        ),
        ("run.failed", {"reason": "research_error"}, RunState.FAILED),
    ],
)
def test_executor_persists_terminal_event_for_sse(
    event_type: str, payload: dict[str, Any], expected_state: RunState
) -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(make_run(), {"goal": "study", "options": {}})
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [{"event": event_type, "data": payload}]

    result = asyncio.run(RunExecutor(worker, events, lambda _: cycle, clock=lambda: NOW).execute_next(identity()))

    assert result is not None and result.state is expected_state
    assert [event[2] for event in events.published] == ["run.state", event_type, "run.state"]
    assert events.published[1][3] == payload
    assert events.published[1][4] == "hermes"


def test_executor_pauses_for_valid_hermes_approval_and_reclaims_same_run() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(make_run(), {"goal": "study", "options": {}})
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [{
        "event": "approval.request", "run_id": "hermes-run-1",
        "request_id": "vendor-request-1", "command": "run --token=supersecret",
        "description": "review password=supersecret", "api_key": "supersecret",
    }]

    class Approvals:
        calls: list[tuple[Any, ...]] = []

        async def request_hermes_approval(self, actor: Identity, run_id: str, request_id: str, preview: dict[str, Any]):
            self.calls.append((actor.lab_id, run_id, request_id, preview))
            worker.run = apply_transition(worker.run, RunState.AWAITING_APPROVAL, NOW)
            return object(), worker.run

    approvals = Approvals()
    executor = RunExecutor(worker, events, lambda _: cycle, approvals=approvals, clock=lambda: NOW)
    paused = asyncio.run(executor.execute_next(identity()))
    assert paused is not None and paused.state is RunState.AWAITING_APPROVAL
    assert worker.released
    assert approvals.calls == [("lab-a", "run-1", "vendor-request-1", {
        "tool": "hermes.tool",
    })]
    assert "supersecret" not in repr(approvals.calls) + repr(events.published)
    worker.run = apply_transition(worker.run, RunState.RUNNING, NOW)
    worker.released = False
    cycle.client.messages = [{"event": "run.failed", "data": {"reason": "error"}}]
    resumed = asyncio.run(executor.execute_next(identity()))
    assert resumed is not None and resumed.state is RunState.FAILED
    assert len(cycle.calls) == 1
    assert cycle.client.event_calls[-1] == ("lab-a", "hermes-run-1")


@pytest.mark.parametrize("vendor_event", [
    {"event": "approval.request", "run_id": "other", "request_id": "req"},
    {"event": "approval.request", "run_id": "hermes-run-1", "request_id": ""},
    {"event": "approval.request", "run_id": "hermes-run-1", "request_id": " req "},
])
def test_executor_rejects_invalid_approval_request(vendor_event: dict[str, str]) -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(make_run(state=RunState.RUNNING, hermes_run_id="hermes-run-1", running_since=NOW), {"goal": "study", "options": {}})
    cycle = FakeCycle()
    cycle.client.messages = [vendor_event]
    with pytest.raises(ValueError, match="approval.request"):
        asyncio.run(RunExecutor(worker, FakeEvents(), lambda _: cycle, approvals=object(), clock=lambda: NOW).execute_next(identity()))
    assert worker.run.state is RunState.RUNNING
    assert worker.released


def test_executor_accepts_bound_vendor_completion_without_artifact_ids() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW), {"goal": "study", "options": {}})
    events = FakeEvents()
    cycle = FakeCycle()
    cycle.client.messages = [{"event": "run.completed", "run_id": "hermes-existing", "output": "done"}]
    result = asyncio.run(asyncio.wait_for(
        RunExecutor(worker, events, lambda _: cycle, clock=lambda: NOW).execute_next(identity()),
        timeout=0.2,
    ))
    assert result is not None and result.state is RunState.COMPLETED
    assert events.published[0][2] == "run.completed"
    assert "artifact_id" not in repr(events.published)


def test_executor_skips_replayed_confirmed_approval_request() -> None:
    from scilab.runs.executor import RunExecutor

    worker = FakeWorker(make_run(state=RunState.RUNNING, hermes_run_id="hermes-existing", running_since=NOW), {"goal": "study", "options": {}})
    cycle = FakeCycle()
    cycle.client.messages = [
        {"event": "approval.request", "run_id": "hermes-existing", "request_id": "req-1"},
        {"event": "run.failed", "data": {"reason": "error"}},
    ]

    class Approvals:
        async def request_hermes_approval(self, *_: Any):
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
    executor = RunExecutor(worker, FakeEvents(), lambda _lab_id: cycle, clock=lambda: NOW)

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
    executor = RunExecutor(worker, FakeEvents(), lambda _lab_id: cycle, clock=lambda: NOW)

    with pytest.raises(ConnectionError):
        asyncio.run(executor.execute_next(identity()))
    result = asyncio.run(executor.execute_next(identity()))

    assert [call[1]["idempotency_key"] for call in cycle.calls] == [
        "request-key-1",
        "request-key-1",
    ]
    assert result is not None and result.state is RunState.COMPLETED
    assert worker.released


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
