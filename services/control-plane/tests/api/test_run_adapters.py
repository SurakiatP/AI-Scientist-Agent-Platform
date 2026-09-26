from __future__ import annotations

import base64
import copy
import json
from contextlib import AbstractContextManager
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError
from fastapi import HTTPException

from scilab.api.run_adapters import (
    RunAdmissionAdapter,
    RunSearchAdapter,
    RunSubmissionAdapter,
)
from scilab.api.rest import RunRequest
from scilab.identity import Identity
from scilab.runs.service import RunIdempotencyConflict, RunService
from scilab.tenancy import AuthorizationError


RUN_COLUMNS = (
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
SEARCH_COLUMNS = RUN_COLUMNS + ("request_payload", "actor")
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def identity(
    lab_id: str = "lab-a",
    principal: str = "user:alice",
    scopes: frozenset[str] = frozenset({"runs:read", "runs:write"}),
) -> Identity:
    return Identity(lab_id, principal, scopes)


class _Transaction(AbstractContextManager["_Transaction"]):
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> _Transaction:
        self.rows = copy.deepcopy(self.database.rows)
        return self

    def __exit__(self, exc_type: object, *_: object) -> bool:
        if exc_type is not None:
            self.database.rows = self.rows
        return False


class _Cursor(AbstractContextManager["_Cursor"]):
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.result: tuple[Any, ...] | None = None
        self.results: list[tuple[Any, ...]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        query = " ".join(sql.split()).lower()
        self.result = None
        self.results = []
        if query.startswith("select set_config"):
            self.database.current_lab = params[1]
        elif query.startswith("insert into runs"):
            row = dict(zip(RUN_COLUMNS, params, strict=True))
            duplicate = any(
                item["lab_id"] == row["lab_id"]
                and item["idempotency_key"] == row["idempotency_key"]
                for item in self.database.rows
            )
            if not duplicate:
                self.database.rows.append(row)
                if "returning id" in query:
                    self.result = (row["id"],)
        elif query.startswith("update runs set request_payload"):
            payload_json, actor, lab_id, run_id = params
            row = self.database.find(lab_id, run_id)
            assert row is not None
            row["request_payload"] = json.loads(payload_json) if payload_json is not None else None
            row["actor"] = actor
            if self.database.fail_request_update:
                raise RuntimeError("request write failed")
            if "returning id" in query:
                self.result = (run_id,)
        elif query.startswith("select request_payload, actor from runs"):
            row = self.database.find(params[0], params[1])
            self.result = None if row is None else (row.get("request_payload"), row.get("actor"))
        elif "idempotency_key = %s" in query:
            row = self.database.find_by_key(params[0], params[1])
            self.result = None if row is None else tuple(row[name] for name in RUN_COLUMNS)
        elif query.startswith("select") and "request_payload" in query:
            self._search(query, params)
        else:
            raise AssertionError(f"unexpected SQL: {query}")

    def _search(self, query: str, params: tuple[Any, ...]) -> None:
        lab_id = params[0]
        offset = 1
        rows = [row for row in self.database.rows if row["lab_id"] == lab_id]
        for clause, field in (("state = %s", "state"), ("actor = %s", "actor")):
            if clause in query:
                value = params[offset]
                offset += 1
                rows = [row for row in rows if row.get(field) == value]
        if "created_at >= %s" in query:
            since = params[offset]
            offset += 1
            rows = [row for row in rows if row["created_at"] >= since]
        if "(created_at, id) < (%s, %s)" in query:
            created_at, run_id = params[offset : offset + 2]
            offset += 2
            rows = [
                row
                for row in rows
                if (row["created_at"], row["id"]) < (created_at, run_id)
            ]
        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        limit = params[offset]
        self.results = [
            tuple(row.get(name) for name in SEARCH_COLUMNS) for row in rows[:limit]
        ]

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.result

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.results


class _Database:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.current_lab: str | None = None
        self.fail_request_update = False

    def transaction(self) -> _Transaction:
        return _Transaction(self)

    def cursor(self) -> _Cursor:
        return _Cursor(self)

    def find(self, lab_id: str, run_id: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.rows
                if row["lab_id"] == lab_id and row["id"] == run_id
            ),
            None,
        )

    def find_by_key(self, lab_id: str, key: str) -> dict[str, Any] | None:
        return next(
            (
                row
                for row in self.rows
                if row["lab_id"] == lab_id and row["idempotency_key"] == key
            ),
            None,
        )


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value


def _input_resolver(actor: Identity, input_ids: list[str] | tuple[str, ...]) -> list[str]:
    if any(value.startswith("lab-b:") and actor.lab_id != "lab-b" for value in input_ids):
        raise LookupError("input not found")
    return list(input_ids)


def _payload(goal: str = "Map evidence") -> dict[str, Any]:
    return {
        "goal": goal,
        "inputs": ["lab-a:input-1"],
        "skill_packs": ["general-research"],
        "budget": {"thb": 40.0, "max_minutes": 45},
        "options": {},
    }


def _adapters(database: _Database, clock: _Clock | None = None) -> tuple[Any, Any, Any]:
    runs = RunService(database, clock=clock or _Clock())
    admission = RunAdmissionAdapter(lambda _identity, _payload, _key: True)
    submission = RunSubmissionAdapter(runs, input_resolver=_input_resolver)
    search = RunSearchAdapter(runs)
    return admission, submission, search


def test_admission_allows_or_denies_and_enforces_run_write_scope() -> None:
    allow = RunAdmissionAdapter(lambda _identity, _payload, _key: True)
    deny = RunAdmissionAdapter(lambda _identity, _payload, _key: False)

    assert allow.check(identity(), _payload(), "request-1") is True
    with pytest.raises(HTTPException) as denied:
        deny.check(identity(), _payload(), "request-2")
    assert denied.value.status_code == 429
    with pytest.raises(AuthorizationError):
        allow.check(identity(scopes=frozenset({"runs:read"})), _payload(), "request-3")


def test_submission_persists_request_and_same_key_returns_original_after_restart() -> None:
    database = _Database()
    clock = _Clock()
    _, submit, _ = _adapters(database, clock)
    request = _payload()

    first = submit.create(identity(), "same-key", request)
    restarted = RunService(database, clock=clock)
    duplicate = RunSubmissionAdapter(restarted, input_resolver=_input_resolver).create(
        identity(), "same-key", request
    )
    result = RunSearchAdapter(restarted).list(identity())

    assert duplicate["id"] == first["id"]
    assert len(database.rows) == 1
    assert result["items"][0]["goal"] == "Map evidence"
    assert result["items"][0]["inputs"] == ["lab-a:input-1"]
    assert result["items"][0]["skill_packs"] == ["general-research"]
    assert result["items"][0]["budget"] == {"thb": 40.0, "max_minutes": 45}
    assert result["items"][0]["options"] == {}
    assert result["items"][0]["actor"] == "user:alice"


def test_submission_conflicts_when_idempotency_key_has_different_payload() -> None:
    database = _Database()
    _, submit, _ = _adapters(database)
    submit.create(identity(), "same-key", _payload())

    with pytest.raises(HTTPException) as error:
        submit.create(identity(), "same-key", _payload("Different goal"))

    assert error.value.status_code == 409
    assert len(database.rows) == 1


def test_submission_rejects_same_key_from_another_principal() -> None:
    database = _Database()
    _, submit, _ = _adapters(database)
    submit.create(identity(principal="user:alice"), "same-key", _payload())
    with pytest.raises(HTTPException) as error:
        submit.create(identity(principal="user:bob"), "same-key", _payload())
    assert error.value.status_code == 409
    assert len(database.rows) == 1


def test_submission_rejects_unsupported_execution_options() -> None:
    database = _Database()
    _, submit, _ = _adapters(database)
    request = _payload()
    request["options"] = {"seed": 7}
    with pytest.raises(HTTPException) as error:
        submit.create(identity(), "unsupported-options", request)
    assert error.value.status_code == 422
    assert database.rows == []


def test_rest_request_rejects_options_before_admission() -> None:
    with pytest.raises(ValidationError):
        RunRequest.model_validate(_payload() | {"options": {"seed": 7}})


def test_run_service_rejects_key_replay_across_principals_without_rest_payload() -> None:
    database = _Database()
    runs = RunService(database, clock=_Clock())
    runs.create(identity(principal="user:alice"), "mcp-key")
    with pytest.raises(RunIdempotencyConflict):
        runs.create(identity(principal="user:bob"), "mcp-key")


def test_submission_rejects_cross_lab_input_before_persisting() -> None:
    database = _Database()
    _, submit, _ = _adapters(database)
    request = _payload()
    request["inputs"] = ["lab-b:private-input"]

    with pytest.raises(LookupError, match="input not found"):
        submit.create(identity("lab-a"), "foreign-input", request)

    assert database.rows == []


def test_submission_rejects_unknown_skill_pack_before_persisting() -> None:
    database = _Database()
    _, submit, _ = _adapters(database)
    request = _payload()
    request["skill_packs"] = ["not-approved"]

    with pytest.raises(HTTPException) as error:
        submit.create(identity(), "unknown-skill", request)

    assert error.value.status_code == 422
    assert database.rows == []


def test_failed_request_write_rolls_back_run_insert() -> None:
    database = _Database()
    database.fail_request_update = True
    _, submit, _ = _adapters(database)

    with pytest.raises(RuntimeError, match="request write failed"):
        submit.create(identity(), "atomic-key", _payload())

    assert database.rows == []


def test_submission_stores_web_channel_for_user_principal_and_search_strips_it() -> None:
    database = _Database()
    _, submit, search = _adapters(database)
    submit.create(identity(principal="user:alice"), "web-key", _payload())

    assert database.rows[0]["request_payload"]["channel"] == "web"
    item = search.list(identity())["items"][0]
    assert "channel" not in item


def test_submission_stores_rest_channel_for_non_user_principal() -> None:
    database = _Database()
    _, submit, _ = _adapters(database)
    submit.create(identity(principal="service:worker"), "rest-key", _payload())

    assert database.rows[0]["request_payload"]["channel"] == "rest"


def test_search_applies_state_actor_since_cursor_and_lab_filters() -> None:
    database = _Database()
    clock = _Clock()
    _, submit, search = _adapters(database, clock)
    first = submit.create(identity(), "first", _payload("first"))
    clock.value = NOW + timedelta(days=1)
    second = submit.create(identity(), "second", _payload("second"))
    clock.value = NOW + timedelta(days=2)
    third = submit.create(identity(principal="user:bob"), "third", _payload("third"))
    database.find("lab-a", second["id"])["state"] = "running"  # type: ignore[index]
    database.find("lab-a", third["id"])["state"] = "failed"  # type: ignore[index]

    assert [item["id"] for item in search.list(identity(), state="running")["items"]] == [
        second["id"]
    ]
    assert [
        item["id"] for item in search.list(identity(), actor="user:alice")["items"]
    ] == [second["id"], first["id"]]
    assert [
        item["id"]
        for item in search.list(identity(), since="2026-01-02T00:00:00Z")["items"]
    ] == [third["id"], second["id"]]

    second_row = database.find("lab-a", second["id"])
    cursor = base64.urlsafe_b64encode(
        json.dumps(
            [second_row["created_at"].isoformat(), second["id"]], separators=(",", ":")
        ).encode()
    ).decode().rstrip("=")
    assert [item["id"] for item in search.list(identity(), cursor=cursor)["items"]] == [
        first["id"]
    ]
    assert search.list(identity("lab-b"))["items"] == []
