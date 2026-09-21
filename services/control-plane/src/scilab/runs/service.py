from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.runs.model import Run, RunState, utc_now
from scilab.runs.state import RunStateError, apply_retry, apply_transition
from scilab.tenancy import require_scope


class RunNotFound(LookupError):
    """Raised for both missing and cross-Lab run identifiers."""


class RunService:
    _columns = (
        "id",
        "lab_id",
        "idempotency_key",
        "state",
        "reason",
        "retry_count",
        "max_minutes",
        "hermes_run_id",
        "created_at",
        "updated_at",
        "queued_at",
        "running_since",
        "runtime_used",
        "last_heartbeat_at",
        "approval_expires_at",
    )
    _select = ", ".join(_columns)

    def __init__(self, connection: Any, *, clock: Callable[[], datetime] = utc_now) -> None:
        self.connection = connection
        self.clock = clock

    @contextmanager
    def _access(self, identity: Identity, scope: str):
        require_scope(identity, scope)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                yield cursor

    @classmethod
    def _from_row(cls, row: Mapping[str, Any] | tuple[Any, ...]) -> Run:
        values = row if isinstance(row, Mapping) else dict(zip(cls._columns, row))
        return Run(**{column: values[column] for column in cls._columns})

    @staticmethod
    def _require_key(idempotency_key: str) -> None:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-blank")

    def create(
        self,
        identity: Identity,
        idempotency_key: str,
        *,
        max_minutes: int = 120,
    ) -> Run:
        self._require_key(idempotency_key)
        if max_minutes <= 0:
            raise ValueError("max_minutes must be positive")
        now = self.clock()
        run_id = str(uuid4())
        with self._access(identity, "runs:write") as cursor:
            cursor.execute(
                """
                INSERT INTO runs (
                    id, lab_id, idempotency_key, state, reason, retry_count,
                    max_minutes, hermes_run_id, created_at, updated_at, queued_at,
                    running_since, runtime_used, last_heartbeat_at, approval_expires_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (lab_id, idempotency_key) DO NOTHING
                """,
                (
                    run_id,
                    identity.lab_id,
                    idempotency_key,
                    RunState.QUEUED.value,
                    None,
                    0,
                    max_minutes,
                    None,
                    now,
                    now,
                    now,
                    None,
                    timedelta(0),
                    None,
                    None,
                ),
            )
            cursor.execute(
                f"SELECT {self._select} FROM runs "
                "WHERE lab_id = %s AND idempotency_key = %s",
                (identity.lab_id, idempotency_key),
            )
            row = cursor.fetchone()
            if row is None:
                raise RunNotFound("run not found")
            return self._from_row(row)

    def _mutate(
        self,
        identity: Identity,
        run_id: str,
        operation: Callable[[Run, datetime], Run],
    ) -> Run:
        now = self.clock()
        with self._access(identity, "runs:write") as cursor:
            cursor.execute(
                f"SELECT {self._select} FROM runs "
                "WHERE lab_id = %s AND id = %s FOR UPDATE",
                (identity.lab_id, run_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise RunNotFound("run not found")
            updated = operation(self._from_row(row), now)
            values = tuple(getattr(updated, column) for column in self._columns[3:])
            cursor.execute(
                """
                UPDATE runs SET
                    state = %s, reason = %s, retry_count = %s, max_minutes = %s,
                    hermes_run_id = %s, created_at = %s, updated_at = %s,
                    queued_at = %s, running_since = %s, runtime_used = %s,
                    last_heartbeat_at = %s, approval_expires_at = %s
                WHERE lab_id = %s AND id = %s
                """,
                values + (identity.lab_id, run_id),
            )
            return updated

    def transition(
        self,
        identity: Identity,
        run_id: str,
        target: RunState,
        *,
        reason: str | None = None,
        hermes_run_id: str | None = None,
        completion_ready: bool = False,
    ) -> Run:
        try:
            target = RunState(target)
        except ValueError as exc:
            raise RunStateError(f"unknown run state: {target!r}") from exc
        return self._mutate(
            identity,
            run_id,
            lambda run, now: apply_transition(
                run,
                target,
                now,
                reason=reason,
                hermes_run_id=hermes_run_id,
                completion_ready=completion_ready,
            ),
        )

    def stop(self, identity: Identity, run_id: str) -> Run:
        return self.transition(identity, run_id, RunState.CANCELLED, reason="stopped")

    def retry(self, identity: Identity, run_id: str) -> Run:
        return self._mutate(identity, run_id, apply_retry)

    def get(self, identity: Identity, run_id: str) -> Run:
        with self._access(identity, "runs:read") as cursor:
            cursor.execute(
                f"SELECT {self._select} FROM runs WHERE lab_id = %s AND id = %s",
                (identity.lab_id, run_id),
            )
            row = cursor.fetchone()
            if row is None:
                raise RunNotFound("run not found")
            return self._from_row(row)

    def list(self, identity: Identity) -> list[Run]:
        with self._access(identity, "runs:read") as cursor:
            cursor.execute(
                f"SELECT {self._select} FROM runs "
                "WHERE lab_id = %s ORDER BY created_at DESC, id DESC",
                (identity.lab_id,),
            )
            return [self._from_row(row) for row in cursor.fetchall()]
