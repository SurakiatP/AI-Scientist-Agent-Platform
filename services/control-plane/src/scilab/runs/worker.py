"""Tenant-scoped durable claims for queued REST Runs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.runs.model import Run, RunState
from scilab.runs.service import RunService
from scilab.tenancy import require_lab, require_scope

LEASE_SECONDS = 90
_CLAIM_COLUMNS = RunService._columns + (
    "request_payload",
    "worker_claim_token",
    "worker_lease_expires_at",
)
_RETURNING = ", ".join(f"r.{column}" for column in _CLAIM_COLUMNS)


@dataclass(frozen=True, slots=True)
class RunClaim:
    run: Run
    request_payload: Mapping[str, Any]
    token: UUID
    lease_expires_at: datetime


class RunWorker:
    """Claim queued Runs within the supplied identity's Lab boundary.

    Expired claims are reclaimed by ``claim_next`` with a new fencing token.
    Callers must renew active work before expiry and discard claims that can no
    longer be renewed. This class does not dispatch work to Hermes.
    """

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    @staticmethod
    def _access(cursor: Any, identity: Identity) -> None:
        require_scope(identity, "runs:write")
        cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))

    def claim_next(self, identity: Identity) -> RunClaim | None:
        require_scope(identity, "runs:write")
        token = uuid4()
        sql = f"""
            WITH candidate AS (
                SELECT lab_id, id
                FROM runs
                WHERE lab_id = %s
                  AND (
                      (state = 'queued' AND request_payload IS NOT NULL)
                      OR (state = 'running' AND hermes_run_id IS NOT NULL)
                  )
                  AND (
                        state = 'running'
                    OR COALESCE(request_payload -> 'options', '{{}}'::jsonb) = '{{}}'::jsonb
                  )
                  AND (
                        worker_lease_expires_at IS NULL
                      OR worker_lease_expires_at <= statement_timestamp()
                  )
                ORDER BY queued_at, id
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE runs AS r
            SET worker_claim_token = %s,
                worker_lease_expires_at = statement_timestamp()
                    + (%s * INTERVAL '1 second')
            FROM candidate
            WHERE r.lab_id = candidate.lab_id AND r.id = candidate.id
            RETURNING {_RETURNING}
        """
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                self._access(cursor, identity)
                cursor.execute(sql, (identity.lab_id, token, LEASE_SECONDS))
                row = cursor.fetchone()
                if row is None:
                    return None
                values = self._values(row)
                payload = values["request_payload"]
                if isinstance(payload, (str, bytes, bytearray)):
                    payload = json.loads(payload)
                if not isinstance(payload, Mapping):
                    raise RuntimeError("queued Run request_payload must be an object")
                return RunClaim(
                    run=RunService._from_row(values),
                    request_payload=dict(payload),
                    token=values["worker_claim_token"],
                    lease_expires_at=values["worker_lease_expires_at"],
                )

    def renew(self, identity: Identity, claim: RunClaim) -> datetime | None:
        self._authorize_claim(identity, claim)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                self._access(cursor, identity)
                cursor.execute(
                "UPDATE runs SET worker_lease_expires_at = "
                "statement_timestamp() + (%s * INTERVAL '1 second'), "
                "last_heartbeat_at = statement_timestamp() "
                "WHERE lab_id = %s AND id = %s AND worker_claim_token = %s "
                "AND state IN ('queued', 'running') "
                    "AND worker_lease_expires_at > statement_timestamp() "
                    "RETURNING worker_lease_expires_at",
                    (LEASE_SECONDS, claim.run.lab_id, claim.run.id, claim.token),
                )
                row = cursor.fetchone()
                return row["worker_lease_expires_at"] if row is not None else None

    def current(self, identity: Identity, claim: RunClaim) -> Run | None:
        self._authorize_claim(identity, claim)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                self._access(cursor, identity)
                cursor.execute(
                    f"SELECT {', '.join(_CLAIM_COLUMNS)} FROM runs "
                    "WHERE lab_id = %s AND id = %s AND worker_claim_token = %s "
                    "AND worker_lease_expires_at > statement_timestamp()",
                    (claim.run.lab_id, claim.run.id, claim.token),
                )
                row = cursor.fetchone()
                return RunService._from_row(self._values(row)) if row is not None else None

    def transition(
        self,
        identity: Identity,
        claim: RunClaim,
        target: RunState,
        *,
        reason: str | None = None,
        hermes_run_id: str | None = None,
        completion_ready: bool = False,
    ) -> Run | None:
        self._authorize_claim(identity, claim)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                self._access(cursor, identity)
                cursor.execute(
                    "SELECT id FROM runs "
                    "WHERE lab_id = %s AND id = %s AND worker_claim_token = %s "
                    "AND worker_lease_expires_at > statement_timestamp() FOR UPDATE",
                    (claim.run.lab_id, claim.run.id, claim.token),
                )
                if cursor.fetchone() is None:
                    return None
                return RunService(self.connection).transition_with_cursor(
                    cursor,
                    identity,
                    claim.run.id,
                    target,
                    reason=reason,
                    hermes_run_id=hermes_run_id,
                    completion_ready=completion_ready,
                )

    def release(self, identity: Identity, claim: RunClaim) -> bool:
        self._authorize_claim(identity, claim)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                self._access(cursor, identity)
                cursor.execute(
                    "UPDATE runs SET worker_claim_token = NULL, "
                    "worker_lease_expires_at = NULL "
                    "WHERE lab_id = %s AND id = %s AND worker_claim_token = %s "
                    "AND worker_lease_expires_at > statement_timestamp() "
                    "RETURNING id",
                    (claim.run.lab_id, claim.run.id, claim.token),
                )
                return cursor.fetchone() is not None

    @staticmethod
    def _authorize_claim(identity: Identity, claim: RunClaim) -> None:
        require_scope(identity, "runs:write")
        require_lab(identity, claim.run.lab_id)

    @staticmethod
    def _values(row: Mapping[str, Any] | tuple[Any, ...]) -> dict[str, Any]:
        if isinstance(row, Mapping):
            return dict(row)
        return dict(zip(_CLAIM_COLUMNS, row, strict=True))
