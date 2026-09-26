from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any, Protocol
from urllib.parse import quote

from scilab.redaction import redact as _redact

_HERMES_DATA_EVENTS = frozenset({"approval.request", "run.completed", "run.failed"})
_HERMES_TOOL_EVENTS = {"tool.started": "tool.started", "tool.completed": "tool.finished"}
_HERMES_DELEGATION_EVENTS = {
    "subagent.start": "delegation.started",
    "subagent.complete": "delegation.finished",
}


def _translate_tool_or_delegation(
    vendor_event: str, payload: Mapping[str, Any], run_id: str, event_id: str | None
) -> dict[str, object] | None:
    """Translate a Hermes tool/subagent SSE frame into an I2-shaped platform event.

    Returns None for a malformed frame (e.g. a blank tool name), so the caller
    falls back to the generic "message" wrapping.
    """
    if payload.get("run_id") != run_id:
        raise ValueError("Hermes event run_id does not match requested run_id")

    if vendor_event in _HERMES_TOOL_EVENTS:
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            return None
        args_redacted = _redact({"preview": payload.get("preview")})
        if vendor_event == "tool.started":
            data = {
                "tool": tool,
                "args_redacted": args_redacted,
                "duration_ms": 0,
                "ok": True,
                "error": None,
            }
        else:
            error_flag = bool(payload.get("error"))
            try:
                duration_ms = round(float(payload.get("duration") or 0) * 1000)
            except (TypeError, ValueError):
                return None
            data = {
                "tool": tool,
                "args_redacted": args_redacted,
                "duration_ms": duration_ms,
                "ok": not error_flag,
                "error": None,
            }
        return {
            "id": event_id,
            "event": _HERMES_TOOL_EVENTS[vendor_event],
            "run_id": run_id,
            "data": data,
        }

    delegation_id = payload.get("delegation_id") or payload.get("subagent_id")
    if not isinstance(delegation_id, str) or not delegation_id.strip():
        return None
    try:
        child_count = int(payload.get("task_count") or 1)
    except (TypeError, ValueError):
        return None
    data = {
        "delegation_id": delegation_id,
        "role": payload.get("role") or "subagent",
        "goal": payload.get("goal") or "",
        "child_count": child_count,
        "schema_valid": None,
    }
    event: dict[str, object] = {
        "id": event_id,
        "event": _HERMES_DELEGATION_EVENTS[vendor_event],
        "run_id": run_id,
        "data": data,
    }
    if vendor_event == "subagent.complete":
        event["usage"] = {
            "model": payload.get("model"),
            "input_tokens": payload.get("input_tokens"),
            "output_tokens": payload.get("output_tokens"),
            "cost_usd": payload.get("cost_usd"),
        }
    return event


class HermesReceiptConflict(ValueError):
    """The vendor could not prove the requested approval receipt."""


class HermesReceiptUnavailable(RuntimeError):
    """The vendor approval receipt could not be checked safely."""


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

    def _headers(
        self, *, accept: str | None = None, session_key: str | None = None
    ) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Hermes-Session-Id": self._session_id,
            "X-Hermes-Session-Key": (
                _nonblank(session_key, "session_key") if session_key is not None else self._session_key
            ),
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
        session_key: str | None = None,
    ) -> str:
        headers = self._headers(session_key=session_key)
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

            def normalize(payload: object) -> dict[str, object]:
                vendor_event = payload.get("event") if isinstance(payload, dict) else None
                if (
                    event_name in (None, "message")
                    and isinstance(payload, dict)
                    and isinstance(vendor_event, str)
                    and vendor_event in _HERMES_DATA_EVENTS
                ):
                    if payload.get("run_id") != run_id:
                        raise ValueError("Hermes event run_id does not match requested run_id")
                    normalized = dict(payload)
                    if event_id is not None:
                        normalized.setdefault("id", event_id)
                    return normalized
                if (
                    event_name in (None, "message")
                    and isinstance(payload, dict)
                    and isinstance(vendor_event, str)
                    and (vendor_event in _HERMES_TOOL_EVENTS or vendor_event in _HERMES_DELEGATION_EVENTS)
                ):
                    translated = _translate_tool_or_delegation(vendor_event, payload, run_id, event_id)
                    if translated is not None:
                        return translated
                return {
                    "id": event_id,
                    "event": event_name or "message",
                    "data": payload,
                }

            async for line in response.aiter_lines():
                if not line:
                    if data:
                        yield normalize(json.loads("\n".join(data)))
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
                yield normalize(json.loads("\n".join(data)))

    async def respond_approval(
        self, lab_id: str, run_id: str, request_id: str, decision: str
    ) -> dict[str, object]:
        if decision not in ("approve", "reject"):
            raise ValueError("decision must be approve or reject")
        run_id = _nonblank(run_id, "run_id")
        request_id = _nonblank(request_id, "request_id")
        if len(request_id) > 256:
            raise ValueError("request_id must be at most 256 characters")
        choice = "once" if decision == "approve" else "deny"
        async with self._semaphore:
            response = await self._transport.request(
                "POST",
                self._url(lab_id, f"/v1/runs/{run_id}/approval"),
                headers=self._headers(),
                json={"choice": choice, "request_id": request_id},
            )
        if response.status_code != 200:
            raise ValueError("Hermes approval was not acknowledged")
        payload = response.json()
        if (
            not isinstance(payload, Mapping)
            or payload.get("object") != "hermes.run.approval_response"
            or payload.get("run_id") != run_id
            or payload.get("request_id") != request_id
            or payload.get("choice") != choice
            or type(payload.get("resolved")) is not int
            or payload["resolved"] != 1
        ):
            raise ValueError("Hermes approval acknowledgment mismatch")
        return dict(payload)

    async def approval_receipt(
        self, lab_id: str, hermes_run_id: str, request_id: str
    ) -> dict[str, object] | None:
        hermes_run_id = _nonblank(hermes_run_id, "hermes_run_id")
        request_id = _nonblank(request_id, "request_id")
        if len(request_id) > 256:
            raise ValueError("request_id must be at most 256 characters")
        url = self._url(
            lab_id,
            f"/v1/runs/{quote(hermes_run_id, safe='')}/approvals/"
            f"{quote(request_id, safe='')}",
        )
        headers = self._headers()
        try:
            async with self._semaphore:
                response = await self._transport.request("GET", url, headers=headers)
        except Exception:
            raise HermesReceiptUnavailable("Hermes approval receipt unavailable") from None

        if response.status_code == 404:
            return None
        if response.status_code == 409:
            raise HermesReceiptConflict("Hermes approval receipt is not confirmed")
        if response.status_code != 200:
            raise HermesReceiptUnavailable("Hermes approval receipt unavailable")
        try:
            payload = response.json()
        except Exception:
            raise HermesReceiptConflict("Hermes approval receipt response mismatch") from None
        if (
            not isinstance(payload, Mapping)
            or payload.get("object") != "hermes.run.approval_response"
            or payload.get("run_id") != hermes_run_id
            or payload.get("request_id") != request_id
            or payload.get("choice") not in ("once", "deny")
            or type(payload.get("resolved")) is not int
            or payload.get("resolved") != 1
            or ("state" in payload and payload.get("state") != "committed")
        ):
            raise HermesReceiptConflict("Hermes approval receipt response mismatch")
        return {
            "object": payload["object"],
            "run_id": payload["run_id"],
            "request_id": payload["request_id"],
            "choice": payload["choice"],
            "resolved": payload["resolved"],
        }

    async def run_status(self, lab_id: str, hermes_run_id: str) -> str:
        hermes_run_id = _nonblank(hermes_run_id, "hermes_run_id")
        url = self._url(lab_id, f"/v1/runs/{quote(hermes_run_id, safe='')}")
        headers = self._headers()
        try:
            async with self._semaphore:
                response = await self._transport.request("GET", url, headers=headers)
        except Exception:
            raise HermesReceiptUnavailable("Hermes Run status unavailable") from None
        if response.status_code != 200:
            raise HermesReceiptUnavailable("Hermes run status unavailable")
        try:
            payload = response.json()
        except Exception:
            raise ValueError("Hermes run status response mismatch") from None
        if (
            not isinstance(payload, Mapping)
            or payload.get("run_id") != hermes_run_id
            or not isinstance(payload.get("status"), str)
            or not payload["status"].strip()
            or payload["status"] != payload["status"].strip()
        ):
            raise ValueError("Hermes run status response mismatch")
        return payload["status"]

    async def stop(self, lab_id: str, run_id: str) -> Any:
        async with self._semaphore:
            response = await self._transport.request(
                "POST",
                self._url(lab_id, f"/v1/runs/{_nonblank(run_id, 'run_id')}/stop"),
                headers=self._headers(),
            )
        return response.json()

    async def chat_completion(
        self,
        lab_id: str,
        *,
        model: str,
        messages: list[Mapping[str, str]],
    ) -> str:
        if not messages:
            raise ValueError("messages must not be empty")
        request = {
            "model": _nonblank(model, "model"),
            "messages": [dict(message) for message in messages],
            "stream": False,
        }
        async with self._semaphore:
            response = await self._transport.request(
                "POST",
                self._url(lab_id, "/v1/chat/completions"),
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=request,
            )
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise ValueError("Hermes completion response must be an object")
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            raise ValueError("Hermes completion response has no choices")
        message = choices[0].get("message")
        if not isinstance(message, Mapping):
            raise ValueError("Hermes completion response has no assistant message")
        return _nonblank(message.get("content"), "completion content")
