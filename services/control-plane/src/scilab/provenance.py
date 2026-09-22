from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from scilab.artifacts import Artifact, ArtifactService
from scilab.contracts import RunManifest
from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.runs.service import RunNotFound
from scilab.tenancy import require_scope


class ManifestSealed(ValueError):
    pass


@dataclass(frozen=True)
class SealedManifest:
    manifest: RunManifest
    artifact: Artifact


def _canonical(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _seal_payload(payload: Mapping[str, Any]) -> tuple[bytes, str]:
    unsigned = dict(payload)
    unsigned.pop("manifest_sha256", None)
    content = _canonical(unsigned)
    return content, hashlib.sha256(content).hexdigest()


_CITATION_ID = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:\S+$")


class ManifestService:
    _columns = ("run_id", "lab_id", "content", "sha256", "artifact_id", "sealed_at")

    def __init__(
        self,
        connection: Any,
        artifacts: ArtifactService,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.connection = connection
        self.artifacts = artifacts
        self.clock = clock

    @contextmanager
    def _access(self, identity: Identity, scope: str):
        require_scope(identity, scope)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                yield cursor

    @staticmethod
    def _require_run(cursor: Any, identity: Identity, run_id: str) -> None:
        cursor.execute(
            "SELECT id, lab_id FROM runs WHERE lab_id = %s AND id = %s",
            (identity.lab_id, run_id),
        )
        if cursor.fetchone() is None:
            raise RunNotFound("run not found")

    @classmethod
    def _row(cls, row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
        return dict(row) if isinstance(row, Mapping) else dict(zip(cls._columns, row))

    @staticmethod
    def _validate_lineage(draft: Mapping[str, Any], artifacts: list[Artifact]) -> None:
        by_id = {artifact.id: artifact for artifact in artifacts}
        for claim in draft.get("claims", []):
            if not claim.get("evidence"):
                raise ValueError("every claim must reference evidence")
            if any(not isinstance(item, str) or not item.strip() for item in claim["evidence"]):
                raise ValueError("evidence identifiers must be non-blank")
            if any(item not in by_id and _CITATION_ID.fullmatch(item) is None for item in claim["evidence"]):
                raise ValueError("evidence must be a registered artifact or citation identifier")
        for item in draft.get("inputs", []):
            artifact = by_id.get(item.get("artifact_id"))
            if artifact is None or artifact.sha256 != item.get("sha256"):
                raise ValueError("manifest input must match a registered artifact")
        for step in draft.get("steps", []):
            linked = [step.get("commands_log"), *step.get("outputs", [])]
            if any(artifact_id not in by_id for artifact_id in linked):
                raise ValueError("manifest steps must reference registered artifacts")

    def _load(self, cursor: Any, identity: Identity, run_id: str) -> dict[str, Any] | None:
        cursor.execute(
            "SELECT run_id, lab_id, content, sha256, artifact_id, sealed_at "
            "FROM run_manifests WHERE lab_id = %s AND run_id = %s",
            (identity.lab_id, run_id),
        )
        row = cursor.fetchone()
        return None if row is None else self._row(row)

    @staticmethod
    def _content(row: Mapping[str, Any]) -> dict[str, Any]:
        content = row["content"]
        if isinstance(content, (str, bytes, bytearray)):
            return json.loads(content)
        return dict(content)

    def seal(
        self,
        identity: Identity,
        run_id: str,
        draft: Mapping[str, Any],
    ) -> SealedManifest:
        if draft.get("run_id") != run_id or draft.get("lab_id") != identity.lab_id:
            raise ValueError("manifest run and Lab must match the request")
        if draft.get("actor") != identity.principal:
            raise ValueError("manifest actor must match the authenticated identity")

        registered = self.artifacts.list_for_run(identity, run_id)
        self._validate_lineage(draft, registered)

        with self._access(identity, "runs:write") as cursor:
            self._require_run(cursor, identity, run_id)
            existing = self._load(cursor, identity, run_id)
            if existing is not None:
                stored_payload = self._content(existing)
                candidate = dict(draft)
                candidate["sealed_at"] = stored_payload["sealed_at"]
                _, candidate_hash = _seal_payload(candidate)
                if candidate_hash != existing["sha256"]:
                    raise ManifestSealed("manifest already sealed")
                return SealedManifest(
                    RunManifest.model_validate(stored_payload),
                    self.artifacts.get(identity, str(existing["artifact_id"])),
                )

            payload = dict(draft)
            payload["sealed_at"] = self.clock().isoformat().replace("+00:00", "Z")
            unsigned, digest = _seal_payload(payload)
            payload["manifest_sha256"] = digest
            manifest = RunManifest.model_validate(payload)
            manifest_bytes = _canonical(manifest.model_dump(mode="json"))
            artifact = self.artifacts.register(
                identity,
                run_id,
                kind="manifest",
                uri=f"s3://{identity.lab_id}/{run_id}/manifest.json",
                content=manifest_bytes,
                produced_by_step=0,
                metadata={"media_type": "application/json"},
            )
            cursor.execute(
                """
                INSERT INTO run_manifests
                    (run_id, lab_id, content, sha256, artifact_id, sealed_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (lab_id, run_id) DO NOTHING
                """,
                (
                    run_id,
                    identity.lab_id,
                    _canonical(manifest.model_dump(mode="json")).decode(),
                    digest,
                    artifact.id,
                    manifest.sealed_at,
                ),
            )
            stored = self._load(cursor, identity, run_id)
            if stored is None or stored["sha256"] != digest:
                raise ManifestSealed("manifest already sealed")
            return SealedManifest(manifest, artifact)

    def verify(self, identity: Identity, run_id: str) -> bool:
        with self._access(identity, "artifacts:read") as cursor:
            self._require_run(cursor, identity, run_id)
            row = self._load(cursor, identity, run_id)
            if row is None:
                raise RunNotFound("manifest not found")
            try:
                payload = self._content(row)
                declared = payload.get("manifest_sha256")
                _, calculated = _seal_payload(payload)
                RunManifest.model_validate(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                return False
            return declared == row["sha256"] == calculated
