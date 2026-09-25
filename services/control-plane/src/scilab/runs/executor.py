from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, get_args

from scilab.contracts import EventType
from scilab.identity import Identity
from scilab.orchestration import ResearchCycle
from scilab.runs.model import Run, RunState, utc_now
from scilab.runs.worker import LEASE_SECONDS, RunClaim, RunWorker
from scilab.tenancy import require_lab

_EVENT_TYPES = frozenset(get_args(EventType))
_RENEW_INTERVAL = LEASE_SECONDS / 3
_TERMINAL_EVENTS = {
    "run.completed": RunState.COMPLETED,
    "run.failed": RunState.FAILED,
}


class RunExecutor:
    """Execute persisted REST Runs through a Lab-configured ResearchCycle."""

    def __init__(
        self,
        worker: RunWorker,
        events: Any,
        cycle_for_lab: Callable[[str], ResearchCycle],
        *,
        approvals: Any = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.worker = worker
        self.events = events
        self.cycle_for_lab = cycle_for_lab
        self.approvals = approvals
        self.clock = clock

    async def execute_next(self, identity: Identity) -> Run | None:
        claim = self.worker.claim_next(identity)
        if claim is None:
            return None
        try:
            return await self._execute(identity, claim)
        finally:
            self.worker.release(identity, claim)

    async def _execute(self, identity: Identity, claim: RunClaim) -> Run | None:
        run = self.worker.current(identity, claim)
        if run is None:
            return None
        require_lab(identity, run.lab_id)
        cycle = self.cycle_for_lab(identity.lab_id)
        require_lab(identity, cycle.lab_id)

        if run.state is RunState.QUEUED:
            request = claim.request_payload
            if request.get("options", {}) != {}:
                raise ValueError("execution options are not supported")
            goal = request.get("goal")
            if not isinstance(goal, str) or not goal.strip():
                raise ValueError("stored Run goal must be non-blank")
            inputs = request.get("inputs", [])
            skill_packs = request.get("skill_packs", [])
            if not _string_list(inputs) or not _string_list(skill_packs):
                raise ValueError("stored Run inputs and skill_packs must be string arrays")

            hermes_run_id = await cycle.run(
                goal,
                idempotency_key=run.idempotency_key,
                inputs=inputs,
                skill_packs=skill_packs,
            )
            updated = self.worker.transition(
                identity,
                claim,
                RunState.RUNNING,
                hermes_run_id=hermes_run_id,
            )
            if updated is None:
                current = self.worker.current(identity, claim)
                if current is not None and current.state is RunState.CANCELLED:
                    await cycle.client.stop(identity.lab_id, hermes_run_id)
                return current
            await self._publish_state(identity, run, updated)
            run = updated
        elif run.state is not RunState.RUNNING or not run.hermes_run_id:
            return run

        if self._timed_out(run):
            return await self._timeout(identity, claim, cycle, run)
        return await self._consume_events(identity, claim, cycle, run)

    async def _consume_events(
        self, identity: Identity, claim: RunClaim, cycle: ResearchCycle, run: Run
    ) -> Run | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, run.max_minutes * 60 - self._elapsed_seconds(run))
        iterator = cycle.client.events(identity.lab_id, run.hermes_run_id).__aiter__()
        pending = asyncio.create_task(anext(iterator))
        try:
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return await self._timeout(identity, claim, cycle, run)
                done, _ = await asyncio.wait({pending}, timeout=min(_RENEW_INTERVAL, remaining))
                if not done:
                    if loop.time() >= deadline:
                        return await self._timeout(identity, claim, cycle, run)
                    if self.worker.renew(identity, claim) is None:
                        return await self._cancel_if_current(identity, claim, cycle, run.hermes_run_id)
                    continue
                try:
                    message = pending.result()
                except StopAsyncIteration:
                    return await self._cancel_if_current(identity, claim, cycle, run.hermes_run_id)
                if self.worker.renew(identity, claim) is None:
                    return await self._cancel_if_current(identity, claim, cycle, run.hermes_run_id)
                if loop.time() >= deadline or self._timed_out(run):
                    return await self._timeout(identity, claim, cycle, run)
                if isinstance(message, Mapping):
                    event_type = message.get("event")
                    if event_type == "approval.request":
                        request_id = message.get("request_id")
                        if (
                            message.get("run_id") != run.hermes_run_id
                            or message.get("lab_id") not in (None, identity.lab_id)
                            or not isinstance(request_id, str)
                            or not request_id.strip()
                            or request_id != request_id.strip()
                            or len(request_id) > 256
                        ):
                            raise ValueError("invalid approval.request binding")
                        if self.approvals is None:
                            raise RuntimeError("approval service unavailable")
                        preview = {"tool": "hermes.tool"}
                        _, awaiting = await self.approvals.request_hermes_approval(
                            identity, run.id, request_id, preview
                        )
                        if awaiting.state is RunState.RUNNING:
                            pending = asyncio.create_task(anext(iterator))
                            continue
                        return awaiting
                    payload = message.get("data")
                    if event_type in _TERMINAL_EVENTS and not isinstance(payload, Mapping):
                        if message.get("run_id") != run.hermes_run_id:
                            raise ValueError("terminal event Run binding mismatch")
                        payload = {
                            key: value for key, value in message.items()
                            if key not in {"event", "id", "run_id"}
                        }
                    if isinstance(event_type, str) and isinstance(payload, Mapping):
                        target = _terminal_target(event_type, payload)
                        if target is not None:
                            transition_options: dict[str, Any] = {}
                            if target is RunState.FAILED:
                                transition_options["reason"] = "error"
                            elif target is RunState.CANCELLED:
                                transition_options["reason"] = "stopped"
                            elif target is RunState.COMPLETED:
                                transition_options["completion_ready"] = True
                            updated = self.worker.transition(
                                identity, claim, target, **transition_options
                            )
                            if updated is not None:
                                if event_type in _TERMINAL_EVENTS:
                                    await self.events.publish_event(
                                        identity, run.id, event_type, payload, "hermes"
                                    )
                                await self._publish_state(identity, run, updated)
                            return updated
                        if event_type in _EVENT_TYPES and event_type != "run.state":
                            await self.events.publish_event(
                                identity, run.id, event_type, payload, "hermes"
                            )
                pending = asyncio.create_task(anext(iterator))
        finally:
            if not pending.done():
                pending.cancel()
                try:
                    await pending
                except asyncio.CancelledError:
                    pass
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    async def _timeout(
        self, identity: Identity, claim: RunClaim, cycle: ResearchCycle, run: Run
    ) -> Run | None:
        updated = self.worker.transition(
            identity, claim, RunState.FAILED, reason="run_timeout"
        )
        if updated is not None:
            await cycle.client.stop(identity.lab_id, run.hermes_run_id)
            await self._publish_state(identity, run, updated)
            return updated
        return await self._cancel_if_current(identity, claim, cycle, run.hermes_run_id)

    async def _cancel_if_current(
        self, identity: Identity, claim: RunClaim, cycle: ResearchCycle, hermes_run_id: str
    ) -> Run | None:
        current = self.worker.current(identity, claim)
        if current is not None and current.state is RunState.CANCELLED:
            await cycle.client.stop(identity.lab_id, hermes_run_id)
        return current

    def _timed_out(self, run: Run) -> bool:
        return self._elapsed_seconds(run) >= run.max_minutes * 60

    def _elapsed_seconds(self, run: Run) -> float:
        elapsed = run.runtime_used
        if run.running_since is not None:
            elapsed += self.clock() - run.running_since
        return elapsed.total_seconds()

    async def _publish_state(self, identity: Identity, old: Run, new: Run) -> None:
        await self.events.publish_event(
            identity,
            old.id,
            "run.state",
            {"from": str(old.state), "to": str(new.state), "reason": new.reason or ""},
            "run-service",
        )


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _terminal_target(event_type: str, payload: Mapping[str, Any]) -> RunState | None:
    target = _TERMINAL_EVENTS.get(event_type)
    if target is not None:
        return target
    state = payload.get("to", payload.get("state")) if event_type == "run.state" else None
    try:
        candidate = RunState(state)
    except (TypeError, ValueError):
        return None
    return candidate if candidate in {RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED} else None
