from __future__ import annotations

import asyncio
import hashlib
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from scilab.artifacts import ArtifactService
from scilab.db import SET_TENANT_SQL
from scilab.hermes import HermesClient
from scilab.identity import Identity
from scilab.lab_knowledge import LabKnowledgeService


NOW = datetime(2026, 9, 23, tzinfo=timezone.utc)


def _identity(lab_id: str = "lab-a") -> Identity:
    return Identity(lab_id, "user:alice", frozenset({"artifacts:read"}))


def _row(
    artifact_id: str,
    lab_id: str,
    kind: str,
    body: bytes,
    *,
    digest: str | None = None,
    created_at: datetime = NOW,
) -> dict[str, Any]:
    return {
        "id": artifact_id,
        "run_id": f"run-{lab_id}",
        "lab_id": lab_id,
        "kind": kind,
        "uri": f"s3://{lab_id}/{artifact_id}.txt",
        "sha256": digest or hashlib.sha256(body).hexdigest(),
        "bytes": len(body),
        "produced_by_step": 1,
        "metadata": {"media_type": "text/markdown"},
        "created_at": created_at,
    }


class _Cursor:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, query: str, params: tuple[object, ...] = ()) -> None:
        normalized = " ".join(query.lower().split())
        if normalized == " ".join(SET_TENANT_SQL.lower().split()):
            return

        self.database.last_query = normalized
        self.database.last_params = params
        if "from artifacts" not in normalized:
            raise AssertionError(f"unexpected SQL: {normalized}")
        if "kind in" in normalized:
            lab_id, limit = params
            rows = [
                row
                for row in self.database.rows
                if row["lab_id"] == lab_id and row["kind"] in {"document", "report"}
            ]
            self.rows = rows[: int(limit)]
        elif "where lab_id = %s and id = %s" in normalized:
            lab_id, artifact_id = params
            self.rows = [
                row
                for row in self.database.rows
                if row["lab_id"] == lab_id and row["id"] == artifact_id
            ]
        else:
            self.rows = list(self.database.rows)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None


class _Database:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.last_query = ""
        self.last_params: tuple[object, ...] = ()

    def transaction(self):
        return nullcontext()

    def cursor(self) -> _Cursor:
        return _Cursor(self)


class _Storage:
    def __init__(self, bodies: dict[str, bytes]) -> None:
        self.bodies = bodies

    def read_bytes(self, uri: str) -> bytes:
        return self.bodies[uri]


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def json(self) -> dict[str, Any]:
        return self.payload


class _Transport:
    def __init__(self, answer: str = "The report records a 42 percent response rate.") -> None:
        self.answer = answer
        self.calls: list[dict[str, Any]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append({"method": method, "url": url, **kwargs})
        return _Response(
            {
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.answer},
                        "finish_reason": "stop",
                    }
                ],
            }
        )


def _service(rows: list[dict[str, Any]], bodies: dict[str, bytes]):
    database = _Database(rows)
    artifacts = ArtifactService(database, _Storage(bodies))
    transport = _Transport()
    hermes = HermesClient(
        {"lab-a": "https://hermes-a.internal", "lab-b": "https://hermes-b.internal"},
        api_key="api-key-1",
        session_id="session-1",
        session_key="session-key-1",
        transport=transport,
    )
    service = LabKnowledgeService(
        artifacts,
        hermes,
        model="scilab-test-model",
    )
    return service, database, transport


def test_artifact_knowledge_listing_is_lab_scoped_filtered_and_limited() -> None:
    body = b"Response rate was 42 percent."
    rows = [
        _row("doc-a", "lab-a", "document", body),
        _row("report-a", "lab-a", "report", body),
        _row("manifest-a", "lab-a", "manifest", body),
        _row("report-b", "lab-b", "report", body),
    ]
    database = _Database(rows)
    service = ArtifactService(database, _Storage({}))
    list_sources = getattr(service, "list_knowledge_sources", None)
    assert callable(list_sources), "ArtifactService needs a bounded Lab source listing"

    sources = list_sources(_identity(), limit=10_000)

    assert [artifact.id for artifact in sources] == ["doc-a", "report-a"]
    assert "where lab_id = %s" in database.last_query
    assert "kind in ('document', 'report')" in database.last_query
    assert "limit %s" in database.last_query
    assert database.last_params == ("lab-a", 32)


def test_ask_lab_answers_only_from_matching_lab_text_and_returns_real_sources() -> None:
    useful = b"The cohort response rate was 42 percent."
    unrelated = b"The compound was stable at room temperature."
    rows = [
        _row("report-a", "lab-a", "report", useful),
        _row("doc-a", "lab-a", "document", unrelated),
        _row("image-a", "lab-a", "image", useful),
        _row("report-b", "lab-b", "report", useful),
    ]
    bodies = {row["uri"]: body for row, body in zip(rows, [useful, unrelated, useful, useful])}
    service, _, transport = _service(rows, bodies)

    result = asyncio.run(service.ask(_identity(), "What was the response rate?"))

    assert result == {
        "answer": "The report records a 42 percent response rate.",
        "sources": ["report-a"],
    }
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://hermes-a.internal/v1/chat/completions"
    assert "/v1/runs" not in call["url"]
    assert call["json"]["stream"] is False
    assert "report-a" in call["json"]["messages"][1]["content"]
    assert "report-b" not in call["json"]["messages"][1]["content"]
    assert "image-a" not in call["json"]["messages"][1]["content"]


def test_ask_lab_returns_unavailable_without_calling_hermes_when_evidence_is_irrelevant() -> None:
    body = b"The compound was stable at room temperature."
    rows = [_row("report-a", "lab-a", "report", body)]
    service, _, transport = _service(rows, {rows[0]["uri"]: body})

    result = asyncio.run(service.ask(_identity(), "What was the response rate?"))

    assert result["answer"].startswith("Evidence unavailable:")
    assert result["sources"] == []
    assert transport.calls == []


def test_ask_lab_ignores_digest_mismatched_artifacts_and_does_not_call_hermes() -> None:
    body = b"The cohort response rate was 42 percent."
    rows = [_row("report-a", "lab-a", "report", body, digest="0" * 64)]
    service, _, transport = _service(rows, {rows[0]["uri"]: body})

    result = asyncio.run(service.ask(_identity(), "What was the response rate?"))

    assert result["answer"].startswith("Evidence unavailable:")
    assert result["sources"] == []
    assert transport.calls == []


def test_ask_lab_bounds_sources_and_completion_context() -> None:
    body = (b"The response rate was 42 percent. " + b"evidence " * 700)
    rows = [
        _row(
            f"report-{index}",
            "lab-a",
            "report",
            body,
            created_at=NOW + timedelta(seconds=index),
        )
        for index in range(8)
    ]
    service, _, transport = _service(rows, {row["uri"]: body for row in rows})

    result = asyncio.run(service.ask(_identity(), "What was the response rate?"))

    messages = transport.calls[0]["json"]["messages"]
    assert len(result["sources"]) <= 4
    assert sum(len(message["content"]) for message in messages) <= 16_000


def test_ask_lab_rejects_questions_that_exceed_the_context_limit() -> None:
    service, _, transport = _service([], {})

    with pytest.raises(ValueError, match="question"):
        asyncio.run(service.ask(_identity(), "x" * 20_000))

    assert transport.calls == []
