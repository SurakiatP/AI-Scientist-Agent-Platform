from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from scilab.artifacts import ArtifactNotFound
from scilab.contracts import EVENT_PAYLOAD_MODELS
from scilab.identity import Identity
from scilab.orchestration import ResearchCycle
from scilab.redaction import redact as _redact
from scilab.runs.completion import (
    build_manifest_draft,
    completed_payload,
    extract_result,
    repair_result,
    tool_log_bytes,
)
from scilab.runs.model import Run, RunState, utc_now
from scilab.runs.worker import LEASE_SECONDS, RunClaim, RunWorker
from scilab.tenancy import require_lab

_LOG = logging.getLogger(__name__)
_RENEW_INTERVAL = LEASE_SECONDS / 3
_TERMINAL_EVENTS = {
    "run.completed": RunState.COMPLETED,
    "run.failed": RunState.FAILED,
}
# Hermes may only inject these translated event types via a named SSE frame; a
# spoofed named frame of any other type (e.g. "cost.updated", "run.state",
# "approval.required") is never passed through or allowed to transition the Run.
_PASSTHROUGH_EVENTS = frozenset(
    {"tool.started", "tool.progress", "tool.finished", "delegation.started", "delegation.finished"}
)
_UNPRICED_WARNING_PREFIX = "unpriced_model:"


class _LeaseLost(Exception):
    """Internal signal: a lease renewal inside completion failed."""
_PLUGIN_PATTERN_PREFIX = "plugin_rule:"
# ponytail: no per-Lab Hermes model-alias config exists yet (grepped hermes.py,
# orchestration.py, lab-operator resources.py, worker_main.py — none). Reuse the
# literal aliases orchestration.py already hard-codes for the PI plan/report stage
# and its most common delegation role to satisfy RunManifest.hermes.model_aliases's
# required {pi, child} pair. Upgrade once a real per-Lab alias map exists.
_MODEL_ALIASES: Mapping[str, str] = {"pi": "sci-pi-frontier", "child": "sci-specialist"}


def _pattern_keys(message: Mapping[str, Any]) -> list[str]:
    """Collect Hermes ``pattern_keys`` (plural) folded together with ``pattern_key`` (singular)."""
    keys: list[str] = []
    plural = message.get("pattern_keys")
    if isinstance(plural, list):
        keys.extend(key for key in plural if isinstance(key, str) and key)
    single = message.get("pattern_key")
    if isinstance(single, str) and single and single not in keys:
        keys.append(single)
    return keys


def _passthrough_is_valid(event_type: str, payload: Mapping[str, Any]) -> bool:
    """A malformed tool/delegation frame must be dropped, not raise out of _execute."""
    model = EVENT_PAYLOAD_MODELS.get(event_type)
    if model is None:
        return True
    try:
        model.model_validate(payload)
        return True
    except ValidationError:
        return False


def _unpriced_models(tool_events: Sequence[Any]) -> list[str]:
    """Models metering couldn't price, recorded as ``cost.updated`` warnings."""
    seen: list[str] = []
    for event in tool_events:
        data = event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event)
        if data.get("type") != "cost.updated":
            continue
        payload = data.get("payload")
        warning = payload.get("warning") if isinstance(payload, Mapping) else None
        if isinstance(warning, str) and warning.startswith(_UNPRICED_WARNING_PREFIX):
            model_name = warning[len(_UNPRICED_WARNING_PREFIX):]
            if model_name and model_name not in seen:
                seen.append(model_name)
    return seen


def _effect_for(pattern_keys: list[str]) -> str:
    """Map Hermes approval pattern keys to an OPA effect.

    Any ``plugin_rule:``-prefixed key (arbitrary escalated tool call) is "unknown";
    a dangerous-shell or ``execute_code`` pattern key is "execute"; no keys at all
    is "unknown".
    """
    if not pattern_keys:
        return "unknown"
    if any(key.startswith(_PLUGIN_PATTERN_PREFIX) for key in pattern_keys):
        return "unknown"
    return "execute"


class RunExecutor:
    """Execute persisted REST Runs through a Lab-configured ResearchCycle."""

    def __init__(
        self,
        worker: RunWorker,
        events: Any,
        cycle_for_lab: Callable[[str], ResearchCycle],
        *,
        approvals: Any = None,
        metering: Any = None,
        artifacts: Any = None,
        manifests: Any = None,
        hermes_image: str | None = None,
        hermes_config_sha256: str | None = None,
        skills_image: str | None = None,
        sandbox_image: str | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.worker = worker
        self.events = events
        self.cycle_for_lab = cycle_for_lab
        self.approvals = approvals
        self.metering = metering
        self.artifacts = artifacts
        self.manifests = manifests
        self.hermes_image = hermes_image
        self.hermes_config_sha256 = hermes_config_sha256
        self.skills_image = skills_image
        self.sandbox_image = sandbox_image
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

            idempotency_key = (
                run.idempotency_key
                if run.retry_count == 0
                else f"{run.idempotency_key}:retry-{run.retry_count}"
            )
            start_kwargs: dict[str, Any] = {
                "idempotency_key": idempotency_key,
                "inputs": inputs,
                "skill_packs": skill_packs,
            }
            if run.context_id:
                start_kwargs["session_key"] = run.context_id
            hermes_run_id = await cycle.run(goal, **start_kwargs)
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
        try:
            return await self._consume_events(identity, claim, cycle, run)
        except (ValueError, ValidationError) as exc:
            # RR-S1: a Hermes binding/protocol violation (e.g. a terminal event
            # or approval.request bound to the wrong run_id) is not transient
            # and will never resolve itself; fail the Run instead of raising,
            # which would otherwise release the claim and let it be re-claimed
            # and re-run on every poll until run_timeout, starving the queue.
            return await self._fail_non_transient(identity, claim, cycle, run, exc)

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
                        pattern_keys = _pattern_keys(message)
                        effect = _effect_for(pattern_keys)
                        preview = _redact(
                            {
                                "command": message.get("command"),
                                "description": message.get("description"),
                                "pattern_keys": pattern_keys,
                            }
                        )
                        _, awaiting = await self.approvals.request_hermes_approval(
                            identity, run.id, request_id, preview, effect=effect
                        )
                        if awaiting.state is RunState.RUNNING:
                            pending = asyncio.create_task(anext(iterator))
                            continue
                        return awaiting
                    if event_type in _TERMINAL_EVENTS:
                        raw_payload = message.get("data")
                        if isinstance(raw_payload, Mapping):
                            event_run_id = raw_payload.get("run_id", message.get("run_id"))
                            payload = {
                                key: value for key, value in raw_payload.items() if key != "run_id"
                            }
                        else:
                            event_run_id = message.get("run_id")
                            payload = {
                                key: value for key, value in message.items()
                                if key not in {"event", "id", "run_id"}
                            }
                        if event_run_id != run.hermes_run_id:
                            raise ValueError("terminal event Run binding mismatch")
                        target = _TERMINAL_EVENTS[event_type]
                        if target is RunState.COMPLETED:
                            return await self._complete_run(identity, claim, cycle, run, payload)
                        updated = self.worker.transition(identity, claim, target, reason="error")
                        if updated is not None:
                            await self.events.publish_event(
                                identity, run.id, event_type, payload, "hermes"
                            )
                            await self._publish_state(identity, run, updated)
                        return updated
                    if isinstance(event_type, str) and event_type in _PASSTHROUGH_EVENTS:
                        payload = message.get("data")
                        if isinstance(payload, Mapping):
                            if _passthrough_is_valid(event_type, payload):
                                if event_type == "delegation.finished":
                                    await self._record_delegation_usage(
                                        identity, claim, run, payload, message.get("usage")
                                    )
                                await self.events.publish_event(
                                    identity, run.id, event_type, payload, "hermes"
                                )
                            else:
                                _LOG.warning(
                                    "dropping malformed %s event for run %s", event_type, run.id
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

    async def _fail_non_transient(
        self, identity: Identity, claim: RunClaim, cycle: ResearchCycle, run: Run, exc: Exception,
    ) -> Run | None:
        _LOG.warning("Run %s failed on a non-transient error: %s", run.id, exc)
        updated = self.worker.transition(identity, claim, RunState.FAILED, reason="error")
        if updated is None:
            return await self._cancel_if_current(identity, claim, cycle, run.hermes_run_id)
        try:
            await cycle.client.stop(identity.lab_id, run.hermes_run_id)
        except Exception:
            _LOG.exception("Hermes stop failed for failed Run %s", run.id)
        await self.events.publish_event(
            identity, run.id, "run.failed", {"reason": "error"}, "run-service"
        )
        await self._publish_state(identity, run, updated)
        return updated

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

    async def _record_delegation_usage(
        self, identity: Identity, claim: RunClaim, run: Run,
        payload: Mapping[str, Any], usage: object,
    ) -> None:
        if self.metering is None or not isinstance(usage, Mapping):
            return
        model = usage.get("model")
        if not isinstance(model, str) or not model.strip():
            return
        await self.metering.record_model_usage_async(
            identity, run.id,
            actor=claim.actor or "service:run-worker",
            source=claim.request_payload.get("channel", "rest"),
            model=model,
            tokens_in=int(usage.get("input_tokens") or 0),
            tokens_out=int(usage.get("output_tokens") or 0),
            metadata={"cost_usd": usage.get("cost_usd"), "delegation_id": payload.get("delegation_id")},
        )

    async def _record_completion_usage(
        self, identity: Identity, claim: RunClaim, run: Run, payload: Mapping[str, Any]
    ) -> None:
        if self.metering is None:
            return
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            return
        runtime = payload.get("runtime")
        model = runtime.get("model") if isinstance(runtime, Mapping) else None
        if not isinstance(model, str) or not model.strip():
            return
        # ponytail: baseline is 0, correct for a fresh Hermes session. A Run that
        # resumes an existing A2A context (run.context_id) shares session token
        # counters with earlier Runs, so its cumulative usage would need the
        # previous completed Run's snapshot as baseline to avoid double-counting;
        # that snapshot isn't stored anywhere cheap to read here, so upgrade this
        # once context-reuse Runs need accurate per-Run cost.
        recorded_in, recorded_out = self.metering.run_token_totals(identity, run.id)
        tokens_in = max(0, int(usage.get("input_tokens") or 0) - recorded_in)
        tokens_out = max(0, int(usage.get("output_tokens") or 0) - recorded_out)
        await self.metering.record_model_usage_async(
            identity, run.id,
            actor=claim.actor or "service:run-worker",
            source=claim.request_payload.get("channel", "rest"),
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )

    async def _complete_run(
        self, identity: Identity, claim: RunClaim, cycle: ResearchCycle, run: Run,
        payload: Mapping[str, Any],
    ) -> Run | None:
        """Parse the PI's claims, register artifacts, and seal the manifest.

        Runs on ``run.completed`` after usage is recorded and before the Run
        transitions to COMPLETED. Any failure to parse a valid claims block or
        to register/seal fails the Run with reason ``manifest_seal_failed``
        instead of completing it (F1/F10).
        """
        await self._record_completion_usage(identity, claim, run, payload)
        current = self.worker.current(identity, claim)
        if current is not None and current.state is RunState.CANCELLED:
            await cycle.client.stop(identity.lab_id, run.hermes_run_id)
            return current

        output = payload.get("output")
        report = output if isinstance(output, str) else ""
        result = extract_result(report) if report else None
        if result is None and report:
            if self.worker.renew(identity, claim) is None:
                return await self._cancel_if_current(identity, claim, cycle, run.hermes_run_id)
            try:
                chat = functools.partial(cycle.client.chat_completion, identity.lab_id)
                result = await repair_result(report, chat)
            except Exception:
                result = None
            if self.worker.renew(identity, claim) is None:
                return await self._cancel_if_current(identity, claim, cycle, run.hermes_run_id)
        if result is None:
            return await self._fail_manifest_seal(identity, claim, run)

        try:
            report_id, manifest_id = await self._register_and_seal(
                identity, claim, run, report, result, payload
            )
        except _LeaseLost:
            return await self._cancel_if_current(identity, claim, cycle, run.hermes_run_id)
        except Exception:
            return await self._fail_manifest_seal(identity, claim, run)

        updated = self.worker.transition(
            identity, claim, RunState.COMPLETED, completion_ready=True
        )
        if updated is not None:
            await self.events.publish_event(
                identity, run.id, "run.completed",
                completed_payload(report, report_id, manifest_id, len(result.claims)),
                "run-service",
            )
            await self._publish_state(identity, run, updated)
        return updated

    async def _fail_manifest_seal(
        self, identity: Identity, claim: RunClaim, run: Run
    ) -> Run | None:
        updated = self.worker.transition(
            identity, claim, RunState.FAILED, reason="manifest_seal_failed"
        )
        if updated is not None:
            await self.events.publish_event(
                identity, run.id, "run.failed",
                {"reason": "manifest_seal_failed"}, "run-service",
            )
            await self._publish_state(identity, run, updated)
        return updated

    async def _register_and_seal(
        self, identity: Identity, claim: RunClaim, run: Run,
        report: str, result: Any, payload: Mapping[str, Any],
    ) -> tuple[str, str]:
        if self.worker.renew(identity, claim) is None:
            raise _LeaseLost()
        manifest_identity = Identity(
            identity.lab_id,
            claim.actor or "service:run-worker",
            frozenset({"runs:read", "runs:write", "artifacts:read"}),
        )
        attempt = f"attempt-{run.retry_count}"
        report_artifact = self.artifacts.register(
            manifest_identity, run.id,
            kind="report",
            uri=f"s3://{identity.lab_id}/{run.id}/{attempt}/report.md",
            content=report.encode("utf-8"),
            produced_by_step=1,
        )
        tool_events = self.events.replay_events(manifest_identity, run.id)
        log_artifact = self.artifacts.register(
            manifest_identity, run.id,
            kind="tool-log",
            uri=f"s3://{identity.lab_id}/{run.id}/{attempt}/tool-log.jsonl",
            content=tool_log_bytes(tool_events),
            produced_by_step=1,
        )
        input_artifacts = []
        for ref in claim.request_payload.get("inputs", []):
            try:
                input_artifacts.append(self.artifacts.get(manifest_identity, ref))
            except ArtifactNotFound:
                continue

        cost = {
            "tokens_in": 0, "tokens_out": 0, "llm_thb": 0.0, "compute_thb": 0.0,
            "unpriced_models": _unpriced_models(tool_events),
        }
        if self.metering is not None:
            summary = self.metering.aggregate_usage(manifest_identity, run_id=run.id)
            cost.update({
                "tokens_in": summary.tokens_in,
                "tokens_out": summary.tokens_out,
                "llm_thb": summary.llm_cost_thb,
                "compute_thb": summary.compute_cost_thb,
            })

        runtime = payload.get("runtime")
        request_payload = {
            **claim.request_payload,
            "actor": claim.actor or "service:run-worker",
            "channel": claim.request_payload.get("channel", "rest"),
        }
        draft = build_manifest_draft(
            run=run,
            request_payload=request_payload,
            hermes_image=self.hermes_image,
            config_sha256=self.hermes_config_sha256,
            model_aliases=_MODEL_ALIASES,
            skills_image=self.skills_image,
            sandbox_image=self.sandbox_image,
            runtime=runtime if isinstance(runtime, Mapping) else {},
            result=result,
            report_artifact_id=report_artifact.id,
            tool_log_artifact_id=log_artifact.id,
            input_artifacts=input_artifacts,
            cost=cost,
        )
        if self.worker.renew(identity, claim) is None:
            raise _LeaseLost()
        sealed = self.manifests.seal(manifest_identity, run.id, draft)
        return report_artifact.id, sealed.artifact.id


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)
