from __future__ import annotations

import inspect
import json
import math
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from fastmcp import Context, FastMCP
from fastmcp.resources import ResourceContent
from fastmcp.server.auth.providers.keycloak import KeycloakAuthProvider
from fastmcp.server.dependencies import get_access_token

from scilab.identity import AuthenticationError, Identity, identity_from_verified_mcp
from scilab.tenancy import require_scope


def _identity() -> Identity:
    access_token = get_access_token()
    if access_token is None:
        raise AuthenticationError("verified MCP access token required")
    claims = dict(access_token.claims)
    claims["scopes"] = access_token.scopes
    if access_token.subject is not None:
        claims.setdefault("sub", access_token.subject)
    claims.setdefault("client_id", access_token.client_id)
    return identity_from_verified_mcp(claims)


def _authorized(scope: str) -> Identity:
    identity = _identity()
    require_scope(identity, scope)
    return identity


def _required_text(value: str, field: str) -> str:
    if not value.strip():
        raise ValueError(f"{field} must be non-blank")
    return value


async def _call(function: Any, *args: Any, **kwargs: Any) -> Any:
    result = function(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def create_app(
    services: Any,
    *,
    realm_url: str | None = None,
    base_url: str | None = None,
    auth_provider: Any | None = None,
) -> Any:
    if auth_provider is None:
        if not realm_url or not base_url:
            raise ValueError("realm_url and base_url are required for Keycloak auth")
        auth_provider = KeycloakAuthProvider(
            realm_url=realm_url,
            base_url=base_url,
            required_scopes=[],
            audience="mcp.scilab",
        )

    mcp = FastMCP("SciLab", auth=auth_provider)

    @mcp.tool
    async def start_research(
        goal: str,
        inputs: list[str] | None = None,
        skill_packs: list[str] | None = None,
        budget_thb: float | None = None,
        max_minutes: int | None = None,
    ) -> Any:
        """Start a Lab research Run; artifact-ID inputs additionally require artifacts:read."""
        identity = _authorized("runs:write")
        _required_text(goal, "goal")
        if any(not item.strip() for item in inputs or []):
            raise ValueError("inputs must contain non-blank values")
        if any(not urlsplit(item).scheme for item in inputs or []):
            require_scope(identity, "artifacts:read")
        if budget_thb is not None and (not math.isfinite(budget_thb) or budget_thb < 0):
            raise ValueError("budget_thb must be finite and non-negative")
        if max_minutes is not None and max_minutes < 1:
            raise ValueError("max_minutes must be positive")
        payload = {"goal": goal}
        payload.update(
            (key, value)
            for key, value in (
                ("inputs", inputs),
                ("skill_packs", skill_packs),
                ("budget_thb", budget_thb),
                ("max_minutes", max_minutes),
            )
            if value is not None
        )
        return await _call(
            services.start_research,
            identity,
            payload,
            str(uuid4()),
        )

    @mcp.tool
    async def get_run(run_id: str) -> Any:
        identity = _authorized("runs:read")
        _required_text(run_id, "run_id")
        return await _call(services.get_run, identity, run_id)

    @mcp.tool
    async def wait_run(
        run_id: str,
        ctx: Context,
        timeout_s: int = 600,
    ) -> Any:
        identity = _authorized("runs:read")
        _required_text(run_id, "run_id")
        if not 1 <= timeout_s <= 1800:
            raise ValueError("timeout_s must be between 1 and 1800")

        async def report_progress(progress: float, message: str) -> None:
            await ctx.report_progress(progress=progress, message=message)

        return await _call(
            services.wait_run,
            identity,
            run_id,
            timeout_s=timeout_s,
            progress=report_progress,
        )

    @mcp.tool
    async def list_skills(filter: str | None = None) -> Any:
        identity = _authorized("runs:read")
        return await _call(services.list_skills, identity, filter=filter)

    @mcp.tool
    async def ask_lab(question: str) -> Any:
        identity = _authorized("artifacts:read")
        _required_text(question, "question")
        return await _call(services.ask_lab, identity, question=question)

    @mcp.tool
    async def get_artifact(artifact_id: str) -> Any:
        identity = _authorized("artifacts:read")
        _required_text(artifact_id, "artifact_id")
        return await _call(services.get_artifact, identity, artifact_id)

    @mcp.tool
    async def approve(
        run_id: str,
        approval_id: str,
        decision: Literal["approve", "reject"],
        note: str | None = None,
    ) -> Any:
        identity = _authorized("runs:approve")
        _required_text(run_id, "run_id")
        _required_text(approval_id, "approval_id")
        return await _call(
            services.approve,
            identity,
            run_id,
            approval_id,
            decision=decision,
            note=note,
        )

    @mcp.resource("scilab://runs/{id}/report", mime_type="text/markdown")
    async def report(id: str) -> list[ResourceContent]:
        identity = _authorized("artifacts:read")
        content = await _call(services.read_report, identity, _required_text(id, "run_id"))
        return [ResourceContent(content=content, mime_type="text/markdown")]

    @mcp.resource("scilab://runs/{id}/manifest", mime_type="application/json")
    async def manifest(id: str) -> list[ResourceContent]:
        identity = _authorized("artifacts:read")
        content = await _call(services.read_manifest, identity, _required_text(id, "run_id"))
        return [ResourceContent(content=json.dumps(content, ensure_ascii=False), mime_type="application/json")]

    @mcp.prompt
    def research_brief(topic: str) -> str:
        _identity()
        _required_text(topic, "topic")
        return f"Research topic: {topic}\nState the question, scope, inputs, and expected evidence."

    # path="/": mounting this app at "/mcp" (see runtime.py) must expose the
    # endpoint at "/mcp", not "/mcp/mcp" (FastMCP's own default route is "/mcp").
    return mcp.http_app(transport="streamable-http", path="/")
