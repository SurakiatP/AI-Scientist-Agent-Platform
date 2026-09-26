"""Pure helpers for turning a PI report into a sealed-manifest draft.

No DB/network access here: `repair_result` takes an injected chat callable
shaped like `HermesClient.chat_completion` (already bound to a lab), and every
other function is a plain transform.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from scilab.contracts import ResearchResult

ChatFn = Callable[..., Awaitable[str]]

_JSON_BLOCK = re.compile(r"```json\s*\n(.*?)\n?```", re.DOTALL)

_REPAIR_PROMPT = (
    "The research report below is missing a valid trailing JSON block that "
    "matches the research-result schema: an object with \"claims\" (each "
    "with text, evidence, confidence), \"artifacts\", \"caveats\", and "
    "optional \"next_steps\". Return ONLY the corrected fenced ```json code "
    "block and nothing else.\n\n{report}"
)

_SUMMARY_MAX_CHARS = 500


def extract_result(report: str) -> ResearchResult | None:
    """Validate the last ```json block at the very end of the report."""
    matches = list(_JSON_BLOCK.finditer(report))
    if not matches:
        return None
    last = matches[-1]
    if report[last.end() :].strip():
        return None
    try:
        data = json.loads(last.group(1))
    except json.JSONDecodeError:
        return None
    try:
        return ResearchResult.model_validate(data)
    except ValidationError:
        return None


async def repair_result(report: str, chat: ChatFn) -> ResearchResult | None:
    """Ask the PI once to return a corrected JSON block; None if still invalid."""
    response = await chat(
        model="sci-pi-frontier",
        messages=[{"role": "user", "content": _REPAIR_PROMPT.format(report=report)}],
    )
    return extract_result(response)


def tool_log_bytes(events: Sequence[Any]) -> bytes:
    """JSONL of tool.* RunEvents, one canonical (stable key order) line each."""
    lines = []
    for event in events:
        data = event.model_dump(mode="json") if hasattr(event, "model_dump") else dict(event)
        if not str(data.get("type", "")).startswith("tool."):
            continue
        lines.append(json.dumps(data, sort_keys=True, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode() if lines else b""


def build_manifest_draft(
    *,
    run: Any,
    request_payload: Mapping[str, Any],
    hermes_image: str,
    config_sha256: str,
    model_aliases: Mapping[str, str],
    skills_image: str,
    sandbox_image: str,
    runtime: Mapping[str, str],
    result: ResearchResult,
    report_artifact_id: str,
    tool_log_artifact_id: str,
    input_artifacts: Sequence[Any],
    cost: Mapping[str, Any],
) -> dict[str, Any]:
    """Assemble an unsealed RunManifest draft (sealed_at/manifest_sha256 added at seal time)."""
    return {
        "run_id": run.id,
        "lab_id": run.lab_id,
        "actor": request_payload["actor"],
        "source": request_payload["channel"],
        "goal": request_payload["goal"],
        "skill_packs": list(request_payload.get("skill_packs", [])),
        "hermes": {
            "image": hermes_image,
            "config_sha256": config_sha256,
            "model_aliases": dict(model_aliases),
        },
        "runtime": dict(runtime),
        "skills_image": skills_image,
        "sandbox_image": sandbox_image,
        "inputs": [
            {
                "artifact_id": artifact.id,
                "sha256": artifact.sha256,
                "name": artifact.uri.rsplit("/", 1)[-1],
            }
            for artifact in input_artifacts
        ],
        "steps": [
            {
                "n": 1,
                "role": "pi",
                "delegation_id": "report",
                "commands_log": tool_log_artifact_id,
                "outputs": [report_artifact_id],
            }
        ],
        "claims": [
            {
                "id": f"c{index}",
                "text": claim.text,
                "evidence": list(claim.evidence),
                "confidence": claim.confidence,
            }
            for index, claim in enumerate(result.claims, start=1)
        ],
        "cost": dict(cost),
    }


def completed_payload(
    report: str, report_id: str, manifest_id: str, claims_count: int
) -> dict[str, Any]:
    first_paragraph = report.strip().split("\n\n", 1)[0].strip()
    return {
        "summary": first_paragraph[:_SUMMARY_MAX_CHARS],
        "report_artifact_id": report_id,
        "manifest_artifact_id": manifest_id,
        "claims_count": claims_count,
    }
