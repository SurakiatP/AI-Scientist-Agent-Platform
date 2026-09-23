from __future__ import annotations

import json as jsonlib
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class UrllibTransport:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        query = urlencode(
            {key: value for key, value in (params or {}).items() if value is not None}
        )
        return f"{self.base_url}{path}{'?' + query if query else ''}"

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        merged = {**self.headers, **dict(headers or {})}
        data = None
        if json is not None:
            data = jsonlib.dumps(json).encode()
            merged["Content-Type"] = "application/json"
        request = Request(self._url(path, params), data=data, headers=merged, method=method)
        with urlopen(request) as response:
            body = response.read()
        return jsonlib.loads(body) if body else None

    def events(
        self, path: str, *, params: Mapping[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        request = Request(
            self._url(path, params),
            headers={**self.headers, "Accept": "text/event-stream"},
        )
        with urlopen(request) as response:
            frame: dict[str, Any] = {}
            for raw in response:
                line = raw.decode().rstrip("\r\n")
                if not line:
                    if frame:
                        yield frame
                        frame = {}
                    continue
                if line.startswith(":"):
                    continue
                key, _, value = line.partition(":")
                value = value.lstrip()
                frame[key] = jsonlib.loads(value) if key == "data" else value
            if frame:
                yield frame


class SciLabClient:
    def __init__(
        self, base_url: str, token: str, transport: Any | None = None
    ) -> None:
        if not base_url.strip() or not token.strip():
            raise ValueError("base_url and token must be non-blank")
        self.transport = transport or UrllibTransport(base_url, token)

    def create_run(
        self,
        lab: str,
        request: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> Any:
        return self.transport.request(
            "POST",
            f"/v1/labs/{lab}/runs",
            headers={"Idempotency-Key": idempotency_key},
            json=dict(request),
        )

    def list_runs(
        self,
        lab: str,
        *,
        state: str | None = None,
        actor: str | None = None,
        since: str | None = None,
        cursor: str | None = None,
    ) -> Any:
        return self.transport.request(
            "GET",
            f"/v1/labs/{lab}/runs",
            params={
                "state": state,
                "actor": actor,
                "since": since,
                "cursor": cursor,
            },
        )

    def get_run(self, run_id: str) -> Any:
        return self.transport.request("GET", f"/v1/runs/{run_id}")

    def events(self, run_id: str, *, from_seq: int = 0) -> Iterator[dict[str, Any]]:
        return self.transport.events(
            f"/v1/runs/{run_id}/events", params={"from_seq": from_seq}
        )

    def stop_run(self, run_id: str) -> Any:
        return self.transport.request("POST", f"/v1/runs/{run_id}/stop")

    def decide_approval(
        self,
        run_id: str,
        approval_id: str,
        decision: str,
        *,
        note: str | None = None,
    ) -> Any:
        return self.transport.request(
            "POST",
            f"/v1/runs/{run_id}/approvals/{approval_id}",
            json={"decision": decision, "note": note},
        )

    def list_artifacts(self, run_id: str) -> Any:
        return self.transport.request("GET", f"/v1/runs/{run_id}/artifacts")

    def get_artifact(self, artifact_id: str) -> Any:
        return self.transport.request("GET", f"/v1/artifacts/{artifact_id}")

    def upload_input(self, lab: str, request: Mapping[str, Any]) -> Any:
        return self.transport.request(
            "POST", f"/v1/labs/{lab}/inputs", json=dict(request)
        )

    def ask(self, lab: str, question: str) -> Any:
        return self.transport.request(
            "POST", f"/v1/labs/{lab}/ask", json={"question": question}
        )

    def list_skills(self, lab: str) -> Any:
        return self.transport.request("GET", f"/v1/labs/{lab}/skills")

    def list_api_keys(self, lab: str) -> Any:
        return self.transport.request("GET", f"/v1/labs/{lab}/api-keys")

    def create_api_key(self, lab: str, request: Mapping[str, Any]) -> Any:
        return self.transport.request(
            "POST", f"/v1/labs/{lab}/api-keys", json=dict(request)
        )

    def delete_api_key(self, lab: str, key_id: str) -> Any:
        return self.transport.request(
            "DELETE", f"/v1/labs/{lab}/api-keys", params={"key_id": key_id}
        )

    def list_peers(self, lab: str) -> Any:
        return self.transport.request("GET", f"/v1/labs/{lab}/peers")

    def create_peer(self, lab: str, request: Mapping[str, Any]) -> Any:
        return self.transport.request(
            "POST", f"/v1/labs/{lab}/peers", json=dict(request)
        )

    def get_usage(self, lab: str, period: str) -> Any:
        return self.transport.request(
            "GET", f"/v1/labs/{lab}/usage", params={"period": period}
        )
