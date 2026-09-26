from __future__ import annotations

from collections.abc import Mapping
from typing import Any


SENSITIVE_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "authorization",
        "cookie",
        "client_secret",
        "private_key",
        "credential",
    }
)


def is_sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return lowered in SENSITIVE_KEYS or lowered.endswith("_token") or lowered.endswith("_secret")


def redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: "[REDACTED]" if is_sensitive_key(key) else redact(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value
