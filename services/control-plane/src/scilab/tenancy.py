from __future__ import annotations

from scilab.identity import ALL_SCOPES, Identity


class AuthorizationError(PermissionError):
    """Raised when an identity cannot access a scope or lab."""


def require_scope(identity: Identity, scope: str) -> None:
    if scope not in ALL_SCOPES or scope not in identity.scopes:
        raise AuthorizationError(f"missing scope: {scope}")


def require_lab(identity: Identity, lab_id: str) -> None:
    if not isinstance(lab_id, str) or identity.lab_id != lab_id:
        raise AuthorizationError("cross-Lab access denied")
