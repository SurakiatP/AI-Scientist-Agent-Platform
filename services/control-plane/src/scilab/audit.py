from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from uuid import uuid4

from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.redaction import redact as redact_sensitive
from scilab.tenancy import AuthorizationError, require_scope


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-blank string")
    return value


@dataclass(frozen=True, slots=True)
class AuditRecord:
    audit_id: str
    run_id: str
    lab_id: str
    actor: str
    source: str
    action: str
    details: dict[str, Any]
    created_at: datetime


class AuditService:
    _COLUMNS = ("audit_id", "run_id", "lab_id", "actor", "source", "action", "details", "created_at")

    def __init__(
        self,
        connection: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.connection = connection
        self.clock = clock

    @contextmanager
    def _access(self, identity: Identity, scope: str):
        require_scope(identity, scope)
        with self.connection.transaction():
            with self.connection.cursor() as cursor:
                cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                yield cursor

    def append(
        self,
        identity: Identity,
        run_id: str,
        actor: str,
        source: str,
        action: str,
        details: Mapping[str, Any],
    ) -> AuditRecord:
        with self._access(identity, "runs:write") as cursor:
            return self.append_with_cursor(
                cursor, identity, run_id, actor, source, action, details
            )

    def append_with_cursor(
        self,
        cursor: Any,
        identity: Identity,
        run_id: str,
        actor: str,
        source: str,
        action: str,
        details: Mapping[str, Any],
    ) -> AuditRecord:
        if "runs:write" not in identity.scopes and "runs:approve" not in identity.scopes:
            raise AuthorizationError("missing scope: runs:write or runs:approve")
        _text(run_id, "run_id")
        _text(actor, "actor")
        _text(source, "source")
        _text(action, "action")
        if not isinstance(details, Mapping):
            raise TypeError("details must be a mapping")
        if "args" in details or "raw_args" in details or "raw_tool_args" in details:
            raise ValueError("raw tool args are not accepted; use args_redacted")

        audit_id = uuid4().hex
        created_at = self.clock()
        safe_details = redact_sensitive(dict(details))
        cursor.execute(
            "INSERT INTO audit_events "
            "(audit_id, run_id, lab_id, actor, source, action, details, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                audit_id,
                run_id,
                identity.lab_id,
                actor,
                source,
                action,
                json.dumps(safe_details, separators=(",", ":"), ensure_ascii=False),
                created_at,
            ),
        )
        return AuditRecord(audit_id, run_id, identity.lab_id, actor, source, action, safe_details, created_at)

    def query(
        self,
        identity: Identity,
        *,
        run_id: str | None = None,
        actor: str | None = None,
        source: str | None = None,
    ) -> list[AuditRecord]:
        filters = ["lab_id = %s"]
        params: list[Any] = [identity.lab_id]
        for column, value in (("run_id", run_id), ("actor", actor), ("source", source)):
            if value is not None:
                filters.append(f"{column} = %s")
                params.append(_text(value, column))
        with self._access(identity, "runs:read") as cursor:
            cursor.execute(
                "SELECT audit_id, run_id, lab_id, actor, source, action, details, created_at "
                "FROM audit_events WHERE "
                + " AND ".join(filters)
                + " ORDER BY created_at ASC, audit_id ASC",
                tuple(params),
            )
            return [self._from_row(row) for row in cursor.fetchall()]

    @classmethod
    def _from_row(cls, row: Mapping[str, Any] | tuple[Any, ...]) -> AuditRecord:
        values = dict(row) if isinstance(row, Mapping) else dict(zip(cls._COLUMNS, row, strict=True))
        details = values["details"]
        if isinstance(details, (str, bytes, bytearray)):
            details = json.loads(details)
        return AuditRecord(
            values["audit_id"],
            values["run_id"],
            values["lab_id"],
            values["actor"],
            values["source"],
            values["action"],
            dict(details),
            values["created_at"],
        )
