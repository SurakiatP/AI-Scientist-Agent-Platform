from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal
from typing import Any

import pytest

from scilab.api.lab_admin import LabAdminService, LastOwnerError
from scilab.identity import Identity


class Cursor:
    def __init__(self, db: "Connection") -> None:
        self.db = db
        self.row: Any = None

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        self.db.queries.append(sql)
        if sql.startswith("SELECT set_config"):
            self.db.tenant = params[1]
        elif "SELECT id FROM labs" in sql:
            self.row = (self.db.tenant,)
        elif "SELECT role FROM lab_memberships" in sql:
            self.row = (self.db.members.get(params[1]),) if params[1] in self.db.members else None
        elif "SELECT 1 FROM lab_memberships" in sql and "role = 'owner'" not in sql:
            self.row = (1,) if params[1] in self.db.members else None
        elif "SELECT 1 FROM lab_memberships" in sql and "role = 'owner'" in sql:
            self.row = (1,) if any(subject != params[1] and role == "owner" for subject, role in self.db.members.items()) else None
        elif sql.startswith("DELETE FROM lab_memberships"):
            del self.db.members[params[1]]
        elif sql.startswith("UPDATE lab_memberships"):
            self.db.members[params[2]] = params[0]
        elif sql.startswith("INSERT INTO lab_memberships"):
            self.db.members[params[1]] = params[2]
        elif sql.startswith("SELECT budget_thb"):
            self.row = (self.db.budget,) if self.db.budget is not None else None
        elif sql.startswith("DELETE FROM lab_budgets"):
            self.db.budget = None
        elif sql.startswith("INSERT INTO lab_budgets"):
            self.db.budget = params[1]
        else:
            raise AssertionError(sql)

    def fetchone(self) -> Any:
        return self.row


class Connection:
    def __init__(self) -> None:
        self.tenant = ""
        self.queries: list[str] = []
        self.members = {"owner-1": "owner"}
        self.budget: Decimal | None = None

    @contextmanager
    def transaction(self):
        yield

    def cursor(self) -> Cursor:
        return Cursor(self)

def test_each_lab_mutation_locks_before_owner_check() -> None:
    db = Connection()
    service = LabAdminService(db)
    owner = Identity("lab-a", "user:owner-1", frozenset({"lab:admin"}))
    operations = (
        lambda: service.add_member(owner, "researcher-1", "researcher"),
        lambda: service.set_budget(owner, Decimal("10")),
        lambda: service.change_member_role(owner, "researcher-1", "viewer"),
        lambda: service.remove_member(owner, "researcher-1"),
    )
    for operation in operations:
        db.queries.clear()
        operation()
        lock = next(i for i, sql in enumerate(db.queries) if "SELECT id FROM labs" in sql and "FOR UPDATE" in sql)
        owner_check = next(i for i, sql in enumerate(db.queries) if "SELECT role FROM lab_memberships" in sql)
        assert lock < owner_check


def test_final_owner_cannot_be_removed_or_demoted() -> None:
    db = Connection()
    service = LabAdminService(db)
    owner = Identity("lab-a", "user:owner-1", frozenset({"lab:admin"}))
    with pytest.raises(LastOwnerError):
        service.remove_member(owner, "owner-1")
    with pytest.raises(LastOwnerError):
        service.change_member_role(owner, "owner-1", "viewer")
    assert db.members == {"owner-1": "owner"}
    assert db.tenant == "lab-a"
    db.members["owner-2"] = "owner"
    service.remove_member(owner, "owner-2")
    assert db.members == {"owner-1": "owner"}


def test_budget_decimal_preserved_and_unconfigured_is_null() -> None:
    db = Connection()
    service = LabAdminService(db)
    owner = Identity("lab-a", "user:owner-1", frozenset({"lab:admin"}))
    assert service.get_budget(owner) is None
    value = Decimal("123456789012345.6789")
    service.set_budget(owner, value)
    assert service.get_budget(owner) == value
    service.set_budget(owner, None)
    assert service.get_budget(owner) is None
