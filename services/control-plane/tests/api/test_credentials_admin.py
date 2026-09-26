from __future__ import annotations

from contextlib import contextmanager
import hashlib
from typing import Any

import pytest
from fastapi import HTTPException

from scilab.api.credentials_admin import A2APeerAdminService, APIKeyAdminService, LabCardLookup
from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.tenancy import AuthorizationError


class Cursor:
    def __init__(self, connection: "Connection") -> None:
        self.connection = connection
        self.rows: list[tuple[Any, ...]] = []
        self.row: tuple[Any, ...] | None = None

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        query = " ".join(sql.split())
        self.connection.queries.append((query, params))
        self.rows = []
        self.row = None

        if query == SET_TENANT_SQL:
            self.connection.settings[params[0]] = params[1]
            if params[0] == TENANT_SETTING:
                self.connection.tenant = params[1]
        elif query.startswith("SELECT peer_name, lab_id, scopes, secret_hash FROM a2a_peers"):
            digest = params[0]
            match = next(
                (item for item in self.connection.peers if item["secret_hash"] == digest), None
            )
            self.row = (
                (match["peer_name"], match["lab_id"], match["scopes"], match["secret_hash"])
                if match else None
            )
        elif query.startswith("SELECT name FROM labs WHERE id"):
            self.row = self.connection.labs.get(params[0])
        elif query.startswith("SELECT key_id, name, scopes FROM api_credentials"):
            lab_id = params[0]
            self.rows = [
                (item["key_id"], item["name"], item["scopes"])
                for item in self.connection.api_keys
                if item["lab_id"] == lab_id == self.connection.tenant
            ]
        elif query.startswith("SELECT peer_name, name, scopes FROM a2a_peers"):
            lab_id = params[0]
            self.rows = [
                (item["peer_name"], item["name"], item["scopes"])
                for item in self.connection.peers
                if item["lab_id"] == lab_id == self.connection.tenant
            ]
        elif query.startswith("INSERT INTO api_credentials"):
            key_id, lab_id, secret_hash, scopes, name = params
            duplicate = any(
                item["lab_id"] == lab_id and item["name"] == name
                for item in self.connection.api_keys
            )
            if lab_id == self.connection.tenant and not duplicate:
                self.connection.api_keys.append({
                    "key_id": key_id,
                    "lab_id": lab_id,
                    "secret_hash": secret_hash,
                    "scopes": scopes,
                    "name": name,
                })
                self.row = (key_id,)
        elif query.startswith("INSERT INTO a2a_peers"):
            peer_name, lab_id, secret_hash, scopes, name = params
            duplicate = any(
                item["lab_id"] == lab_id and item["name"] == name
                for item in self.connection.peers
            )
            if lab_id == self.connection.tenant and not duplicate:
                self.connection.peers.append({
                    "peer_name": peer_name,
                    "lab_id": lab_id,
                    "secret_hash": secret_hash,
                    "scopes": scopes,
                    "name": name,
                })
                self.row = (peer_name,)
        elif query.startswith("DELETE FROM api_credentials"):
            key_id, lab_id = params
            for index, item in enumerate(self.connection.api_keys):
                if (item["key_id"] == key_id and item["lab_id"] == lab_id
                        and lab_id == self.connection.tenant):
                    del self.connection.api_keys[index]
                    self.row = (key_id,)
                    break
        else:
            raise AssertionError(f"unexpected query: {query}")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows


class Connection:
    def __init__(self) -> None:
        self.tenant: str | None = None
        self.settings: dict[str, Any] = {}
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.api_keys: list[dict[str, Any]] = []
        self.peers: list[dict[str, Any]] = []
        self.labs: dict[str, tuple[Any, ...]] = {}

    @contextmanager
    def transaction(self):
        yield

    def cursor(self) -> Cursor:
        return Cursor(self)


def admin(lab_id: str = "lab-a") -> Identity:
    return Identity(lab_id, "user:owner", frozenset({"lab:admin"}))


def test_credential_services_deny_non_admin_before_database_access() -> None:
    connection = Connection()
    key_service = APIKeyAdminService(connection)
    peer_service = A2APeerAdminService(connection)
    reader = Identity("lab-a", "user:reader", frozenset({"runs:read"}))

    with pytest.raises(AuthorizationError):
        key_service.list(reader)
    with pytest.raises(AuthorizationError):
        key_service.create(reader, {"name": "ci"})
    with pytest.raises(AuthorizationError):
        key_service.delete(reader, "key-1")
    with pytest.raises(AuthorizationError):
        peer_service.list(reader)
    with pytest.raises(AuthorizationError):
        peer_service.create(reader, {"name": "hermes"})

    assert connection.queries == []


def test_list_and_delete_are_tenant_scoped_and_keep_legacy_null_names() -> None:
    connection = Connection()
    connection.api_keys.extend([
        {"key_id": "old-a", "lab_id": "lab-a", "secret_hash": b"a" * 32,
         "scopes": ["runs:read"], "name": None},
        {"key_id": "old-b", "lab_id": "lab-b", "secret_hash": b"b" * 32,
         "scopes": ["runs:read"], "name": None},
    ])
    connection.peers.extend([
        {"peer_name": "legacy-a", "lab_id": "lab-a", "secret_hash": b"a" * 32,
         "scopes": ["runs:read"], "name": None},
        {"peer_name": "legacy-b", "lab_id": "lab-b", "secret_hash": b"b" * 32,
         "scopes": ["runs:read"], "name": None},
    ])
    keys = APIKeyAdminService(connection)
    peers = A2APeerAdminService(connection)

    assert keys.list(admin()) == [{
        "id": "old-a", "key_id": "old-a", "name": "old-a",
        "scopes": ["runs:read"],
    }]
    assert peers.list(admin()) == [{
        "id": "legacy-a", "peer_name": "legacy-a", "name": "legacy-a",
        "scopes": ["runs:read"],
    }]
    assert connection.api_keys[0]["name"] is None
    assert connection.peers[0]["name"] is None
    with pytest.raises(LookupError):
        keys.delete(admin(), "old-b")
    assert [item["key_id"] for item in connection.api_keys] == ["old-a", "old-b"]


def test_duplicate_names_conflict_within_a_lab_but_are_tenant_local() -> None:
    connection = Connection()
    keys = APIKeyAdminService(connection)
    peers = A2APeerAdminService(connection)

    keys.create(admin("lab-a"), {"name": "automation"})
    with pytest.raises(HTTPException) as key_conflict:
        keys.create(admin("lab-a"), {"name": "automation"})
    assert key_conflict.value.status_code == 409
    keys.create(admin("lab-b"), {"name": "automation"})

    peer_a = peers.create(admin("lab-a"), {"name": "hermes"})
    with pytest.raises(HTTPException) as peer_conflict:
        peers.create(admin("lab-a"), {"name": "hermes"})
    assert peer_conflict.value.status_code == 409
    peer_b = peers.create(admin("lab-b"), {"name": "hermes"})
    assert peer_a["peer_name"] != peer_b["peer_name"]


def test_create_returns_secret_once_and_persists_only_its_digest() -> None:
    connection = Connection()
    keys = APIKeyAdminService(connection)
    peers = A2APeerAdminService(connection)

    key = keys.create(admin(), {"name": "automation"})
    peer = peers.create(admin(), {"name": "hermes"})

    for response, stored, identifier in (
        (key, connection.api_keys[0], "key_id"),
        (peer, connection.peers[0], "peer_name"),
    ):
        secret = response["secret"]
        assert isinstance(secret, str) and len(secret) >= 40
        assert response["id"] == response[identifier]
        assert response[identifier] == stored[identifier]
        assert stored["secret_hash"] == hashlib.sha256(
            secret.encode("utf-8")
        ).digest()
        assert secret not in stored.values()
        assert response["scopes"] == ["runs:read", "runs:write"]

    listed_keys = keys.list(admin())
    listed_peers = peers.list(admin())
    assert listed_keys == [{
        "id": key["key_id"], "key_id": key["key_id"], "name": "automation",
        "scopes": ["runs:read", "runs:write"],
    }]
    assert listed_peers == [{
        "id": peer["peer_name"], "peer_name": peer["peer_name"], "name": "hermes",
        "scopes": ["runs:read", "runs:write"],
    }]
    assert all("secret" not in item and "secret_hash" not in item
               for item in listed_keys + listed_peers)


def test_create_accepts_only_a_nonblank_name() -> None:
    service = APIKeyAdminService(Connection())

    for body in ({}, {"name": "  "}, {"name": "ci", "lab_id": "lab-b"}):
        with pytest.raises(HTTPException) as invalid:
            service.create(admin(), body)
        assert invalid.value.status_code == 422


def test_lookup_resolves_a_bearer_secret_to_its_peer_record() -> None:
    connection = Connection()
    peers = A2APeerAdminService(connection)
    created = peers.create(admin(), {"name": "hermes"})

    resolved = peers.lookup(created["secret"])

    assert resolved is not None
    assert resolved["peer_name"] == created["peer_name"]
    assert resolved["lab_id"] == "lab-a"
    assert peers.lookup("not-a-real-secret") is None


def test_lab_card_lookup_scopes_by_tenant_and_returns_public_fields() -> None:
    connection = Connection()
    connection.labs["lab-a"] = ("Lab A",)
    cards = LabCardLookup(connection)

    card = cards.lookup("lab-a")

    assert card == {"name": "Lab A", "description": "SciLab research Lab Lab A"}
    assert connection.tenant == "lab-a"
    assert cards.lookup("lab-missing") is None
    assert cards.lookup("") is None
    assert cards.lookup("   ") is None
