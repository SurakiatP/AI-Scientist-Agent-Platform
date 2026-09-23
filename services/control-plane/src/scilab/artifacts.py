from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.runs.service import RunNotFound
from scilab.tenancy import require_scope


class ArtifactNotFound(LookupError):
    pass


class ArtifactConflict(ValueError):
    pass


_MAX_KNOWLEDGE_SOURCE_LIMIT = 32


@dataclass(frozen=True)
class Artifact:
    id: str
    run_id: str
    lab_id: str
    kind: str
    uri: str
    sha256: str
    bytes: int
    produced_by_step: int
    metadata: dict[str, Any]
    created_at: datetime


class ArtifactService:
    _columns = (
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
    )
    _select = ", ".join(_columns)

    def __init__(
        self,
        connection: Any,
        storage: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        self.connection = connection
        self.storage = storage
        self.clock = clock
        self.id_factory = id_factory

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
    def _from_row(cls, row: Mapping[str, Any] | tuple[Any, ...]) -> Artifact:
        values = dict(row) if isinstance(row, Mapping) else dict(zip(cls._columns, row))
        metadata = values["metadata"]
        if isinstance(metadata, (str, bytes, bytearray)):
            metadata = json.loads(metadata)
        values["metadata"] = dict(metadata)
        return Artifact(**values)

    def register(
        self,
        identity: Identity,
        run_id: str,
        *,
        kind: str,
        uri: str,
        content: bytes,
        produced_by_step: int,
        metadata: Mapping[str, Any] | None = None,
    ) -> Artifact:
        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        if not kind.strip() or not uri.strip():
            raise ValueError("kind and uri must be non-blank")
        if produced_by_step < 0:
            raise ValueError("produced_by_step must be non-negative")
        if not self.storage.owns(uri, lab_id=identity.lab_id, run_id=run_id):
            raise ValueError("artifact URI is outside the authenticated Lab and run")

        digest = hashlib.sha256(content).hexdigest()
        prepared_metadata = dict(metadata or {})
        candidate = Artifact(
            id=self.id_factory(),
            run_id=run_id,
            lab_id=identity.lab_id,
            kind=kind,
            uri=uri,
            sha256=digest,
            bytes=len(content),
            produced_by_step=produced_by_step,
            metadata=prepared_metadata,
            created_at=self.clock(),
        )
        with self._access(identity, "runs:write") as cursor:
            self._require_run(cursor, identity, run_id)
            cursor.execute(
                f"SELECT {self._select} FROM artifacts "
                "WHERE lab_id = %s AND run_id = %s AND uri = %s",
                (identity.lab_id, run_id, uri),
            )
            row = cursor.fetchone()
            if row is not None:
                stored = self._from_row(row)
                immutable = ("kind", "sha256", "bytes", "produced_by_step", "metadata")
                if any(getattr(stored, field) != getattr(candidate, field) for field in immutable):
                    raise ArtifactConflict("artifact URI already registered with different immutable data")
                return stored

            self.storage.put(
                uri,
                content,
                metadata=prepared_metadata,
                if_none_match=True,
            )
            cursor.execute(
                """
                INSERT INTO artifacts
                    (id, run_id, lab_id, kind, uri, sha256, bytes,
                     produced_by_step, metadata, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (lab_id, run_id, uri) DO NOTHING
                """,
                (
                    candidate.id,
                    candidate.run_id,
                    candidate.lab_id,
                    candidate.kind,
                    candidate.uri,
                    candidate.sha256,
                    candidate.bytes,
                    candidate.produced_by_step,
                    json.dumps(candidate.metadata, sort_keys=True, separators=(",", ":")),
                    candidate.created_at,
                ),
            )
            cursor.execute(
                f"SELECT {self._select} FROM artifacts "
                "WHERE lab_id = %s AND run_id = %s AND uri = %s",
                (identity.lab_id, run_id, uri),
            )
            row = cursor.fetchone()
            if row is None:
                raise ArtifactNotFound("artifact not found")
            stored = self._from_row(row)
            immutable = ("kind", "sha256", "bytes", "produced_by_step", "metadata")
            if any(getattr(stored, field) != getattr(candidate, field) for field in immutable):
                raise ArtifactConflict("artifact URI already registered with different immutable data")
            return stored

    def list_for_run(self, identity: Identity, run_id: str) -> list[Artifact]:
        with self._access(identity, "artifacts:read") as cursor:
            self._require_run(cursor, identity, run_id)
            cursor.execute(
                f"SELECT {self._select} FROM artifacts "
                "WHERE lab_id = %s AND run_id = %s ORDER BY id",
                (identity.lab_id, run_id),
            )
            return [self._from_row(row) for row in cursor.fetchall()]

    def list_knowledge_sources(
        self, identity: Identity, *, limit: int = _MAX_KNOWLEDGE_SOURCE_LIMIT
    ) -> list[Artifact]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        bounded_limit = min(limit, _MAX_KNOWLEDGE_SOURCE_LIMIT)
        with self._access(identity, "artifacts:read") as cursor:
            cursor.execute(
                f"SELECT {self._select} FROM artifacts "
                "WHERE lab_id = %s AND kind IN ('document', 'report') "
                "ORDER BY created_at DESC, id LIMIT %s",
                (identity.lab_id, bounded_limit),
            )
            return [self._from_row(row) for row in cursor.fetchall()]

    def get(self, identity: Identity, artifact_id: str) -> Artifact:
        with self._access(identity, "artifacts:read") as cursor:
            cursor.execute(
                f"SELECT {self._select} FROM artifacts WHERE lab_id = %s AND id = %s",
                (identity.lab_id, artifact_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise ArtifactNotFound("artifact not found")
            return self._from_row(row)

    def presign(self, identity: Identity, artifact_id: str) -> str:
        artifact = self.get(identity, artifact_id)
        return self.storage.presign(artifact.uri, expires_in=900)

    def read_bytes(self, identity: Identity, artifact_id: str) -> bytes:
        artifact = self.get(identity, artifact_id)
        body = self.storage.read_bytes(artifact.uri)
        if not isinstance(body, bytes) or hashlib.sha256(body).hexdigest() != artifact.sha256:
            raise ValueError("artifact content digest mismatch")
        return body
