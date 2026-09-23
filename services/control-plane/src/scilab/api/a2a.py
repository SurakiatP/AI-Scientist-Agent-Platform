"""Lab-scoped A2A v1 JSON-RPC facade over the canonical Run resource."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers.request_handler import RequestHandler
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.routes.common import DefaultServerCallContextBuilder
from a2a.types import (
    AgentCard,
    InvalidParamsError,
    ListTasksResponse,
    Task,
    TaskPushNotificationConfig,
    TaskNotFoundError,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    UnsupportedOperationError,
)
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict, ParseDict

from scilab.identity import AuthenticationError, Identity, authenticate_a2a_peer
from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.sse import stream_events
from scilab.tenancy import AuthorizationError, require_lab, require_scope


_STATES = {
    "queued": "TASK_STATE_SUBMITTED",
    "running": "TASK_STATE_WORKING",
    "awaiting_approval": "TASK_STATE_INPUT_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "cancelled": "TASK_STATE_CANCELED",
}


def state_for_run(state: str) -> str:
    return _STATES[str(state)]


async def _call(function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = function(*args, **kwargs)
    return await result if inspect.isawaitable(result) else result


def _task(run: Any, context_id: str | None = None) -> Task:
    run_id = str(run.id)
    return Task(
        id=run_id,
        context_id=context_id or getattr(run, "context_id", None) or run_id,
        status=TaskStatus(state=TaskState.Value(state_for_run(run.state))),
    )


def _https_callback(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("HTTPS callback URL required")
    try:
        address = ip_address(parsed.hostname)
    except ValueError:
        if parsed.hostname.lower() == "localhost" or parsed.hostname.lower().endswith(".localhost"):
            raise ValueError("HTTPS callback must not target localhost") from None
    else:
        if not address.is_global:
            raise ValueError("HTTPS callback must not target a private address")


async def send_push_notification(
    url: str,
    body: bytes,
    secret: bytes,
    post: Callable[[str, bytes, dict[str, str]], Any],
) -> None:
    """Send a peer-registered callback; caller resolves its secret from Vault."""
    _https_callback(url)
    if not isinstance(secret, bytes) or not secret:
        raise ValueError("signing secret required")
    if not isinstance(body, bytes):
        raise TypeError("body must be raw bytes")
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    await _call(post, url, body, {"X-A2A-Signature": f"sha256={signature}"})


class PushDispatcher:
    """Deliver persisted Run state updates to registered peers using Vault secrets."""

    def __init__(self, configs: Any, vault: Any, post: Callable[..., Any]) -> None:
        self.configs = configs
        self.vault = vault
        self.post = post

    async def dispatch(self, event: Any) -> None:
        if event.type != "run.state":
            return
        state = event.payload.to
        failures: list[tuple[str, Exception]] = []
        for config in await _call(self.configs.for_run, event.lab_id, event.run_id):
            peer_name = str(config.get("peer_name", "<unknown>"))
            try:
                secret_ref = config["secret_ref"]
                if not secret_ref.startswith(f"vault://{event.lab_id}/a2a/{peer_name}/"):
                    raise ValueError("push secret must belong to authenticated Lab and peer")
                update = TaskStatusUpdateEvent(
                    task_id=event.run_id,
                    context_id=config["context_id"],
                    status=TaskStatus(state=TaskState.Value(state_for_run(state))),
                )
                body = json.dumps(
                    MessageToDict(update), separators=(",", ":"), ensure_ascii=False
                ).encode("utf-8")
                secret = await _call(self.vault.read, secret_ref)
                await send_push_notification(config["url"], body, secret, self.post)
            except Exception as exc:
                failures.append((peer_name, exc))
        if failures:
            error = RuntimeError("A2A push delivery failed for one or more peers")
            error.failures = failures
            raise error


class PushConfigStore:
    """Persist peer callbacks, never the per-peer signing secret itself."""

    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def register(self, identity: Identity, config: TaskPushNotificationConfig) -> TaskPushNotificationConfig:
        require_scope(identity, "runs:read")
        if not identity.principal.startswith("peer:"):
            raise AuthorizationError("A2A peer required")
        _https_callback(config.url)
        peer_name = identity.principal.removeprefix("peer:")
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
            cursor.execute(
                "SELECT push_secret_ref FROM a2a_peers WHERE lab_id = %s AND peer_name = %s",
                (identity.lab_id, peer_name),
            )
            row = cursor.fetchone()
            secret_ref = row[0] if row else None
            if not isinstance(secret_ref, str) or not secret_ref.startswith(
                f"vault://{identity.lab_id}/a2a/{peer_name}/"
            ):
                raise ValueError("peer needs a same-Lab Vault push signing reference")
            config_id = config.id or str(uuid4())
            cursor.execute(
                "INSERT INTO a2a_push_callbacks (id, lab_id, run_id, peer_name, url) "
                "VALUES (%s, %s, %s, %s, %s)",
                (config_id, identity.lab_id, config.task_id, peer_name, config.url),
            )
        return TaskPushNotificationConfig(id=config_id, task_id=config.task_id, url=config.url)

    def for_run(self, lab_id: str, run_id: str) -> list[dict[str, str]]:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, lab_id))
            cursor.execute(
                "SELECT c.url, c.peer_name, p.push_secret_ref, COALESCE(r.context_id, r.id) "
                "FROM a2a_push_callbacks c "
                "JOIN a2a_peers p ON p.lab_id = c.lab_id AND p.peer_name = c.peer_name "
                "JOIN runs r ON r.lab_id = c.lab_id AND r.id = c.run_id "
                "WHERE c.lab_id = %s AND c.run_id = %s",
                (lab_id, run_id),
            )
            return [
                {"url": url, "peer_name": peer_name, "secret_ref": secret_ref,
                 "context_id": context_id}
                for url, peer_name, secret_ref, context_id in cursor.fetchall()
            ]


class A2ARunSubmission:
    """Bind an A2A message to the canonical Run and its Hermes session."""

    def __init__(
        self,
        runs: Any,
        cycle_for_lab: Callable[[str], Any],
        *,
        admission: Any | None = None,
        events: Any | None = None,
    ) -> None:
        self.runs = runs
        self.cycle_for_lab = cycle_for_lab
        self.admission = admission
        self.events = events

    async def create(self, identity: Identity, message: Any, context_id: str) -> Any:
        require_scope(identity, "runs:write")
        if not context_id or not context_id.strip():
            raise ValueError("contextId required")
        goal = "\n".join(
            part.text if part.text else json.dumps(MessageToDict(part.data), ensure_ascii=False)
            for part in message.parts
            if part.text or part.HasField("data")
        ).strip()
        if not goal:
            raise InvalidParamsError("A2A message needs text or JSON content")
        key = message.message_id or str(uuid4())
        cycle = self.cycle_for_lab(identity.lab_id)
        require_lab(identity, cycle.lab_id)
        if self.admission is None or self.events is None:
            raise RuntimeError("A2A admission and event services are required")
        await _call(self.admission.check, identity, MessageToDict(message), key)
        run = await _call(self.runs.create, identity, key, context_id=context_id)
        if run.context_id != context_id:
            raise ValueError("messageId already belongs to a different contextId")
        if run.hermes_run_id:
            return run
        hermes_run_id = await _call(
            cycle.run, goal, idempotency_key=key, session_key=context_id
        )
        updated = await _call(
            self.runs.transition, identity, run.id, "running", hermes_run_id=hermes_run_id
        )
        await _call(
            self.events.publish_event, identity, run.id, "run.state",
            {"from": str(run.state), "to": str(updated.state), "reason": updated.reason or ""},
            "run-service",
        )
        return updated


class _ContextBuilder(DefaultServerCallContextBuilder):
    def build(self, request: Request) -> ServerCallContext:
        context = super().build(request)
        context.state["identity"] = request.state.identity
        context.state["lab"] = request.path_params["lab"]
        return context


class _Handler(RequestHandler):
    def __init__(self, services: Any, event_streamer: Callable[..., Any]) -> None:
        self.services = services
        self.event_streamer = event_streamer

    @staticmethod
    def _identity(context: ServerCallContext, scope: str) -> Identity:
        identity = context.state["identity"]
        require_lab(identity, context.state["lab"])
        require_scope(identity, scope)
        return identity

    async def on_message_send(self, params: Any, context: ServerCallContext) -> Task:
        identity = self._identity(context, "runs:write")
        message = params.message
        context_id = message.context_id or str(uuid4())
        run = await _call(self.services.run_submission.create, identity, message, context_id)
        return _task(run, context_id)

    async def on_get_task(self, params: Any, context: ServerCallContext) -> Task:
        identity = self._identity(context, "runs:read")
        try:
            return _task(await _call(self.services.runs.get, identity, params.id))
        except LookupError as exc:
            raise TaskNotFoundError from exc

    async def on_list_tasks(self, params: Any, context: ServerCallContext) -> ListTasksResponse:
        identity = self._identity(context, "runs:read")
        runs = await _call(self.services.runs.list, identity)
        return ListTasksResponse(tasks=[_task(run) for run in runs])

    async def on_cancel_task(self, params: Any, context: ServerCallContext) -> Task:
        identity = self._identity(context, "runs:write")
        try:
            self._identity(context, "runs:read")
            run = await _call(self.services.runs.get, identity, params.id)
            if run.hermes_run_id:
                cycle = self.services.run_submission.cycle_for_lab(identity.lab_id)
                require_lab(identity, cycle.lab_id)
                await _call(cycle.client.stop, identity.lab_id, run.hermes_run_id)
            stopped = await _call(self.services.runs.stop, identity, params.id)
            await _call(
                self.services.events.publish_event, identity, params.id, "run.state",
                {"from": str(run.state), "to": str(stopped.state), "reason": stopped.reason or ""},
                "run-service",
            )
            return _task(stopped)
        except LookupError as exc:
            raise TaskNotFoundError from exc

    async def _updates(
        self, identity: Identity, run_id: str, context_id: str
    ) -> AsyncIterator[TaskStatusUpdateEvent]:
        frames = self.event_streamer(self.services.events, identity, run_id, 0)
        async for frame in frames:
            if "event: run.state\n" not in frame:
                continue
            for line in frame.splitlines():
                if not line.startswith("data: "):
                    continue
                data = json.loads(line[6:])
                payload = data.get("payload", {})
                state = data.get("state") or payload.get("to")
                if state in _STATES:
                    yield TaskStatusUpdateEvent(
                        task_id=run_id,
                        context_id=context_id,
                        status=TaskStatus(state=TaskState.Value(state_for_run(state))),
                    )

    async def on_message_send_stream(
        self, params: Any, context: ServerCallContext
    ) -> AsyncIterator[TaskStatusUpdateEvent]:
        identity = self._identity(context, "runs:read")
        task = await self.on_message_send(params, context)
        async for update in self._updates(identity, task.id, task.context_id):
            yield update

    async def on_subscribe_to_task(
        self, params: Any, context: ServerCallContext
    ) -> AsyncIterator[TaskStatusUpdateEvent]:
        identity = self._identity(context, "runs:read")
        task = await self.on_get_task(params, context)
        async for update in self._updates(identity, task.id, task.context_id):
            yield update

    async def on_create_task_push_notification_config(self, params: Any, context: ServerCallContext) -> Any:
        identity = self._identity(context, "runs:read")
        try:
            _https_callback(params.url)
        except ValueError as exc:
            raise InvalidParamsError(str(exc)) from exc
        await _call(self.services.runs.get, identity, params.task_id)
        return await _call(self.services.push_configs.register, identity, params)

    async def on_get_task_push_notification_config(self, params: Any, context: ServerCallContext) -> Any:
        raise UnsupportedOperationError

    async def on_list_task_push_notification_configs(self, params: Any, context: ServerCallContext) -> Any:
        raise UnsupportedOperationError

    async def on_delete_task_push_notification_config(self, params: Any, context: ServerCallContext) -> Any:
        raise UnsupportedOperationError

    async def on_get_extended_agent_card(self, params: Any, context: ServerCallContext) -> Any:
        raise UnsupportedOperationError


def create_app(
    services: Any,
    peers: Iterable[Mapping[str, Any]] | Callable[[str], Any],
    lab_cards: Mapping[str, Mapping[str, Any]],
    *,
    event_streamer: Callable[..., Any] = stream_events,
    push_dispatcher: PushDispatcher | None = None,
) -> FastAPI:
    app = FastAPI(title="SciLab A2A", version="1.0")
    peer_records = tuple(peers) if not callable(peers) else None
    if push_dispatcher is not None:
        services.events.push_notification = push_dispatcher.dispatch

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Callable[..., Any]) -> Any:
        if not request.url.path.startswith("/labs/"):
            return await call_next(request)
        if request.url.scheme != "https":
            return JSONResponse({"detail": "TLS required"}, status_code=403)
        lab = request.url.path.split("/", 3)[2]
        if lab not in lab_cards:
            return JSONResponse({"detail": "Lab not found"}, status_code=404)
        if request.url.path.endswith("/.well-known/agent-card.json"):
            return await call_next(request)
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return JSONResponse({"detail": "Bearer token required"}, status_code=401)
        try:
            secret = authorization[7:]
            if peer_records is None:
                record = await _call(peers, secret)
                records = (record,) if record is not None else ()
            else:
                records = peer_records
            identity = authenticate_a2a_peer(secret, records)
        except AuthenticationError:
            return JSONResponse({"detail": "Peer rejected"}, status_code=401)
        try:
            require_lab(identity, lab)
        except AuthorizationError:
            return JSONResponse({"detail": "Lab denied"}, status_code=403)
        try:
            method = (await request.json()).get("method")
        except (ValueError, AttributeError):
            method = None
        scope = (
            "runs:write" if method in {"SendMessage", "SendStreamingMessage", "CancelTask"}
            else "runs:read" if method in {
                "GetTask", "ListTasks", "SubscribeToTask", "CreateTaskPushNotificationConfig"
            } else None
        )
        if scope:
            try:
                require_scope(identity, scope)
                if method in {"SendStreamingMessage", "CancelTask"}:
                    require_scope(identity, "runs:read")
            except AuthorizationError:
                return JSONResponse({"detail": "Scope denied"}, status_code=403)
        request.state.identity = identity
        return await call_next(request)

    @app.get("/labs/{lab}/.well-known/agent-card.json")
    async def agent_card(lab: str, request: Request) -> dict[str, Any]:
        card = lab_cards[lab]
        url = str(request.base_url).rstrip("/") + f"/labs/{lab}"
        v1_card = {
            "name": card["name"],
            "description": card["description"],
            "version": "1.0",
            "supportedInterfaces": [
                {"url": url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
            ],
            "capabilities": {
                "streaming": True,
                "pushNotifications": True,
            },
            "securitySchemes": {
                "bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}}
            },
            "securityRequirements": [{"schemes": {"bearer": {"list": []}}}],
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain", "text/markdown", "application/json"],
            "skills": list(card.get("skills", [
                {"id": "research_task", "name": "Run research task", "tags": ["research"]},
                {"id": "ask_lab", "name": "Ask lab", "tags": ["qa"]},
                {"id": "literature_review", "name": "Literature review", "tags": ["literature"]},
            ])),
        }
        return MessageToDict(ParseDict(v1_card, AgentCard()))

    app.router.routes.extend(
        create_jsonrpc_routes(_Handler(services, event_streamer), "/labs/{lab}", _ContextBuilder())
    )
    return app
