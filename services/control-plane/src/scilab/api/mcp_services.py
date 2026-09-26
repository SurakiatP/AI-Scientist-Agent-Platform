"""Lab-scoped MCP adapters over the control-plane services."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import time
from collections.abc import Callable, Mapping
from typing import Any

from fastapi.encoders import jsonable_encoder

from scilab.artifacts import ArtifactService
from scilab.approvals import ApprovalService
from scilab.events import EventService
from scilab.identity import Identity
from scilab.lab_knowledge import LabKnowledgeService
from scilab.metering import MeteringService
from scilab.provenance import ManifestService
from scilab.research_inputs import validate_research_inputs
from scilab.runs.model import RunState
from scilab.runs.service import RunService
from scilab.skill_catalog import get_skill_pack
from scilab.tenancy import require_scope


async def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = function(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


class MCPServices:
    def __init__(
        self,
        *,
        runs: RunService,
        events: EventService,
        metering: MeteringService,
        approvals: ApprovalService,
        artifacts: ArtifactService,
        manifests: ManifestService,
        admission: Any,
        knowledge: LabKnowledgeService,
    ) -> None:
        self.runs = runs
        self.events = events
        self.metering = metering
        self.approvals = approvals
        self.artifacts = artifacts
        self.manifests = manifests
        self.admission = admission
        self.knowledge = knowledge

    async def start_research(
        self,
        identity: Identity,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        require_scope(identity, "runs:write")
        if not isinstance(payload, Mapping):
            raise ValueError("request payload must be a mapping")
        request = dict(payload)
        if "lab_id" in request:
            raise ValueError("Lab is derived from the authenticated identity")
        unsupported = request.keys() - {
            "goal", "inputs", "skill_packs", "budget_thb", "max_minutes"
        }
        if unsupported:
            raise ValueError(f"unsupported request fields: {', '.join(sorted(unsupported))}")
        goal = request.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            raise ValueError("goal must be non-blank")
        inputs = validate_research_inputs(identity, request.get("inputs"), self.artifacts)
        skill_packs = request.get("skill_packs")
        if skill_packs is not None:
            if not isinstance(skill_packs, list) or any(
                not isinstance(pack, str) for pack in skill_packs
            ):
                raise ValueError("skill_packs must be a list of approved pack names")
            for pack in skill_packs:
                get_skill_pack(pack)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-blank")
        max_minutes = request.get("max_minutes")
        if max_minutes is not None and (
            isinstance(max_minutes, bool)
            or not isinstance(max_minutes, int)
            or max_minutes <= 0
        ):
            raise ValueError("max_minutes must be a positive integer")
        budget_thb = request.get("budget_thb")
        if budget_thb is not None and (
            isinstance(budget_thb, bool)
            or not isinstance(budget_thb, (int, float))
            or not math.isfinite(budget_thb)
            or budget_thb < 0
        ):
            raise ValueError("budget_thb must be a finite non-negative number")

        await _call(self.admission.check, identity, request, idempotency_key)
        request_payload = {
            "goal": goal,
            "inputs": list(inputs),
            "skill_packs": list(skill_packs) if skill_packs is not None else [],
            "budget": {"thb": budget_thb, "max_minutes": max_minutes},
            "options": {},
            "channel": "mcp",
        }
        create_kwargs: dict[str, Any] = {
            "request_payload": request_payload,
            "actor": identity.principal,
        }
        if max_minutes is not None:
            create_kwargs["max_minutes"] = max_minutes
        if budget_thb is not None:
            create_kwargs["budget_thb"] = budget_thb
        # ENQUEUE only: the worker starts Hermes off the queued Run (Q22).
        run = await _call(
            self.runs.create, identity, idempotency_key, **create_kwargs
        )
        return {"run_id": run.id, "state": str(run.state)}

    def get_run(self, identity: Identity, run_id: str) -> dict[str, Any]:
        run = self.runs.get(identity, run_id)
        usage = self.metering.aggregate_usage(identity, run_id=run_id)
        pending_approval: Any = None
        if run.state is RunState.AWAITING_APPROVAL:
            events = self.events.replay_events(identity, run_id, from_seq=0)
            pending_approval = next(
                (
                    event.payload
                    for event in reversed(events)
                    if event.type == "approval.required"
                ),
                {"status": "unavailable"},
            )
        return {
            "run_id": run.id,
            "state": str(run.state),
            "progress": None,
            "partial_findings": None,
            "cost": usage.cost_thb,
            "budget_thb": run.budget_thb,
            "pending_approval": pending_approval,
        }

    def get_events(
        self, identity: Identity, run_id: str, from_seq: int = 0
    ) -> list[dict[str, Any]]:
        return jsonable_encoder(
            self.events.replay_events(identity, run_id, from_seq=from_seq)
        )

    async def wait_run(
        self,
        identity: Identity,
        run_id: str,
        *,
        timeout_s: int,
        progress: Callable[[float, str], Any],
    ) -> dict[str, Any]:
        if not 1 <= timeout_s <= 1800:
            raise ValueError("timeout_s must be between 1 and 1800")
        deadline = time.monotonic() + timeout_s
        from_seq = 0
        while True:
            snapshot = self.get_run(identity, run_id)
            for event in self.get_events(identity, run_id, from_seq=from_seq):
                from_seq = max(from_seq, int(event["seq"]))
                await _call(progress, float(from_seq), str(event["type"]))
            if snapshot["state"] in {"completed", "failed", "cancelled"}:
                return snapshot
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return {**snapshot, "timed_out": True}
            await asyncio.sleep(min(1.0, remaining))

    def get_usage(self, identity: Identity, run_id: str) -> dict[str, Any]:
        usage = self.metering.aggregate_usage(identity, run_id=run_id)
        result = jsonable_encoder(usage)
        result["tokens"] = usage.tokens
        result["cost_thb"] = usage.cost_thb
        return result

    def list_skills(
        self, identity: Identity, *, filter: str | None = None
    ) -> list[dict[str, Any]]:
        require_scope(identity, "runs:read")
        skills = get_skill_pack("general-research")
        if filter is not None:
            if not isinstance(filter, str):
                raise ValueError("filter must be text")
            skills = [skill for skill in skills if filter.casefold() in skill.casefold()]
        return [{"pack": "general-research", "skills": skills}]

    async def ask_lab(self, identity: Identity, *, question: str) -> dict[str, object]:
        require_scope(identity, "artifacts:read")
        return await _call(self.knowledge.ask, identity, question)

    async def approve(
        self,
        identity: Identity,
        run_id: str,
        approval_id: str,
        decision: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        await _call(
            self.approvals.decide_approval,
            identity,
            approval_id,
            decision,
            note=note,
            run_id=run_id,
        )
        return {"ok": True}

    def get_artifact(self, identity: Identity, artifact_id: str) -> dict[str, Any]:
        artifact = self.artifacts.get(identity, artifact_id)
        return {**jsonable_encoder(artifact), "url": self.artifacts.presign(identity, artifact_id)}

    def get_report(self, identity: Identity, run_id: str) -> dict[str, Any]:
        reports = [
            artifact
            for artifact in self.artifacts.list_for_run(identity, run_id)
            if artifact.kind == "report"
        ]
        if not reports:
            return {"status": "unavailable"}
        report = max(reports, key=lambda artifact: artifact.created_at)
        return {
            "status": "available",
            "artifact_id": report.id,
            "sha256": report.sha256,
            "bytes": report.bytes,
            "url": self.artifacts.presign(identity, report.id),
            "content_type": "text/markdown",
        }

    def get_manifest(self, identity: Identity, run_id: str) -> dict[str, Any]:
        manifest = next(
            (
                artifact
                for artifact in self.artifacts.list_for_run(identity, run_id)
                if artifact.kind == "manifest"
            ),
            None,
        )
        if manifest is None:
            return {"status": "unavailable"}
        return {
            "status": "available",
            "artifact_id": manifest.id,
            "sha256": manifest.sha256,
            "bytes": manifest.bytes,
            "url": self.artifacts.presign(identity, manifest.id),
            "content_type": "application/json",
            "verified": self.manifests.verify(identity, run_id),
        }

    def read_report(self, identity: Identity, run_id: str) -> str:
        reports = [
            artifact for artifact in self.artifacts.list_for_run(identity, run_id)
            if artifact.kind == "report"
        ]
        if not reports:
            raise LookupError("report unavailable")
        report = max(reports, key=lambda artifact: artifact.created_at)
        return self.artifacts.read_bytes(identity, report.id).decode("utf-8")

    def read_manifest(self, identity: Identity, run_id: str) -> dict[str, Any]:
        manifests = [
            artifact for artifact in self.artifacts.list_for_run(identity, run_id)
            if artifact.kind == "manifest"
        ]
        if not manifests:
            raise LookupError("manifest unavailable")
        if not self.manifests.verify(identity, run_id):
            raise ValueError("manifest verification failed")
        manifest = max(manifests, key=lambda artifact: artifact.created_at)
        content = json.loads(self.artifacts.read_bytes(identity, manifest.id))
        if not isinstance(content, dict) or content.get("run_id") != run_id or content.get("lab_id") != identity.lab_id:
            raise ValueError("manifest does not match authenticated Run and Lab")
        return content
