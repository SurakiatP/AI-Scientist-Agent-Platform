from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.runs.model import Run, RunState, _normalize_budget_thb, utc_now
from scilab.runs.state import RunStateError, apply_retry, apply_transition
from scilab.tenancy import require_scope


class RunNotFound(LookupError):
    """Raised for both missing and cross-Lab run identifiers."""


class RunIdempotencyConflict(ValueError):
    """Raised when a Lab reuses a Run key with a different request payload."""


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
        "context_id",
        "budget_thb",
    )
    _select = ", ".join(_columns)
    _search_columns = _columns + ("request_payload", "actor")

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
        context_id: str | None = None,
        budget_thb: float | None = None,
        request_payload: Mapping[str, Any] | None = None,
        actor: str | None = None,
    ) -> Run:
        self._require_key(idempotency_key)
        budget_thb = _normalize_budget_thb(budget_thb)
        request_json: str | None = None
        if request_payload is not None:
            if not isinstance(request_payload, Mapping):
                raise ValueError("request_payload must be a mapping")
            if not isinstance(actor, str) or not actor.strip():
                raise ValueError("actor must be non-blank with request_payload")
            try:
                request_json = json.dumps(
                    dict(request_payload),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("request_payload must contain JSON values") from exc
        if actor is not None and actor != identity.principal:
            raise ValueError("actor must match authenticated principal")
        actor = identity.principal
        if context_id is not None and (
            not isinstance(context_id, str) or not context_id.strip()
        ):
            raise ValueError("context_id must be non-blank when provided")
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
                    running_since, runtime_used, last_heartbeat_at,
                    approval_expires_at, context_id, budget_thb
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (lab_id, idempotency_key) DO NOTHING
                RETURNING id
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
                    context_id,
                    budget_thb,
                ),
            )
            inserted = cursor.fetchone() is not None
            if inserted:
                cursor.execute(
                    "UPDATE runs SET request_payload = %s::jsonb, actor = %s "
                    "WHERE lab_id = %s AND id = %s RETURNING id",
                    (request_json, actor, identity.lab_id, run_id),
                )
                if cursor.fetchone() is None:
                    raise RunNotFound("run not found")
            cursor.execute(
                f"SELECT {self._select} FROM runs "
                "WHERE lab_id = %s AND idempotency_key = %s",
                (identity.lab_id, idempotency_key),
            )
            row = cursor.fetchone()
            if row is None:
                raise RunNotFound("run not found")
            if not inserted:
                run = self._from_row(row)
                cursor.execute(
                    "SELECT request_payload, actor FROM runs "
                    "WHERE lab_id = %s AND id = %s",
                    (identity.lab_id, run.id),
                )
                stored_row = cursor.fetchone()
                stored_payload = (
                    stored_row.get("request_payload")
                    if isinstance(stored_row, Mapping)
                    else stored_row[0] if stored_row is not None else None
                )
                stored_actor = (
                    stored_row.get("actor")
                    if isinstance(stored_row, Mapping)
                    else stored_row[1] if stored_row is not None else None
                )
                try:
                    same_request = stored_payload is not None and json.dumps(
                        stored_payload,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ) == request_json
                except (TypeError, ValueError):
                    same_request = False
                if (request_json is not None and not same_request) or stored_actor != actor:
                    raise RunIdempotencyConflict(
                        "idempotency key already has a different request payload"
                    )
            return self._from_row(row)

    def _mutate(
        self,
        identity: Identity,
        run_id: str,
        operation: Callable[[Run, datetime], Run],
    ) -> Run:
        with self._access(identity, "runs:write") as cursor:
            return self._mutate_with_cursor(
                cursor, identity, run_id, operation, now=self.clock()
            )

    def _mutate_with_cursor(
        self,
        cursor: Any,
        identity: Identity,
        run_id: str,
        operation: Callable[[Run, datetime], Run],
        *,
        now: datetime,
    ) -> Run:
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
                last_heartbeat_at = %s, approval_expires_at = %s,
                context_id = %s, budget_thb = %s
            WHERE lab_id = %s AND id = %s
            """,
            values + (identity.lab_id, run_id),
        )
        return updated

    def transition_with_cursor(
        self,
        cursor: Any,
        identity: Identity,
        run_id: str,
        target: RunState,
        *,
        reason: str | None = None,
        hermes_run_id: str | None = None,
        completion_ready: bool = False,
    ) -> Run:
        require_scope(identity, "runs:write")
        try:
            target = RunState(target)
        except ValueError as exc:
            raise RunStateError(f"unknown run state: {target!r}") from exc
        return self._mutate_with_cursor(
            cursor,
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
            now=self.clock(),
        )

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
        def retry_supported(run: Run, now: datetime) -> Run:
            if run.reason == "unsupported_stored_options":
                raise RunStateError("Run with unsupported stored options cannot be retried")
            return apply_retry(run, now)

        return self._mutate(identity, run_id, retry_supported)

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

    def list_active(self, identity: Identity) -> list[Run]:
        """Queued and running Runs for the lease-timeout reconciliation loop."""
        with self._access(identity, "runs:read") as cursor:
            cursor.execute(
                f"SELECT {self._select} FROM runs "
                "WHERE lab_id = %s AND state IN (%s, %s) "
                "ORDER BY created_at DESC, id DESC",
                (identity.lab_id, RunState.QUEUED.value, RunState.RUNNING.value),
            )
            return [self._from_row(row) for row in cursor.fetchall()]

    def lease_expires_at(self, identity: Identity, run_id: str) -> datetime | None:
        """Claim lease expiry for one Run, or ``None`` if no worker holds it.

        Used by the reconcile loop (Q25) to gate ``heartbeat_loss`` on an
        actually expired worker claim instead of a merely stale heartbeat: an
        unclaimed Run (e.g. resumed after approval, waiting for the single
        per-Lab worker) must keep waiting rather than being failed.
        """
        with self._access(identity, "runs:read") as cursor:
            cursor.execute(
                "SELECT worker_claim_token, worker_lease_expires_at FROM runs "
                "WHERE lab_id = %s AND id = %s",
                (identity.lab_id, run_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            token, lease_expires_at = (
                (row["worker_claim_token"], row["worker_lease_expires_at"])
                if isinstance(row, Mapping)
                else (row[0], row[1])
            )
            return lease_expires_at if token is not None else None

    def search_submissions(
        self,
        identity: Identity,
        *,
        state: str | None = None,
        actor: str | None = None,
        since: datetime | None = None,
        after: tuple[datetime, str] | None = None,
        limit: int = 101,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        filters = ["lab_id = %s"]
        params: list[Any] = [identity.lab_id]
        if state is not None:
            filters.append("state = %s")
            params.append(state)
        if actor is not None:
            filters.append("actor = %s")
            params.append(actor)
        if since is not None:
            filters.append("created_at >= %s")
            params.append(since)
        if after is not None:
            filters.append("(created_at, id) < (%s, %s)")
            params.extend(after)
        params.append(limit)
        with self._access(identity, "runs:read") as cursor:
            cursor.execute(
                f"SELECT {', '.join(self._search_columns)} FROM runs WHERE "
                + " AND ".join(filters)
                + " ORDER BY created_at DESC, id DESC LIMIT %s",
                tuple(params),
            )
            rows = cursor.fetchall()
            return [
                dict(row)
                if isinstance(row, Mapping)
                else dict(zip(self._search_columns, row, strict=True))
                for row in rows
            ]
