from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from io import BytesIO
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from fastapi import HTTPException
from minio import Minio

from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.tenancy import require_scope

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class UploadedInputNotFound(LookupError):
    """Raised when one or more uploaded input references are unavailable."""


class InputUploadService:
    def __init__(
        self,
        connection: Any,
        storage: Minio,
        *,
        bucket: str,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[], str] = lambda: str(uuid4()),
    ) -> None:
        if max_upload_bytes <= 0:
            raise ValueError("max_upload_bytes must be positive")
        if not bucket.strip():
            raise ValueError("bucket must be non-blank")
        self.connection = connection
        self.storage = storage
        self.bucket = bucket
        self.max_upload_bytes = max_upload_bytes
        self.clock = clock
        self.id_factory = id_factory

    def create(self, identity: Identity, body: Mapping[str, Any]) -> dict[str, str]:
        require_scope(identity, "runs:write")
        if not isinstance(body, Mapping) or set(body) != {
            "name",
            "media_type",
            "data_base64",
        }:
            raise HTTPException(status_code=422, detail="invalid input upload request")
        name = body["name"]
        media_type = body["media_type"]
        encoded = body["data_base64"]
        if (
            not isinstance(name, str)
            or not name.strip()
            or "\x00" in name
            or not isinstance(media_type, str)
            or not media_type.strip()
            or any(character in media_type for character in "\r\n\x00")
            or not isinstance(encoded, str)
        ):
            raise HTTPException(status_code=422, detail="invalid input upload request")
        if len(encoded) > ((self.max_upload_bytes + 2) // 3) * 4:
            raise HTTPException(status_code=413, detail="file exceeds upload limit")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise HTTPException(
                status_code=422, detail="invalid base64 file data"
            ) from None
        if len(content) > self.max_upload_bytes:
            raise HTTPException(status_code=413, detail="file exceeds upload limit")
        input_id = self.id_factory()
        object_key = f"labs/{quote(identity.lab_id, safe='')}/inputs/{input_id}"
        digest = hashlib.sha256(content).hexdigest()

        try:
            with self.connection.transaction():
                with self.connection.cursor() as cursor:
                    cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                    cursor.execute(
                        """
                        INSERT INTO uploaded_inputs
                            (id, lab_id, name, media_type, bucket_name, object_key,
                             sha256, bytes, status, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'pending', %s)
                        """,
                        (
                            input_id,
                            identity.lab_id,
                            name.strip(),
                            media_type.strip(),
                            self.bucket,
                            object_key,
                            digest,
                            len(content),
                            self.clock(),
                        ),
                    )
        except Exception:
            raise HTTPException(
                status_code=503, detail="input upload could not be stored"
            ) from None

        try:
            with self.connection.transaction():
                with self.connection.cursor() as cursor:
                    cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                    cursor.execute(
                        "SELECT id FROM uploaded_inputs "
                        "WHERE lab_id = %s AND id = %s AND status = 'pending' FOR UPDATE",
                        (identity.lab_id, input_id),
                    )
                    if cursor.fetchone() is None:
                        raise RuntimeError("pending upload disappeared")
                    self.storage.put_object(
                        self.bucket,
                        object_key,
                        BytesIO(content),
                        len(content),
                        content_type=media_type.strip(),
                    )
                    cursor.execute(
                        "UPDATE uploaded_inputs SET status = 'ready' "
                        "WHERE lab_id = %s AND id = %s AND status = 'pending' RETURNING id",
                        (identity.lab_id, input_id),
                    )
                    if cursor.fetchone() is None:
                        raise RuntimeError("pending upload disappeared")
        except Exception:
            recovery_pending = False
            try:
                self.storage.remove_object(self.bucket, object_key)
            except Exception:
                # The committed pending row retains the object key for reconciliation.
                recovery_pending = True
            else:
                try:
                    with self.connection.transaction():
                        with self.connection.cursor() as cursor:
                            cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                            cursor.execute(
                                "DELETE FROM uploaded_inputs "
                                "WHERE lab_id = %s AND id = %s AND status = 'pending'",
                                (identity.lab_id, input_id),
                            )
                except Exception:
                    # The pending row is safe and can be reconciled later.
                    recovery_pending = True
            raise HTTPException(
                status_code=503,
                detail=(
                    "input upload pending recovery"
                    if recovery_pending else "input upload could not be stored"
                ),
            ) from None

        return {"artifact_id": input_id}

    def validate_refs(self, identity: Identity, ids: Sequence[str]) -> list[str]:
        require_scope(identity, "runs:write")
        if isinstance(ids, (str, bytes)) or not isinstance(ids, Sequence):
            raise ValueError("input references must be a sequence of IDs")
        if any(
            not isinstance(input_id, str)
            or not input_id.strip()
            or any(character.isspace() for character in input_id)
            for input_id in ids
        ):
            raise ValueError("input references must be non-blank IDs")
        references = list(ids)
        if not references:
            return []

        unique_ids = list(dict.fromkeys(references))
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                cursor.execute(
                    "SELECT id FROM uploaded_inputs "
                    "WHERE lab_id = %s AND id = ANY(%s) AND status = 'ready'",
                    (identity.lab_id, unique_ids),
                )
                rows = cursor.fetchall()
        found = {
            row["id"] if isinstance(row, Mapping) else row[0]
            for row in rows
        }
        if not set(unique_ids).issubset(found):
            raise UploadedInputNotFound("uploaded input not found")
        return references

    def reconcile_pending(self, identity: Identity, *, before: datetime, limit: int = 100) -> int:
        """Remove stale, never-ready objects; retained rows make retries safe."""
        require_scope(identity, "lab:admin")
        if before.tzinfo is None or before.utcoffset() is None or not 1 <= limit <= 100:
            raise ValueError("aware cutoff and limit 1..100 required")
        try:
            with self.connection.transaction():
                with self.connection.cursor() as cursor:
                    cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                    cursor.execute(
                        "SELECT id, bucket_name, object_key FROM uploaded_inputs "
                        "WHERE lab_id = %s AND status = 'pending' AND created_at < %s "
                        "ORDER BY created_at LIMIT %s FOR UPDATE SKIP LOCKED",
                        (identity.lab_id, before, limit),
                    )
                    pending = cursor.fetchall()
                    for row in pending:
                        input_id, bucket, object_key = (
                            (row["id"], row["bucket_name"], row["object_key"])
                            if isinstance(row, Mapping)
                            else row
                        )
                        self.storage.remove_object(bucket, object_key)
                        cursor.execute(
                            "DELETE FROM uploaded_inputs "
                            "WHERE lab_id = %s AND id = %s AND status = 'pending'",
                            (identity.lab_id, input_id),
                        )
        except Exception:
            raise HTTPException(status_code=503, detail="pending upload cleanup failed") from None
        return len(pending)
