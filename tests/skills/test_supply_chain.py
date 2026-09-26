from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from jsonschema import ValidationError, validate


ROOT = Path(__file__).parents[2]
PACK = ROOT / "skills/packs/general-research.yaml"
BUILDER = ROOT / "scripts/build_skill_image.py"
UPSTREAM_SKILLS = [
    "paper-lookup", "scientific-writing", "peer-review", "experimental-design",
    "statistical-analysis", "scientific-critical-thinking", "hypothesis-generation",
]
PLATFORM_SKILLS = [
    "sci-run-protocol", "artifact-store", "provenance-manifest", "approval-etiquette",
]
SOURCE_COMMIT = "45af7aefb40e0ef12fda62c6f01d1067ea1e1222"


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_pack() -> dict:
    assert PACK.is_file(), "general-research pack manifest is missing"
    return yaml.safe_load(PACK.read_text())


def _fixture_files(tmp_path: Path, *, passed: bool = True, stale: bool = False) -> dict[str, Path]:
    skills = tmp_path / "skills"
    for name in UPSTREAM_SKILLS:
        path = skills / name
        path.mkdir(parents=True)
        (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test fixture\n---\n")
    generated = datetime.now(UTC) - (timedelta(days=8) if stale else timedelta(hours=1))
    scan = {
        "generated_at": generated.isoformat(),
        "source_commit": SOURCE_COMMIT,
        "skills": {
            name: {"passed": passed, "content_sha256": _tree_hash(skills / name)}
            for name in UPSTREAM_SKILLS
        },
    }
    evaluation = {
        "source_commit": SOURCE_COMMIT,
        "skills": {name: {"passed": passed} for name in UPSTREAM_SKILLS},
    }
    paths = {
        "skills": skills,
        "scan": tmp_path / "scan.json",
        "evaluation": tmp_path / "evaluation.json",
        "lock": tmp_path / "uv.lock",
        "pyproject": tmp_path / "pyproject.toml",
        "sbom": tmp_path / "sbom.json",
    }
    paths["scan"].write_text(json.dumps(scan))
    paths["evaluation"].write_text(json.dumps(evaluation))
    paths["lock"].write_text("version = 1\n")
    paths["pyproject"].write_text(
        '[project]\nname = "skill-image"\nversion = "0.0.0"\nrequires-python = ">=3.13,<3.14"\ndependencies = []\n'
    )
    paths["sbom"].write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6"}))
    return paths


def _fake_tools(tmp_path: Path) -> tuple[Path, Path, Path]:
    log = tmp_path / "tools.log"
    builder = tmp_path / "builder"
    builder.write_text(
        "#!/usr/bin/env python3\nimport json, pathlib, sys\n"
        f"log=pathlib.Path({str(log)!r})\n"
        "with log.open('a') as f: f.write(json.dumps(['builder', *sys.argv[1:]]) + '\\n')\n"
        "if sys.argv[1] == 'build': pathlib.Path(sys.argv[sys.argv.index('--iidfile') + 1]).write_text('sha256:' + 'b' * 64)\n"
    )
    signer = tmp_path / "signer"
    signer.write_text(
        "#!/usr/bin/env python3\nimport json, pathlib, sys\n"
        f"log=pathlib.Path({str(log)!r})\n"
        "with log.open('a') as f: f.write(json.dumps(['signer', *sys.argv[1:]]) + '\\n')\n"
        "pathlib.Path(sys.argv[sys.argv.index('--output-signature') + 1]).write_text('signed')\n"
    )
    builder.chmod(0o755)
    signer.chmod(0o755)
    return builder, signer, log


def _run_builder(tmp_path: Path, pack: dict, *, passed: bool = True, stale: bool = False, mutate_files=None):
    files = _fixture_files(tmp_path, passed=passed, stale=stale)
    if mutate_files:
        mutate_files(files)
    pack_path = tmp_path / "pack.yaml"
    pack_path.write_text(yaml.safe_dump(pack, sort_keys=False))
    builder, signer, log = _fake_tools(tmp_path)
    output = tmp_path / "pack-manifest.json"
    signature = tmp_path / "pack-manifest.sig"
    result = subprocess.run(
        [
            sys.executable, str(BUILDER),
            "--pack", str(pack_path), "--scan-report", str(files["scan"]),
            "--evaluation-report", str(files["evaluation"]),
            "--skill-source", str(files["skills"]), "--lockfile", str(files["lock"]),
            "--pyproject", str(files["pyproject"]),
            "--sbom", str(files["sbom"]), "--builder", str(builder),
            "--signer", str(signer), "--image", "scilab/general-research:v2.69.0",
            "--output-manifest", str(output), "--signature-output", str(signature),
        ],
        text=True, capture_output=True, check=False,
    )
    return result, output, signature, log


def test_general_research_pack_is_exact_and_immutable() -> None:
    pack = _load_pack()
    assert pack["source"] == {
        "repository": "https://github.com/K-Dense-AI/scientific-agent-skills.git",
        "tag": "v2.69.0", "commit": SOURCE_COMMIT,
    }
    assert [item["name"] for item in pack["skills"]] == UPSTREAM_SKILLS
    assert all(item["license"] == "MIT" for item in pack["skills"])
    assert pack["policy"] == {
        "scan_max_age_days": 7, "python": ">=3.13,<3.14", "runtime_mount": "read-only",
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda pack: pack["source"].update(tag="main"), "pinned tag"),
        (lambda pack: pack["source"].update(commit="main"), "source commit"),
        (lambda pack: pack["skills"].append({"name": "literature-review", "license": "MIT"}), "unapproved skill"),
        (lambda pack: pack["skills"][0].pop("license"), "license"),
        (lambda pack: pack["policy"].update(runtime_mount="read-write"), "read-only"),
    ],
)
def test_invalid_pack_is_blocked(tmp_path: Path, mutation, message: str) -> None:
    pack = copy.deepcopy(_load_pack())
    mutation(pack)
    result, output, signature, _ = _run_builder(tmp_path, pack)
    assert result.returncode != 0 and message in result.stderr.lower()
    assert not output.exists() and not signature.exists()


@pytest.mark.parametrize(("passed", "stale", "message"), [(False, False, "scan"), (True, True, "stale")])
def test_failed_or_stale_scan_is_blocked(tmp_path: Path, passed: bool, stale: bool, message: str) -> None:
    result, output, signature, _ = _run_builder(tmp_path, _load_pack(), passed=passed, stale=stale)
    assert result.returncode != 0 and message in result.stderr.lower()
    assert not output.exists() and not signature.exists()


def test_failed_evaluation_is_blocked(tmp_path: Path) -> None:
    def fail(files: dict[str, Path]) -> None:
        report = json.loads(files["evaluation"].read_text())
        report["skills"][UPSTREAM_SKILLS[0]]["passed"] = False
        files["evaluation"].write_text(json.dumps(report))

    result, output, signature, _ = _run_builder(tmp_path, _load_pack(), mutate_files=fail)
    assert result.returncode != 0 and "evaluation" in result.stderr.lower()
    assert not output.exists() and not signature.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda files: (files["skills"] / "literature-review").mkdir(), "unapproved skill"),
        (lambda files: (files["skills"] / UPSTREAM_SKILLS[0] / "SKILL.md").write_text("changed"), "content hash"),
        (lambda files: files["lock"].unlink(), "lockfile"),
        (lambda files: files["sbom"].unlink(), "sbom"),
    ],
)
def test_untrusted_build_inputs_are_blocked(tmp_path: Path, mutation, message: str) -> None:
    result, output, signature, _ = _run_builder(tmp_path, _load_pack(), mutate_files=mutation)
    assert result.returncode != 0 and message in result.stderr.lower()
    assert not output.exists() and not signature.exists()


def test_build_seals_evidence_signs_and_proves_skill_mount_read_only(tmp_path: Path) -> None:
    result, output, signature, log = _run_builder(tmp_path, _load_pack())
    assert result.returncode == 0, result.stderr
    manifest = json.loads(output.read_text())
    assert manifest["source_commit"] == SOURCE_COMMIT
    assert manifest["image"] == "scilab/general-research:v2.69.0"
    assert manifest["image_digest"] == "sha256:" + "b" * 64
    assert manifest["skills"] == UPSTREAM_SKILLS
    assert manifest["platform_skills"] == PLATFORM_SKILLS
    assert manifest["license_inventory"] == {name: "MIT" for name in UPSTREAM_SKILLS}
    assert set(manifest["evidence_sha256"]) == {
        "pack", "scan", "evaluation", "sbom", "lockfile", "skill_source",
        "build_context", "pyproject",
    }
    seal = manifest.pop("sealed_sha256")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    assert seal == hashlib.sha256(canonical).hexdigest()
    assert signature.read_text() == "signed"
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands[0][:3] == ["builder", "build", "--iidfile"]
    assert commands[1][:4] == ["builder", "run", "--rm", "--mount"]
    assert commands[1][4].startswith("type=bind,src=")
    assert commands[1][4].endswith(",dst=/opt/scilab/skills,readonly")
    assert commands[1][5] == "sha256:" + "b" * 64
    assert "/opt/scilab/skills/.scilab-write-test" in commands[1][-1]
    assert commands[2][:3] == ["signer", "sign-blob", "--output-signature"]


@pytest.mark.parametrize("name", PLATFORM_SKILLS)
def test_platform_skill_has_installable_scenario_contract(name: str) -> None:
    path = ROOT / f"skills/platform/{name}/SKILL.md"
    assert path.is_file(), f"missing platform skill: {name}"
    content = path.read_text()
    frontmatter = yaml.safe_load(content.split("---", 2)[1])
    assert frontmatter["name"] == name and frontmatter["description"].startswith("Use ")
    assert "## Contract" in content and "## Scenario proof" in content
    assert all(label in content for label in ("Allowed:", "Denied:", "Edge:", "Requirement IDs:"))
    assert "TODO" not in content and "<placeholder>" not in content.lower()


def test_platform_skill_contract_fields_match_versioned_schemas() -> None:
    event = json.loads((ROOT / "contracts/events/run-event.schema.json").read_text())
    manifest = json.loads((ROOT / "contracts/provenance/run-manifest.schema.json").read_text())
    result = json.loads((ROOT / "contracts/delegation/research-result.schema.json").read_text())
    texts = {name: (ROOT / f"skills/platform/{name}/SKILL.md").read_text() for name in PLATFORM_SKILLS}
    for field in event["required"]:
        assert f"`{field}`" in texts["sci-run-protocol"]
    for field in event["$defs"]["ApprovalPayload"]["required"]:
        assert f"`{field}`" in texts["approval-etiquette"]
    for field in event["$defs"]["ArtifactPayload"]["required"]:
        assert f"`{field}`" in texts["artifact-store"]
    for field in manifest["required"]:
        assert f"`{field}`" in texts["provenance-manifest"]
    for field in result["required"]:
        assert f"`{field}`" in texts["sci-run-protocol"]


def test_sci_run_protocol_allowed_denied_and_edge_scenarios() -> None:
    event_schema = json.loads((ROOT / "contracts/events/run-event.schema.json").read_text())
    result_schema = json.loads((ROOT / "contracts/delegation/research-result.schema.json").read_text())
    event = {
        "event_id": "evt-1", "run_id": "run-1", "lab_id": "lab-1",
        "ts": "2026-09-22T00:00:00Z", "type": "delegation.finished", "seq": 1,
        "payload": {
            "delegation_id": "del-1", "role": "reviewer", "goal": "critique",
            "child_count": 1, "schema_valid": False,
        },
        "source": "hermes",
    }
    validate(event, event_schema)
    validate({"claims": [], "artifacts": [], "caveats": ["schema repair exhausted"]}, result_schema)
    with pytest.raises(ValidationError):
        validate({**event, "payload": {"delegation_id": "del-1"}}, event_schema)
    assert event["payload"]["schema_valid"] is False


def test_artifact_store_allowed_denied_and_binary_edge_scenarios() -> None:
    schema = json.loads((ROOT / "contracts/events/run-event.schema.json").read_text())
    artifact_schema = schema["$defs"]["ArtifactPayload"]
    content = b"\x00\xffscientific-evidence"
    artifact = {
        "artifact_id": "artifact-1", "kind": "binary", "uri": "s3://lab/run/output.bin",
        "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content),
        "produced_by_step": 2,
    }
    validate(artifact, artifact_schema)
    with pytest.raises(ValidationError):
        validate({key: value for key, value in artifact.items() if key != "sha256"}, artifact_schema)
    assert hashlib.sha256(content).hexdigest() == artifact["sha256"]
    assert len(content) == artifact["bytes"]


def test_provenance_manifest_allowed_denied_and_tamper_edge_scenarios() -> None:
    schema = json.loads((ROOT / "contracts/provenance/run-manifest.schema.json").read_text())
    manifest = {
        "run_id": "run-1", "lab_id": "lab-1", "actor": "researcher-1", "source": "rest",
        "goal": "test hypothesis", "skill_packs": ["general-research"],
        "hermes": {
            "image": "hermes@sha256:abc", "config_sha256": "a" * 64,
            "model_aliases": {"pi": "pi-model", "child": "child-model"},
        },
        "skills_image": "skills@sha256:def", "sandbox_image": "sandbox@sha256:ghi",
        "runtime": {"provider": "openrouter", "model": "pi-model"},
        "inputs": [{"artifact_id": "input-1", "sha256": "b" * 64, "name": "question.txt"}],
        "steps": [{"n": 1, "role": "pi", "delegation_id": "del-1", "commands_log": "log-1", "outputs": ["artifact-1"]}],
        "claims": [{"id": "claim-1", "text": "supported", "evidence": ["artifact-1"], "confidence": 0.8}],
        "cost": {"tokens_in": 10, "tokens_out": 20, "llm_thb": 1.0, "compute_thb": 0.5},
        "sealed_at": "2026-09-22T00:00:00Z", "manifest_sha256": "c" * 64,
    }
    validate(manifest, schema)
    invalid = copy.deepcopy(manifest)
    invalid["claims"][0]["confidence"] = 1.1
    with pytest.raises(ValidationError):
        validate(invalid, schema)
    original = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    tampered = copy.deepcopy(manifest)
    tampered["claims"][0]["text"] = "changed"
    assert hashlib.sha256(json.dumps(tampered, sort_keys=True).encode()).hexdigest() != original


def test_approval_etiquette_allowed_denied_and_expiry_edge_scenarios() -> None:
    schema = json.loads((ROOT / "contracts/events/run-event.schema.json").read_text())
    approval_schema = schema["$defs"]["ApprovalPayload"]
    approval = {
        "approval_id": "approval-1", "action": "publish", "reason": "external side effect",
        "policy_rule": "human-review", "expires_at": "2026-09-23T00:00:00Z",
        "preview": {"target": "report-1"},
    }
    validate(approval, approval_schema)
    with pytest.raises(ValidationError):
        validate({key: value for key, value in approval.items() if key != "approval_id"}, approval_schema)
    expiry = datetime.fromisoformat(approval["expires_at"].replace("Z", "+00:00"))
    assert datetime(2026, 9, 24, tzinfo=UTC) > expiry
