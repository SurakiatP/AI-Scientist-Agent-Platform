from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


class IdentityError(ValueError):
    """Raised when trusted identity data is malformed."""


class AuthenticationError(IdentityError):
    """Raised when a credential cannot be authenticated."""


ALL_SCOPES = frozenset(
    {"runs:read", "runs:write", "runs:approve", "artifacts:read", "lab:admin"}
)
ROLE_SCOPES = {
    "owner": frozenset(
        {"runs:read", "runs:write", "runs:approve", "artifacts:read", "lab:admin"}
    ),
    "researcher": frozenset({"runs:read", "runs:write", "artifacts:read"}),
    "viewer": frozenset({"runs:read", "artifacts:read"}),
}


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IdentityError(f"{field} must be a non-blank string")
    return value


def _scopes(value: Any) -> frozenset[str]:
    if isinstance(value, (str, bytes, Mapping)) or value is None:
        raise IdentityError("scopes must be an iterable of scope strings")
    try:
        result = frozenset(value)
    except TypeError as exc:
        raise IdentityError("scopes must be an iterable of scope strings") from exc
    if not result or any(
        not isinstance(scope, str) or not scope.strip() for scope in result
    ):
        raise IdentityError("scopes cannot be empty or contain blank values")
    unknown = result - ALL_SCOPES
    if unknown:
        raise IdentityError(f"unknown scopes: {sorted(unknown)}")
    return result


@dataclass(frozen=True, slots=True)
class Identity:
    lab_id: str
    principal: str
    scopes: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "lab_id", _text(self.lab_id, "lab_id"))
        object.__setattr__(self, "principal", _text(self.principal, "principal"))
        object.__setattr__(self, "scopes", _scopes(self.scopes))


def _field(record: Mapping[str, Any] | object, name: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(name)
    return getattr(record, name, None)


def identity_from_verified_oidc(
    subject: str, membership: Mapping[str, Any] | object
) -> Identity:
    subject = _text(subject, "subject")
    membership_subject = _text(_field(membership, "subject"), "membership.subject")
    if membership_subject != subject:
        raise IdentityError("membership subject does not match verified subject")
    lab_id = _text(_field(membership, "lab_id"), "membership.lab_id")
    role = _text(_field(membership, "role"), "membership.role")
    try:
        scopes = ROLE_SCOPES[role]
    except KeyError as exc:
        raise IdentityError(f"unknown role: {role}") from exc
    return Identity(lab_id, f"user:{subject}", scopes)


def identity_from_verified_mcp(verified_token: Mapping[str, Any]) -> Identity:
    if not isinstance(verified_token, Mapping):
        raise AuthenticationError("verified MCP token must be a mapping")
    audience = verified_token.get("aud")
    if audience != "mcp.scilab" and not (
        isinstance(audience, (list, tuple)) and "mcp.scilab" in audience
    ):
        raise AuthenticationError("MCP audience must be exactly mcp.scilab")

    principal_type = verified_token.get("scilab_principal_type")
    if principal_type == "user":
        principal_field = "sub"
    elif principal_type == "client":
        principal_field = "client_id"
    else:
        raise AuthenticationError("MCP principal type must be user or client")

    try:
        principal_id = _text(verified_token.get(principal_field), principal_field)
        lab_id = _text(verified_token.get("lab_id"), "lab_id")
        token_scopes = verified_token.get("scopes")
        if not isinstance(token_scopes, (list, tuple, set, frozenset)) or any(
            not isinstance(scope, str) or not scope.strip() for scope in token_scopes
        ):
            raise IdentityError("MCP scopes must be non-blank scope strings")
        return Identity(
            lab_id,
            f"{principal_type}:{principal_id}",
            frozenset(token_scopes) & ALL_SCOPES,
        )
    except IdentityError as exc:
        raise AuthenticationError(str(exc)) from exc


def credential_digest(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        if not secret.strip():
            raise AuthenticationError("credential is blank")
        raw = secret.encode("utf-8")
    elif isinstance(secret, bytes):
        if not secret:
            raise AuthenticationError("credential is blank")
        raw = secret
    else:
        raise AuthenticationError("credential must be text or bytes")
    return hashlib.sha256(raw).digest()


def _stored_digest(value: Any) -> bytes | None:
    if isinstance(value, bytes):
        return value if len(value) == hashlib.sha256().digest_size else None
    if not isinstance(value, str) or len(value) != hashlib.sha256().digest_size * 2:
        return None
    if any(character not in "0123456789abcdefABCDEF" for character in value):
        return None
    try:
        return bytes.fromhex(value)
    except ValueError:
        return None


def _authenticate(
    secret: str | bytes,
    records: Iterable[Mapping[str, Any] | object],
    *,
    id_field: str,
    principal_prefix: str,
) -> Identity:
    digest = credential_digest(secret)
    for record in records:
        stored_digest = _stored_digest(_field(record, "secret_hash"))
        if stored_digest is None or not hmac.compare_digest(digest, stored_digest):
            continue
        try:
            identifier = _text(_field(record, id_field), id_field)
            lab_id = _text(_field(record, "lab_id"), "lab_id")
            return Identity(
                lab_id,
                f"{principal_prefix}:{identifier}",
                _field(record, "scopes"),
            )
        except IdentityError as exc:
            raise AuthenticationError("credential record is invalid") from exc
    raise AuthenticationError("credential rejected")


def authenticate_api_key(
    secret: str | bytes, records: Iterable[Mapping[str, Any] | object]
) -> Identity:
    return _authenticate(secret, records, id_field="key_id", principal_prefix="key")


def authenticate_a2a_peer(
    secret: str | bytes, records: Iterable[Mapping[str, Any] | object]
) -> Identity:
    return _authenticate(secret, records, id_field="peer_name", principal_prefix="peer")
