from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.auth import AccessToken, TokenVerifier

from scilab.api.mcp import create_app


class _TokenVerifier(TokenVerifier):
    def __init__(self, tokens: dict[str, AccessToken]) -> None:
        super().__init__()
        self.tokens = tokens

    async def verify_token(self, token: str) -> AccessToken | None:
        return self.tokens.get(token)


@asynccontextmanager
async def _mcp_client(app: Any, token: str) -> Any:
    def httpx_client_factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://mcp.test",
            **kwargs,
        )

    transport = StreamableHttpTransport(
        "http://mcp.test/",
        auth=token,
        httpx_client_factory=httpx_client_factory,
    )
    async with app.router.lifespan_context(app):
        async with Client(transport) as client:
            yield client


async def _call_mcp(
    app: Any,
    token: str,
    name: str,
    arguments: dict[str, Any],
    *,
    progress_handler: Any | None = None,
    raise_on_error: bool = True,
) -> Any:
    async with _mcp_client(app, token) as client:
        return await client.call_tool(
            name,
            arguments,
            progress_handler=progress_handler,
            raise_on_error=raise_on_error,
        )


@dataclass
class _Services:
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def start_research(
        self,
        identity: Any,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, str]:
        self.calls.append((identity, payload, idempotency_key))
        return {"run_id": "run-2", "state": "queued"}

    def get_run(self, identity: Any, run_id: str) -> dict[str, str]:
        self.calls.append((identity, run_id))
        return {"run_id": run_id, "lab_id": identity.lab_id}

    async def wait_run(
        self,
        identity: Any,
        run_id: str,
        *,
        timeout_s: int,
        progress: Any,
    ) -> dict[str, str]:
        self.calls.append((identity, run_id, timeout_s))
        await progress(0.5, "Halfway")
        return {"run_id": run_id, "state": "completed"}

    def list_skills(
        self, identity: Any, *, filter: str | None
    ) -> list[dict[str, Any]]:
        self.calls.append((identity, filter))
        return [{"pack": "general-research", "skills": ["literature-search"]}]

    async def ask_lab(self, identity: Any, *, question: str) -> dict[str, Any]:
        self.calls.append((identity, question))
        return {"answer": "Use the lab protocol.", "sources": ["memory-1"]}

    def get_artifact(self, identity: Any, artifact_id: str) -> dict[str, Any]:
        self.calls.append((identity, artifact_id))
        return {"artifact_id": artifact_id, "text": "measured result"}

    def approve(
        self,
        identity: Any,
        run_id: str,
        approval_id: str,
        *,
        decision: str,
        note: str | None,
    ) -> dict[str, bool]:
        self.calls.append((identity, run_id, approval_id, decision, note))
        return {"ok": True}

    def read_report(self, identity: Any, run_id: str) -> str:
        self.calls.append(("report", identity, run_id))
        return "# Findings\nEvidence-backed report."

    def read_manifest(self, identity: Any, run_id: str) -> dict[str, str]:
        self.calls.append(("manifest", identity, run_id))
        return {"run_id": run_id, "manifest_sha256": "abc123"}


def _token(scopes: list[str] | None = None) -> AccessToken:
    return AccessToken(
        token="verified-token",
        client_id="mcp-client",
        scopes=scopes or ["runs:read"],
        subject="user-123",
        resource="mcp.scilab",
        claims={
            "aud": "mcp.scilab",
            "lab_id": "lab-from-verified-claims",
            "scilab_principal_type": "user",
            "sub": "user-123",
        },
    )


def test_get_run_uses_verified_identity_over_streamable_http() -> None:
    services = _Services()
    app = create_app(
        services,
        auth_provider=_TokenVerifier({"verified-token": _token()}),
    )

    result = asyncio.run(
        _call_mcp(app, "verified-token", "get_run", {"run_id": "run-1"})
    )

    assert result.structured_content == {
        "run_id": "run-1",
        "lab_id": "lab-from-verified-claims",
    }
    assert services.calls[0][0].principal == "user:user-123"
    assert services.calls[0][1] == "run-1"


def test_start_research_forwards_inputs_and_budget_to_service() -> None:
    services = _Services()
    app = create_app(
        services,
        auth_provider=_TokenVerifier(
        {"writer-token": _token(["runs:write", "artifacts:read"])}
        ),
    )

    result = asyncio.run(
        _call_mcp(
            app,
            "writer-token",
            "start_research",
            {
                "goal": "Compare the candidate compounds",
                "inputs": ["artifact-1", "https://data.example/study.csv"],
                "skill_packs": ["general-research"],
                "budget_thb": 80.0,
                "max_minutes": 45,
            },
        )
    )

    assert result.structured_content == {"run_id": "run-2", "state": "queued"}
    identity, payload, idempotency_key = services.calls[0]
    assert identity.principal == "user:user-123"
    assert payload == {
        "goal": "Compare the candidate compounds",
        "inputs": ["artifact-1", "https://data.example/study.csv"],
        "skill_packs": ["general-research"],
        "budget_thb": 80.0,
        "max_minutes": 45,
    }
    assert idempotency_key


def test_start_research_artifact_input_checks_read_scope_at_tool_boundary() -> None:
    services = _Services()
    app = create_app(
        services, auth_provider=_TokenVerifier({"writer-token": _token(["runs:write"])})
    )
    with pytest.raises(ExceptionGroup) as denied:
        asyncio.run(
            _call_mcp(
                app, "writer-token", "start_research",
                {"goal": "Research", "inputs": ["artifact-1"]},
            )
        )
    assert denied.value.subgroup(lambda error: "artifacts:read" in str(error)) is not None
    assert services.calls == []


def test_start_research_url_only_needs_write_scope() -> None:
    services = _Services()
    app = create_app(
        services, auth_provider=_TokenVerifier({"writer-token": _token(["runs:write"])})
    )
    asyncio.run(
        _call_mcp(
            app, "writer-token", "start_research",
            {"goal": "Research", "inputs": ["https://data.example/study.csv"]},
        )
    )
    assert services.calls[0][1]["inputs"] == ["https://data.example/study.csv"]


def test_wait_run_caps_timeout_and_forwards_progress_over_streamable_http() -> None:
    services = _Services()
    app = create_app(
        services,
        auth_provider=_TokenVerifier({"reader-token": _token()}),
    )
    progress_updates: list[tuple[float, float | None, str | None]] = []

    def progress_handler(
        progress: float, total: float | None, message: str | None
    ) -> None:
        progress_updates.append((progress, total, message))

    result = asyncio.run(
        _call_mcp(
            app,
            "reader-token",
            "wait_run",
            {"run_id": "run-3", "timeout_s": 1800},
            progress_handler=progress_handler,
        )
    )

    assert result.structured_content == {"run_id": "run-3", "state": "completed"}
    assert services.calls[0][1:] == ("run-3", 1800)
    assert progress_updates == [(0.5, None, "Halfway")]


def test_list_skills_is_lab_scoped_and_passes_filter() -> None:
    services = _Services()
    app = create_app(
        services,
        auth_provider=_TokenVerifier({"reader-token": _token()}),
    )

    result = asyncio.run(
        _call_mcp(
            app,
            "reader-token",
            "list_skills",
            {"filter": "literature"},
        )
    )

    assert json.loads(result.content[0].text) == [
        {"pack": "general-research", "skills": ["literature-search"]}
    ]
    assert services.calls[0][0].lab_id == "lab-from-verified-claims"
    assert services.calls[0][1] == "literature"


def test_ask_lab_uses_artifact_read_scope_and_returns_adapter_answer() -> None:
    services = _Services()
    app = create_app(
        services,
        auth_provider=_TokenVerifier(
        {"writer-token": _token(["artifacts:read"])}
        ),
    )

    result = asyncio.run(
        _call_mcp(
            app,
            "writer-token",
            "ask_lab",
            {"question": "Which protocol should I use?"},
        )
    )

    assert result.structured_content == {
        "answer": "Use the lab protocol.",
        "sources": ["memory-1"],
    }
    assert services.calls[0][0].lab_id == "lab-from-verified-claims"


def test_get_artifact_uses_artifact_read_scope() -> None:
    services = _Services()
    app = create_app(
        services,
        auth_provider=_TokenVerifier(
            {"artifact-token": _token(["artifacts:read"])}
        ),
    )

    result = asyncio.run(
        _call_mcp(
            app,
            "artifact-token",
            "get_artifact",
            {"artifact_id": "artifact-1"},
        )
    )

    assert result.structured_content == {
        "artifact_id": "artifact-1",
        "text": "measured result",
    }
    assert services.calls[0][0].lab_id == "lab-from-verified-claims"
    assert services.calls[0][1] == "artifact-1"


def test_approve_requires_runs_approve_and_forwards_decision() -> None:
    services = _Services()
    app = create_app(
        services,
        auth_provider=_TokenVerifier(
            {"approver-token": _token(["runs:approve"])}
        ),
    )

    result = asyncio.run(
        _call_mcp(
            app,
            "approver-token",
            "approve",
            {
                "run_id": "run-4",
                "approval_id": "approval-1",
                "decision": "approve",
                "note": "Reviewed",
            },
        )
    )

    assert result.structured_content == {"ok": True}
    assert services.calls[0][1:] == (
        "run-4",
        "approval-1",
        "approve",
        "Reviewed",
    )


def test_run_resources_have_contract_mime_types_and_tools_are_registered() -> None:
    services = _Services()
    app = create_app(
        services,
        auth_provider=_TokenVerifier(
            {"reader-token": _token(["runs:read", "artifacts:read"])}
        ),
    )

    async def read_surfaces() -> Any:
        async with _mcp_client(app, "reader-token") as client:
            tools = await client.list_tools()
            report = await client.read_resource("scilab://runs/run-5/report")
            manifest = await client.read_resource("scilab://runs/run-5/manifest")
            return {tool.name for tool in tools}, report[0], manifest[0]

    tool_names, report, manifest = asyncio.run(read_surfaces())

    assert tool_names == {
        "start_research",
        "get_run",
        "wait_run",
        "ask_lab",
        "list_skills",
        "get_artifact",
        "approve",
    }
    assert report.mimeType == "text/markdown"
    assert report.text == "# Findings\nEvidence-backed report."
    assert manifest.mimeType == "application/json"
    assert json.loads(manifest.text) == {
        "run_id": "run-5",
        "manifest_sha256": "abc123",
    }


def test_research_brief_prompt_uses_topic_without_starting_a_run() -> None:
    services = _Services()
    app = create_app(
        services,
        auth_provider=_TokenVerifier({"writer-token": _token(["runs:write"])}),
    )

    async def read_prompt() -> Any:
        async with _mcp_client(app, "writer-token") as client:
            return await client.get_prompt("research_brief", {"topic": "battery safety"})

    result = asyncio.run(read_prompt())
    assert "battery safety" in result.messages[0].content.text
    assert services.calls == []


def test_keycloak_provider_does_not_require_openid_for_client_tokens(monkeypatch):
    captured = {}

    def provider(**kwargs):
        captured.update(kwargs)
        return _TokenVerifier({})

    monkeypatch.setattr("scilab.api.mcp.KeycloakAuthProvider", provider)
    create_app(_Services(), realm_url="https://id.example/realms/sci", base_url="https://mcp.example")
    assert captured["audience"] == "mcp.scilab"
    assert captured["required_scopes"] == []
