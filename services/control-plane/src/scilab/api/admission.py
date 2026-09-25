"""PostgreSQL-backed per-Lab Run admission."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.tenancy import require_scope


class AdmissionConfigurationError(ValueError):
    """Raised when the required deployment rate limit is absent or invalid."""


class PostgresRunAdmission:
    _limit_variable = "SCILAB_RUN_ADMISSION_PER_MINUTE"

    def __init__(self, connection: Any, *, requests_per_minute: int) -> None:
        if (
            isinstance(requests_per_minute, bool)
            or not isinstance(requests_per_minute, int)
            or requests_per_minute <= 0
        ):
            raise AdmissionConfigurationError(
                f"{self._limit_variable} must be a positive integer"
            )
        self.connection = connection
        self.requests_per_minute = requests_per_minute

    @classmethod
    def from_environment(
        cls, connection: Any, environ: Mapping[str, str] | None = None
    ) -> PostgresRunAdmission:
        value = (os.environ if environ is None else environ).get(cls._limit_variable)
        if (
            not isinstance(value, str)
            or not value.strip()
            or not value.strip().isascii()
            or not value.strip().isdecimal()
        ):
            raise AdmissionConfigurationError(
                f"{cls._limit_variable} must be configured as a positive integer"
            )
        return cls(connection, requests_per_minute=int(value.strip()))

    def admit(
        self, identity: Identity, payload: Mapping[str, Any], idempotency_key: str
    ) -> bool:
        require_scope(identity, "runs:write")
        if not isinstance(payload, Mapping):
            raise ValueError("Run payload must be an object")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("Idempotency-Key must be non-blank")

        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (identity.lab_id,),
                )
                cursor.execute(
                    "SELECT 1 FROM runs WHERE lab_id = %s AND idempotency_key = %s",
                    (identity.lab_id, idempotency_key),
                )
                if cursor.fetchone() is not None:
                    return True

                cursor.execute(
                    "DELETE FROM run_admission_attempts WHERE lab_id = %s "
                    "AND admitted_at <= clock_timestamp() - interval '1 minute'",
                    (identity.lab_id,),
                )
                cursor.execute(
                    "INSERT INTO run_admission_attempts "
                    "(lab_id, idempotency_key, admitted_at) "
                    "VALUES (%s, %s, clock_timestamp()) "
                    "ON CONFLICT (lab_id, idempotency_key) DO NOTHING "
                    "RETURNING idempotency_key",
                    (identity.lab_id, idempotency_key),
                )
                if cursor.fetchone() is None:
                    return True

                cursor.execute(
                    "SELECT count(*) FROM run_admission_attempts "
                    "WHERE lab_id = %s "
                    "AND admitted_at > clock_timestamp() - interval '1 minute'",
                    (identity.lab_id,),
                )
                if cursor.fetchone()[0] > self.requests_per_minute:
                    cursor.execute(
                        "DELETE FROM run_admission_attempts "
                        "WHERE lab_id = %s AND idempotency_key = %s",
                        (identity.lab_id, idempotency_key),
                    )
                    return False
        return True
