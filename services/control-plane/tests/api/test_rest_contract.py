from __future__ import annotations

import importlib
import importlib.util
import asyncio
from pathlib import Path
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from scilab.identity import Identity


ROOT = Path(__file__).parents[4]


class ASGIClient:
    def __init__(self, app: Any) -> None:
        self.app = app

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        import httpx

        async def send() -> Any:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)


ROUTES = {
    ("POST", "/v1/labs/{lab}/runs"),
    ("GET", "/v1/labs/{lab}/runs"),
    ("GET", "/v1/runs/{id}"),
    ("GET", "/v1/runs/{id}/events"),
    ("POST", "/v1/runs/{id}/stop"),
    ("POST", "/v1/runs/{id}/approvals/{approval_id}"),
    ("GET", "/v1/runs/{id}/artifacts"),
    ("GET", "/v1/artifacts/{id}"),
    ("POST", "/v1/labs/{lab}/inputs"),
    ("POST", "/v1/labs/{lab}/ask"),
    ("GET", "/v1/labs/{lab}/skills"),
    ("GET", "/v1/labs/{lab}/api-keys"),
    ("POST", "/v1/labs/{lab}/api-keys"),
    ("DELETE", "/v1/labs/{lab}/api-keys"),
    ("GET", "/v1/labs/{lab}/peers"),
    ("POST", "/v1/labs/{lab}/peers"),
    ("GET", "/v1/labs/{lab}/usage"),
}


def _rest(*, skip_missing: bool = False) -> Any:
    try:
        spec = importlib.util.find_spec("scilab.api.rest")
    except ModuleNotFoundError:
        spec = None
    if spec is None and skip_missing:
        pytest.skip("Wave 12 REST module is not implemented")
    assert spec is not None, "Wave 12 REST module is not implemented"
    return importlib.import_module("scilab.api.rest")


@dataclass
class FakeRuns:
    calls: list[tuple[str, Any]] = field(default_factory=list)

    def get(self, identity: Identity, run_id: str) -> dict[str, Any]:
        self.calls.append(("get", identity, run_id))
        return {"id": run_id, "lab_id": identity.lab_id, "state": "running"}

    def stop(self, identity: Identity, run_id: str) -> dict[str, Any]:
        self.calls.append(("stop", identity, run_id))
        return {"id": run_id, "lab_id": identity.lab_id, "state": "cancelled"}


@dataclass
class FakeRunSubmission:
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def create(
        self, identity: Identity, key: str, request: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append((identity, key, request))
        return {"id": "run-1", "lab_id": identity.lab_id, "state": "queued"}


@dataclass
class FakeRunSearch:
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def list(self, identity: Identity, **filters: Any) -> dict[str, Any]:
        self.calls.append((identity, filters))
        return {"items": [], "next_cursor": filters.get("cursor")}


@dataclass
class FakeAdmission:
    allowed: bool = True
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def check(
        self, identity: Identity, request: dict[str, Any], idempotency_key: str
    ) -> None:
        self.calls.append((identity, request, idempotency_key))
        if not self.allowed:
            from fastapi import HTTPException

            raise HTTPException(status_code=429, detail="request not admitted")


@dataclass
class FakeEvents:
    pass


@dataclass
class FakeEventStreamer:
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    async def __call__(
        self, service: Any, identity: Identity, run_id: str, from_seq: int
    ):
        self.calls.append((service, identity, run_id, from_seq))
        yield "id: 8\nevent: run.started\ndata: {}\n\n"


@dataclass
class FakeApprovals:
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def decide_approval(
        self,
        identity: Identity,
        approval_id: str,
        decision: str,
        *,
        note: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append((identity, run_id, approval_id, decision, note))
        return {"id": approval_id, "run_id": run_id, "status": decision}


@dataclass
class FakeArtifacts:
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def list_for_run(self, identity: Identity, run_id: str) -> list[dict[str, Any]]:
        self.calls.append(("list", identity, run_id))
        return [{"id": "artifact-1", "run_id": run_id}]

    def get(self, identity: Identity, artifact_id: str) -> dict[str, Any]:
        self.calls.append(("get", identity, artifact_id))
        return {"id": artifact_id, "lab_id": identity.lab_id}

    def presign(self, identity: Identity, artifact_id: str) -> str:
        self.calls.append(("presign", identity, artifact_id))
        return "https://objects.invalid/a?expires=900"


@dataclass
class FakeLabResource:
    name: str
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def create(self, identity: Identity, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("create", identity, body))
        return {"id": f"{self.name}-1", **body}

    def list(self, identity: Identity) -> list[dict[str, Any]]:
        self.calls.append(("list", identity))
        return []

    def delete(self, identity: Identity, key_id: str) -> None:
        self.calls.append(("delete", identity, key_id))


@dataclass
class FakeInputs:
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def create(self, identity: Identity, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((identity, body))
        return {"artifact_id": "input-1", **body}


@dataclass
class FakeAsk:
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    async def ask(self, identity: Identity, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((identity, body))
        return {"answer": "stateless"}


@dataclass
class FakeSkills:
    calls: list[Identity] = field(default_factory=list)

    def list(self, identity: Identity) -> list[dict[str, Any]]:
        self.calls.append(identity)
        return [{"name": "literature-search"}]


@dataclass
class FakeUsage:
    calls: list[tuple[Any, ...]] = field(default_factory=list)

    def get(self, identity: Identity, period: str) -> dict[str, Any]:
        self.calls.append((identity, period))
        return {"period": period, "tokens_in": 0, "tokens_out": 0}


def _identity(request: Any) -> Identity:
    scopes = request.headers.get(
        "X-Test-Scopes",
        "runs:read runs:write runs:approve artifacts:read lab:admin",
    ).split()
    return Identity(
        request.headers.get("X-Test-Lab", "lab-a"),
        f"user:{request.headers.get('X-Test-Principal', 'owner-1').strip()}",
        scopes,
    )


@pytest.fixture
def api() -> tuple[Any, Any]:
    rest = _rest(skip_missing=True)
    services = SimpleNamespace(
        admission=FakeAdmission(),
        runs=FakeRuns(),
        run_submission=FakeRunSubmission(),
        run_search=FakeRunSearch(),
        events=FakeEvents(),
        event_streamer=FakeEventStreamer(),
        approvals=FakeApprovals(),
        artifacts=FakeArtifacts(),
        inputs=FakeInputs(),
        ask=FakeAsk(),
        skills=FakeSkills(),
        api_keys=FakeLabResource("key"),
        peers=FakeLabResource("peer"),
        usage=FakeUsage(),
    )
    return (
        ASGIClient(
            rest.create_app(
                services,
                _identity,
                event_streamer=services.event_streamer,
            )
        ),
        services,
    )


def test_registers_exact_tor_route_matrix() -> None:
    rest = _rest()
    services = SimpleNamespace(
        admission=FakeAdmission(),
        runs=FakeRuns(),
        run_submission=FakeRunSubmission(),
        run_search=FakeRunSearch(),
        events=FakeEvents(),
        event_streamer=FakeEventStreamer(),
        approvals=FakeApprovals(),
        artifacts=FakeArtifacts(),
        inputs=FakeInputs(),
        ask=FakeAsk(),
        skills=FakeSkills(),
        api_keys=FakeLabResource("key"),
        peers=FakeLabResource("peer"),
        usage=FakeUsage(),
    )
    app = rest.create_app(
        services,
        _identity,
        event_streamer=services.event_streamer,
    )
    actual = {
        (method, route.path)
        for route in app.routes
        for method in getattr(route, "methods", set())
        if route.path.startswith("/v1/")
        and method not in {"HEAD", "OPTIONS"}
    }
    assert actual == ROUTES


def test_create_run_returns_202_and_forwards_tor_fields(api: tuple[Any, Any]) -> None:
    client, services = api
    body = {
        "goal": "Map evidence",
        "inputs": ["input-1"],
        "skill_packs": ["general-research"],
        "budget": {"thb": 500.0, "max_minutes": 45},
        "options": {},
    }
    response = client.post(
        "/v1/labs/lab-a/runs",
        headers={"Idempotency-Key": "idem-1"},
        json=body,
    )
    assert response.status_code == 202
    assert response.json()["id"] == "run-1"
    identity, key, forwarded = services.run_submission.calls[-1]
    assert (identity.lab_id, identity.principal, key, forwarded) == (
        "lab-a",
        "user:owner-1",
        "idem-1",
        body,
    )
    assert services.admission.calls[-1][1:] == (body, "idem-1")


def test_create_run_uses_injected_admission_decision(api: tuple[Any, Any]) -> None:
    client, services = api
    services.admission.allowed = False
    response = client.post(
        "/v1/labs/lab-a/runs",
        headers={"Idempotency-Key": "idem-denied"},
        json={
            "goal": "Map evidence",
            "inputs": [],
            "skill_packs": ["general-research"],
            "budget": {"thb": 10, "max_minutes": 30},
            "options": {},
        },
    )
    assert response.status_code == 429
    assert response.json() == {"detail": "request not admitted"}
    assert services.run_submission.calls == []


def test_list_runs_forwards_filters_and_opaque_cursor(api: tuple[Any, Any]) -> None:
    client, services = api
    response = client.get(
        "/v1/labs/lab-a/runs",
        params={
            "state": "running",
            "actor": "Data Scientist",
            "since": "2026-09-22T00:00:00Z",
            "cursor": "opaque+/=",
        },
    )
    assert response.status_code == 200
    filters = services.run_search.calls[-1][1]
    assert filters == {
        "state": "running",
        "actor": "Data Scientist",
        "since": "2026-09-22T00:00:00Z",
        "cursor": "opaque+/=",
    }
    assert response.json()["next_cursor"] == "opaque+/="


def test_sse_preserves_resume_and_heartbeat_contract(api: tuple[Any, Any]) -> None:
    client, services = api
    response = client.get("/v1/runs/run-1/events", params={"from_seq": 7})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "id: 8" in response.text
    _, identity, run_id, from_seq = services.event_streamer.calls[-1]
    assert (identity.lab_id, run_id, from_seq) == ("lab-a", "run-1", 7)


def test_artifact_get_includes_fifteen_minute_url(api: tuple[Any, Any]) -> None:
    client, services = api
    response = client.get("/v1/artifacts/artifact-1")
    assert response.status_code == 200
    assert response.json()["url"].endswith("expires=900")
    assert [call[0] for call in services.artifacts.calls[-2:]] == ["get", "presign"]


@pytest.mark.parametrize(
    ("path", "body", "required_scope"),
    [
        ("/v1/runs/run-1/approvals/approval-1", {"decision": "approve"}, "runs:approve"),
        ("/v1/labs/lab-a/api-keys", {"name": "ci"}, "lab:admin"),
        ("/v1/labs/lab-a/peers", {"name": "hermes"}, "lab:admin"),
    ],
)
def test_privileged_writes_require_scope(
    api: tuple[Any, Any], path: str, body: dict[str, Any], required_scope: str
) -> None:
    client, _ = api
    response = client.post(
        path,
        headers={"X-Test-Scopes": "runs:read runs:write artifacts:read"},
        json=body,
    )
    assert response.status_code == 403
    assert required_scope in response.json()["detail"]


def test_delete_api_key_requires_one_key_id_and_never_bulk(api: tuple[Any, Any]) -> None:
    client, services = api
    missing = client.delete("/v1/labs/lab-a/api-keys")
    assert missing.status_code == 422
    assert services.api_keys.calls == []

    deleted = client.delete("/v1/labs/lab-a/api-keys", params={"key_id": "key-7"})
    assert deleted.status_code == 204
    operation, identity, key_id = services.api_keys.calls[-1]
    assert (operation, identity.lab_id, key_id) == ("delete", "lab-a", "key-7")


def test_cross_lab_path_is_denied_before_service_call(api: tuple[Any, Any]) -> None:
    client, services = api
    response = client.get(
        "/v1/labs/lab-b/runs",
        headers={"X-Test-Lab": " lab-a ", "X-Test-Principal": " owner-1 "},
    )
    assert response.status_code == 403
    assert services.run_search.calls == []


def test_ask_is_stateless_and_has_no_sandbox_dependency(api: tuple[Any, Any]) -> None:
    client, services = api
    assert not hasattr(services, "sandbox")
    first = client.post("/v1/labs/lab-a/ask", json={"question": "What evidence?"})
    second = client.post("/v1/labs/lab-a/ask", json={"question": "Repeat?"})
    assert first.json() == {"answer": "stateless"}
    assert second.json() == {"answer": "stateless"}
    assert [call[1] for call in services.ask.calls] == [
        {"question": "What evidence?"},
        {"question": "Repeat?"},
    ]


@pytest.mark.parametrize("period", ["daily", "monthly"])
def test_usage_supports_only_tor_periods(api: tuple[Any, Any], period: str) -> None:
    client, services = api
    response = client.get("/v1/labs/lab-a/usage", params={"period": period})
    assert response.status_code == 200
    assert response.json()["period"] == period
    assert services.usage.calls[-1][1] == period

    invalid = client.get("/v1/labs/lab-a/usage", params={"period": "hourly"})
    assert invalid.status_code == 422


@pytest.mark.parametrize(
    ("method", "path", "body", "expected"),
    [
        ("GET", "/v1/runs/run-1", None, {"id": "run-1", "state": "running"}),
        ("POST", "/v1/runs/run-1/stop", None, {"id": "run-1", "state": "cancelled"}),
        (
            "GET",
            "/v1/runs/run-1/artifacts",
            None,
            [{"id": "artifact-1", "run_id": "run-1"}],
        ),
        (
            "POST",
            "/v1/labs/lab-a/inputs",
            {"filename": "input.csv"},
            {"artifact_id": "input-1"},
        ),
        (
            "POST",
            "/v1/runs/run-1/approvals/approval-1",
            {"decision": "reject", "note": "unsafe"},
            {"id": "approval-1", "run_id": "run-1", "status": "reject"},
        ),
        ("GET", "/v1/labs/lab-a/skills", None, [{"name": "literature-search"}]),
        ("GET", "/v1/labs/lab-a/api-keys", None, []),
        ("POST", "/v1/labs/lab-a/api-keys", {"name": "ci"}, {"id": "key-1"}),
        ("GET", "/v1/labs/lab-a/peers", None, []),
        ("POST", "/v1/labs/lab-a/peers", {"name": "hermes"}, {"id": "peer-1"}),
    ],
)
def test_remaining_tor_endpoints_delegate_without_policy_duplication(
    api: tuple[Any, Any],
    method: str,
    path: str,
    body: dict[str, Any] | None,
    expected: Any,
) -> None:
    client, _ = api
    kwargs = {"json": body} if body is not None else {}
    response = client.request(method, path, **kwargs)
    assert response.status_code == 200
    actual = response.json()
    if isinstance(expected, list):
        assert actual == expected
    else:
        assert {key: actual[key] for key in expected} == expected


def test_checked_in_openapi_31_matches_tor_routes() -> None:
    import yaml
    from openapi_spec_validator import validate

    path = ROOT / "contracts/openapi/scilab.yaml"
    assert path.exists(), "OpenAPI contract is not implemented"
    document = yaml.safe_load(path.read_text())
    validate(document)
    assert document["openapi"].startswith("3.1.")
    actual = {
        (method.upper(), route)
        for route, operations in document["paths"].items()
        for method in operations
        if method.upper() in {"GET", "POST", "DELETE"}
    }
    assert actual == ROUTES
    delete = document["paths"]["/v1/labs/{lab}/api-keys"]["delete"]
    key_id = next(
        parameter
        for parameter in delete["parameters"]
        if parameter["name"] == "key_id"
    )
    assert key_id["in"] == "query"
    assert key_id["required"] is True


def test_python_sdk_exercises_required_run_flow() -> None:
    path = ROOT / "sdk/python/scilab_client.py"
    assert path.exists(), "Python SDK is not implemented"
    spec = importlib.util.spec_from_file_location("scilab_client", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class Transport:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def request(self, method: str, path: str, **kwargs: Any) -> Any:
            self.calls.append((method, path, kwargs))
            if path.endswith("/artifacts/artifact-1"):
                return {"id": "artifact-1", "url": "https://objects.invalid/a"}
            return {"id": "run-1"}

        def events(self, path: str, **kwargs: Any):
            self.calls.append(("EVENTS", path, kwargs))
            yield {"id": "8", "event": "run.started", "data": {"state": "running"}}

    transport = Transport()
    client = module.SciLabClient("https://api.invalid", "token-1", transport)
    created = client.create_run(
        "lab-a",
        {
            "goal": "Map evidence",
            "inputs": [],
            "skill_packs": ["general-research"],
            "budget": {"thb": 10, "max_minutes": 30},
            "options": {},
        },
        idempotency_key="idem-1",
    )
    status = client.get_run(created["id"])
    event = next(client.events(created["id"], from_seq=7))
    client.decide_approval(created["id"], "approval-1", "approve")
    client.stop_run(created["id"])
    artifact = client.get_artifact("artifact-1")

    assert status["id"] == "run-1"
    assert event["id"] == "8"
    assert artifact["url"] == "https://objects.invalid/a"
    assert transport.calls == [
        (
            "POST",
            "/v1/labs/lab-a/runs",
            {
                "headers": {"Idempotency-Key": "idem-1"},
                "json": {
                    "goal": "Map evidence",
                    "inputs": [],
                    "skill_packs": ["general-research"],
                    "budget": {"thb": 10, "max_minutes": 30},
                    "options": {},
                },
            },
        ),
        ("GET", "/v1/runs/run-1", {}),
        ("EVENTS", "/v1/runs/run-1/events", {"params": {"from_seq": 7}}),
        (
            "POST",
            "/v1/runs/run-1/approvals/approval-1",
            {"json": {"decision": "approve", "note": None}},
        ),
        ("POST", "/v1/runs/run-1/stop", {}),
        ("GET", "/v1/artifacts/artifact-1", {}),
    ]
