from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request


class IdentityCursor:
    def __init__(self, connection: "IdentityConnection") -> None:
        self.connection = connection
        self.row: dict[str, str] | None = None

    def __enter__(self) -> "IdentityCursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        if sql.startswith("SELECT set_config"):
            self.connection.settings[params[0]] = params[1]
        elif "FROM lab_memberships" in sql:
            subject = self.connection.settings["scilab.bootstrap_subject"]
            lab_id = self.connection.settings["scilab.bootstrap_lab_id"]
            if (subject, lab_id) == (
                self.connection.membership["subject"],
                self.connection.membership["lab_id"],
            ):
                self.row = self.connection.membership

    def fetchone(self) -> dict[str, str] | None:
        return self.row


class IdentityConnection:
    def __init__(self, membership: dict[str, str]) -> None:
        self.membership = membership
        self.settings: dict[str, str] = {}

    @contextmanager
    def transaction(self):
        yield

    def cursor(self) -> IdentityCursor:
        return IdentityCursor(self)


class StaticJwks:
    def __init__(self, key: Any) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, _token: str) -> SimpleNamespace:
        return SimpleNamespace(key=self.key)


def _resolver(membership: dict[str, str] | None = None) -> tuple[Any, Any, Any]:
    from scilab.api.oidc import OIDCIdentityResolver

    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    connection = IdentityConnection(
        membership
        or {"subject": "subject-1", "lab_id": "lab-a", "role": "owner"}
    )
    resolver = OIDCIdentityResolver(
        issuer="https://id.example/realms/sci",
        audience="scilab-rest",
        jwks_uri="https://id.example/realms/sci/protocol/openid-connect/certs",
        connection=connection,
        jwks_client=StaticJwks(signing_key.public_key()),
    )
    return resolver, signing_key, connection


def _token(
    signing_key: Any,
    *,
    issuer: str = "https://id.example/realms/sci",
    audience: str = "scilab-rest",
    subject: str = "subject-1",
    lab_id: str = "lab-a",
    algorithm: str = "RS256",
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "sub": subject,
            "lab_id": lab_id,
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        signing_key if algorithm != "none" else "",
        algorithm=algorithm,
        headers={"kid": "test-key"},
    )


def test_oidc_identity_comes_from_verified_claim_and_lab_membership() -> None:
    resolver, signing_key, connection = _resolver()
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/me",
            "headers": [(b"authorization", f"Bearer {_token(signing_key)}".encode())],
        }
    )

    identity = resolver(request)

    assert identity.lab_id == "lab-a"
    assert identity.principal == "user:subject-1"
    assert "lab:admin" in identity.scopes
    assert connection.settings["scilab.bootstrap_subject"] == "subject-1"
    assert connection.settings["scilab.bootstrap_lab_id"] == "lab-a"


@pytest.mark.parametrize(
    "claims",
    [
        {"issuer": "https://attacker.example"},
        {"audience": "other-api"},
    ],
)
def test_oidc_rejects_wrong_issuer_or_audience_before_membership_lookup(
    claims: dict[str, str],
) -> None:
    resolver, signing_key, connection = _resolver()
    token = _token(
        signing_key,
        issuer=claims.get("issuer", "https://id.example/realms/sci"),
        audience=claims.get("audience", "scilab-rest"),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/me",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )

    with pytest.raises(HTTPException) as raised:
        resolver(request)

    assert raised.value.status_code == 401
    assert "scilab.bootstrap_subject" not in connection.settings


def test_oidc_rejects_invalid_signature_and_unverified_algorithm() -> None:
    resolver, _, _ = _resolver()
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in (
        _token(other_key),
        _token(rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="none"),
    ):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/v1/me",
                "headers": [(b"authorization", f"Bearer {token}".encode())],
            }
        )
        with pytest.raises(HTTPException) as raised:
            resolver(request)
        assert raised.value.status_code == 401


def test_oidc_rejects_subject_without_lab_membership() -> None:
    resolver, signing_key, _ = _resolver(
        {"subject": "someone-else", "lab_id": "lab-a", "role": "owner"}
    )
    token = _token(signing_key)
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/me",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )

    with pytest.raises(HTTPException) as raised:
        resolver(request)

    assert raised.value.status_code == 401
