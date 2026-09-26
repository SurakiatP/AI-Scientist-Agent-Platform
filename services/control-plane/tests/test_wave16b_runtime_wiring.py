"""End-to-end wiring proofs for wave16b: runtime app mounting, worker_main.run_worker's
register/seal/metering/reconcile wiring, and ApprovalService -> AuditService cursor sharing.

# ponytail: fixtures are copied (not imported) from tests/api/test_runtime.py and
# tests/runs/test_worker_main.py -- tests/ has no __init__.py package markers, so
# cross-file imports would be fragile. Only the minimum needed is copied.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from scilab.approvals import ApprovalService, OPAClient
from scilab.artifacts import Artifact, ArtifactService
from scilab.events import EventService
from scilab.identity import Identity, credential_digest
from scilab.metering import MeteringService
from scilab.provenance import ManifestService
from scilab.runs import worker_main
from scilab.runs.model import Run, RunState
from scilab.runs.service import RunService
from scilab.runs.state import apply_transition
from scilab.runs.worker import LEASE_SECONDS, RunClaim

# ===================== Part 1: create_runtime_app mounts /a2a + /mcp + REST =====================


def _runtime_config() -> dict[str, str]:
    return {
        "SCILAB_DATABASE_URL": "postgresql://runtime@db/scilab",
        "SCILAB_OIDC_ISSUER": "https://id.example/realms/scilab",
        "SCILAB_OIDC_AUDIENCE": "scilab-rest",
        "SCILAB_OIDC_JWKS_URI": "https://id.example/realms/scilab/certs",
        "SCILAB_MINIO_URL": "https://storage.example:9000",
        "SCILAB_MINIO_ACCESS_KEY": "test-access",
        "SCILAB_MINIO_SECRET_KEY": "test-secret",
        "SCILAB_INPUT_BUCKET": "inputs",
        "SCILAB_ARTIFACT_BUCKET": "artifacts",
        "SCILAB_NATS_URL": "nats://nats.example:4222",
        "SCILAB_OPA_URL": "http://opa.example:8181",
        "SCILAB_RUN_ADMISSION_PER_MINUTE": "7",
        "POD_NAMESPACE": "scilab",
        "SCILAB_KEYCLOAK_REALM_URL": "https://id.example/realms/scilab",
        "SCILAB_MCP_BASE_URL": "https://mcp.example",
    }


_PEER_SECRET = "peer-secret-for-tests"


class _RuntimeDb:
    def __init__(self) -> None:
        self.row: Any = None

    def transaction(self) -> Any:
        return nullcontext()

    def cursor(self) -> "_RuntimeDb":
        return self

    def __enter__(self) -> "_RuntimeDb":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple = ()) -> None:
        if "FROM lab_memberships" in sql:
            self.row = {"subject": "subject-1", "lab_id": "lab-a", "role": "owner"} if params == ("subject-1", "lab-a") else None
        elif "FROM a2a_peers" in sql:
            self.row = ("peer-a", "lab-a", ["runs:read"], params[0]) if params and params[0] == credential_digest(_PEER_SECRET) else None
        elif "FROM labs" in sql:
            self.row = ("Lab A",) if params and params[0] == "lab-a" else None
        elif sql.strip() == "SELECT 1":
            self.row = (1,)
        else:
            self.row = None

    def fetchone(self) -> Any:
        return self.row

    def close(self) -> None:
        return None


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtime_module: Any) -> str:
    monkeypatch.setattr("psycopg.connect", lambda *_a, **_k: _RuntimeDb())
    monkeypatch.setattr("minio.Minio", lambda *_a, **_k: SimpleNamespace(bucket_exists=lambda _b: True))
    monkeypatch.setattr("boto3.client", lambda *_a, **_k: SimpleNamespace(head_bucket=lambda **_k: None, close=lambda: None))

    class _Nats:
        async def close(self) -> None:
            return None

    async def connect_nats(*_a: Any, **_k: Any) -> _Nats:
        return _Nats()

    monkeypatch.setattr("nats.connect", connect_nats)
    monkeypatch.setattr(runtime_module.ssl, "create_default_context", lambda **_k: object())
    account = tmp_path / "serviceaccount"
    account.mkdir()
    (account / "token").write_text("service-token")
    (account / "ca.crt").write_text("test-ca")
    monkeypatch.setattr(runtime_module, "SERVICE_ACCOUNT_DIRECTORY", account)

    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class _StaticJwks:
        def __init__(self, _uri: str) -> None:
            return None

        def get_signing_key_from_jwt(self, _token: str) -> SimpleNamespace:
            return SimpleNamespace(key=signing_key.public_key())

    monkeypatch.setattr("scilab.api.oidc.jwt.PyJWKClient", _StaticJwks)
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": "subject-1",
            "lab_id": "lab-a",
            "iss": _runtime_config()["SCILAB_OIDC_ISSUER"],
            "aud": _runtime_config()["SCILAB_OIDC_AUDIENCE"],
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        signing_key,
        algorithm="RS256",
    )


def _a2a_request(app: Any, method: str, path: str) -> httpx.Response:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://testserver") as client:
            return await client.request(method, path)

    return asyncio.run(send())


def test_runtime_app_mounts_a2a_and_mcp_and_still_serves_rest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """create_runtime_app(): /a2a serves the agent card, /mcp requires a token (401), and REST routes still respond."""
    from scilab.api import runtime as runtime_module

    token = _patch_runtime(monkeypatch, tmp_path, runtime_module)
    with TestClient(runtime_module.create_runtime_app(_runtime_config())) as client:
        card = _a2a_request(client.app, "GET", "/a2a/labs/lab-a/.well-known/agent-card.json")
        assert card.status_code == 200
        assert card.json()["name"] == "Lab A"

        assert client.get("/mcp/").status_code == 401

        me = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        assert me.status_code == 200


# ===================== Part 2: run_worker wires register/seal/usage/due_transition =====================


def _worker_environment() -> dict[str, str]:
    return {
        "SCILAB_LAB_ID": "lab-a",
        "SCILAB_DATABASE_URL": "postgresql://worker@db/scilab",
        "SCILAB_HERMES_API_KEY": "lab-key",
        "POD_NAMESPACE": "scilab",
        "SCILAB_NATS_URL": "nats://nats:4222",
        "SCILAB_PI_PROVIDER": "pi-provider",
        "SCILAB_REVIEWER_PROVIDER": "reviewer-provider",
        "SCILAB_MINIO_URL": "https://storage.example:9000",
        "SCILAB_MINIO_ACCESS_KEY": "access",
        "SCILAB_MINIO_SECRET_KEY": "secret",
        "SCILAB_INPUT_BUCKET": "inputs",
        "SCILAB_OPA_URL": "http://opa.internal:8181",
        "SCILAB_MODEL_PRICES_THB": '{"sci-pi-frontier": {"in": 10, "out": 20}}',
        "SCILAB_ARTIFACT_BUCKET": "artifacts",
        "SCILAB_HERMES_IMAGE": "registry.internal/hermes:16",
        "SCILAB_SKILLS_IMAGE": "registry.internal/skills:16",
        "SCILAB_HERMES_CONFIG_SHA256": "a" * 64,
        "SCILAB_SANDBOX_IMAGE": "registry.internal/sandbox:16",
    }


def _completed_output() -> str:
    return (
        "Report body.\n\n```json\n"
        '{"claims": [{"text": "finding one", "evidence": ["doi:10.1/xyz"], "confidence": 0.9}], '
        '"artifacts": [], "caveats": [], "next_steps": []}\n```'
    )


def _sse_lines() -> list[str]:
    events = [
        {"event": "tool.started", "run_id": "hermes-run-1", "tool": "search_papers"},
        {"event": "tool.completed", "run_id": "hermes-run-1", "tool": "search_papers"},
        {
            "event": "subagent.complete", "run_id": "hermes-run-1", "delegation_id": "delegation-1",
            "model": "sci-specialist", "input_tokens": 120, "output_tokens": 80, "cost_usd": 0.02,
        },
        {
            "event": "run.completed", "run_id": "hermes-run-1", "output": _completed_output(),
            "usage": {"input_tokens": 900, "output_tokens": 450},
            "runtime": {"provider": "hermes", "model": "sci-pi-frontier"},
        },
    ]
    lines: list[str] = []
    for event in events:
        lines += [f"data: {json.dumps(event)}", ""]
    return lines


def _canned_run(state: RunState = RunState.QUEUED) -> Run:
    now = datetime.now(timezone.utc)
    return Run(
        id="run-1", lab_id="lab-a", idempotency_key="request-key-1", state=state, reason=None,
        retry_count=0, max_minutes=120, hermes_run_id=None, created_at=now, updated_at=now,
        queued_at=now, running_since=None, runtime_used=timedelta(0), last_heartbeat_at=None,
        approval_expires_at=None, context_id=None, budget_thb=None,
    )


class _OneShotWorker:
    """Duck-types RunWorker. claim_next fires once so poll_forever's loop reaches its
    asyncio.sleep instead of busy-spinning once the run is COMPLETED (reusing
    test_executor.py's always-on FakeWorker.claim_next would starve the sibling
    reconcile_runs_forever coroutine forever)."""

    def __init__(self, run: Run, payload: dict[str, Any], order: list[str]) -> None:
        self.run = run
        self.order = order
        deadline = datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)
        self.claim = RunClaim(run, payload, uuid4(), deadline, None)
        self.transitions: list[RunState] = []
        self._available = True

    def claim_next(self, _identity: Identity) -> RunClaim | None:
        if not self._available:
            return None
        self._available = False
        return self.claim

    def renew(self, _identity: Identity, _claim: RunClaim) -> datetime:
        return datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)

    def current(self, _identity: Identity, _claim: RunClaim) -> Run:
        return self.run

    def transition(self, _identity: Identity, _claim: RunClaim, target: RunState, **kwargs: Any) -> Run:
        self.run = apply_transition(self.run, target, datetime.now(timezone.utc), **kwargs)
        self.transitions.append(target)
        if target is RunState.COMPLETED:
            self.order.append("transition_completed")
        return self.run

    def release(self, _identity: Identity, _claim: RunClaim) -> bool:
        return True


class _Cursor:
    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def execute(self, sql: str, _params: tuple = ()) -> None:
        statement = " ".join(sql.lower().split())
        if statement == "select 1" or "set_config" in statement:
            return
        raise AssertionError(f"unexpected SQL against dummy worker connection: {statement}")


class _Connection:
    def transaction(self) -> Any:
        return nullcontext()

    def cursor(self) -> _Cursor:
        return _Cursor()

    def close(self) -> None:
        return None


class _SSEResponse:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    def raise_for_status(self) -> None:
        return None

    async def aiter_lines(self) -> Any:
        for line in self._lines:
            yield line


class _SSEContext:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def __aenter__(self) -> _SSEResponse:
        return _SSEResponse(self._lines)

    async def __aexit__(self, *_a: Any) -> None:
        return None


class _FakeHermesTransport:
    """Fakes httpx.AsyncClient under _HermesHTTPTransport, scripting one run's SSE
    stream through the real hermes.py translation code."""

    def __init__(self, sse_lines: list[str]) -> None:
        self._sse_lines = sse_lines

    async def __aenter__(self) -> "_FakeHermesTransport":
        return self

    async def __aexit__(self, *_a: Any) -> None:
        return None

    async def request(self, method: str, url: str, **_kwargs: Any) -> Any:
        if url.endswith("/v1/runs"):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"run_id": "hermes-run-1"})
        raise AssertionError(f"unexpected request {method} {url}")

    def stream(self, _method: str, _url: str, **_kwargs: Any) -> _SSEContext:
        return _SSEContext(self._sse_lines)


def test_run_worker_wires_register_seal_usage_and_due_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    """run_worker(): the scripted Hermes SSE run drives ArtifactService.register (report +
    tool-log) and ManifestService.seal (before the COMPLETED transition) and
    MeteringService.record_model_usage_async (delegation + completion delta); its
    reconcile_runs_forever loop drives due_transition. All 4 spies are on the real
    classes/module; only DB/network I/O is faked."""
    connections = iter([_Connection(), _Connection(), _Connection(), _Connection()])
    monkeypatch.setattr(worker_main.psycopg, "connect", lambda *_a, **_k: next(connections))

    async def connect_nats(_url: str) -> Any:
        return SimpleNamespace(close=_async_noop)

    monkeypatch.setattr(worker_main.nats, "connect", connect_nats)
    transport = _FakeHermesTransport(_sse_lines())
    monkeypatch.setattr(worker_main.httpx, "AsyncClient", lambda **_k: transport)
    monkeypatch.setattr(worker_main, "Minio", lambda *_a, **_k: object())
    monkeypatch.setattr(worker_main.boto3, "client", lambda *_a, **_k: SimpleNamespace(head_bucket=lambda **_k: None))

    order: list[str] = []
    worker = _OneShotWorker(_canned_run(), {"goal": "Investigate X", "inputs": [], "skill_packs": []}, order)
    monkeypatch.setattr(worker_main, "RunWorker", lambda _connection: worker)
    monkeypatch.setattr(worker_main, "reconcile_pending_forever", _blocked_forever)
    monkeypatch.setattr(worker_main, "reconcile_approvals_forever", _blocked_forever)

    register_calls: list[str] = []

    def fake_register(_self: Any, _identity: Identity, _run_id: str, *, kind: str, uri: str, content: bytes, produced_by_step: int, metadata: dict[str, Any] | None = None) -> Artifact:
        register_calls.append(kind)
        return Artifact(id=f"artifact-{kind}", run_id="run-1", lab_id="lab-a", kind=kind, uri=uri, sha256="deadbeef", bytes=len(content), produced_by_step=produced_by_step, metadata=metadata or {}, created_at=datetime.now(timezone.utc))

    monkeypatch.setattr(ArtifactService, "register", fake_register)
    monkeypatch.setattr(EventService, "publish_event", _async_noop_method)
    monkeypatch.setattr(EventService, "replay_events", lambda *_a, **_k: [])

    def fake_seal(_self: Any, _identity: Identity, _run_id: str, _draft: dict[str, Any]) -> SimpleNamespace:
        order.append("seal")
        return SimpleNamespace(artifact=SimpleNamespace(id="manifest-artifact-1"))

    monkeypatch.setattr(ManifestService, "seal", fake_seal)

    usage_calls: list[dict[str, Any]] = []

    async def fake_record_usage(_self: Any, _identity: Identity, run_id: str, *, actor: str, source: str, model: str, tokens_in: int, tokens_out: int, metadata: dict[str, Any] | None = None) -> None:
        usage_calls.append({"run_id": run_id, "model": model, "tokens_in": tokens_in, "tokens_out": tokens_out})

    monkeypatch.setattr(MeteringService, "record_model_usage_async", fake_record_usage)
    monkeypatch.setattr(MeteringService, "run_token_totals", lambda _self, _identity, _run_id: (0, 0))
    monkeypatch.setattr(MeteringService, "aggregate_usage", lambda _self, _identity, *, run_id=None, **_k: SimpleNamespace(tokens_in=0, tokens_out=0, llm_cost_thb=0.0, compute_cost_thb=0.0))

    due_calls: list[Run] = []

    def fake_due_transition(run: Run, _now: datetime) -> None:
        due_calls.append(run)
        return None

    monkeypatch.setattr(worker_main, "due_transition", fake_due_transition)
    monkeypatch.setattr(RunService, "list_active", lambda _self, _identity: [_canned_run(RunState.RUNNING)])

    real_sleep = asyncio.sleep

    async def fake_sleep(_delay: float) -> None:
        await real_sleep(0)
        if "seal" in order and due_calls:
            raise asyncio.CancelledError

    monkeypatch.setattr(worker_main.asyncio, "sleep", fake_sleep)

    settings = worker_main.WorkerSettings.from_environment(_worker_environment())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(asyncio.wait_for(worker_main.run_worker(settings), timeout=5))

    assert register_calls == ["report", "tool-log"]
    assert order.count("seal") == 1
    assert {(c["model"], c["tokens_in"], c["tokens_out"]) for c in usage_calls} == {
        ("sci-specialist", 120, 80),
        ("sci-pi-frontier", 900, 450),
    }
    assert due_calls
    assert worker.transitions[-1] is RunState.COMPLETED
    assert order.index("seal") < order.index("transition_completed")


async def _async_noop(*_a: Any, **_k: Any) -> None:
    return None


async def _async_noop_method(*_a: Any, **_k: Any) -> None:
    return None


async def _blocked_forever(*_a: Any, **_k: Any) -> None:
    await asyncio.Event().wait()


# ===================== Part 3: approval decision reaches AuditService on the same cursor =====================


class _ApprovalCursor:
    def __init__(self, approval_row: dict[str, Any], run_row: dict[str, Any]) -> None:
        self._approval_row = approval_row
        self._run_row = run_row
        self.executed: list[str] = []
        self._result: Any = None

    def __enter__(self) -> "_ApprovalCursor":
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def execute(self, sql: str, _params: tuple = ()) -> None:
        statement = " ".join(sql.lower().split())
        self.executed.append(statement)
        if "set_config" in statement:
            self._result = None
        elif "from approvals" in statement and "for update" in statement:
            self._result = dict(self._approval_row)
        elif "from runs" in statement and "for update" in statement:
            self._result = dict(self._run_row)
        elif statement.startswith("update approvals set status"):
            self._result = None
        elif statement.startswith("update runs set state"):
            self._result = None
        elif statement.startswith("insert into audit_events"):
            self._result = None
        else:
            raise AssertionError(f"unexpected SQL: {statement}")

    def fetchone(self) -> Any:
        return self._result


class _ApprovalConnection:
    def __init__(self, approval_row: dict[str, Any], run_row: dict[str, Any]) -> None:
        self._approval_row = approval_row
        self._run_row = run_row
        self.cursors_created: list[_ApprovalCursor] = []

    def transaction(self) -> Any:
        return nullcontext()

    def cursor(self) -> _ApprovalCursor:
        cursor = _ApprovalCursor(self._approval_row, self._run_row)
        self.cursors_created.append(cursor)
        return cursor

    def close(self) -> None:
        return None


def test_approval_decision_reaches_audit_append_with_cursor_on_same_cursor() -> None:
    """decide_approval()'s approvals/runs UPDATEs and the real AuditService.append_with_cursor
    INSERT run through the exact same `with connection.cursor()` instance -- ApprovalService is
    constructed with no audit= override, exactly as worker_main.run_worker / the API routes do."""
    now = datetime.now(timezone.utc)
    approval_row = {
        "id": "approval-1", "run_id": "run-1", "lab_id": "lab-a", "action": "hermes.tool",
        "effect": "execute", "action_fingerprint": "fp-1", "status": "pending", "reason": "needs review",
        "policy_rule": "rule-1", "preview": {}, "requested_at": now - timedelta(minutes=5),
        "expires_at": now + timedelta(hours=1), "decided_at": None, "actor": None, "note": None,
        "hermes_request_id": None,
    }
    run_row = {
        "id": "run-1", "lab_id": "lab-a", "idempotency_key": "request-key-1",
        "state": RunState.AWAITING_APPROVAL, "reason": None, "retry_count": 0, "max_minutes": 120,
        "hermes_run_id": "hermes-run-1", "created_at": now, "updated_at": now, "queued_at": now,
        "running_since": now, "runtime_used": timedelta(0), "last_heartbeat_at": now,
        "approval_expires_at": None, "context_id": None, "budget_thb": None,
    }
    connection = _ApprovalConnection(approval_row, run_row)
    identity = Identity("lab-a", "user:reviewer-1", frozenset({"runs:approve", "runs:write"}))

    approvals = ApprovalService(connection, OPAClient("http://opa.internal:8181"), EventService(connection, None))
    decided = approvals.decide_approval(identity, "approval-1", "approve")

    assert decided.status == "approved"
    assert len(connection.cursors_created) == 1
    cursor = connection.cursors_created[0]
    assert any(stmt.startswith("update approvals set status") for stmt in cursor.executed)
    assert any(stmt.startswith("insert into audit_events") for stmt in cursor.executed)
