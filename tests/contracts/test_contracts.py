import json
from datetime import datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from scilab.contracts import ResearchResult, RunEvent, RunManifest


ROOT = Path(__file__).parents[2]

VALID_EVENT = {
    "event_id": "evt_01J8",
    "run_id": "run_01J8",
    "lab_id": "lab_bio_01",
    "ts": "2026-09-19T08:12:03.211Z",
    "type": "run.state",
    "seq": 143,
    "payload": {"from": "queued", "to": "running", "reason": "run accepted"},
    "source": "run-service",
}

VALID_PAYLOADS = {
    "run.state": {"from": "queued", "to": "running", "reason": "run accepted"},
    "tool.started": {
        "tool": "python",
        "args_redacted": {"script": "analysis.py"},
        "duration_ms": 0,
        "ok": True,
    },
    "tool.finished": {
        "tool": "python",
        "args_redacted": {"script": "analysis.py"},
        "duration_ms": 1250,
        "ok": True,
    },
    "delegation.started": {
        "delegation_id": "delegation_1",
        "role": "scrna-analysis",
        "goal": "Analyze the dataset",
        "child_count": 1,
    },
    "delegation.finished": {
        "delegation_id": "delegation_1",
        "role": "scrna-analysis",
        "goal": "Analyze the dataset",
        "child_count": 1,
        "schema_valid": True,
    },
    "approval.required": {
        "approval_id": "approval_1",
        "action": "execute_tool",
        "reason": "policy requires approval",
        "policy_rule": "require-human-approval",
        "expires_at": "2026-09-20T08:12:03Z",
        "preview": {"tool": "python"},
    },
    "artifact.registered": {
        "artifact_id": "art_1",
        "kind": "dataset",
        "uri": "s3://scilab/art_1",
        "sha256": "def456",
        "bytes": 1024,
        "produced_by_step": 1,
    },
    "cost.updated": {
        "tokens_in": 100,
        "tokens_out": 20,
        "llm_cost_thb": 0.5,
        "compute_cost_thb": 0.2,
        "budget_remaining_thb": 149.3,
    },
    "run.completed": {
        "summary": "Analysis completed",
        "report_artifact_id": "art_report",
        "manifest_artifact_id": "art_manifest",
        "claims_count": 2,
    },
}

VALID_MANIFEST = {
    "run_id": "run_01J8",
    "lab_id": "lab_bio_01",
    "actor": "user:santipong",
    "source": "web",
    "goal": "Analyze the dataset",
    "skill_packs": ["core", "omics"],
    "hermes": {
        "image": "registry/scilab/hermes:0.9.3-p2",
        "config_sha256": "abc123",
        "model_aliases": {"pi": "sci-pi-frontier", "child": "sci-specialist"},
    },
    "skills_image": "registry/scilab/skills:2026.09.2",
    "sandbox_image": "registry/scilab/sandbox-bio:2026.09",
    "inputs": [{"artifact_id": "art_1", "sha256": "def456", "name": "pbmc.h5ad"}],
    "steps": [
        {
            "n": 1,
            "role": "scrna-analysis",
            "delegation_id": "delegation_1",
            "commands_log": "art_log_1",
            "outputs": ["art_output_1"],
        }
    ],
    "claims": [
        {
            "id": "c1",
            "text": "The result is reproducible",
            "evidence": ["art_1", "pmid:12345678"],
            "confidence": 0.8,
        }
    ],
    "cost": {"tokens_in": 812345, "tokens_out": 90211, "llm_thb": 96.4, "compute_thb": 31.0},
    "sealed_at": "2026-09-19T08:12:03.211Z",
    "manifest_sha256": "fedcba",
}

VALID_RESULT = {
    "claims": [{"text": "A claim", "evidence": ["art_1"], "confidence": 0.5}],
    "artifacts": ["art_1"],
    "caveats": ["Small sample"],
}


@pytest.mark.parametrize(
    ("model", "example"),
    [(RunEvent, VALID_EVENT), (RunManifest, VALID_MANIFEST), (ResearchResult, VALID_RESULT)],
)
def test_valid_examples_validate(model, example):
    assert model.model_validate(example)


@pytest.mark.parametrize(
    ("model", "schema_path"),
    [
        (RunEvent, "contracts/events/run-event.schema.json"),
        (RunManifest, "contracts/provenance/run-manifest.schema.json"),
        (ResearchResult, "contracts/delegation/research-result.schema.json"),
    ],
)
def test_checked_in_schemas_match_models(model, schema_path):
    checked_in = json.loads((ROOT / schema_path).read_text())
    assert checked_in == model.model_json_schema()


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("run.state", {"from": "queued", "to": "running"}),
        (
            "cost.updated",
            {
                "tokens_in": 100,
                "tokens_out": 20,
                "llm_cost_thb": 0.5,
                "compute_cost_thb": 0.2,
            },
        ),
    ],
)
def test_checked_in_schema_rejects_bad_typed_payloads(event_type, payload):
    schema = json.loads((ROOT / "contracts/events/run-event.schema.json").read_text())
    event = {**VALID_EVENT, "type": event_type, "payload": payload}
    assert list(Draft202012Validator(schema).iter_errors(event))


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("tool.progress", {"from": "queued", "to": "running", "reason": "progress"}),
        (
            "run.failed",
            {
                "tokens_in": 100,
                "tokens_out": 20,
                "llm_cost_thb": 0.5,
                "compute_cost_thb": 0.2,
                "budget_remaining_thb": 149.3,
            },
        ),
    ],
)
def test_checked_in_schema_accepts_generic_payloads_matching_typed_shapes(event_type, payload):
    schema = json.loads((ROOT / "contracts/events/run-event.schema.json").read_text())
    event = {**VALID_EVENT, "type": event_type, "payload": payload}
    assert Draft202012Validator(schema).is_valid(event)


def test_checked_in_schema_rejects_date_without_time():
    schema = json.loads((ROOT / "contracts/events/run-event.schema.json").read_text())
    event = {**VALID_EVENT, "ts": "2026-09-19"}
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    assert list(validator.iter_errors(event))


@pytest.mark.parametrize("event_type,payload", VALID_PAYLOADS.items())
def test_tor_payloads_validate(event_type, payload):
    event = {**VALID_EVENT, "type": event_type, "payload": payload}
    assert RunEvent.model_validate(event)


@pytest.mark.parametrize("event_type,payload", [("tool.progress", {"progress": 0.5}), ("run.failed", {"error": 123})])
def test_generic_payloads_remain_json_objects(event_type, payload):
    event = {**VALID_EVENT, "type": event_type, "payload": payload}
    assert RunEvent.model_validate(event)


def test_generic_payload_matching_typed_shape_remains_a_dict():
    payload = {"from": "a", "to": "b", "reason": "c"}
    event = {**VALID_EVENT, "type": "tool.progress", "payload": payload}
    validated = RunEvent.model_validate(event)
    assert validated.payload == payload
    assert isinstance(validated.payload, dict)


@pytest.mark.parametrize("event_type", VALID_PAYLOADS)
def test_specified_payload_rejects_missing_field(event_type):
    payload = dict(VALID_PAYLOADS[event_type])
    required = next(field for field in payload if field not in {"error", "schema_valid"})
    del payload[required]
    with pytest.raises(ValidationError):
        RunEvent.model_validate({**VALID_EVENT, "type": event_type, "payload": payload})


@pytest.mark.parametrize("event_type", VALID_PAYLOADS)
def test_specified_payload_rejects_extra_field(event_type):
    payload = {**VALID_PAYLOADS[event_type], "unexpected": True}
    with pytest.raises(ValidationError):
        RunEvent.model_validate({**VALID_EVENT, "type": event_type, "payload": payload})


@pytest.mark.parametrize(
    ("event_type", "field", "value"),
    [
        ("run.state", "to", 1),
        ("tool.finished", "duration_ms", "1250"),
        ("delegation.finished", "child_count", "1"),
        ("approval.required", "expires_at", "not-a-date"),
        ("artifact.registered", "bytes", "1024"),
        ("cost.updated", "tokens_in", "100"),
        ("run.completed", "claims_count", "2"),
    ],
)
def test_specified_payload_rejects_wrong_field_type(event_type, field, value):
    payload = {**VALID_PAYLOADS[event_type], field: value}
    with pytest.raises(ValidationError):
        RunEvent.model_validate({**VALID_EVENT, "type": event_type, "payload": payload})


def test_runtime_types_are_strict():
    with pytest.raises(ValidationError):
        RunEvent.model_validate({**VALID_EVENT, "seq": "143"})


def test_tor_datetime_strings_are_parsed_at_json_boundary():
    event = RunEvent.model_validate(VALID_EVENT)
    manifest = RunManifest.model_validate(VALID_MANIFEST)
    approval = RunEvent.model_validate({**VALID_EVENT, "type": "approval.required", "payload": VALID_PAYLOADS["approval.required"]})
    assert isinstance(event.ts, datetime)
    assert isinstance(manifest.sealed_at, datetime)
    assert isinstance(approval.payload.expires_at, datetime)


@pytest.mark.parametrize("timestamp", ["2026-09-19", "2026-09-19T08:12:03"])
def test_runtime_rejects_incomplete_timestamp(timestamp):
    with pytest.raises(ValidationError):
        RunEvent.model_validate({**VALID_EVENT, "ts": timestamp})


def test_event_requires_lab_id():
    event = {**VALID_EVENT}
    del event["lab_id"]
    with pytest.raises(ValidationError):
        RunEvent.model_validate(event)


def test_event_rejects_unknown_type():
    with pytest.raises(ValidationError):
        RunEvent.model_validate({**VALID_EVENT, "type": "tool.unknown"})


def test_event_payload_must_be_an_object():
    with pytest.raises(ValidationError):
        RunEvent.model_validate({**VALID_EVENT, "payload": ["not", "an", "object"]})


@pytest.mark.parametrize("confidence", [-0.1, 1.1])
def test_claim_confidence_is_bounded(confidence):
    result = {**VALID_RESULT, "claims": [{"text": "x", "evidence": ["art_1"], "confidence": confidence}]}
    with pytest.raises(ValidationError):
        ResearchResult.model_validate(result)


def test_manifest_claim_confidence_is_bounded():
    manifest = {**VALID_MANIFEST, "claims": [{"id": "c1", "text": "x", "evidence": [], "confidence": 1.1}]}
    with pytest.raises(ValidationError):
        RunManifest.model_validate(manifest)


def test_nested_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        RunManifest.model_validate({**VALID_MANIFEST, "hermes": {**VALID_MANIFEST["hermes"], "extra": True}})


def test_optional_next_steps_can_be_omitted():
    result = ResearchResult.model_validate(VALID_RESULT)
    assert result.next_steps == []
