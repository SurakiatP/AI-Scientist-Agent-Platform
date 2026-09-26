from __future__ import annotations

from scilab.redaction import SENSITIVE_KEYS, is_sensitive_key, redact


def test_sensitive_keys_cover_the_required_union() -> None:
    assert SENSITIVE_KEYS == {
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


def test_is_sensitive_key_matches_case_insensitively_and_by_suffix() -> None:
    assert is_sensitive_key("Password") is True
    assert is_sensitive_key("API_KEY") is True
    assert is_sensitive_key("refresh_token") is True
    assert is_sensitive_key("client_secret") is True
    assert is_sensitive_key("safe_field") is False
    assert is_sensitive_key(None) is False
    assert is_sensitive_key(1) is False


def test_redact_replaces_sensitive_keys_and_recurses_without_mutating_input() -> None:
    payload = {
        "query": "safe",
        "api_key": "must-not-leak",
        "nested": {"password": "must-not-leak", "kept": "ok"},
        "items": [{"token": "must-not-leak"}, "plain"],
        "tuple_items": ({"secret": "must-not-leak"},),
    }
    original = {
        "query": "safe",
        "api_key": "must-not-leak",
        "nested": {"password": "must-not-leak", "kept": "ok"},
        "items": [{"token": "must-not-leak"}, "plain"],
        "tuple_items": ({"secret": "must-not-leak"},),
    }

    redacted = redact(payload)

    assert payload == original
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["nested"]["kept"] == "ok"
    assert redacted["items"][0]["token"] == "[REDACTED]"
    assert redacted["items"][1] == "plain"
    assert redacted["tuple_items"] == [{"secret": "[REDACTED]"}]
    assert "must-not-leak" not in repr(redacted)


def test_redact_passes_through_non_collection_values() -> None:
    assert redact("plain") == "plain"
    assert redact(42) == 42
    assert redact(None) is None


def test_events_audit_and_approvals_share_the_same_redaction_function() -> None:
    from scilab import approvals, audit, events

    assert events._redact is redact
    assert audit.redact_sensitive is redact
    assert approvals._redact is redact
