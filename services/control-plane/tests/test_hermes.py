from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from scilab.hermes import HermesClient


class FakeResponse:
    def __init__(
        self,
        payload: dict[str, object] | None = None,
        lines: tuple[str, ...] = (),
        status_code: int = 200,
    ) -> None:
        self.payload = payload or {}
        self.lines = lines
        self.status_code = status_code

    def json(self) -> dict[str, object]:
        return self.payload

    async def aiter_lines(self) -> AsyncIterator[str]:
        for line in self.lines:
            yield line


class FakeTransport:
    def __init__(self, responses: list[FakeResponse] | None = None) -> None:
        self.responses = list(responses or [])
        self.calls: list[dict[str, object]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.responses.pop(0) if self.responses else FakeResponse({"id": "run-1"})


def client(transport: FakeTransport) -> HermesClient:
    return HermesClient(
        lab_endpoints={
            "lab-a": "https://hermes-a.internal",
            "lab-b": "https://hermes-b.internal",
        },
        api_key="api-key-1",
        session_id="session-1",
        session_key="session-key-1",
        transport=transport,
    )


def test_start_run_uses_the_lab_runs_endpoint_and_protocol_headers() -> None:
    async def scenario() -> None:
        transport = FakeTransport([FakeResponse({"run_id": "run-7"})])

        run_id = await client(transport).start_run(
            "lab-b",
            {"input": "find evidence"},
            idempotency_key="idem-7",
        )

        assert run_id == "run-7"
        call = transport.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == "https://hermes-b.internal/v1/runs"
        assert "chat/completions" not in str(call["url"])
        assert call["json"] == {"input": "find evidence"}
        assert call["headers"] == {
            "Authorization": "Bearer api-key-1",
            "X-Hermes-Session-Id": "session-1",
            "X-Hermes-Session-Key": "session-key-1",
            "Idempotency-Key": "idem-7",
        }

    asyncio.run(scenario())


def test_chat_completion_uses_stateless_openai_compatible_endpoint() -> None:
    async def scenario() -> None:
        transport = FakeTransport(
            [
                FakeResponse(
                    {
                        "id": "chatcmpl-1",
                        "object": "chat.completion",
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": "The report records a 42 percent response rate.",
                                },
                                "finish_reason": "stop",
                            }
                        ],
                    }
                )
            ]
        )
        hermes = client(transport)
        complete = getattr(hermes, "chat_completion", None)
        assert callable(complete), "HermesClient needs a separate chat completion method"

        answer = await complete(
            "lab-a",
            model="scilab-test-model",
            messages=[
                {"role": "system", "content": "Answer from supplied evidence."},
                {"role": "user", "content": "What was the response rate?"},
            ],
        )

        assert answer == "The report records a 42 percent response rate."
        assert transport.calls == [
            {
                "method": "POST",
                "url": "https://hermes-a.internal/v1/chat/completions",
                "headers": {"Authorization": "Bearer api-key-1"},
                "json": {
                    "model": "scilab-test-model",
                    "messages": [
                        {"role": "system", "content": "Answer from supplied evidence."},
                        {"role": "user", "content": "What was the response rate?"},
                    ],
                    "stream": False,
                },
            }
        ]

    asyncio.run(scenario())


def test_start_run_rejects_a_response_without_run_id() -> None:
    async def scenario() -> None:
        transport = FakeTransport([FakeResponse({"id": "legacy-id"})])

        with pytest.raises(ValueError, match="run_id"):
            await client(transport).start_run(
                "lab-a",
                {"input": "find evidence"},
                idempotency_key="idem-legacy",
            )

    asyncio.run(scenario())


def test_events_reads_vendor_data_event_and_named_sse() -> None:
    async def scenario() -> None:
        transport = FakeTransport(
            [
                FakeResponse(
                    lines=(
                        "id: 1",
                        "event: run.state",
                        'data: {"state":"running"}',
                        "",
                        "event: message",
                        'data: {"event":"approval.request","run_id":"run-1","request_id":"req-1"}',
                        "",
                        'data: {"event":"run.completed","run_id":"run-1"}',
                        "",
                        'data: {"state":"untyped"}',
                        "",
                        'data: {"event":"unknown.event","run_id":"run-1"}',
                        "",
                    )
                )
            ]
        )

        events = [event async for event in client(transport).events("lab-a", "run-1")]

        assert events == [
            {"id": "1", "event": "run.state", "data": {"state": "running"}},
            {"event": "approval.request", "run_id": "run-1", "request_id": "req-1"},
            {"event": "run.completed", "run_id": "run-1"},
            {"id": None, "event": "message", "data": {"state": "untyped"}},
            {
                "id": None,
                "event": "message",
                "data": {"event": "unknown.event", "run_id": "run-1"},
            },
        ]
        call = transport.calls[0]
        assert call["method"] == "GET"
        assert call["url"] == "https://hermes-a.internal/v1/runs/run-1/events"
        assert call["headers"] == {
            "Authorization": "Bearer api-key-1",
            "X-Hermes-Session-Id": "session-1",
            "X-Hermes-Session-Key": "session-key-1",
            "Accept": "text/event-stream",
        }

    asyncio.run(scenario())


def test_events_rejects_invalid_json() -> None:
    async def scenario() -> None:
        transport = FakeTransport([FakeResponse(lines=("data: {bad", ""))])
        with pytest.raises(ValueError):
            [event async for event in client(transport).events("lab-a", "run-1")]

    asyncio.run(scenario())


def test_events_rejects_a_vendor_frame_for_another_run() -> None:
    async def scenario() -> None:
        transport = FakeTransport(
            [
                FakeResponse(
                    lines=(
                        'data: {"event":"approval.request","run_id":"run-2","request_id":"req-1"}',
                        "",
                    )
                )
            ]
        )
        with pytest.raises(ValueError, match="run_id"):
            [event async for event in client(transport).events("lab-a", "run-1")]

    asyncio.run(scenario())


@pytest.mark.parametrize("decision,choice", [("approve", "once"), ("reject", "deny")])
def test_respond_approval_posts_exact_request_and_validates_ack(
    decision: str, choice: str
) -> None:
    async def scenario() -> None:
        ack = {
            "object": "hermes.run.approval_response",
            "run_id": "run-1",
            "request_id": "req-1",
            "choice": choice,
            "resolved": 1,
        }
        transport = FakeTransport([FakeResponse(ack)])
        result = await client(transport).respond_approval(
            "lab-a", "run-1", "req-1", decision
        )
        assert result == ack
        assert transport.calls == [
            {
                "method": "POST",
                "url": "https://hermes-a.internal/v1/runs/run-1/approval",
                "headers": {
                    "Authorization": "Bearer api-key-1",
                    "X-Hermes-Session-Id": "session-1",
                    "X-Hermes-Session-Key": "session-key-1",
                },
                "json": {"choice": choice, "request_id": "req-1"},
            }
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "value", "status"),
    [
        ("resolved", 0, 409),
        ("run_id", "run-2", 200),
        ("request_id", "req-2", 200),
        ("choice", "deny", 200),
        ("resolved", True, 200),
        ("object", "wrong.response", 200),
    ],
)
def test_respond_approval_rejects_unconfirmed_ack(
    field: str, value: object, status: int
) -> None:
    async def scenario() -> None:
        payload: dict[str, object] = {
            "object": "hermes.run.approval_response",
            "run_id": "run-1",
            "request_id": "req-1",
            "choice": "once",
            "resolved": 1,
        }
        payload[field] = value
        transport = FakeTransport([FakeResponse(payload, status_code=status)])
        with pytest.raises(ValueError):
            await client(transport).respond_approval("lab-a", "run-1", "req-1", "approve")

    asyncio.run(scenario())


def test_respond_approval_rejects_oversized_request_id_before_http() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        with pytest.raises(ValueError, match="request_id"):
            await client(transport).respond_approval(
                "lab-a", "run-1", "r" * 257, "approve"
            )
        assert transport.calls == []

    asyncio.run(scenario())


def test_respond_approval_rejects_invalid_decision_without_http() -> None:
    async def scenario() -> None:
        transport = FakeTransport()
        with pytest.raises(ValueError):
            await client(transport).respond_approval("lab-a", "run-1", "req-1", "always")
        assert transport.calls == []

    asyncio.run(scenario())


def test_stop_posts_to_the_run_stop_endpoint() -> None:
    async def scenario() -> None:
        transport = FakeTransport([FakeResponse({"stopped": True})])

        await client(transport).stop("lab-a", "run-1")

        call = transport.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == "https://hermes-a.internal/v1/runs/run-1/stop"
        assert call["headers"] == {
            "Authorization": "Bearer api-key-1",
            "X-Hermes-Session-Id": "session-1",
            "X-Hermes-Session-Key": "session-key-1",
        }

    asyncio.run(scenario())


def test_ask_lab_starts_a_run_through_the_runs_api() -> None:
    async def scenario() -> None:
        transport = FakeTransport([FakeResponse({"run_id": "run-ask"})])

        await client(transport).ask_lab(
            "lab-a",
            "What does the evidence show?",
            idempotency_key="idem-ask",
            role="Literature",
        )

        call = transport.calls[0]
        assert call["method"] == "POST"
        assert call["url"] == "https://hermes-a.internal/v1/runs"
        assert "chat/completions" not in str(call["url"])
        assert call["headers"]["Idempotency-Key"] == "idem-ask"
        assert call["json"] == {
            "input": '{"prompt":"What does the evidence show?","role":"Literature"}'
        }

    asyncio.run(scenario())


def test_default_concurrency_makes_the_eleventh_local_call_wait() -> None:
    class BlockingTransport(FakeTransport):
        def __init__(self) -> None:
            super().__init__()
            self.ten_started = asyncio.Event()
            self.release = asyncio.Event()
            self.active = 0
            self.max_active = 0

        async def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            self.calls.append({"method": method, "url": url, **kwargs})
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active == 10:
                self.ten_started.set()
            try:
                await self.release.wait()
                return FakeResponse({"run_id": f"run-{len(self.calls)}"})
            finally:
                self.active -= 1

    async def scenario() -> None:
        transport = BlockingTransport()
        hermes = client(transport)
        tasks = [
            asyncio.create_task(
                hermes.start_run(
                    "lab-a",
                    {"prompt": str(index)},
                    idempotency_key=f"idem-{index}",
                )
            )
            for index in range(11)
        ]

        await transport.ten_started.wait()
        await asyncio.sleep(0)
        assert len(transport.calls) == 10

        transport.release.set()
        await asyncio.gather(*tasks)
        assert transport.max_active == 10

    asyncio.run(scenario())


def test_approval_receipt_uses_the_lab_endpoint_and_returns_only_bound_fields() -> None:
    async def scenario() -> None:
        transport = FakeTransport(
            [
                FakeResponse(
                    {
                        "object": "hermes.run.approval_response",
                        "run_id": "hermes-1",
                        "request_id": "req-1",
                        "choice": "once",
                        "resolved": 1,
                        "state": "committed",
                        "command": "must-not-escape",
                    }
                )
            ]
        )
        receipt = await client(transport).approval_receipt("lab-b", "hermes-1", "req-1")

        assert receipt == {
            "object": "hermes.run.approval_response",
            "run_id": "hermes-1",
            "request_id": "req-1",
            "choice": "once",
            "resolved": 1,
        }
        assert transport.calls == [
            {
                "method": "GET",
                "url": "https://hermes-b.internal/v1/runs/hermes-1/approvals/req-1",
                "headers": {
                    "Authorization": "Bearer api-key-1",
                    "X-Hermes-Session-Id": "session-1",
                    "X-Hermes-Session-Key": "session-key-1",
                },
            }
        ]

    asyncio.run(scenario())


def test_approval_receipt_returns_none_for_404() -> None:
    async def scenario() -> None:
        transport = FakeTransport([FakeResponse(status_code=404)])

        assert await client(transport).approval_receipt("lab-a", "hermes-1", "req-1") is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status_code", "updates"),
    [
        (409, {"detail": "must-not-escape"}),
        (200, {"state": "prepared"}),
        (200, {"object": "wrong.object"}),
        (200, {"run_id": "hermes-2"}),
        (200, {"request_id": "req-2"}),
        (200, {"choice": "always"}),
        (200, {"resolved": 0}),
        (200, {"resolved": True}),
    ],
)
def test_approval_receipt_rejects_unconfirmed_or_unbound_receipts(
    status_code: int, updates: dict[str, object]
) -> None:
    async def scenario() -> None:
        payload: dict[str, object] = {
            "object": "hermes.run.approval_response",
            "run_id": "hermes-1",
            "request_id": "req-1",
            "choice": "once",
            "resolved": 1,
        }
        payload.update(updates)
        transport = FakeTransport([FakeResponse(payload, status_code=status_code)])

        with pytest.raises(ValueError) as error:
            await client(transport).approval_receipt("lab-a", "hermes-1", "req-1")

        assert type(error.value).__name__ == "HermesReceiptConflict"
        assert "must-not-escape" not in str(error.value)

    asyncio.run(scenario())


def test_approval_receipt_rejects_an_oversized_request_id_before_http() -> None:
    async def scenario() -> None:
        transport = FakeTransport()

        with pytest.raises(ValueError, match="request_id"):
            await client(transport).approval_receipt("lab-a", "hermes-1", "r" * 257)

        assert transport.calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("status_code", [401, 503])
def test_approval_receipt_sanitizes_non_success_http_errors(status_code: int) -> None:
    async def scenario() -> None:
        transport = FakeTransport(
            [FakeResponse({"detail": "must-not-escape"}, status_code=status_code)]
        )

        with pytest.raises(RuntimeError) as error:
            await client(transport).approval_receipt("lab-a", "hermes-1", "req-1")

        assert type(error.value).__name__ == "HermesReceiptUnavailable"
        assert "must-not-escape" not in str(error.value)

    asyncio.run(scenario())


def test_approval_receipt_sanitizes_transport_timeouts() -> None:
    class TimeoutTransport:
        async def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
            raise TimeoutError("must-not-escape")

    async def scenario() -> None:
        with pytest.raises(RuntimeError) as error:
            await client(TimeoutTransport()).approval_receipt("lab-a", "hermes-1", "req-1")

        assert type(error.value).__name__ == "HermesReceiptUnavailable"
        assert "must-not-escape" not in str(error.value)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", ["running", "interrupted", "completed", "failed", "cancelled"])
def test_run_status_returns_only_the_exact_lab_run_status(status: str) -> None:
    async def scenario() -> None:
        transport = FakeTransport(
            [FakeResponse({"run_id": "hermes-1", "status": status, "command": "private"})]
        )

        assert await client(transport).run_status("lab-b", "hermes-1") == status
        assert transport.calls == [
            {
                "method": "GET",
                "url": "https://hermes-b.internal/v1/runs/hermes-1",
                "headers": {
                    "Authorization": "Bearer api-key-1",
                    "X-Hermes-Session-Id": "session-1",
                    "X-Hermes-Session-Key": "session-key-1",
                },
            }
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"run_id": "hermes-2", "status": "running"},
        {"run_id": "hermes-1", "status": " "},
        {"run_id": "hermes-1", "status": 1},
    ],
)
def test_run_status_rejects_malformed_or_wrong_run_response(
    payload: dict[str, object],
) -> None:
    async def scenario() -> None:
        transport = FakeTransport([FakeResponse(payload)])

        with pytest.raises(ValueError, match="run status"):
            await client(transport).run_status("lab-a", "hermes-1")

    asyncio.run(scenario())


def test_hermes_run_and_request_ids_are_encoded_as_path_segments() -> None:
    async def scenario() -> None:
        hermes_run_id = "h?1%2F"
        request_id = "req?part%2F"
        transport = FakeTransport(
            [
                FakeResponse(
                    {
                        "object": "hermes.run.approval_response",
                        "run_id": hermes_run_id,
                        "request_id": request_id,
                        "choice": "once",
                        "resolved": 1,
                    }
                ),
                FakeResponse({"run_id": hermes_run_id, "status": "running"}),
            ]
        )
        hermes = client(transport)

        assert await hermes.approval_receipt("lab-a", hermes_run_id, request_id) == {
            "object": "hermes.run.approval_response",
            "run_id": hermes_run_id,
            "request_id": request_id,
            "choice": "once",
            "resolved": 1,
        }
        assert await hermes.run_status("lab-a", hermes_run_id) == "running"
        assert [call["url"] for call in transport.calls] == [
            "https://hermes-a.internal/v1/runs/h%3F1%252F/approvals/req%3Fpart%252F",
            "https://hermes-a.internal/v1/runs/h%3F1%252F",
        ]

    asyncio.run(scenario())
