from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from scilab.artifacts import Artifact
from scilab.contracts import CompletedPayload, ResearchResult, RunManifest
from scilab.provenance import ManifestService
from scilab.runs.completion import (
    build_manifest_draft,
    completed_payload,
    extract_result,
    repair_result,
    tool_log_bytes,
)

VALID_RESULT: dict[str, object] = {
    "claims": [
        {
            "text": "Result A holds under the tested conditions.",
            "evidence": ["artifact-out-1", "pmid:111"],
            "confidence": 0.9,
        }
    ],
    "artifacts": ["artifact-out-1"],
    "caveats": ["Small sample"],
    "next_steps": ["Replicate"],
}


def report_with_block(payload: object = VALID_RESULT, *, trailing: str = "") -> str:
    block = json.dumps(payload)
    return f"# Findings\n\nSome narrative text.\n\n```json\n{block}\n```{trailing}"


# --- extract_result -----------------------------------------------------


def test_extract_result_parses_trailing_json_block() -> None:
    result = extract_result(report_with_block())

    assert isinstance(result, ResearchResult)
    assert result.claims[0].text == "Result A holds under the tested conditions."
    assert result.claims[0].evidence == ["artifact-out-1", "pmid:111"]
    assert result.claims[0].confidence == 0.9


def test_extract_result_returns_none_when_block_missing() -> None:
    assert extract_result("# Findings\n\nNo fenced block here.") is None


def test_extract_result_returns_none_for_malformed_json() -> None:
    report = "# Findings\n\n```json\n{not valid json\n```"

    assert extract_result(report) is None


def test_extract_result_returns_none_for_schema_invalid_payload() -> None:
    invalid = {"claims": [], "caveats": []}  # missing required "artifacts"

    assert extract_result(report_with_block(invalid)) is None


def test_extract_result_returns_none_when_block_is_not_the_last_content() -> None:
    report = report_with_block(trailing="\n\nOne more paragraph after the block.")

    assert extract_result(report) is None


# --- repair_result --------------------------------------------------------


def test_repair_result_calls_chat_exactly_once_and_returns_corrected_result() -> None:
    calls: list[dict[str, object]] = []

    async def chat(*, model: str, messages: list[dict[str, str]]) -> str:
        calls.append({"model": model, "messages": messages})
        return report_with_block()

    result = asyncio.run(repair_result("broken report", chat))

    assert isinstance(result, ResearchResult)
    assert len(calls) == 1
    assert calls[0]["model"] == "sci-pi-frontier"
    assert "broken report" in calls[0]["messages"][0]["content"]


def test_repair_result_returns_none_when_second_attempt_still_invalid() -> None:
    calls: list[int] = []

    async def chat(*, model: str, messages: list[dict[str, str]]) -> str:
        calls.append(1)
        return "still not a valid json block"

    result = asyncio.run(repair_result("broken report", chat))

    assert result is None
    assert len(calls) == 1


# --- tool_log_bytes --------------------------------------------------------


def test_tool_log_bytes_keeps_only_tool_events_with_stable_key_order() -> None:
    events = [
        {"type": "run.state", "seq": 1},
        {"b": 2, "type": "tool.started", "a": 1},
        {"type": "tool.finished", "a": 1, "b": 2},
    ]

    body = tool_log_bytes(events)
    lines = body.decode().splitlines()

    assert len(lines) == 2
    assert lines[0] == json.dumps({"a": 1, "b": 2, "type": "tool.started"}, separators=(",", ":"))
    assert lines[1] == json.dumps({"a": 1, "b": 2, "type": "tool.finished"}, separators=(",", ":"))


def test_tool_log_bytes_empty_when_no_tool_events() -> None:
    assert tool_log_bytes([{"type": "run.state"}]) == b""


# --- build_manifest_draft / completed_payload ------------------------------


@dataclass(frozen=True)
class FakeRun:
    id: str
    lab_id: str


def artifact(id_: str, *, uri: str, sha256: str) -> Artifact:
    return Artifact(
        id=id_,
        run_id="run-1",
        lab_id="lab-a",
        kind="binary",
        uri=uri,
        sha256=sha256,
        bytes=10,
        produced_by_step=0,
        metadata={},
        created_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
    )


def test_build_manifest_draft_produces_lineage_valid_sealable_manifest() -> None:
    run = FakeRun(id="run-1", lab_id="lab-a")
    request_payload = {
        "actor": "user:lab-a",
        "channel": "rest",
        "goal": "Investigate the question.",
        "skill_packs": ["general-research"],
    }
    input_artifact = artifact("artifact-in-1", uri="s3://lab-a/run-1/input.bin", sha256="a" * 64)
    report_artifact = artifact("artifact-report-1", uri="s3://lab-a/run-1/report.md", sha256="b" * 64)
    log_artifact = artifact("artifact-log-1", uri="s3://lab-a/run-1/tool.log", sha256="c" * 64)
    result = ResearchResult.model_validate(
        {
            **VALID_RESULT,
            "claims": [
                {
                    "text": "Result A holds under the tested conditions.",
                    "evidence": [report_artifact.id, "pmid:111"],
                    "confidence": 0.9,
                }
            ],
        }
    )

    draft = build_manifest_draft(
        run=run,
        request_payload=request_payload,
        hermes_image="registry/hermes@sha256:" + "d" * 64,
        config_sha256="e" * 64,
        model_aliases={"pi": "sci-pi-frontier", "child": "sci-specialist"},
        skills_image="registry/skills@sha256:" + "f" * 64,
        sandbox_image="registry/sandbox@sha256:" + "0" * 64,
        runtime={"provider": "openrouter", "model": "pi-model"},
        result=result,
        report_artifact_id=report_artifact.id,
        tool_log_artifact_id=log_artifact.id,
        input_artifacts=[input_artifact],
        cost={"tokens_in": 10, "tokens_out": 5, "llm_thb": 1.0, "compute_thb": 0.5},
    )

    assert draft["run_id"] == "run-1"
    assert draft["lab_id"] == "lab-a"
    assert draft["actor"] == "user:lab-a"
    assert draft["source"] == "rest"
    assert draft["claims"][0]["id"] == "c1"
    assert draft["claims"][0]["evidence"] == [report_artifact.id, "pmid:111"]
    assert draft["steps"][0]["commands_log"] == log_artifact.id
    assert draft["steps"][0]["outputs"] == [report_artifact.id]
    assert draft["inputs"][0]["artifact_id"] == input_artifact.id

    registered = [input_artifact, report_artifact, log_artifact]
    ManifestService._validate_lineage(draft, registered)

    sealed = dict(draft)
    sealed["sealed_at"] = "2026-09-26T00:00:00Z"
    sealed["manifest_sha256"] = "1" * 64
    manifest = RunManifest.model_validate(sealed)
    assert manifest.claims[0].evidence[0] == report_artifact.id


def test_completed_payload_matches_strict_contract() -> None:
    report = "This is the summary paragraph.\n\nMore detail follows below."

    payload = completed_payload(report, "report-1", "manifest-1", 3)
    validated = CompletedPayload.model_validate(payload)

    assert validated.summary == "This is the summary paragraph."
    assert validated.report_artifact_id == "report-1"
    assert validated.manifest_artifact_id == "manifest-1"
    assert validated.claims_count == 3


def test_completed_payload_bounds_a_long_first_paragraph() -> None:
    report = "x" * 900

    payload = completed_payload(report, "report-1", "manifest-1", 0)

    assert len(payload["summary"]) <= 500
    CompletedPayload.model_validate(payload)
