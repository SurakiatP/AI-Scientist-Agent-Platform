from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

import pytest
from fastapi import HTTPException

from scilab.api.admission import AdmissionConfigurationError, PostgresRunAdmission
from scilab.api.run_adapters import RunAdmissionAdapter
from scilab.identity import Identity


_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_admission_limit_requires_explicit_positive_deployment_configuration() -> None:
    with pytest.raises(AdmissionConfigurationError):
        PostgresRunAdmission.from_environment(object(), {})

    for value in ("", "0", "-1", "1.5", "unlimited"):
        with pytest.raises(AdmissionConfigurationError):
            PostgresRunAdmission.from_environment(
                object(), {"SCILAB_RUN_ADMISSION_PER_MINUTE": value}
            )


def test_admission_limit_uses_deployment_value() -> None:
    admission = PostgresRunAdmission.from_environment(
        object(), {"SCILAB_RUN_ADMISSION_PER_MINUTE": "7"}
    )

    assert admission.requests_per_minute == 7


class _Database:
    def __init__(self) -> None:
        self.attempts: dict[tuple[str, str], datetime] = {}
        self.runs: set[tuple[str, str]] = set()
        self.current_lab: str | None = None
        self.now = _NOW
        self.guard = Lock()
        self.lab_locks: dict[str, Lock] = {}

    def connection(self) -> _Connection:
        return _Connection(self)


class _Transaction(AbstractContextManager["_Transaction"]):
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.lock: Lock | None = None
        self.previous_lab: str | None = None

    def __enter__(self) -> _Transaction:
        self.previous_lab = self.connection.database.current_lab
        self.connection.active_transaction = self
        return self

    def __exit__(self, *_: object) -> bool:
        self.connection.database.current_lab = self.previous_lab
        self.connection.active_transaction = None
        if self.lock is not None:
            self.lock.release()
        return False


class _Connection:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.active_transaction: _Transaction | None = None

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _Cursor(AbstractContextManager["_Cursor"]):
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.result: tuple[Any, ...] | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        query = " ".join(sql.split()).lower()
        database = self.connection.database
        self.result = None
        if query.startswith("select set_config"):
            database.current_lab = params[1]
        elif "pg_advisory_xact_lock" in query:
            self._require_tenant(params[0])
            lab_id = params[0]
            with database.guard:
                lock = database.lab_locks.setdefault(lab_id, Lock())
            lock.acquire()
            assert self.connection.active_transaction is not None
            self.connection.active_transaction.lock = lock
        elif query.startswith("select 1 from runs"):
            self._require_tenant(params[0])
            self.result = (1,) if (params[0], params[1]) in database.runs else None
        elif query.startswith("delete from run_admission_attempts") and "admitted_at" in query:
            self._require_tenant(params[0])
            cutoff = database.now - timedelta(minutes=1)
            with database.guard:
                database.attempts = {
                    key: at for key, at in database.attempts.items()
                    if key[0] != params[0] or at > cutoff
                }
        elif query.startswith("insert into run_admission_attempts"):
            self._require_tenant(params[0])
            key = (params[0], params[1])
            with database.guard:
                if key not in database.attempts:
                    database.attempts[key] = database.now
                    self.result = (params[1],)
        elif query.startswith("select count(*) from run_admission_attempts"):
            self._require_tenant(params[0])
            cutoff = database.now - timedelta(minutes=1)
            with database.guard:
                count = sum(
                    lab_id == params[0] and at > cutoff
                    for (lab_id, _), at in database.attempts.items()
                )
            self.result = (count,)
        elif query.startswith("delete from run_admission_attempts"):
            self._require_tenant(params[0])
            with database.guard:
                database.attempts.pop((params[0], params[1]), None)
        else:
            raise AssertionError(f"unexpected SQL: {query}")

    def _require_tenant(self, lab_id: str) -> None:
        if self.connection.database.current_lab != lab_id:
            raise AssertionError("query did not set its Lab RLS context")

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.result


def _identity(lab_id: str = "lab-a") -> Identity:
    return Identity(lab_id, "user:alice", frozenset({"runs:write"}))


def _admission(database: _Database, limit: int) -> RunAdmissionAdapter:
    policy = PostgresRunAdmission(
        database.connection(), requests_per_minute=limit
    )
    return RunAdmissionAdapter(policy.admit)


def test_rate_limit_denies_excess_requests_and_isolated_by_lab() -> None:
    database = _Database()
    lab_a = _admission(database, 1)
    lab_b = _admission(database, 1)

    assert lab_a.check(_identity(), {}, "first")
    with pytest.raises(HTTPException) as denied:
        lab_a.check(_identity(), {}, "second")
    assert denied.value.status_code == 429

    database.now += timedelta(minutes=1)
    assert lab_a.check(_identity(), {}, "third")
    assert lab_b.check(_identity("lab-b"), {}, "first")
    assert len(database.attempts) == 2


def test_idempotent_retry_does_not_consume_another_admission() -> None:
    database = _Database()
    admission = _admission(database, 1)

    assert admission.check(_identity(), {"goal": "first"}, "same-key")
    assert admission.check(_identity(), {"goal": "retry"}, "same-key")
    with pytest.raises(HTTPException) as denied:
        admission.check(_identity(), {}, "new-key")
    assert denied.value.status_code == 429
    assert set(database.attempts) == {("lab-a", "same-key")}

    database.runs.add(("lab-a", "same-key"))
    database.now += timedelta(minutes=2)
    assert admission.check(_identity(), {}, "same-key")
    assert set(database.attempts) == {("lab-a", "same-key")}


def test_concurrent_distinct_submissions_respect_per_lab_limit() -> None:
    database = _Database()
    limit = 4

    def submit(index: int) -> bool:
        return _admission(database, limit).check(
            _identity(), {}, f"request-{index}"
        )

    def try_submit(index: int) -> bool:
        try:
            return submit(index)
        except HTTPException as exc:
            assert exc.status_code == 429
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(try_submit, range(24)))

    assert sum(results) == limit
    assert len(database.attempts) == limit
