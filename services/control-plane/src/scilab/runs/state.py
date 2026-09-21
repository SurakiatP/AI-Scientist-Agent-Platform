from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from scilab.runs.model import Run, RunState


class RunStateError(ValueError):
    """Raised when a run lifecycle operation violates the state contract."""


CANONICAL_REASONS = frozenset(
    {
        "queue_timeout",
        "run_timeout",
        "heartbeat_loss",
        "error",
        "stopped",
        "approval_rejected",
        "approval_expired",
        "budget_exhausted",
    }
)

_ALLOWED = {
    RunState.QUEUED: frozenset({RunState.RUNNING, RunState.FAILED, RunState.CANCELLED}),
    RunState.RUNNING: frozenset(
        {
            RunState.AWAITING_APPROVAL,
            RunState.COMPLETED,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.AWAITING_APPROVAL: frozenset({RunState.RUNNING, RunState.CANCELLED}),
    RunState.FAILED: frozenset(),
    RunState.COMPLETED: frozenset(),
    RunState.CANCELLED: frozenset(),
}


def _require_reason(reason: str | None, allowed: frozenset[str], default: str) -> str:
    value = default if reason is None else reason
    if value not in CANONICAL_REASONS or value not in allowed:
        raise RunStateError(f"invalid reason: {value!r}")
    return value


def _runtime_at(run: Run, now: datetime) -> timedelta:
    if run.running_since is None:
        return run.runtime_used
    elapsed = now - run.running_since
    if elapsed < timedelta(0):
        raise RunStateError("clock moved backwards")
    return run.runtime_used + elapsed


def _stop_running(run: Run, now: datetime, *, state: RunState, reason: str | None) -> Run:
    return replace(
        run,
        state=state,
        reason=reason,
        updated_at=now,
        running_since=None,
        runtime_used=_runtime_at(run, now),
        last_heartbeat_at=None,
        approval_expires_at=None,
    )


def apply_transition(
    run: Run,
    target: RunState,
    now: datetime,
    *,
    reason: str | None = None,
    hermes_run_id: str | None = None,
    completion_ready: bool = False,
) -> Run:
    target = RunState(target)
    if target == run.state or target not in _ALLOWED[run.state]:
        raise RunStateError(f"transition {run.state.value} -> {target.value} is not allowed")

    if run.state is RunState.QUEUED:
        if target is RunState.RUNNING:
            hermes_id = hermes_run_id or run.hermes_run_id
            if not isinstance(hermes_id, str) or not hermes_id.strip():
                raise RunStateError("queued -> running requires hermes_run_id")
            return replace(
                run,
                state=target,
                reason=None,
                hermes_run_id=hermes_id,
                updated_at=now,
                running_since=now,
                last_heartbeat_at=now,
                approval_expires_at=None,
            )
        if target is RunState.FAILED:
            return replace(
                run,
                state=target,
                reason=_require_reason(reason, frozenset({"queue_timeout"}), "queue_timeout"),
                updated_at=now,
            )
        return replace(
            run,
            state=target,
            reason=_require_reason(reason, frozenset({"stopped"}), "stopped"),
            updated_at=now,
        )

    if run.state is RunState.RUNNING:
        if target is RunState.AWAITING_APPROVAL:
            return replace(
                run,
                state=target,
                reason=None,
                updated_at=now,
                running_since=None,
                runtime_used=_runtime_at(run, now),
                last_heartbeat_at=None,
                approval_expires_at=now + timedelta(hours=24),
            )
        if target is RunState.COMPLETED:
            if not completion_ready:
                raise RunStateError("completion requires completion_ready")
            return _stop_running(run, now, state=target, reason=None)
        if target is RunState.FAILED:
            return _stop_running(
                run,
                now,
                state=target,
                reason=_require_reason(
                    reason,
                frozenset({"run_timeout", "heartbeat_loss", "error"}),
                    "error",
                ),
            )
        return _stop_running(
            run,
            now,
            state=target,
            reason=_require_reason(
                reason,
                frozenset({"stopped", "budget_exhausted"}),
                "stopped",
            ),
        )

    if target is RunState.RUNNING:
        hermes_id = hermes_run_id or run.hermes_run_id
        return replace(
            run,
            state=target,
            reason=None,
            hermes_run_id=hermes_id,
            updated_at=now,
            running_since=now,
            last_heartbeat_at=now,
            approval_expires_at=None,
        )
    return replace(
        run,
        state=target,
        reason=_require_reason(
            reason,
            frozenset({"stopped", "approval_rejected", "approval_expired"}),
            "stopped",
        ),
        updated_at=now,
        approval_expires_at=None,
    )


def apply_retry(run: Run, now: datetime) -> Run:
    if run.state is not RunState.FAILED:
        raise RunStateError("only failed runs can be retried")
    if run.retry_count >= 2:
        raise RunStateError("retry limit reached")
    return replace(
        run,
        state=RunState.QUEUED,
        reason=None,
        retry_count=run.retry_count + 1,
        updated_at=now,
        queued_at=now,
        running_since=None,
        runtime_used=timedelta(0),
        hermes_run_id=None,
        last_heartbeat_at=None,
        approval_expires_at=None,
    )


def due_transition(run: Run, now: datetime) -> tuple[RunState, str] | None:
    if run.state is RunState.QUEUED and now - run.queued_at >= timedelta(minutes=30):
        return RunState.FAILED, "queue_timeout"
    if run.state is RunState.RUNNING:
        if _runtime_at(run, now) >= timedelta(minutes=run.max_minutes):
            return RunState.FAILED, "run_timeout"
        if run.last_heartbeat_at is not None and now - run.last_heartbeat_at > timedelta(seconds=90):
            return RunState.FAILED, "heartbeat_loss"
    if (
        run.state is RunState.AWAITING_APPROVAL
        and run.approval_expires_at is not None
        and now >= run.approval_expires_at
    ):
        return RunState.CANCELLED, "approval_expired"
    return None
