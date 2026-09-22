from __future__ import annotations

import hashlib
import json
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scilab.identity import Identity


NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[3]


def identity(lab_id: str = "lab-a", *scopes: str) -> Identity:
    return Identity(
        lab_id,
        f"user:{lab_id}",
        frozenset(scopes or {"runs:write", "artifacts:read"}),
    )


class Cursor:
    def __init__(self, database: "Database") -> None:
        self.database = database
        self.result: list[dict[str, object]] = []

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, *_: object) -> None:
        pass

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        compact = " ".join(sql.split()).lower()
        if compact.startswith("select set_config"):
            self.database.current_lab_id = str(params[1])
            return
        if compact.startswith("select id, lab_id from runs"):
            lab_id, run_id = params[:2]
            self.result = [
                row
                for row in self.database.runs
                if row["lab_id"] == lab_id and row["id"] == run_id
            ]
            return
        if compact.startswith("insert into artifacts"):
            candidate = dict(
                zip(
                    (
                        "id",
                        "run_id",
                        "lab_id",
                        "kind",
                        "uri",
                        "sha256",
                        "bytes",
                        "produced_by_step",
                        "metadata",
                        "created_at",
                    ),
                    params,
                )
            )
            existing = next(
                (
                    row
                    for row in self.database.artifacts
                    if row["lab_id"] == candidate["lab_id"]
                    and row["run_id"] == candidate["run_id"]
                    and row["uri"] == candidate["uri"]
                ),
                None,
            )
            if existing is None:
                self.database.artifacts.append(candidate)
            elif any(
                existing[key] != candidate[key]
                for key in ("kind", "sha256", "bytes", "produced_by_step", "metadata")
            ):
                raise ValueError("artifact URI already registered with different immutable data")
            return
        if " from artifacts " in f" {compact} ":
            if "where lab_id = %s and id = %s" in compact:
                lab_id, artifact_id = params
                rows = [
                    row
                    for row in self.database.artifacts
                    if row["lab_id"] == lab_id and row["id"] == artifact_id
                ]
            elif "uri = %s" in compact:
                lab_id, run_id, uri = params
                rows = [
                    row
                    for row in self.database.artifacts
                    if row["lab_id"] == lab_id
                    and row["run_id"] == run_id
                    and row["uri"] == uri
                ]
            else:
                lab_id, run_id = params
                rows = [
                    row
                    for row in self.database.artifacts
                    if row["lab_id"] == lab_id and row["run_id"] == run_id
                ]
            self.result = sorted(rows, key=lambda row: str(row["id"]))
            return
        if compact.startswith("insert into run_manifests"):
            run_id, lab_id, content, sha256, artifact_id, sealed_at = params
            key = (str(lab_id), str(run_id))
            existing = self.database.manifests.get(key)
            candidate = {
                "run_id": run_id,
                "lab_id": lab_id,
                "content": content,
                "sha256": sha256,
                "artifact_id": artifact_id,
                "sealed_at": sealed_at,
            }
            if existing is None:
                self.database.manifests[key] = candidate
            elif existing["sha256"] != sha256:
                raise ValueError("manifest already sealed")
            return
        if " from run_manifests " in f" {compact} ":
            lab_id, run_id = params
            row = self.database.manifests.get((str(lab_id), str(run_id)))
            self.result = [] if row is None else [row]
            return
        raise AssertionError(f"unexpected SQL: {compact}")

    def fetchone(self) -> dict[str, object] | None:
        return self.result[0] if self.result else None

    def fetchall(self) -> list[dict[str, object]]:
        return list(self.result)


class Database:
    def __init__(self) -> None:
        self.runs: list[dict[str, object]] = []
        self.artifacts: list[dict[str, object]] = []
        self.manifests: dict[tuple[str, str], dict[str, object]] = {}
        self.current_lab_id: str | None = None

    def transaction(self):
        return nullcontext()

    def cursor(self) -> Cursor:
        return Cursor(self)

    def add_run(self, run_id: str = "run-1", lab_id: str = "lab-a") -> None:
        self.runs.append({"id": run_id, "lab_id": lab_id})


class Store:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.objects: dict[str, bytes] = {}

    @staticmethod
    def owns(uri: str, *, lab_id: str, run_id: str) -> bool:
        return uri.startswith(f"s3://{lab_id}/{run_id}/")

    def put(
        self,
        uri: str,
        content: bytes,
        *,
        metadata: dict[str, object],
        if_none_match: bool,
    ) -> None:
        assert if_none_match is True
        if uri in self.objects and self.objects[uri] != content:
            raise ValueError("immutable object already exists")
        self.objects[uri] = content

    def presign(self, uri: str, *, expires_in: int) -> str:
        self.calls.append((uri, expires_in))
        return f"https://objects.example/{uri}?expires={expires_in}"


def artifact_service(database: Database):
    from scilab.artifacts import ArtifactService

    signer = Store()
    service = ArtifactService(
        database,
        signer,
        clock=lambda: NOW,
        id_factory=lambda: f"artifact-{len(database.artifacts) + 1}",
    )
    return service, signer


def register(service, *, uri: str, content: bytes, step: int):
    return service.register(
        identity(),
        "run-1",
        kind="binary",
        uri=uri,
        content=content,
        produced_by_step=step,
        metadata={"media_type": "application/octet-stream"},
    )


def test_artifact_registration_is_hashed_sized_scoped_and_idempotent() -> None:
    from scilab.runs.service import RunNotFound

    database = Database()
    database.add_run()
    service, signer = artifact_service(database)
    content = b"\x00\xffscientific evidence"

    artifact = register(service, uri="s3://lab-a/run-1/evidence.bin", content=content, step=2)
    retried = register(service, uri=artifact.uri, content=content, step=2)

    assert retried == artifact
    assert artifact.sha256 == hashlib.sha256(content).hexdigest()
    assert artifact.bytes == len(content)
    assert artifact.produced_by_step == 2
    assert artifact.metadata == {"media_type": "application/octet-stream"}
    assert service.list_for_run(identity(), "run-1") == [artifact]
    assert service.presign(identity(), artifact.id).endswith("expires=900")
    assert signer.calls == [(artifact.uri, 900)]

    with pytest.raises(RunNotFound):
        service.list_for_run(identity("lab-b"), "run-1")
    with pytest.raises(ValueError, match="outside"):
        register(service, uri="s3://lab-b/run-1/stolen.bin", content=b"x", step=1)


def manifest_draft(input_artifact, output_artifact, log_artifact) -> dict[str, object]:
    return {
        "run_id": "run-1",
        "lab_id": "lab-a",
        "actor": "user:lab-a",
        "source": "rest",
        "goal": "Analyze evidence",
        "skill_packs": ["general-research"],
        "hermes": {
            "image": "registry/hermes@sha256:abc",
            "config_sha256": "a" * 64,
            "model_aliases": {"pi": "sci-pi", "child": "sci-child"},
        },
        "skills_image": "registry/skills@sha256:def",
        "sandbox_image": "registry/sandbox@sha256:123",
        "inputs": [
            {
                "artifact_id": input_artifact.id,
                "sha256": input_artifact.sha256,
                "name": "question.bin",
            }
        ],
        "steps": [
            {
                "n": 1,
                "role": "data-scientist",
                "delegation_id": "delegation-1",
                "commands_log": log_artifact.id,
                "outputs": [output_artifact.id],
            }
        ],
        "claims": [
            {
                "id": "claim-1",
                "text": "Evidence supports the result",
                "evidence": [output_artifact.id, "pmid:12345678"],
                "confidence": 0.8,
            }
        ],
        "cost": {"tokens_in": 10, "tokens_out": 5, "llm_thb": 1.0, "compute_thb": 0.5},
    }


def test_manifest_seals_lineage_deterministically_and_detects_tampering() -> None:
    from scilab.provenance import ManifestService

    database = Database()
    database.add_run()
    artifacts, signer = artifact_service(database)
    source = register(artifacts, uri="s3://lab-a/run-1/input.bin", content=b"input", step=0)
    output = register(artifacts, uri="s3://lab-a/run-1/output.bin", content=b"output", step=1)
    log = register(artifacts, uri="s3://lab-a/run-1/commands.log", content=b"command", step=1)
    service = ManifestService(database, artifacts, clock=lambda: NOW)
    draft = manifest_draft(source, output, log)

    sealed = service.seal(identity(), "run-1", draft)
    retried = service.seal(identity(), "run-1", draft)

    assert retried == sealed
    assert sealed.artifact.kind == "manifest"
    stored_manifest = signer.objects[sealed.artifact.uri]
    assert json.loads(stored_manifest) == sealed.manifest.model_dump(mode="json")
    assert sealed.artifact.sha256 == hashlib.sha256(stored_manifest).hexdigest()
    assert sealed.manifest.inputs[0].artifact_id == source.id
    assert sealed.manifest.steps[0].outputs == [output.id]
    assert sealed.manifest.claims[0].evidence[0] == output.id
    assert service.verify(identity(), "run-1") is True

    stored = database.manifests[("lab-a", "run-1")]
    tampered = json.loads(str(stored["content"]))
    tampered["claims"][0]["text"] = "tampered"
    stored["content"] = json.dumps(tampered, sort_keys=True, separators=(",", ":"))
    assert service.verify(identity(), "run-1") is False


def test_manifest_rejects_missing_claim_evidence_and_post_seal_change() -> None:
    from scilab.provenance import ManifestService, ManifestSealed

    database = Database()
    database.add_run()
    artifacts, _ = artifact_service(database)
    source = register(artifacts, uri="s3://lab-a/run-1/input.bin", content=b"input", step=0)
    output = register(artifacts, uri="s3://lab-a/run-1/output.bin", content=b"output", step=1)
    log = register(artifacts, uri="s3://lab-a/run-1/commands.log", content=b"command", step=1)
    service = ManifestService(database, artifacts, clock=lambda: NOW)
    draft = manifest_draft(source, output, log)

    missing = json.loads(json.dumps(draft))
    missing["claims"][0]["evidence"] = []
    with pytest.raises(ValueError, match="evidence"):
        service.seal(identity(), "run-1", missing)

    untraceable = json.loads(json.dumps(draft))
    untraceable["claims"][0]["evidence"] = ["artifact-from-another-run"]
    with pytest.raises(ValueError, match="registered artifact or citation"):
        service.seal(identity(), "run-1", untraceable)

    service.seal(identity(), "run-1", draft)
    changed = json.loads(json.dumps(draft))
    changed["goal"] = "Changed after sealing"
    with pytest.raises(ManifestSealed):
        service.seal(identity(), "run-1", changed)


def test_artifact_migration_is_additive_tenant_scoped_and_retry_safe() -> None:
    sql = (ROOT / "services/control-plane/migrations/004_artifacts.sql").read_text().lower()

    assert "create table if not exists artifacts" in sql
    assert "create table if not exists run_manifests" in sql
    assert "sha256" in sql and "octet_length" in sql
    assert "unique (lab_id, run_id, uri)" in sql
    assert "foreign key (artifact_id, lab_id) references artifacts(id, lab_id)" in sql
    assert "prevent_run_manifest_mutation" in sql
    assert "enable row level security" in sql
    assert "force row level security" in sql
    assert "current_setting('scilab.current_lab_id', true)" in sql
