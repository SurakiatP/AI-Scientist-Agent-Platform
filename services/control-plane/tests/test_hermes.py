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
    ) -> None:
        self.payload = payload or {}
        self.lines = lines

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


def test_events_reads_run_sse() -> None:
    async def scenario() -> None:
        transport = FakeTransport(
            [
                FakeResponse(
                    lines=(
                        "id: 1",
                        "event: run.state",
                        'data: {"state":"running"}',
                        "",
                    )
                )
            ]
        )

        events = [event async for event in client(transport).events("lab-a", "run-1")]

        assert events == [
            {"id": "1", "event": "run.state", "data": {"state": "running"}}
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
