from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol


class HermesTransport(Protocol):
    async def request(self, method: str, url: str, **kwargs: object) -> Any:
        ...


def _nonblank(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonblank")
    return value


class HermesClient:
    def __init__(
        self,
        lab_endpoints: Mapping[str, str],
        api_key: str,
        session_id: str,
        session_key: str,
        transport: HermesTransport,
        max_concurrency: int = 10,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self._lab_endpoints = {
            _nonblank(lab_id, "lab_id"): _nonblank(endpoint, "endpoint")
            for lab_id, endpoint in lab_endpoints.items()
        }
        self._api_key = _nonblank(api_key, "api_key")
        self._session_id = _nonblank(session_id, "session_id")
        self._session_key = _nonblank(session_key, "session_key")
        self._transport = transport
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def _url(self, lab_id: str, path: str) -> str:
        lab_id = _nonblank(lab_id, "lab_id")
        try:
            endpoint = self._lab_endpoints[lab_id]
        except KeyError as exc:
            raise ValueError(f"Unknown lab_id: {lab_id}") from exc
        return f"{_nonblank(endpoint, 'endpoint').rstrip('/')}{path}"

    def _headers(self, *, accept: str | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Hermes-Session-Id": self._session_id,
            "X-Hermes-Session-Key": self._session_key,
        }
        if accept is not None:
            headers["Accept"] = accept
        return headers

    async def start_run(
        self,
        lab_id: str,
        request: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> str:
        headers = self._headers()
        headers["Idempotency-Key"] = _nonblank(idempotency_key, "idempotency_key")
        async with self._semaphore:
            response = await self._transport.request(
                "POST",
                self._url(lab_id, "/v1/runs"),
                headers=headers,
                json=dict(request),
            )
            run_id = response.json().get("run_id")
        return _nonblank(run_id, "run_id")

    async def events(self, lab_id: str, run_id: str) -> AsyncIterator[dict[str, object]]:
        headers = self._headers(accept="text/event-stream")
        async with self._semaphore:
            response = await self._transport.request(
                "GET",
                self._url(lab_id, f"/v1/runs/{_nonblank(run_id, 'run_id')}/events"),
                headers=headers,
            )
            event_id: str | None = None
            event_name: str | None = None
            data: list[str] = []

            async for line in response.aiter_lines():
                if not line:
                    if data:
                        yield {
                            "id": event_id,
                            "event": event_name or "message",
                            "data": json.loads("\n".join(data)),
                        }
                    event_id = None
                    event_name = None
                    data = []
                    continue
                if line.startswith(":"):
                    continue
                field, separator, value = line.partition(":")
                if separator and value.startswith(" "):
                    value = value[1:]
                if field == "id":
                    event_id = value
                elif field == "event":
                    event_name = value
                elif field == "data":
                    data.append(value)

            if data:
                yield {
                    "id": event_id,
                    "event": event_name or "message",
                    "data": json.loads("\n".join(data)),
                }

    async def stop(self, lab_id: str, run_id: str) -> Any:
        async with self._semaphore:
            response = await self._transport.request(
                "POST",
                self._url(lab_id, f"/v1/runs/{_nonblank(run_id, 'run_id')}/stop"),
                headers=self._headers(),
            )
        return response.json()

    async def ask_lab(
        self,
        lab_id: str,
        question: str,
        *,
        idempotency_key: str,
        **context: object,
    ) -> str:
        payload = {
            "prompt": _nonblank(question, "question"),
            **context,
        }
        return await self.start_run(
            lab_id,
            {
                "input": json.dumps(
                    payload,
                    separators=(",", ":"),
                    ensure_ascii=False,
                )
            },
            idempotency_key=idempotency_key,
        )
