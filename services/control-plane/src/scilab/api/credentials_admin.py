"""Lab-scoped API-key and A2A-peer administration services."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import secrets
from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from scilab.db import SET_TENANT_SQL, TENANT_SETTING, load_a2a_peer_credential
from scilab.identity import Identity, credential_digest
from scilab.tenancy import require_scope

_INITIAL_SCOPES = ("runs:read", "runs:write")


@contextmanager
def _tenant_cursor(connection: Any, identity: Identity):
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
            yield cursor


def _name(body: Mapping[str, Any]) -> str:
    if not isinstance(body, Mapping) or set(body) != {"name"}:
        raise HTTPException(status_code=422, detail="request body must contain only name")
    name = body["name"]
    if not isinstance(name, str) or not name.strip():
        raise HTTPException(status_code=422, detail="name must be a non-blank string")
    return name.strip()


def _column(row: Any, name: str, index: int) -> Any:
    return row[name] if isinstance(row, Mapping) else row[index]


class APIKeyAdminService:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def list(self, identity: Identity) -> list[dict[str, Any]]:
        require_scope(identity, "lab:admin")
        with _tenant_cursor(self.connection, identity) as cursor:
            cursor.execute(
                "SELECT key_id, name, scopes FROM api_credentials "
                "WHERE lab_id = %s ORDER BY key_id",
                (identity.lab_id,),
            )
            return [
                {
                    "id": _column(row, "key_id", 0),
                    "key_id": _column(row, "key_id", 0),
                    "name": _column(row, "name", 1) or _column(row, "key_id", 0),
                    "scopes": list(_column(row, "scopes", 2)),
                }
                for row in cursor.fetchall()
            ]

    def create(self, identity: Identity, body: dict[str, Any]) -> dict[str, Any]:
        require_scope(identity, "lab:admin")
        name = _name(body)
        key_id = f"key_{uuid4().hex}"
        secret = secrets.token_urlsafe(32)
        with _tenant_cursor(self.connection, identity) as cursor:
            cursor.execute(
                "INSERT INTO api_credentials (key_id, lab_id, secret_hash, scopes, name) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (lab_id, name) DO NOTHING RETURNING key_id",
                (key_id, identity.lab_id, credential_digest(secret),
                 list(_INITIAL_SCOPES), name),
            )
            if cursor.fetchone() is None:
                raise HTTPException(status_code=409, detail="API key name already exists")
        return {
            "id": key_id,
            "key_id": key_id,
            "name": name,
            "scopes": list(_INITIAL_SCOPES),
            "secret": secret,
        }

    def delete(self, identity: Identity, key_id: str) -> None:
        require_scope(identity, "lab:admin")
        if not isinstance(key_id, str) or not key_id.strip():
            raise HTTPException(status_code=422, detail="key_id must be non-blank")
        with _tenant_cursor(self.connection, identity) as cursor:
            cursor.execute(
                "DELETE FROM api_credentials WHERE key_id = %s AND lab_id = %s "
                "RETURNING key_id",
                (key_id, identity.lab_id),
            )
            if cursor.fetchone() is None:
                raise LookupError("API key not found")


class A2APeerAdminService:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def list(self, identity: Identity) -> list[dict[str, Any]]:
        require_scope(identity, "lab:admin")
        with _tenant_cursor(self.connection, identity) as cursor:
            cursor.execute(
                "SELECT peer_name, name, scopes FROM a2a_peers "
                "WHERE lab_id = %s ORDER BY peer_name",
                (identity.lab_id,),
            )
            return [
                {
                    "id": _column(row, "peer_name", 0),
                    "peer_name": _column(row, "peer_name", 0),
                    "name": _column(row, "name", 1) or _column(row, "peer_name", 0),
                    "scopes": list(_column(row, "scopes", 2)),
                }
                for row in cursor.fetchall()
            ]

    def create(self, identity: Identity, body: dict[str, Any]) -> dict[str, Any]:
        require_scope(identity, "lab:admin")
        name = _name(body)
        peer_name = f"peer_{uuid4().hex}"
        secret = secrets.token_urlsafe(32)
        with _tenant_cursor(self.connection, identity) as cursor:
            cursor.execute(
                "INSERT INTO a2a_peers (peer_name, lab_id, secret_hash, scopes, name) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (lab_id, name) DO NOTHING RETURNING peer_name",
                (peer_name, identity.lab_id, credential_digest(secret),
                 list(_INITIAL_SCOPES), name),
            )
            if cursor.fetchone() is None:
                raise HTTPException(status_code=409, detail="A2A peer name already exists")
        return {
            "id": peer_name,
            "peer_name": peer_name,
            "name": name,
            "scopes": list(_INITIAL_SCOPES),
            "secret": secret,
        }

    def lookup(self, secret: str) -> Mapping[str, Any] | None:
        """Resolve a peer bearer secret for scilab.api.a2a's per-request peer lookup."""
        return load_a2a_peer_credential(self.connection, secret)


class LabCardLookup:
    """Public per-Lab Agent Card fields for scilab.api.a2a; tenant-scoped like every Lab row read."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def lookup(self, lab_id: str) -> dict[str, Any] | None:
        if not isinstance(lab_id, str) or not lab_id.strip():
            return None
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, lab_id))
                cursor.execute("SELECT name FROM labs WHERE id = %s", (lab_id,))
                row = cursor.fetchone()
        if row is None:
            return None
        name = _column(row, "name", 0)
        # ponytail: labs has no description column; synthesize one instead of a migration.
        return {"name": name, "description": f"SciLab research Lab {name}"}
