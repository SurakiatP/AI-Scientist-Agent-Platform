from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from scilab.runs.model import Run, RunState
from scilab.runs.state import (
    RunStateError,
    apply_retry,
    apply_transition,
    due_transition,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def make_run(
    state: RunState = RunState.QUEUED,
    *,
    created_at: datetime = NOW,
    queued_at: datetime = NOW,
    running_since: datetime | None = None,
    runtime_used: timedelta = timedelta(0),
    last_heartbeat_at: datetime | None = None,
    approval_expires_at: datetime | None = None,
    retry_count: int = 0,
) -> Run:
    return Run(
        id="run-1",
        lab_id="lab-a",
        idempotency_key="request-1",
        state=state,
        reason=None,
        retry_count=retry_count,
        max_minutes=120,
        hermes_run_id="hermes-1" if state is not RunState.QUEUED else None,
        created_at=created_at,
        updated_at=created_at,
        queued_at=queued_at,
        running_since=running_since,
        runtime_used=runtime_used,
        last_heartbeat_at=last_heartbeat_at,
        approval_expires_at=approval_expires_at,
    )


@pytest.mark.parametrize(
    ("source", "target", "kwargs"),
    [
        (RunState.QUEUED, RunState.RUNNING, {"hermes_run_id": "hermes-2"}),
        (RunState.QUEUED, RunState.FAILED, {"reason": "queue_timeout"}),
        (RunState.QUEUED, RunState.CANCELLED, {"reason": "stopped"}),
        (RunState.RUNNING, RunState.AWAITING_APPROVAL, {}),
        (RunState.RUNNING, RunState.COMPLETED, {"completion_ready": True}),
        (RunState.RUNNING, RunState.FAILED, {"reason": "error"}),
        (RunState.RUNNING, RunState.CANCELLED, {"reason": "stopped"}),
        (RunState.AWAITING_APPROVAL, RunState.RUNNING, {}),
        (RunState.AWAITING_APPROVAL, RunState.CANCELLED, {"reason": "approval_rejected"}),
    ],
)
def test_allowed_transitions_are_applied(source, target, kwargs):
    running_at = NOW if source is not RunState.QUEUED else None
    run = make_run(source, running_since=running_at, last_heartbeat_at=running_at)

    result = apply_transition(run, target, NOW + timedelta(seconds=1), **kwargs)

    assert result.state is target
    assert result.updated_at == NOW + timedelta(seconds=1)


def test_unlisted_and_self_transitions_are_rejected():
    allowed = {
        RunState.QUEUED: {RunState.RUNNING, RunState.FAILED, RunState.CANCELLED},
        RunState.RUNNING: {
            RunState.AWAITING_APPROVAL,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
        },
        RunState.AWAITING_APPROVAL: {RunState.RUNNING, RunState.CANCELLED},
        RunState.FAILED: set(),
        RunState.COMPLETED: set(),
        RunState.CANCELLED: set(),
    }

    for source in RunState:
        for target in RunState:
            if target in allowed[source]:
                continue
            with pytest.raises(RunStateError):
                apply_transition(make_run(source), target, NOW)


def test_running_requires_hermes_and_completion_proof():
    with pytest.raises(RunStateError):
        apply_transition(make_run(), RunState.RUNNING, NOW)

    run = make_run(RunState.RUNNING, running_since=NOW, last_heartbeat_at=NOW)
    with pytest.raises(RunStateError):
        apply_transition(run, RunState.COMPLETED, NOW)


def test_budget_exhaustion_cancels_but_does_not_fail():
    run = make_run(RunState.RUNNING, running_since=NOW, last_heartbeat_at=NOW)

    cancelled = apply_transition(
        run,
        RunState.CANCELLED,
        NOW,
        reason="budget_exhausted",
    )

    assert cancelled.state is RunState.CANCELLED
    assert cancelled.reason == "budget_exhausted"
    with pytest.raises(RunStateError):
        apply_transition(run, RunState.FAILED, NOW, reason="budget_exhausted")

def test_approval_resume_does_not_add_initial_dispatch_requirement():
    run = replace(make_run(RunState.AWAITING_APPROVAL), hermes_run_id=None)

    resumed = apply_transition(run, RunState.RUNNING, NOW)

    assert resumed.state is RunState.RUNNING
    assert resumed.hermes_run_id == run.hermes_run_id


def test_retry_preserves_identity_resets_attempt_and_allows_only_two_retries():
    failed = make_run(RunState.FAILED, retry_count=1, running_since=NOW, last_heartbeat_at=NOW)

    queued = apply_retry(failed, NOW + timedelta(minutes=1))

    assert queued.id == failed.id
    assert queued.lab_id == failed.lab_id
    assert queued.idempotency_key == failed.idempotency_key
    assert queued.retry_count == 2
    assert queued.state is RunState.QUEUED
    assert queued.reason is None
    assert queued.hermes_run_id is None
    assert queued.running_since is None
    assert queued.runtime_used == timedelta(0)
    assert queued.last_heartbeat_at is None
    assert queued.approval_expires_at is None

    with pytest.raises(RunStateError):
        apply_retry(queued, NOW)
    with pytest.raises(RunStateError):
        apply_retry(make_run(RunState.FAILED, retry_count=2), NOW)


@pytest.mark.parametrize(
    ("run", "at", "expected"),
    [
        (make_run(), NOW + timedelta(minutes=30), (RunState.FAILED, "queue_timeout")),
        (
            make_run(RunState.RUNNING, running_since=NOW, last_heartbeat_at=NOW),
            NOW + timedelta(minutes=120),
            (RunState.FAILED, "run_timeout"),
        ),
        (
            make_run(RunState.RUNNING, running_since=NOW, last_heartbeat_at=NOW),
            NOW + timedelta(seconds=90),
            None,
        ),
        (
            make_run(RunState.RUNNING, running_since=NOW, last_heartbeat_at=NOW),
            NOW + timedelta(seconds=91),
            (RunState.FAILED, "heartbeat_loss"),
        ),
        (
            make_run(
                RunState.AWAITING_APPROVAL,
                approval_expires_at=NOW + timedelta(hours=24),
            ),
            NOW + timedelta(hours=24),
            (RunState.CANCELLED, "approval_expired"),
        ),
    ],
)
def test_due_transition_uses_exact_deadlines(run, at, expected):
    assert due_transition(run, at) == expected


def test_runtime_accumulates_only_while_running():
    run = make_run(
        RunState.RUNNING,
        running_since=NOW,
        last_heartbeat_at=NOW,
        runtime_used=timedelta(minutes=10),
    )

    awaiting = apply_transition(
        run,
        RunState.AWAITING_APPROVAL,
        NOW + timedelta(minutes=5),
    )

    assert awaiting.runtime_used == timedelta(minutes=15)
    assert awaiting.approval_expires_at == NOW + timedelta(minutes=5, hours=24)
