import hashlib
import hmac

import pytest

from scilab.identity import (
    ROLE_SCOPES,
    AuthenticationError,
    Identity,
    IdentityError,
    authenticate_a2a_peer,
    authenticate_api_key,
    identity_from_verified_mcp,
    identity_from_verified_oidc,
)


def test_identity_is_immutable_and_rejects_blank_or_unknown_values():
    identity = Identity("lab-a", "user:alice", frozenset({"runs:read"}))

    with pytest.raises((AttributeError, TypeError)):
        identity.lab_id = "lab-b"
    with pytest.raises(IdentityError):
        Identity("", "user:alice", frozenset({"runs:read"}))
    with pytest.raises(IdentityError):
        Identity("lab-a", " ", frozenset({"runs:read"}))
    with pytest.raises(IdentityError):
        Identity("lab-a", "user:alice", frozenset({"unknown:scope"}))
    with pytest.raises(IdentityError):
        Identity("lab-a", "user:alice", frozenset({""}))


def test_role_scope_matrix_is_exact():
    assert ROLE_SCOPES == {
        "owner": frozenset(
            {"runs:read", "runs:write", "runs:approve", "artifacts:read", "lab:admin"}
        ),
        "researcher": frozenset({"runs:read", "runs:write", "artifacts:read"}),
        "viewer": frozenset({"runs:read", "artifacts:read"}),
    }


def test_verified_oidc_uses_database_membership_for_lab_and_role():
    identity = identity_from_verified_oidc(
        "alice-sub",
        {"subject": "alice-sub", "lab_id": "lab-from-db", "role": "researcher"},
    )

    assert identity == Identity(
        "lab-from-db", "user:alice-sub", frozenset(ROLE_SCOPES["researcher"])
    )


@pytest.mark.parametrize(
    "membership",
    [
        {"lab_id": "lab-a", "role": "admin"},
        {"lab_id": "lab-a"},
        {"role": "researcher"},
    ],
)
def test_verified_oidc_rejects_invalid_database_membership(membership):
    with pytest.raises(IdentityError):
        identity_from_verified_oidc("alice-sub", membership)


def test_verified_mcp_requires_exact_audience_and_takes_lab_and_scopes_from_token():
    identity = identity_from_verified_mcp(
        {
            "aud": "mcp.scilab",
            "client_id": "agent-1",
            "lab_id": "lab-a",
            "scopes": ["runs:read"],
        }
    )

    assert identity == Identity("lab-a", "client:agent-1", frozenset({"runs:read"}))

    with pytest.raises(AuthenticationError):
        identity_from_verified_mcp(
            {
                "aud": "other",
                "client_id": "agent-1",
                "lab_id": "lab-a",
                "scopes": ["runs:read"],
            }
        )
    with pytest.raises(AuthenticationError):
        identity_from_verified_mcp(
            {"client_id": "agent-1", "lab_id": "lab-a", "scopes": ["runs:read"]}
        )
    with pytest.raises(AuthenticationError):
        identity_from_verified_mcp(
            {"aud": "mcp.scilab", "client_id": "agent-1", "scopes": ["runs:read"]}
        )
    with pytest.raises(AuthenticationError):
        identity_from_verified_mcp(
            {
                "aud": "mcp.scilab",
                "client_id": "agent-1",
                "lab_id": "lab-a",
                "scopes": [],
            }
        )


def test_api_key_authentication_hashes_candidate_and_uses_constant_time_compare(monkeypatch):
    candidate = bytes(range(32))
    expected_hash = hashlib.sha256(candidate).hexdigest()
    expected_digest = hashlib.sha256(candidate).digest()
    comparisons = []
    original_compare = hmac.compare_digest

    def record_compare(left, right):
        comparisons.append((left, right))
        return original_compare(left, right)

    monkeypatch.setattr("scilab.identity.hmac.compare_digest", record_compare)
    identity = authenticate_api_key(
        candidate,
        [
            {
                "key_id": "key-1",
                "lab_id": "lab-a",
                "scopes": ["runs:read"],
                "secret_hash": expected_hash,
            }
        ],
    )

    assert identity == Identity("lab-a", "key:key-1", frozenset({"runs:read"}))
    assert comparisons == [(expected_digest, expected_digest)]
    with pytest.raises(AuthenticationError):
        authenticate_api_key(bytes(reversed(range(32))), [])


def test_a2a_authentication_returns_peer_identity_without_plaintext_record():
    candidate = bytes(range(31, -1, -1))
    identity = authenticate_a2a_peer(
        candidate,
        [
            {
                "peer_name": "hermes",
                "lab_id": "lab-a",
                "scopes": ["runs:read", "artifacts:read"],
                "secret_hash": hashlib.sha256(candidate).hexdigest(),
            }
        ],
    )

    assert identity == Identity(
        "lab-a", "peer:hermes", frozenset({"runs:read", "artifacts:read"})
    )
    with pytest.raises(AuthenticationError):
        authenticate_a2a_peer(candidate, [{"peer_name": "hermes", "lab_id": "lab-a"}])


def test_credential_with_unknown_scope_is_rejected():
    candidate = bytes(range(16))
    record = {
        "key_id": "key-1",
        "lab_id": "lab-a",
        "scopes": ["not-a-scope"],
        "secret_hash": hashlib.sha256(candidate).hexdigest(),
    }

    with pytest.raises(IdentityError):
        authenticate_api_key(candidate, [record])


@pytest.mark.parametrize(
    "membership",
    [
        {"lab_id": "lab-a", "role": "researcher"},
        {"subject": "other-sub", "lab_id": "lab-a", "role": "researcher"},
    ],
)
def test_verified_oidc_requires_subject_bound_membership(membership):
    with pytest.raises(IdentityError):
        identity_from_verified_oidc("alice-sub", membership)


@pytest.mark.parametrize(
    "token",
    [
        {"aud": "mcp.scilab", "lab_id": "lab-a", "scopes": ["runs:read"]},
        {
            "aud": "mcp.scilab",
            "client_id": "",
            "lab_id": "lab-a",
            "scopes": ["runs:read"],
        },
    ],
)
def test_verified_mcp_requires_client_id_from_verified_token(token):
    with pytest.raises(AuthenticationError):
        identity_from_verified_mcp(token)


def test_verified_mcp_has_no_untrusted_parallel_client_id_input():
    with pytest.raises(TypeError):
        identity_from_verified_mcp(
            {
                "aud": "mcp.scilab",
                "client_id": "verified-agent",
                "lab_id": "lab-a",
                "scopes": ["runs:read"],
            },
            client_id="untrusted-agent",
        )


@pytest.mark.parametrize(
    "scopes",
    [
        {},
        {"lab:admin": False},
        "runs:read",
        b"runs:read",
        [],
        [""],
        [1],
        ["not-a-scope"],
    ],
)
def test_scopes_reject_mappings_empty_values_and_malformed_items(scopes):
    with pytest.raises(IdentityError):
        Identity("lab-a", "user:alice", scopes)


@pytest.mark.parametrize(
    "authenticate",
    [authenticate_api_key, authenticate_a2a_peer],
)
def test_api_and_a2a_reject_malformed_scopes(authenticate):
    candidate = b"credential"
    identifier = "key-1" if authenticate is authenticate_api_key else "peer-1"
    field = "key_id" if authenticate is authenticate_api_key else "peer_name"
    with pytest.raises(AuthenticationError):
        authenticate(
            candidate,
            [
                {
                    field: identifier,
                    "lab_id": "lab-a",
                    "scopes": {"lab:admin": False},
                    "secret_hash": hashlib.sha256(candidate).digest(),
                }
            ],
        )


@pytest.mark.parametrize(
    "stored_hash", [b"short", b"x" * 33, "0" * 63, "g" * 64, "é" * 64]
)
def test_malformed_stored_hash_is_controlled_authentication_failure(stored_hash):
    candidate = b"credential"
    with pytest.raises(AuthenticationError):
        authenticate_api_key(
            candidate,
            [
                {
                    "key_id": "key-1",
                    "lab_id": "lab-a",
                    "scopes": ["runs:read"],
                    "secret_hash": stored_hash,
                }
            ],
        )


def test_valid_hash_with_malformed_identity_is_controlled_authentication_failure():
    candidate = b"credential"
    with pytest.raises(AuthenticationError):
        authenticate_api_key(
            candidate,
            [
                {
                    "key_id": "",
                    "lab_id": "lab-a",
                    "scopes": ["runs:read"],
                    "secret_hash": hashlib.sha256(candidate).digest(),
                }
            ],
        )
