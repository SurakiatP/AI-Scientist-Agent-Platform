from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from scilab.identity import Identity
from scilab.sse import stream_events
from scilab.tenancy import AuthorizationError, require_lab, require_scope


class RunBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thb: float = Field(ge=0)
    max_minutes: int = Field(gt=0)


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    inputs: list[str]
    skill_packs: list[str]
    budget: RunBudget
    options: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    note: str | None = None


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)


IdentityResolver = Callable[[Request], Identity | Awaitable[Identity]]


async def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = function(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _normalize_identity(identity: Identity) -> Identity:
    return Identity(
        identity.lab_id.strip(),
        identity.principal.strip(),
        (scope.strip() for scope in identity.scopes),
    )


def _json(value: Any) -> Any:
    return jsonable_encoder(value)


def create_app(
    services: Any,
    identity_resolver: IdentityResolver,
    *,
    event_streamer: Callable[..., Any] = stream_events,
) -> FastAPI:
    app = FastAPI(title="SciLab REST API", version="1.0.0")

    @app.exception_handler(AuthorizationError)
    async def authorization_error(_: Request, exc: AuthorizationError) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(LookupError)
    async def not_found(_: Request, exc: LookupError) -> Response:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    async def identity(request: Request) -> Identity:
        resolved = await _call(identity_resolver, request)
        if not isinstance(resolved, Identity):
            raise HTTPException(status_code=401, detail="authentication required")
        return _normalize_identity(resolved)

    async def lab_identity(request: Request, lab: str, scope: str) -> Identity:
        current = await identity(request)
        require_lab(current, lab.strip())
        require_scope(current, scope)
        return current

    @app.post("/v1/labs/{lab}/runs", status_code=202)
    async def create_run(
        lab: str,
        body: RunRequest,
        request: Request,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=1),
    ) -> Any:
        current = await lab_identity(request, lab, "runs:write")
        payload = body.model_dump()
        await _call(
            services.admission.check,
            current,
            payload,
            idempotency_key,
        )
        return _json(
            await _call(
                services.run_submission.create,
                current,
                idempotency_key,
                payload,
            )
        )

    @app.get("/v1/labs/{lab}/runs")
    async def list_runs(
        lab: str,
        request: Request,
        state: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        cursor: str | None = None,
    ) -> Any:
        current = await lab_identity(request, lab, "runs:read")
        return _json(
            await _call(
                services.run_search.list,
                current,
                state=state,
                actor=actor,
                since=since,
                cursor=cursor,
            )
        )

    @app.get("/v1/runs/{id}")
    async def get_run(id: str, request: Request) -> Any:
        current = await identity(request)
        require_scope(current, "runs:read")
        return _json(await _call(services.runs.get, current, id))

    @app.get("/v1/runs/{id}/events")
    async def get_events(
        id: str,
        request: Request,
        from_seq: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        current = await identity(request)
        require_scope(current, "runs:read")
        stream = event_streamer(services.events, current, id, from_seq)
        return StreamingResponse(stream, media_type="text/event-stream")

    @app.post("/v1/runs/{id}/stop")
    async def stop_run(id: str, request: Request) -> Any:
        current = await identity(request)
        require_scope(current, "runs:write")
        return _json(await _call(services.runs.stop, current, id))

    @app.post("/v1/runs/{id}/approvals/{approval_id}")
    async def decide_approval(
        id: str,
        approval_id: str,
        body: ApprovalRequest,
        request: Request,
    ) -> Any:
        current = await identity(request)
        require_scope(current, "runs:approve")
        return _json(
            await _call(
                services.approvals.decide_approval,
                current,
                approval_id,
                body.decision,
                note=body.note,
                run_id=id,
            )
        )

    @app.get("/v1/runs/{id}/artifacts")
    async def list_artifacts(id: str, request: Request) -> Any:
        current = await identity(request)
        require_scope(current, "artifacts:read")
        return _json(await _call(services.artifacts.list_for_run, current, id))

    @app.get("/v1/artifacts/{id}")
    async def get_artifact(id: str, request: Request) -> Any:
        current = await identity(request)
        require_scope(current, "artifacts:read")
        artifact = _json(await _call(services.artifacts.get, current, id))
        artifact["url"] = await _call(services.artifacts.presign, current, id)
        return artifact

    @app.post("/v1/labs/{lab}/inputs")
    async def create_input(lab: str, body: dict[str, Any], request: Request) -> Any:
        current = await lab_identity(request, lab, "runs:write")
        return _json(await _call(services.inputs.create, current, body))

    @app.post("/v1/labs/{lab}/ask")
    async def ask(lab: str, body: AskRequest, request: Request) -> Any:
        current = await lab_identity(request, lab, "runs:write")
        return _json(await _call(services.ask.ask, current, body.model_dump()))

    @app.get("/v1/labs/{lab}/skills")
    async def list_skills(lab: str, request: Request) -> Any:
        current = await lab_identity(request, lab, "runs:read")
        return _json(await _call(services.skills.list, current))

    @app.get("/v1/labs/{lab}/api-keys")
    async def list_api_keys(lab: str, request: Request) -> Any:
        current = await lab_identity(request, lab, "lab:admin")
        return _json(await _call(services.api_keys.list, current))

    @app.post("/v1/labs/{lab}/api-keys")
    async def create_api_key(
        lab: str, body: dict[str, Any], request: Request
    ) -> Any:
        current = await lab_identity(request, lab, "lab:admin")
        return _json(await _call(services.api_keys.create, current, body))

    @app.delete("/v1/labs/{lab}/api-keys", status_code=204)
    async def delete_api_key(
        lab: str,
        request: Request,
        key_id: str = Query(min_length=1),
    ) -> Response:
        current = await lab_identity(request, lab, "lab:admin")
        await _call(services.api_keys.delete, current, key_id)
        return Response(status_code=204)

    @app.get("/v1/labs/{lab}/peers")
    async def list_peers(lab: str, request: Request) -> Any:
        current = await lab_identity(request, lab, "lab:admin")
        return _json(await _call(services.peers.list, current))

    @app.post("/v1/labs/{lab}/peers")
    async def create_peer(lab: str, body: dict[str, Any], request: Request) -> Any:
        current = await lab_identity(request, lab, "lab:admin")
        return _json(await _call(services.peers.create, current, body))

    @app.get("/v1/labs/{lab}/usage")
    async def get_usage(
        lab: str,
        request: Request,
        period: Literal["daily", "monthly"],
    ) -> Any:
        current = await lab_identity(request, lab, "runs:read")
        return _json(await _call(services.usage.get, current, period))

    return app
