from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from scilab.identity import Identity, credential_digest
from scilab.tenancy import require_lab


TENANT_SETTING = "scilab.current_lab_id"
SET_TENANT_SQL = "SELECT set_config(%s, %s, true)"
BOOTSTRAP_SUBJECT_SETTING = "scilab.bootstrap_subject"
BOOTSTRAP_LAB_SETTING = "scilab.bootstrap_lab_id"
BOOTSTRAP_DIGEST_SETTING = "scilab.bootstrap_secret_digest"


def execute_in_lab(
    connection: Any,
    identity: Identity,
    lab_id: str,
    sql: str,
    params: Sequence[Any] = (),
) -> list[tuple[Any, ...]]:
    require_lab(identity, lab_id)
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, lab_id))
            cursor.execute(sql, params)
            return cursor.fetchall()


def _bootstrap_fetch_one(
    connection: Any,
    settings: Sequence[tuple[str, str]],
    sql: str,
    params: Sequence[Any],
    columns: tuple[str, ...],
) -> Mapping[str, Any] | None:
    with connection.transaction():
        with connection.cursor() as cursor:
            for setting, value in settings:
                cursor.execute(SET_TENANT_SQL, (setting, value))
            cursor.execute(sql, params)
            row = cursor.fetchone()
            if row is None:
                return None
            if isinstance(row, Mapping):
                return row
            return dict(zip(columns, row))


def load_membership(
    connection: Any, subject: str, lab_id: str
) -> Mapping[str, Any] | None:
    return _bootstrap_fetch_one(
        connection,
        (
            (BOOTSTRAP_SUBJECT_SETTING, subject),
            (BOOTSTRAP_LAB_SETTING, lab_id),
        ),
        """
        SELECT subject, lab_id, role
        FROM lab_memberships
        WHERE subject = %s AND lab_id = %s
        LIMIT 1
        """,
        (subject, lab_id),
        ("subject", "lab_id", "role"),
    )


def load_api_key_credential(
    connection: Any, secret: str | bytes
) -> Mapping[str, Any] | None:
    digest = credential_digest(secret)
    return _bootstrap_fetch_one(
        connection,
        ((BOOTSTRAP_DIGEST_SETTING, digest.hex()),),
        """
        SELECT key_id, lab_id, scopes, secret_hash
        FROM api_credentials
        WHERE secret_hash = %s
        LIMIT 1
        """,
        (digest,),
        ("key_id", "lab_id", "scopes", "secret_hash"),
    )


def load_a2a_peer_credential(
    connection: Any, secret: str | bytes
) -> Mapping[str, Any] | None:
    digest = credential_digest(secret)
    return _bootstrap_fetch_one(
        connection,
        ((BOOTSTRAP_DIGEST_SETTING, digest.hex()),),
        """
        SELECT peer_name, lab_id, scopes, secret_hash
        FROM a2a_peers
        WHERE secret_hash = %s
        LIMIT 1
        """,
        (digest,),
        ("peer_name", "lab_id", "scopes", "secret_hash"),
    )
