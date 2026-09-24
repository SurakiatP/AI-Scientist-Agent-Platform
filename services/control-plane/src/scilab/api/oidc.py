"""Verified Keycloak bearer token to tenant-scoped Identity adapter."""

from __future__ import annotations

from typing import Any

import jwt
from fastapi import HTTPException, Request

from scilab.db import load_membership
from scilab.identity import Identity, identity_from_verified_oidc


class OIDCIdentityResolver:
    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_uri: str,
        connection: Any,
        jwks_client: Any | None = None,
    ) -> None:
        if not issuer or not audience or not jwks_uri:
            raise ValueError("OIDC issuer, audience and JWKS URI are required")
        self.issuer = issuer
        self.audience = audience
        self.connection = connection
        self.jwks_client = jwks_client or jwt.PyJWKClient(jwks_uri)

    def __call__(self, request: Request) -> Identity:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise HTTPException(status_code=401, detail="bearer token required")
        try:
            signing_key = self.jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
            subject = claims["sub"]
            lab_id = claims["lab_id"]
            if not isinstance(subject, str) or not subject.strip() or not isinstance(lab_id, str) or not lab_id.strip():
                raise ValueError("invalid OIDC identity claims")
            membership = load_membership(self.connection, subject, lab_id)
            if membership is None:
                raise ValueError("subject has no Lab membership")
            return identity_from_verified_oidc(subject, membership)
        except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="invalid OIDC identity") from exc
