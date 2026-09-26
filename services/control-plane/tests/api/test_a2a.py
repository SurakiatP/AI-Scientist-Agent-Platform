"""TOR A.6.3: public A2A contract for one Lab."""

import asyncio
import hashlib
import hmac
import importlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from a2a.types import AgentCard, Message, Part, Role, TaskPushNotificationConfig
from google.protobuf.json_format import MessageToDict, ParseDict
from jsonschema import validate

from scilab.identity import Identity, credential_digest


def request(app: object, method: str, path: str, **kwargs: object) -> httpx.Response:
    base_url = kwargs.pop("base_url", "https://a2a.scilab.example")
    if method == "POST" and path.startswith("/labs/"):
        kwargs["headers"] = {"A2A-Version": "1.0", **kwargs.get("headers", {})}

    async def send() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=base_url
        ) as client:
            return await client.request(method, path, **kwargs)

    return asyncio.run(send())


def test_agent_card_is_scoped_to_lab_and_advertises_tor_modes() -> None:
    create_app = importlib.import_module("scilab.api.a2a").create_app
    app = create_app(
        services=SimpleNamespace(),
        peers=[],
        lab_cards={"lab-bio": {"name": "SciLab — Lab Bio 01", "description": "Research lab"}},
    )

    response = request(app, "GET", "/labs/lab-bio/.well-known/agent-card.json")

    assert response.status_code == 200
    card = response.json()
    assert card["name"] == "SciLab — Lab Bio 01"
    assert "url" not in card
    assert "security" not in card
    assert "stateTransitionHistory" not in card["capabilities"]
    parsed = ParseDict(card, AgentCard())
    assert parsed.name == "SciLab — Lab Bio 01"
    assert parsed.supported_interfaces[0].url == "https://a2a.scilab.example/labs/lab-bio"
    assert parsed.security_schemes["bearer"].http_auth_security_scheme.scheme.lower() == "bearer"
    assert parsed.security_requirements[0].schemes["bearer"].list == []
    assert card["defaultInputModes"] == ["text/plain", "application/json"]
    assert card["defaultOutputModes"] == ["text/plain", "text/markdown", "application/json"]
    assert card["supportedInterfaces"] == [{
        "url": "https://a2a.scilab.example/labs/lab-bio",
        "protocolBinding": "JSONRPC", "protocolVersion": "1.0"
    }]
    assert {skill["id"] for skill in card["skills"]} == {
        "research_task", "ask_lab", "literature_review"
    }
    ask_lab_skill = next(skill for skill in card["skills"] if skill["id"] == "ask_lab")
    assert "requires:artifacts:read" in ask_lab_skill["tags"]
    schema = json.loads((Path(__file__).parents[4] / "contracts/a2a/agent-card.schema.json").read_text())
    validate(card, schema)
    assert request(app, "GET", "/labs/other/.well-known/agent-card.json").status_code == 404


def test_lab_cards_may_be_a_callable_looked_up_per_request() -> None:
    create_app = importlib.import_module("scilab.api.a2a").create_app
    calls: list[str] = []

    def lookup(lab: str) -> dict[str, str] | None:
        calls.append(lab)
        return {"name": "Bio", "description": "Research lab"} if lab == "lab-bio" else None

    app = create_app(services=SimpleNamespace(), peers=[], lab_cards=lookup)

    found = request(app, "GET", "/labs/lab-bio/.well-known/agent-card.json")
    missing = request(app, "GET", "/labs/other/.well-known/agent-card.json")

    assert found.status_code == 200
    assert found.json()["name"] == "Bio"
    assert missing.status_code == 404
    assert calls == ["lab-bio", "lab-bio", "other"]


def test_a2a_app_mounted_under_parent_app_enforces_auth_and_tls() -> None:
    from fastapi import FastAPI

    create_app = importlib.import_module("scilab.api.a2a").create_app
    inner = create_app(
        services=SimpleNamespace(),
        peers=[{"peer_name": "peer-a", "lab_id": "lab-bio",
                "secret_hash": credential_digest("registered-peer"),
                "scopes": ["runs:read"]}],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )
    parent = FastAPI()
    parent.mount("/a2a", inner)
    body = {"jsonrpc": "2.0", "id": "get-1", "method": "GetTask", "params": {"id": "run-1"}}

    unauthenticated = request(parent, "POST", "/a2a/labs/lab-bio", json=body)
    assert unauthenticated.status_code == 401

    insecure = request(
        parent, "POST", "/a2a/labs/lab-bio", json=body,
        headers={"Authorization": "Bearer registered-peer"},
        base_url="http://a2a.scilab.example",
    )
    assert insecure.status_code == 403

    assert request(
        parent, "GET", "/a2a/labs/lab-bio/.well-known/agent-card.json"
    ).status_code == 200


def test_registered_peer_send_message_creates_lab_run_with_context() -> None:
    class Submission:
        def __init__(self) -> None:
            self.calls: list[tuple[object, object, object]] = []

        def create(
            self, identity: object, message: object, context_id: str, params: object = None
        ) -> object:
            self.calls.append((identity, message, context_id))
            return SimpleNamespace(id="run-1", state="queued")

    create_app = importlib.import_module("scilab.api.a2a").create_app
    submission = Submission()
    app = create_app(
        services=SimpleNamespace(run_submission=submission),
        peers=[{
            "peer_name": "peer-a",
            "lab_id": "lab-bio",
            "secret_hash": credential_digest("registered-peer"),
            "scopes": ["runs:read", "runs:write"],
        }],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )
    body = {
        "jsonrpc": "2.0", "id": "req-1", "method": "SendMessage",
        "params": {"message": {
            "messageId": "message-1", "contextId": "context-1", "role": "ROLE_USER",
            "parts": [{"text": "Review literature"}],
        }},
    }

    response = request(
        app, "POST", "/labs/lab-bio", json=body,
        headers={"Authorization": "Bearer registered-peer"},
    )

    assert response.status_code == 200
    assert response.json()["result"]["task"]["id"] == "run-1"
    identity, message, context_id = submission.calls[0]
    assert identity.lab_id == "lab-bio"
    assert identity.principal == "peer:peer-a"
    assert context_id == "context-1"
    assert "Review literature" in str(message)

    denied = request(app, "POST", "/labs/lab-bio", json=body,
                     headers={"Authorization": "Bearer unknown"})
    assert denied.status_code == 401
    assert len(submission.calls) == 1
    assert request(app, "POST", "/labs/other", json=body,
                   headers={"Authorization": "Bearer registered-peer"}).status_code in {403, 404}
    assert len(submission.calls) == 1


def test_send_message_with_ask_lab_skill_calls_ask_and_creates_no_run() -> None:
    ask_calls: list[tuple[object, object]] = []
    run_calls: list[str] = []

    class Ask:
        async def ask(self, identity: object, body: dict[str, str]) -> dict[str, object]:
            ask_calls.append((identity, body))
            return {"answer": "42", "sources": []}

    class Submission:
        def create(self, *args: object) -> object:
            run_calls.append("create")
            raise AssertionError("ask_lab must not create a Run")

    create_app = importlib.import_module("scilab.api.a2a").create_app
    app = create_app(
        services=SimpleNamespace(ask=Ask(), run_submission=Submission()),
        peers=[{
            "peer_name": "peer-a", "lab_id": "lab-bio",
            "secret_hash": credential_digest("registered-peer"),
            "scopes": ["runs:read", "runs:write", "artifacts:read"],
        }],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )
    body = {
        "jsonrpc": "2.0", "id": "ask-1", "method": "SendMessage",
        "params": {"message": {
            "messageId": "msg-1", "contextId": "context-1", "role": "ROLE_USER",
            "parts": [{"text": "What is the result?"}],
            "metadata": {"skill": "ask_lab"},
        }},
    }

    response = request(app, "POST", "/labs/lab-bio", json=body,
                       headers={"Authorization": "Bearer registered-peer"})

    assert response.status_code == 200
    message = response.json()["result"]["message"]
    assert message["parts"][0]["text"] == "42"
    assert message["contextId"] == "context-1"
    assert run_calls == []
    assert len(ask_calls) == 1
    identity, ask_body = ask_calls[0]
    assert identity.lab_id == "lab-bio"
    assert identity.principal == "peer:peer-a"
    assert ask_body == {"question": "What is the result?"}


def test_send_message_with_ask_lab_skill_requires_artifacts_read_scope() -> None:
    ask_calls: list[str] = []

    class Ask:
        async def ask(self, identity: object, body: dict[str, str]) -> dict[str, object]:
            ask_calls.append("ask")
            return {"answer": "42", "sources": []}

    create_app = importlib.import_module("scilab.api.a2a").create_app
    app = create_app(
        services=SimpleNamespace(ask=Ask()),
        peers=[{
            "peer_name": "peer-a", "lab_id": "lab-bio",
            "secret_hash": credential_digest("registered-peer"),
            "scopes": ["runs:read", "runs:write"],
        }],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )
    body = {
        "jsonrpc": "2.0", "id": "ask-1", "method": "SendMessage",
        "params": {"message": {
            "messageId": "msg-1", "contextId": "context-1", "role": "ROLE_USER",
            "parts": [{"text": "What is the result?"}],
            "metadata": {"skill": "ask_lab"},
        }},
    }

    response = request(app, "POST", "/labs/lab-bio", json=body,
                       headers={"Authorization": "Bearer registered-peer"})

    assert response.status_code == 403
    assert ask_calls == []


def test_peer_registered_after_app_start_is_accepted_by_lookup() -> None:
    class Submission:
        def create(
            self, identity: object, message: object, context_id: str, params: object = None
        ) -> object:
            return SimpleNamespace(id="run-1", state="queued", context_id=context_id)

    records: dict[str, dict[str, object]] = {}
    create_app = importlib.import_module("scilab.api.a2a").create_app
    app = create_app(
        services=SimpleNamespace(run_submission=Submission()),
        peers=lambda token: records.get(token),
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )
    records["registered-peer"] = {
        "peer_name": "peer-a", "lab_id": "lab-bio",
        "secret_hash": credential_digest("registered-peer"),
        "scopes": ["runs:read", "runs:write"],
    }
    response = request(
        app, "POST", "/labs/lab-bio",
        json={"jsonrpc": "2.0", "id": "send-1", "method": "SendMessage",
              "params": {"message": {"messageId": "msg-1", "contextId": "context-1",
                                     "role": "ROLE_USER", "parts": [{"text": "Research"}]}}},
        headers={"Authorization": "Bearer registered-peer"},
    )

    assert response.status_code == 200
    assert response.json()["result"]["task"]["id"] == "run-1"


def test_peer_requires_tls_and_run_scope() -> None:
    class Runs:
        def get(self, identity: object, run_id: str) -> object:
            raise AssertionError("unauthorized peer reached RunService")

    create_app = importlib.import_module("scilab.api.a2a").create_app
    app = create_app(
        services=SimpleNamespace(runs=Runs()),
        peers=[{"peer_name": "writer", "lab_id": "lab-bio",
                "secret_hash": credential_digest("write-only"),
                "scopes": ["runs:write"]}],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )
    body = {"jsonrpc": "2.0", "id": "get-1", "method": "GetTask", "params": {"id": "run-1"}}
    headers = {"Authorization": "Bearer write-only"}

    assert request(app, "POST", "/labs/lab-bio", json=body, headers=headers,
                   base_url="http://a2a.scilab.example").status_code == 403
    scoped = request(app, "POST", "/labs/lab-bio", json=body, headers=headers)
    assert scoped.status_code != 500
    assert scoped.status_code == 403 or "error" in scoped.json()
    if scoped.status_code == 200:
        assert scoped.json()["error"]["code"] != -32603


def test_write_only_peer_cannot_stream_or_cancel_before_read_scope_check() -> None:
    create_app = importlib.import_module("scilab.api.a2a").create_app
    calls: list[str] = []

    class Submission:
        def create(self, *args: object) -> object:
            calls.append("create")
            return SimpleNamespace(id="run-1", state="queued")

    class Runs:
        def stop(self, *args: object) -> object:
            calls.append("stop")
            return SimpleNamespace(id="run-1", state="cancelled")

    app = create_app(
        services=SimpleNamespace(run_submission=Submission(), runs=Runs()),
        peers=[{"peer_name": "peer-a", "lab_id": "lab-bio",
                "secret_hash": credential_digest("write-only"), "scopes": ["runs:write"]}],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )
    headers = {"Authorization": "Bearer write-only"}
    for method, params in (
        ("SendStreamingMessage", {"message": {"messageId": "msg-1", "role": "ROLE_USER",
                                             "parts": [{"text": "Research"}]}}),
        ("CancelTask", {"id": "run-1"}),
    ):
        response = request(app, "POST", "/labs/lab-bio", headers=headers,
                           json={"jsonrpc": "2.0", "id": method, "method": method,
                                 "params": params})
        assert response.status_code == 403
    assert calls == []


def test_run_states_map_to_tor_a2a_states() -> None:
    state_for_run = importlib.import_module("scilab.api.a2a").state_for_run

    assert {state: state_for_run(state) for state in (
        "queued", "running", "awaiting_approval", "completed", "failed", "cancelled"
    )} == {
        "queued": "TASK_STATE_SUBMITTED",
        "running": "TASK_STATE_WORKING",
        "awaiting_approval": "TASK_STATE_INPUT_REQUIRED",
        "completed": "TASK_STATE_COMPLETED",
        "failed": "TASK_STATE_FAILED",
        "cancelled": "TASK_STATE_CANCELED",
    }


def test_get_list_and_cancel_use_the_same_run_resource() -> None:
    class Runs:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str]] = []

        def get(self, identity: object, run_id: str) -> object:
            self.calls.append(("get", identity.lab_id, run_id))
            return SimpleNamespace(id=run_id, state="running", context_id=None, hermes_run_id=None)

        def list(self, identity: object) -> list[object]:
            self.calls.append(("list", identity.lab_id, ""))
            return [SimpleNamespace(id="run-1", state="running")]

        def stop(self, identity: object, run_id: str) -> object:
            self.calls.append(("stop", identity.lab_id, run_id))
            return SimpleNamespace(id=run_id, state="cancelled", reason="stopped")

    create_app = importlib.import_module("scilab.api.a2a").create_app
    runs = Runs()
    app = create_app(
        services=SimpleNamespace(
            runs=runs,
            events=SimpleNamespace(publish_event=lambda *args: None),
        ),
        peers=[{"peer_name": "peer-a", "lab_id": "lab-bio",
                "secret_hash": credential_digest("registered-peer"),
                "scopes": ["runs:read", "runs:write"]}],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )

    def rpc(method: str, params: dict[str, str]) -> dict[str, object]:
        response = request(app, "POST", "/labs/lab-bio",
                           json={"jsonrpc": "2.0", "id": method, "method": method, "params": params},
                           headers={"Authorization": "Bearer registered-peer"})
        assert response.status_code == 200
        return response.json()["result"]

    existing = rpc("GetTask", {"id": "run-1"})
    assert existing["status"]["state"] == "TASK_STATE_WORKING"
    assert existing["contextId"] == "run-1"
    assert rpc("ListTasks", {})["tasks"][0]["id"] == "run-1"
    assert rpc("CancelTask", {"id": "run-1"})["status"]["state"] == "TASK_STATE_CANCELED"
    assert runs.calls == [
        ("get", "lab-bio", "run-1"), ("list", "lab-bio", ""),
        ("get", "lab-bio", "run-1"),
        ("stop", "lab-bio", "run-1"),
    ]


def test_subscribe_to_task_streams_run_state_over_sse() -> None:
    class Runs:
        def get(self, identity: object, run_id: str) -> object:
            assert (identity.lab_id, run_id) == ("lab-bio", "run-1")
            return SimpleNamespace(id=run_id, state="running")

    async def events(service: object, identity: object, run_id: str, from_seq: int = 0):
        assert (identity.lab_id, run_id, from_seq) == ("lab-bio", "run-1", 0)
        yield 'id: 1\nevent: run.state\ndata: {"state":"running"}\n\n'

    create_app = importlib.import_module("scilab.api.a2a").create_app
    app = create_app(
        services=SimpleNamespace(runs=Runs(), events=object()),
        peers=[{"peer_name": "peer-a", "lab_id": "lab-bio",
                "secret_hash": credential_digest("registered-peer"),
                "scopes": ["runs:read", "runs:write"]}],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
        event_streamer=events,
    )
    response = request(
        app, "POST", "/labs/lab-bio",
        json={"jsonrpc": "2.0", "id": "sub-1", "method": "SubscribeToTask",
              "params": {"id": "run-1"}},
        headers={"Authorization": "Bearer registered-peer"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "TASK_STATE_WORKING" in response.text


def test_send_streaming_message_submits_then_streams() -> None:
    class Submission:
        def create(
            self, identity: object, message: object, context_id: str, params: object = None
        ) -> object:
            assert (identity.lab_id, context_id) == ("lab-bio", "context-1")
            return SimpleNamespace(id="run-1", state="queued")

    async def events(service: object, identity: object, run_id: str, from_seq: int = 0):
        assert (identity.lab_id, run_id) == ("lab-bio", "run-1")
        yield 'id: 1\nevent: run.state\ndata: {"state":"running"}\n\n'

    create_app = importlib.import_module("scilab.api.a2a").create_app
    app = create_app(
        services=SimpleNamespace(run_submission=Submission(), events=object()),
        peers=[{"peer_name": "peer-a", "lab_id": "lab-bio",
                "secret_hash": credential_digest("registered-peer"),
                "scopes": ["runs:read", "runs:write"]}],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
        event_streamer=events,
    )
    response = request(
        app, "POST", "/labs/lab-bio",
        json={"jsonrpc": "2.0", "id": "stream-1", "method": "SendStreamingMessage",
              "params": {"message": {"messageId": "message-1", "contextId": "context-1",
                                     "role": "ROLE_USER", "parts": [{"text": "Research"}]} }},
        headers={"Authorization": "Bearer registered-peer"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "TASK_STATE_WORKING" in response.text


def test_push_notification_signs_exact_body_and_rejects_http_callback() -> None:
    send_push_notification = importlib.import_module("scilab.api.a2a").send_push_notification
    calls: list[tuple[str, bytes, dict[str, str]]] = []

    async def post(url: str, body: bytes, headers: dict[str, str]) -> None:
        calls.append((url, body, headers))

    body = json.dumps({"task": {"id": "run-1"}}, separators=(",", ":")).encode()
    asyncio.run(send_push_notification(
        "https://peer.example/webhook", body, b"test-signing-secret", post
    ))
    assert len(calls) == 1
    url, sent_body, headers = calls[0]
    assert url == "https://peer.example/webhook"
    assert sent_body == body
    assert headers["X-A2A-Signature"] == "sha256=" + hmac.new(
        b"test-signing-secret", body, hashlib.sha256
    ).hexdigest()

    import pytest

    with pytest.raises(ValueError, match="HTTPS"):
        asyncio.run(send_push_notification(
            "http://peer.example/webhook", body, b"test-signing-secret", post
        ))
    assert len(calls) == 1


def test_registered_push_dispatch_uses_lab_peer_vault_secret() -> None:
    PushDispatcher = importlib.import_module("scilab.api.a2a").PushDispatcher

    class Configs:
        def for_run(self, lab_id: str, run_id: str) -> list[dict[str, str]]:
            assert (lab_id, run_id) == ("lab-bio", "run-1")
            return [{"url": "https://peer.example/webhook", "peer_name": "peer-a",
                     "context_id": "context-1",
                     "secret_ref": "vault://lab-bio/a2a/peer-a/push"}]

    class Vault:
        def __init__(self) -> None:
            self.refs: list[str] = []

        async def read(self, ref: str) -> bytes:
            self.refs.append(ref)
            return b"test-vault-secret"

    posts: list[tuple[str, bytes, dict[str, str]]] = []

    async def post(url: str, body: bytes, headers: dict[str, str]) -> None:
        posts.append((url, body, headers))

    vault = Vault()
    dispatcher = PushDispatcher(Configs(), vault, post)
    event = SimpleNamespace(type="run.state", lab_id="lab-bio", run_id="run-1",
                            payload=SimpleNamespace(to="running"))
    asyncio.run(dispatcher.dispatch(event))

    assert vault.refs == ["vault://lab-bio/a2a/peer-a/push"]
    assert len(posts) == 1
    url, body, headers = posts[0]
    assert url == "https://peer.example/webhook"
    assert json.loads(body)["taskId"] == "run-1"
    assert json.loads(body)["contextId"] == "context-1"
    assert json.loads(body)["status"]["state"] == "TASK_STATE_WORKING"
    assert headers["X-A2A-Signature"] == "sha256=" + hmac.new(
        b"test-vault-secret", body, hashlib.sha256
    ).hexdigest()


def test_bind_push_dispatcher_wires_dispatch_to_event_service() -> None:
    a2a = importlib.import_module("scilab.api.a2a")
    events = SimpleNamespace(push_notification=None)
    dispatcher = SimpleNamespace(dispatch=lambda event: None)

    a2a.bind_push_dispatcher(events, dispatcher)

    assert events.push_notification is dispatcher.dispatch


def test_create_app_does_not_wire_push_notification_at_construction() -> None:
    a2a = importlib.import_module("scilab.api.a2a")
    events = SimpleNamespace(push_notification=None)

    a2a.create_app(
        services=SimpleNamespace(events=events), peers=[],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )

    assert events.push_notification is None


def test_registered_peer_can_register_only_https_task_callback() -> None:
    class Runs:
        def get(self, identity: object, run_id: str) -> object:
            assert (identity.lab_id, run_id) == ("lab-bio", "run-1")
            return SimpleNamespace(id=run_id, state="running")

    class Configs:
        def __init__(self) -> None:
            self.peer_names: list[str] = []

        def register(self, identity: object, config: object) -> object:
            self.peer_names.append(identity.principal)
            return config

    create_app = importlib.import_module("scilab.api.a2a").create_app
    configs = Configs()
    app = create_app(
        services=SimpleNamespace(runs=Runs(), push_configs=configs),
        peers=[{"peer_name": "peer-a", "lab_id": "lab-bio",
                "secret_hash": credential_digest("registered-peer"),
                "scopes": ["runs:read", "runs:write"]}],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )

    def register(url: str) -> httpx.Response:
        return request(
            app, "POST", "/labs/lab-bio",
            json={"jsonrpc": "2.0", "id": "push-1",
                  "method": "CreateTaskPushNotificationConfig",
                  "params": {"taskId": "run-1", "url": url}},
            headers={"Authorization": "Bearer registered-peer"},
        )

    accepted = register("https://peer.example/webhook")
    assert accepted.status_code == 200
    assert accepted.json()["result"]["url"] == "https://peer.example/webhook"
    assert configs.peer_names == ["peer:peer-a"]
    rejected = register("http://peer.example/webhook")
    assert rejected.status_code == 200
    assert rejected.json()["error"]["code"] != -32603
    assert configs.peer_names == ["peer:peer-a"]


def test_push_config_store_persists_only_vault_reference_in_lab_scope() -> None:
    PushConfigStore = importlib.import_module("scilab.api.a2a").PushConfigStore

    class Connection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []
            self.rows: list[object] = []

        def transaction(self) -> object:
            return self

        def cursor(self) -> object:
            return self

        def __enter__(self) -> object:
            return self

        def __exit__(self, *_: object) -> None:
            pass

        def execute(self, sql: str, params: object) -> None:
            self.calls.append((sql, params))
            if "SELECT push_secret_ref" in sql:
                self.rows = [("vault://lab-bio/a2a/peer-a/push",)]
            elif "SELECT c.url" in sql:
                self.rows = [("https://peer.example/webhook", "peer-a",
                              "vault://lab-bio/a2a/peer-a/push", "context-1")]

        def fetchone(self) -> object:
            return self.rows[0] if self.rows else None

        def fetchall(self) -> list[object]:
            return self.rows

    connection = Connection()
    store = PushConfigStore(connection)
    identity = Identity("lab-bio", "peer:peer-a", frozenset({"runs:read"}))
    registered = store.register(identity, TaskPushNotificationConfig(
        task_id="run-1", url="https://peer.example/webhook"
    ))

    assert registered.task_id == "run-1"
    assert registered.id
    assert store.for_run("lab-bio", "run-1") == [{
        "url": "https://peer.example/webhook", "peer_name": "peer-a",
        "secret_ref": "vault://lab-bio/a2a/peer-a/push", "context_id": "context-1"
    }]
    assert sum("SELECT set_config" in sql for sql, _ in connection.calls) == 2
    assert any("INSERT INTO a2a_push_callbacks" in sql for sql, _ in connection.calls)
    assert all(b"test-vault-secret" not in repr(params).encode() for _, params in connection.calls)


def test_push_migration_preserves_peers_and_enforces_tenant_rls() -> None:
    sql = (Path(__file__).parents[2] / "migrations/008_a2a_push_callbacks.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS push_secret_ref" in sql
    assert "CREATE TABLE IF NOT EXISTS a2a_push_callbacks" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "CREATE POLICY a2a_push_callbacks_tenant" in sql
    assert "DROP TABLE" not in sql


def test_a2a_submission_enqueues_run_with_a2a_channel_and_no_hermes_call() -> None:
    Submission = importlib.import_module("scilab.api.a2a").A2ARunSubmission

    class Runs:
        def __init__(self) -> None:
            self.calls: list[tuple[Identity, str, dict[str, object]]] = []

        async def create(self, identity: Identity, key: str, **kwargs: object) -> object:
            self.calls.append((identity, key, kwargs))
            return SimpleNamespace(id="run-1", state="queued",
                                   context_id=kwargs["context_id"], hermes_run_id=None)

    identity = Identity("lab-bio", "peer:peer-a", frozenset({"runs:write"}))
    runs = Runs()
    submission = Submission(runs, admission=SimpleNamespace(check=lambda *args: None))
    message = Message(message_id="msg-1", context_id="context-1", role=Role.ROLE_USER,
                      parts=[Part(text="Review literature")])

    result = asyncio.run(submission.create(identity, message, "context-1"))

    assert result.state == "queued"
    assert result.context_id == "context-1"
    identity_arg, key_arg, kwargs = runs.calls[0]
    assert (identity_arg, key_arg) == (identity, "msg-1")
    assert kwargs["context_id"] == "context-1"
    assert kwargs["actor"] == "peer:peer-a"
    assert "budget_thb" not in kwargs
    assert "max_minutes" not in kwargs
    assert kwargs["request_payload"] == {
        "goal": "Review literature", "inputs": [], "skill_packs": [],
        "budget": None, "options": {}, "channel": "a2a",
    }


def test_a2a_submission_passes_metadata_budget_to_run_service() -> None:
    Submission = importlib.import_module("scilab.api.a2a").A2ARunSubmission

    class Runs:
        def __init__(self) -> None:
            self.calls: list[tuple[Identity, str, dict[str, object]]] = []

        async def create(self, identity: Identity, key: str, **kwargs: object) -> object:
            self.calls.append((identity, key, kwargs))
            return SimpleNamespace(id="run-1", state="queued",
                                   context_id=kwargs["context_id"], hermes_run_id=None)

    identity = Identity("lab-bio", "peer:peer-a", frozenset({"runs:write"}))
    runs = Runs()
    submission = Submission(runs, admission=SimpleNamespace(check=lambda *args: None))
    message = Message(message_id="msg-1", context_id="context-1", role=Role.ROLE_USER,
                      parts=[Part(text="Review literature")],
                      metadata={"budget_thb": 42.5, "max_minutes": 30})

    result = asyncio.run(submission.create(identity, message, "context-1"))

    assert result.state == "queued"
    _, _, kwargs = runs.calls[0]
    assert kwargs["budget_thb"] == 42.5
    assert kwargs["max_minutes"] == 30
    assert kwargs["request_payload"]["budget"] == {"thb": 42.5, "max_minutes": 30}


def test_a2a_submission_metadata_budget_defaults_max_minutes() -> None:
    Submission = importlib.import_module("scilab.api.a2a").A2ARunSubmission

    class Runs:
        def __init__(self) -> None:
            self.calls: list[tuple[Identity, str, dict[str, object]]] = []

        async def create(self, identity: Identity, key: str, **kwargs: object) -> object:
            self.calls.append((identity, key, kwargs))
            return SimpleNamespace(id="run-1", state="queued",
                                   context_id=kwargs["context_id"], hermes_run_id=None)

    identity = Identity("lab-bio", "peer:peer-a", frozenset({"runs:write"}))
    runs = Runs()
    submission = Submission(runs, admission=SimpleNamespace(check=lambda *args: None))
    message = Message(message_id="msg-1", context_id="context-1", role=Role.ROLE_USER,
                      parts=[Part(text="Review literature")],
                      metadata={"budget_thb": 10})

    asyncio.run(submission.create(identity, message, "context-1"))

    _, _, kwargs = runs.calls[0]
    assert kwargs["budget_thb"] == 10.0
    assert kwargs["max_minutes"] == 120
    assert kwargs["request_payload"]["budget"] == {"thb": 10.0, "max_minutes": 120}


@pytest.mark.parametrize("metadata", [
    {"budget_thb": -1},
    {"budget_thb": "not-a-number"},
    {"budget_thb": float("inf")},
    {"budget_thb": 10, "max_minutes": 0},
    {"budget_thb": 10, "max_minutes": -5},
    {"budget_thb": 10, "max_minutes": 1.5},
])
def test_a2a_submission_rejects_invalid_metadata_budget(metadata: dict[str, object]) -> None:
    a2a = importlib.import_module("scilab.api.a2a")

    class Runs:
        async def create(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("invalid budget must not create a Run")

    identity = Identity("lab-bio", "peer:peer-a", frozenset({"runs:write"}))
    submission = a2a.A2ARunSubmission(Runs(), admission=SimpleNamespace(check=lambda *args: None))
    message = Message(message_id="msg-1", context_id="context-1", role=Role.ROLE_USER,
                      parts=[Part(text="Review literature")], metadata=metadata)

    with pytest.raises(a2a.InvalidParamsError):
        asyncio.run(submission.create(identity, message, "context-1"))


def test_http_a2a_send_message_enqueues_run_via_run_service_contract() -> None:
    a2a = importlib.import_module("scilab.api.a2a")

    class Runs:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        async def create(self, identity: Identity, idempotency_key: str, *, context_id=None,
                         budget_thb=None, request_payload=None, actor=None) -> object:
            self.calls.append((identity, idempotency_key, context_id, budget_thb,
                              request_payload, actor))
            return SimpleNamespace(id="run-1", state="queued", context_id=context_id,
                                   hermes_run_id=None)

    runs = Runs()
    submission = a2a.A2ARunSubmission(runs, admission=SimpleNamespace(check=lambda *args: None))
    app = a2a.create_app(
        services=SimpleNamespace(run_submission=submission),
        peers=[{"peer_name": "peer-a", "lab_id": "lab-bio",
                "secret_hash": credential_digest("registered-peer"),
                "scopes": ["runs:read", "runs:write"]}],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )

    response = request(
        app, "POST", "/labs/lab-bio",
        json={"jsonrpc": "2.0", "id": "send-1", "method": "SendMessage",
              "params": {"message": {"messageId": "msg-1", "contextId": "context-1",
                                     "role": "ROLE_USER", "parts": [{"text": "Research"}]}}},
        headers={"Authorization": "Bearer registered-peer"},
    )

    assert response.status_code == 200
    task = response.json()["result"]["task"]
    assert task["id"] == "run-1"
    assert task["status"]["state"] == "TASK_STATE_SUBMITTED"
    identity, key, context_id, budget_thb, request_payload, actor = runs.calls[0]
    assert identity.principal == "peer:peer-a"
    assert key == "msg-1"
    assert context_id == "context-1"
    assert budget_thb is None
    assert actor == "peer:peer-a"
    assert request_payload["channel"] == "a2a"
    assert request_payload["goal"] == "Research"
    assert request_payload["budget"] is None
    assert "100" not in json.dumps(request_payload)


def test_http_a2a_send_message_with_params_metadata_budget_sets_run_budget() -> None:
    a2a = importlib.import_module("scilab.api.a2a")

    class Runs:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        async def create(self, identity: Identity, idempotency_key: str, *, context_id=None,
                         budget_thb=None, max_minutes=120, request_payload=None,
                         actor=None) -> object:
            self.calls.append((identity, idempotency_key, context_id, budget_thb,
                              max_minutes, request_payload, actor))
            return SimpleNamespace(id="run-1", state="queued", context_id=context_id,
                                   hermes_run_id=None)

    runs = Runs()
    submission = a2a.A2ARunSubmission(runs, admission=SimpleNamespace(check=lambda *args: None))
    app = a2a.create_app(
        services=SimpleNamespace(run_submission=submission),
        peers=[{"peer_name": "peer-a", "lab_id": "lab-bio",
                "secret_hash": credential_digest("registered-peer"),
                "scopes": ["runs:read", "runs:write"]}],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )

    response = request(
        app, "POST", "/labs/lab-bio",
        json={"jsonrpc": "2.0", "id": "send-1", "method": "SendMessage",
              "params": {"message": {"messageId": "msg-1", "contextId": "context-1",
                                     "role": "ROLE_USER", "parts": [{"text": "Research"}]},
                        "metadata": {"budget_thb": 75, "max_minutes": 45}}},
        headers={"Authorization": "Bearer registered-peer"},
    )

    assert response.status_code == 200
    identity, key, context_id, budget_thb, max_minutes, request_payload, actor = runs.calls[0]
    assert budget_thb == 75.0
    assert max_minutes == 45
    assert request_payload["budget"] == {"thb": 75.0, "max_minutes": 45}


def test_http_a2a_send_message_with_invalid_metadata_budget_rejects_and_creates_no_run() -> None:
    a2a = importlib.import_module("scilab.api.a2a")

    class Runs:
        async def create(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("invalid budget must not create a Run")

    submission = a2a.A2ARunSubmission(Runs(), admission=SimpleNamespace(check=lambda *args: None))
    app = a2a.create_app(
        services=SimpleNamespace(run_submission=submission),
        peers=[{"peer_name": "peer-a", "lab_id": "lab-bio",
                "secret_hash": credential_digest("registered-peer"),
                "scopes": ["runs:read", "runs:write"]}],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )

    response = request(
        app, "POST", "/labs/lab-bio",
        json={"jsonrpc": "2.0", "id": "send-1", "method": "SendMessage",
              "params": {"message": {"messageId": "msg-1", "contextId": "context-1",
                                     "role": "ROLE_USER", "parts": [{"text": "Research"}],
                                     "metadata": {"budget_thb": -5}}}},
        headers={"Authorization": "Bearer registered-peer"},
    )

    assert response.status_code == 200
    assert "error" in response.json()


def test_a2a_submission_checks_admission_before_enqueueing_run() -> None:
    a2a = importlib.import_module("scilab.api.a2a")
    identity = Identity("lab-bio", "peer:peer-a", frozenset({"runs:write"}))
    order: list[str] = []
    admission_calls: list[tuple[object, dict[str, object], str]] = []

    class Admission:
        async def check(
            self, actor: object, payload: dict[str, object], idempotency_key: str
        ) -> None:
            order.append("admission")
            admission_calls.append((actor, payload, idempotency_key))

    class Runs:
        async def create(self, identity: object, key: str, **kwargs: object) -> object:
            order.append("create")
            return SimpleNamespace(
                id="run-1", state="queued", context_id=kwargs["context_id"], hermes_run_id=None
            )

    message = Message(
        message_id="msg-1", context_id="context-1", role=Role.ROLE_USER,
        parts=[Part(text="Research")],
    )
    submission = a2a.A2ARunSubmission(Runs(), admission=Admission())

    result = asyncio.run(submission.create(identity, message, "context-1"))

    assert result.id == "run-1"
    assert admission_calls == [(identity, MessageToDict(message), "msg-1")]
    assert order == ["admission", "create"]


def test_a2a_submission_fails_closed_without_admission_service() -> None:
    a2a = importlib.import_module("scilab.api.a2a")
    identity = Identity("lab-bio", "peer:peer-a", frozenset({"runs:write"}))
    created: list[str] = []

    class Runs:
        async def create(self, *args: object, **kwargs: object) -> object:
            created.append("run")
            return SimpleNamespace(id="run-1", state="queued")

    with pytest.raises(RuntimeError, match="admission"):
        asyncio.run(
            a2a.A2ARunSubmission(Runs()).create(
                identity,
                Message(
                    message_id="msg-1", context_id="context-1",
                    role=Role.ROLE_USER, parts=[Part(text="Research")],
                ),
                "context-1",
            )
        )

    assert created == []


def test_cancel_task_only_stops_the_run_and_publishes_state() -> None:
    # ponytail: the worker stops Hermes when it observes the cancelled run state (Q22);
    # A2A cancel must not call Hermes directly, so no Hermes double exists in this test at all.
    a2a = importlib.import_module("scilab.api.a2a")
    identity = Identity("lab-bio", "peer:peer-a", frozenset({"runs:read", "runs:write"}))
    order: list[tuple[object, ...]] = []

    class Runs:
        async def get(self, actor: object, run_id: str) -> object:
            return SimpleNamespace(
                id=run_id, state="running", context_id="context-1",
                hermes_run_id="hermes-42",
            )

        async def stop(self, actor: object, run_id: str) -> object:
            order.append(("run-stop", actor, run_id))
            return SimpleNamespace(
                id=run_id, state="cancelled", context_id="context-1",
                hermes_run_id="hermes-42", reason="stopped",
            )

    class Events:
        async def publish_event(self, *args: object) -> None:
            order.append(("event", *args))

    app = a2a.create_app(
        services=SimpleNamespace(runs=Runs(), events=Events()),
        peers=[{
            "peer_name": "peer-a", "lab_id": "lab-bio",
            "secret_hash": credential_digest("registered-peer"),
            "scopes": ["runs:read", "runs:write"],
        }],
        lab_cards={"lab-bio": {"name": "Bio", "description": "Research lab"}},
    )
    response = request(
        app, "POST", "/labs/lab-bio",
        json={
            "jsonrpc": "2.0", "id": "cancel-1", "method": "CancelTask",
            "params": {"id": "run-1"},
        },
        headers={"Authorization": "Bearer registered-peer"},
    )

    assert response.status_code == 200
    assert response.json()["result"]["status"]["state"] == "TASK_STATE_CANCELED"
    assert order == [
        ("run-stop", identity, "run-1"),
        (
            "event", identity, "run-1", "run.state",
            {"from": "running", "to": "cancelled", "reason": "stopped"},
            "run-service",
        ),
    ]


def test_push_dispatch_continues_after_peer_failure_and_reports_it() -> None:
    PushDispatcher = importlib.import_module("scilab.api.a2a").PushDispatcher
    attempted: list[str] = []

    class Configs:
        def for_run(self, lab_id: str, run_id: str) -> list[dict[str, str]]:
            return [
                {
                    "url": "https://first.example/webhook", "peer_name": "peer-first",
                    "context_id": "context-1", "secret_ref": "vault://lab-bio/a2a/peer-first/push",
                },
                {
                    "url": "https://second.example/webhook", "peer_name": "peer-second",
                    "context_id": "context-1", "secret_ref": "vault://lab-bio/a2a/peer-second/push",
                },
            ]

    class Vault:
        async def read(self, ref: str) -> bytes:
            return b"test-vault-secret"

    async def post(url: str, body: bytes, headers: dict[str, str]) -> None:
        attempted.append(url)
        if url.startswith("https://first."):
            raise OSError("first peer unavailable")

    event = SimpleNamespace(
        type="run.state", lab_id="lab-bio", run_id="run-1",
        payload=SimpleNamespace(to="running"),
    )

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(PushDispatcher(Configs(), Vault(), post).dispatch(event))

    assert attempted == [
        "https://first.example/webhook", "https://second.example/webhook"
    ]
    assert [
        (peer, str(error)) for peer, error in raised.value.failures
    ] == [("peer-first", "first peer unavailable")]
