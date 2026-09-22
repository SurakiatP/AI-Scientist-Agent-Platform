#!/usr/bin/env python3
"""Build and sign the governed skill image after all supply-chain gates pass."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml


REPOSITORY = "https://github.com/K-Dense-AI/scientific-agent-skills.git"
TAG = "v2.69.0"
SOURCE_COMMIT = "45af7aefb40e0ef12fda62c6f01d1067ea1e1222"
UPSTREAM_SKILLS = (
    "paper-lookup",
    "scientific-writing",
    "peer-review",
    "experimental-design",
    "statistical-analysis",
    "scientific-critical-thinking",
    "hypothesis-generation",
)
PLATFORM_SKILLS = (
    "sci-run-protocol",
    "artifact-store",
    "provenance-manifest",
    "approval-etiquette",
)
PLATFORM_SOURCE = Path(__file__).parents[1] / "skills/platform"
POLICY = {
    "scan_max_age_days": 7,
    "python": ">=3.13,<3.14",
    "runtime_mount": "read-only",
}
IMAGE_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
DOCKERFILE = """FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY skills /opt/scilab/skills
RUN chmod -R a-w /opt/scilab/skills
ENV PATH="/app/.venv/bin:$PATH"
"""
WRITE_PROBE = """from pathlib import Path
p = Path('/opt/scilab/skills/.scilab-write-test')
try:
    p.write_text('blocked')
except OSError:
    raise SystemExit(0)
raise SystemExit(1)
"""


class GateError(ValueError):
    pass


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tree_hash(root: Path) -> str:
    if root.is_symlink():
        raise GateError(f"symlink is not allowed in skill source: {root}")
    digest = hashlib.sha256()
    files = sorted(item for item in root.rglob("*") if item.is_file())
    if not files:
        raise GateError(f"skill has no files: {root.name}")
    for path in files:
        if path.is_symlink():
            raise GateError(f"symlink is not allowed in skill source: {path}")
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_yaml(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    value = yaml.safe_load(raw)
    if not isinstance(value, dict):
        raise GateError("pack must be a mapping")
    return value, raw


def _load_json(path: Path, label: str) -> tuple[dict, bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise GateError(f"{label} is required") from exc
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise GateError(f"{label} is invalid")
    return value, raw


def _validate_pack(pack: dict) -> dict[str, str]:
    source = pack.get("source")
    if not isinstance(source, dict):
        raise GateError("source is required")
    if source.get("repository") != REPOSITORY:
        raise GateError("source repository is not approved")
    if source.get("tag") != TAG:
        raise GateError("pinned tag is required")
    if source.get("commit") != SOURCE_COMMIT:
        raise GateError("source commit is not approved")

    skills = pack.get("skills")
    if not isinstance(skills, list):
        raise GateError("skills allowlist is required")
    licenses: dict[str, str] = {}
    for skill in skills:
        if not isinstance(skill, dict) or not isinstance(skill.get("name"), str):
            raise GateError("skill entry is invalid")
        name = skill["name"]
        if name not in UPSTREAM_SKILLS:
            raise GateError(f"unapproved skill: {name}")
        if name in licenses:
            raise GateError(f"duplicate skill: {name}")
        if skill.get("license") != "MIT":
            raise GateError(f"missing or unapproved license: {name}")
        licenses[name] = "MIT"
    if tuple(licenses) != UPSTREAM_SKILLS:
        raise GateError("skills allowlist is not exact")

    if pack.get("policy") != POLICY:
        policy = pack.get("policy")
        if not isinstance(policy, dict):
            raise GateError("policy is required")
        if policy.get("runtime_mount") != "read-only":
            raise GateError("runtime mount must be read-only")
        if policy.get("scan_max_age_days") != 7:
            raise GateError("scan max age must be 7 days")
        if policy.get("python") != ">=3.13,<3.14":
            raise GateError("python policy mismatch")
        raise GateError("policy is not approved")
    return licenses


def _validate_report(report: dict, label: str) -> dict[str, dict]:
    if report.get("source_commit") != SOURCE_COMMIT:
        raise GateError(f"{label} source commit mismatch")
    skills = report.get("skills")
    if not isinstance(skills, dict) or tuple(skills) != UPSTREAM_SKILLS:
        raise GateError(f"{label} skills mismatch")
    if any(not isinstance(item, dict) or item.get("passed") is not True for item in skills.values()):
        raise GateError(f"{label} failed")
    return skills


def _validate_scan(report: dict) -> dict[str, dict]:
    generated_at = report.get("generated_at")
    if not isinstance(generated_at, str):
        raise GateError("scan generated_at is required")
    try:
        generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GateError("scan generated_at is invalid") from exc
    if generated.tzinfo is None:
        raise GateError("scan generated_at must be timezone-aware")
    age = datetime.now(UTC) - generated.astimezone(UTC)
    if age < -timedelta(minutes=5):
        raise GateError("scan generated_at is in the future")
    if age > timedelta(days=7):
        raise GateError("scan is stale")
    return _validate_report(report, "scan")


def _validate_skill_source(source: Path, scan_skills: dict[str, dict]) -> str:
    if not source.is_dir():
        raise GateError("skill source is required")
    if source.is_symlink():
        raise GateError("skill source symlink is not allowed")
    expected = set(UPSTREAM_SKILLS)
    entries = list(source.iterdir())
    actual = {item.name for item in entries}
    extra = actual - expected
    if extra:
        raise GateError(f"unapproved skill in build source: {sorted(extra)[0]}")
    missing = expected - actual
    if missing:
        raise GateError(f"missing skill in build source: {sorted(missing)[0]}")
    for name in UPSTREAM_SKILLS:
        expected_hash = scan_skills[name].get("content_sha256")
        if not isinstance(expected_hash, str) or _tree_hash(source / name) != expected_hash:
            raise GateError(f"scan content hash mismatch: {name}")
    return _tree_hash(source)


def _required_bytes(path: Path, label: str) -> bytes:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise GateError(f"{label} is required") from exc
    if not data:
        raise GateError(f"{label} is empty")
    return data


def _build_image(
    builder: Path, source: Path, lockfile: Path, pyproject: Path, image: str
) -> tuple[str, str]:
    with tempfile.TemporaryDirectory() as temporary:
        context = Path(temporary) / "context"
        skills = context / "skills"
        skills.mkdir(parents=True)
        for name in UPSTREAM_SKILLS:
            shutil.copytree(source / name, skills / name)
        for name in PLATFORM_SKILLS:
            platform = PLATFORM_SOURCE / name
            if not (platform / "SKILL.md").is_file():
                raise GateError(f"platform skill is not installable: {name}")
            shutil.copytree(platform, skills / name)
        shutil.copyfile(lockfile, context / "uv.lock")
        shutil.copyfile(pyproject, context / "pyproject.toml")
        (context / "Dockerfile").write_text(DOCKERFILE)
        iidfile = Path(temporary) / "image.iid"
        build = subprocess.run(
            [str(builder), "build", "--iidfile", str(iidfile), "-t", image, str(context)],
            text=True, capture_output=True, check=False,
        )
        if build.returncode != 0:
            raise GateError("image build failed")
        try:
            digest = iidfile.read_text().strip()
        except OSError as exc:
            raise GateError("image build did not produce a digest") from exc
        if not IMAGE_DIGEST_RE.fullmatch(digest):
            raise GateError("image build produced an invalid digest")

        context_hash = _tree_hash(skills)
        mount = f"type=bind,src={skills.resolve()},dst=/opt/scilab/skills,readonly"
        probe = subprocess.run(
            [str(builder), "run", "--rm", "--mount", mount, digest, "python", "-c", WRITE_PROBE],
            text=True, capture_output=True, check=False,
        )
        if probe.returncode != 0:
            raise GateError("read-only skills mount probe failed")
    return digest, context_hash


def _write_signed(manifest: dict, output: Path, signature: Path, signer: Path) -> None:
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    manifest["sealed_sha256"] = _sha256(canonical)
    with tempfile.TemporaryDirectory() as temporary:
        temp_manifest = Path(temporary) / "manifest.json"
        temp_signature = Path(temporary) / "manifest.sig"
        temp_manifest.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        signed = subprocess.run(
            [str(signer), "sign-blob", "--output-signature", str(temp_signature), str(temp_manifest)],
            text=True, capture_output=True, check=False,
        )
        if signed.returncode != 0 or not temp_signature.is_file():
            raise GateError("manifest signing failed")
        shutil.copyfile(temp_manifest, output)
        shutil.copyfile(temp_signature, signature)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    for name in ("pack", "scan-report", "evaluation-report", "skill-source", "lockfile", "pyproject", "sbom", "builder", "signer", "output-manifest", "signature-output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args(argv)
    args.output_manifest.unlink(missing_ok=True)
    args.signature_output.unlink(missing_ok=True)

    try:
        pack, pack_bytes = _load_yaml(args.pack)
        licenses = _validate_pack(pack)
        scan, scan_bytes = _load_json(args.scan_report, "scan report")
        evaluation, evaluation_bytes = _load_json(args.evaluation_report, "evaluation report")
        scan_skills = _validate_scan(scan)
        _validate_report(evaluation, "evaluation")
        source_hash = _validate_skill_source(args.skill_source, scan_skills)
        lock_bytes = _required_bytes(args.lockfile, "lockfile")
        pyproject_bytes = _required_bytes(args.pyproject, "pyproject")
        sbom, sbom_bytes = _load_json(args.sbom, "sbom")
        if sbom.get("bomFormat") != "CycloneDX":
            raise GateError("sbom must use CycloneDX")
        image_digest, context_hash = _build_image(
            args.builder, args.skill_source, args.lockfile, args.pyproject, args.image
        )
        manifest = {
            "source_commit": SOURCE_COMMIT,
            "image": args.image,
            "image_digest": image_digest,
            "skills": list(UPSTREAM_SKILLS),
            "platform_skills": list(PLATFORM_SKILLS),
            "license_inventory": licenses,
            "runtime_mount": "read-only",
            "python": ">=3.13,<3.14",
            "evidence_sha256": {
                "pack": _sha256(pack_bytes),
                "scan": _sha256(scan_bytes),
                "evaluation": _sha256(evaluation_bytes),
                "sbom": _sha256(sbom_bytes),
                "lockfile": _sha256(lock_bytes),
                "pyproject": _sha256(pyproject_bytes),
                "skill_source": source_hash,
                "build_context": context_hash,
            },
        }
        _write_signed(manifest, args.output_manifest, args.signature_output, args.signer)
    except (GateError, OSError, ValueError, json.JSONDecodeError, yaml.YAMLError) as exc:
        args.output_manifest.unlink(missing_ok=True)
        args.signature_output.unlink(missing_ok=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
