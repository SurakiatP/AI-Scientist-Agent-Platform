"""Tenant-scoped Lab membership and budget administration."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.tenancy import AuthorizationError, require_scope


class LastOwnerError(ValueError):
    """A Lab must always have at least one owner."""


class MemberConflictError(ValueError):
    """The requested membership already exists."""


class LabAdminService:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def _cursor(self, identity: Identity):
        require_scope(identity, "lab:admin")
        return self.connection.transaction()

    @staticmethod
    def _human(identity: Identity) -> str:
        if not identity.principal.startswith("user:") or not identity.principal[5:]:
            raise AuthorizationError("Lab mutation requires a human owner")
        return identity.principal[5:]

    @staticmethod
    def _tenant(cursor: Any, identity: Identity) -> None:
        cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))

    @staticmethod
    def _owner(cursor: Any, identity: Identity) -> None:
        subject = LabAdminService._human(identity)
        cursor.execute("SELECT id FROM labs WHERE id = %s FOR UPDATE", (identity.lab_id,))
        cursor.execute(
            "SELECT role FROM lab_memberships WHERE lab_id = %s AND subject = %s",
            (identity.lab_id, subject),
        )
        row = cursor.fetchone()
        role = row["role"] if isinstance(row, dict) else row[0] if row else None
        if role != "owner":
            raise AuthorizationError("Lab mutation requires owner membership")

    @staticmethod
    def _member(subject: str, role: str) -> dict[str, str]:
        return {"subject": subject, "role": role}

    def list_members(self, identity: Identity) -> list[dict[str, str]]:
        with self._cursor(identity):
            with self.connection.cursor() as cursor:
                self._tenant(cursor, identity)
                cursor.execute("SELECT subject, role FROM lab_memberships WHERE lab_id = %s ORDER BY subject", (identity.lab_id,))
                return [self._member(row["subject"], row["role"]) if isinstance(row, dict) else self._member(row[0], row[1]) for row in cursor.fetchall()]

    def add_member(self, identity: Identity, subject: str, role: str) -> dict[str, str]:
        subject = subject.strip()
        if not subject or role not in {"owner", "researcher", "viewer"}:
            raise ValueError("invalid subject or role")
        with self._cursor(identity):
            with self.connection.cursor() as cursor:
                self._tenant(cursor, identity)
                self._owner(cursor, identity)
                cursor.execute("SELECT 1 FROM lab_memberships WHERE lab_id = %s AND subject = %s", (identity.lab_id, subject))
                if cursor.fetchone():
                    raise MemberConflictError("member already exists")
                cursor.execute("INSERT INTO lab_memberships (lab_id, subject, role) VALUES (%s, %s, %s)", (identity.lab_id, subject, role))
        return self._member(subject, role)

    def change_member_role(self, identity: Identity, subject: str, role: str) -> dict[str, str]:
        if not subject.strip() or role not in {"owner", "researcher", "viewer"}:
            raise ValueError("invalid subject or role")
        with self._cursor(identity):
            with self.connection.cursor() as cursor:
                self._tenant(cursor, identity)
                self._owner(cursor, identity)
                cursor.execute("SELECT role FROM lab_memberships WHERE lab_id = %s AND subject = %s", (identity.lab_id, subject))
                row = cursor.fetchone()
                if not row:
                    raise LookupError("member not found")
                old_role = row["role"] if isinstance(row, dict) else row[0]
                if old_role == "owner" and role != "owner":
                    self._require_another_owner(cursor, identity, subject)
                cursor.execute("UPDATE lab_memberships SET role = %s WHERE lab_id = %s AND subject = %s", (role, identity.lab_id, subject))
        return self._member(subject, role)

    def remove_member(self, identity: Identity, subject: str) -> None:
        if not subject.strip():
            raise ValueError("invalid subject")
        with self._cursor(identity):
            with self.connection.cursor() as cursor:
                self._tenant(cursor, identity)
                self._owner(cursor, identity)
                cursor.execute("SELECT role FROM lab_memberships WHERE lab_id = %s AND subject = %s", (identity.lab_id, subject))
                row = cursor.fetchone()
                if not row:
                    raise LookupError("member not found")
                role = row["role"] if isinstance(row, dict) else row[0]
                if role == "owner":
                    self._require_another_owner(cursor, identity, subject)
                cursor.execute("DELETE FROM lab_memberships WHERE lab_id = %s AND subject = %s", (identity.lab_id, subject))

    @staticmethod
    def _require_another_owner(cursor: Any, identity: Identity, subject: str) -> None:
        cursor.execute("SELECT 1 FROM lab_memberships WHERE lab_id = %s AND role = 'owner' AND subject <> %s LIMIT 1", (identity.lab_id, subject))
        if cursor.fetchone() is None:
            raise LastOwnerError("cannot remove final Lab owner")

    def get_budget(self, identity: Identity) -> Decimal | None:
        with self._cursor(identity):
            with self.connection.cursor() as cursor:
                self._tenant(cursor, identity)
                cursor.execute("SELECT budget_thb FROM lab_budgets WHERE lab_id = %s", (identity.lab_id,))
                row = cursor.fetchone()
                return (row["budget_thb"] if isinstance(row, dict) else row[0]) if row else None

    def set_budget(self, identity: Identity, budget_thb: Decimal | None) -> None:
        if budget_thb is not None and (not budget_thb.is_finite() or budget_thb < 0):
            raise ValueError("budget must be finite and non-negative")
        with self._cursor(identity):
            with self.connection.cursor() as cursor:
                self._tenant(cursor, identity)
                self._owner(cursor, identity)
                if budget_thb is None:
                    cursor.execute("DELETE FROM lab_budgets WHERE lab_id = %s", (identity.lab_id,))
                else:
                    cursor.execute(
                        "INSERT INTO lab_budgets (lab_id, budget_thb, configured_at) VALUES (%s, %s, %s) "
                        "ON CONFLICT (lab_id) DO UPDATE SET budget_thb = EXCLUDED.budget_thb, configured_at = EXCLUDED.configured_at",
                        (identity.lab_id, budget_thb, datetime.now(UTC)),
                    )
