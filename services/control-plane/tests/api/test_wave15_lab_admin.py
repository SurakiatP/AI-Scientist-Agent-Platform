from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import HTTPException

from scilab.identity import Identity


class ASGIClient:
    def __init__(self, app: Any) -> None:
        self.app = app

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", path, **kwargs)


class FakeLabAdmin:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.budget: Decimal | None = None

    def list_members(self, identity: Identity) -> list[dict[str, str]]:
        self.calls.append(("list_members", identity))
        return [{"subject": "oidc-1", "role": "owner"}]

    def add_member(self, identity: Identity, subject: str, role: str) -> dict[str, str]:
        self.calls.append(("add_member", identity, subject, role))
        return {"subject": subject, "role": role}

    def change_member_role(
        self, identity: Identity, subject: str, role: str
    ) -> dict[str, str]:
        self.calls.append(("change_member_role", identity, subject, role))
        return {"subject": subject, "role": role}

    def remove_member(self, identity: Identity, subject: str) -> None:
        self.calls.append(("remove_member", identity, subject))
        if subject == "owner-1":
            from scilab.api.lab_admin import LastOwnerError

            raise LastOwnerError("cannot remove the final Lab owner")

    def get_budget(self, identity: Identity) -> Decimal | None:
        self.calls.append(("get_budget", identity))
        return self.budget

    def set_budget(self, identity: Identity, budget_thb: Decimal | None) -> None:
        self.calls.append(("set_budget", identity, budget_thb))
        self.budget = budget_thb


def _rest() -> Any:
    import importlib

    return importlib.import_module("scilab.api.rest")


def _identity(request: Any) -> Identity:
    scopes = request.headers.get(
        "X-Test-Scopes",
        "runs:read runs:write runs:approve artifacts:read lab:admin",
    ).split()
    return Identity(
        request.headers.get("X-Test-Lab", "lab-a"),
        f"{request.headers.get('X-Test-Principal-Type', 'user')}:{request.headers.get('X-Test-Principal', 'owner-1')}",
        frozenset(scopes),
    )


@pytest.fixture
def api() -> tuple[ASGIClient, FakeLabAdmin]:
    rest = _rest()
    admin = FakeLabAdmin()
    services = SimpleNamespace(
        admission=SimpleNamespace(check=lambda *args, **kwargs: None),
        run_submission=SimpleNamespace(create=lambda *args, **kwargs: {}),
        run_search=SimpleNamespace(list=lambda *args, **kwargs: {}),
        runs=SimpleNamespace(get=lambda *args, **kwargs: {}, stop=lambda *args, **kwargs: {}),
        events=SimpleNamespace(),
        approvals=SimpleNamespace(decide_approval=lambda *args, **kwargs: {}),
        artifacts=SimpleNamespace(
            list_for_run=lambda *args, **kwargs: [],
            get=lambda *args, **kwargs: {},
            presign=lambda *args, **kwargs: "",
        ),
        inputs=SimpleNamespace(create=lambda *args, **kwargs: {}),
        ask=SimpleNamespace(ask=lambda *args, **kwargs: {}),
        skills=SimpleNamespace(list=lambda *args, **kwargs: []),
        api_keys=SimpleNamespace(list=lambda *args, **kwargs: []),
        peers=SimpleNamespace(list=lambda *args, **kwargs: []),
        usage=SimpleNamespace(get=lambda *args, **kwargs: {}),
        lab_admin=admin,
    )

    async def event_streamer(*args: Any, **kwargs: Any):
        yield ""

    return ASGIClient(
        rest.create_app(services, _identity, event_streamer=event_streamer)
    ), admin


def test_wave15_routes_are_additive_and_me_returns_resolved_identity(
    api: tuple[ASGIClient, FakeLabAdmin],
) -> None:
    client, _ = api
    response = client.get("/v1/me")
    assert response.status_code == 200
    assert response.json() == {
        "lab_id": "lab-a",
        "principal": "user:owner-1",
        "scopes": [
            "artifacts:read",
            "lab:admin",
            "runs:approve",
            "runs:read",
            "runs:write",
        ],
    }


def test_member_crud_requires_admin_and_mutations_require_human_principal(
    api: tuple[ASGIClient, FakeLabAdmin],
) -> None:
    client, admin = api
    assert client.get("/v1/labs/lab-a/members").json() == {
        "members": [{"subject": "oidc-1", "role": "owner"}]
    }

    added = client.post(
        "/v1/labs/lab-a/members", json={"subject": "oidc-2", "role": "researcher"}
    )
    assert added.status_code == 201
    assert added.json() == {"subject": "oidc-2", "role": "researcher"}
    changed = client.patch(
        "/v1/labs/lab-a/members/oidc-2", json={"role": "viewer"}
    )
    assert changed.status_code == 200
    removed = client.delete("/v1/labs/lab-a/members/oidc-2")
    assert removed.status_code == 204

    denied = client.post(
        "/v1/labs/lab-a/members",
        headers={"X-Test-Principal-Type": "key"},
        json={"subject": "oidc-3", "role": "viewer"},
    )
    assert denied.status_code == 403
    no_scope = client.get(
        "/v1/labs/lab-a/members", headers={"X-Test-Scopes": "runs:read"}
    )
    assert no_scope.status_code == 403
    assert [call[0] for call in admin.calls] == [
        "list_members",
        "add_member",
        "change_member_role",
        "remove_member",
    ]


def test_budget_uses_decimal_and_null_for_unbudgeted(
    api: tuple[ASGIClient, FakeLabAdmin],
) -> None:
    client, admin = api
    assert client.get("/v1/labs/lab-a/budget").json() == {"budget_thb": None}

    updated = client.put(
        "/v1/labs/lab-a/budget",
        content=b'{"budget_thb":123456789012345.6789}',
        headers={"Content-Type": "application/json"},
    )
    assert updated.status_code == 200
    assert admin.budget == Decimal("123456789012345.6789")

    cleared = client.put("/v1/labs/lab-a/budget", json={"budget_thb": None})
    assert cleared.status_code == 200
    assert cleared.json() == {"budget_thb": None}
    for invalid in (-1, True, "NaN", "Infinity"):
        response = client.put(
            "/v1/labs/lab-a/budget", json={"budget_thb": invalid}
        )
        assert response.status_code == 422


def test_cross_lab_admin_request_is_denied_before_service_call(
    api: tuple[ASGIClient, FakeLabAdmin],
) -> None:
    client, admin = api
    response = client.get("/v1/labs/lab-b/members")
    assert response.status_code == 403
    assert admin.calls == []


def test_final_owner_conflict_is_reported_as_conflict(
    api: tuple[ASGIClient, FakeLabAdmin],
) -> None:
    client, _ = api
    response = client.delete("/v1/labs/lab-a/members/owner-1")
    assert response.status_code == 409


def test_invalid_member_inputs_are_rejected_before_service_call(
    api: tuple[ASGIClient, FakeLabAdmin],
) -> None:
    client, admin = api
    for body in ({"subject": "   ", "role": "viewer"}, {"subject": "valid", "role": "admin"}):
        assert client.post("/v1/labs/lab-a/members", json=body).status_code == 422
    assert client.patch("/v1/labs/lab-a/members/%20", json={"role": "viewer"}).status_code == 422
    assert admin.calls == []
