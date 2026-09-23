"""A2A context must select the Hermes session for one submission only."""

import asyncio

from scilab.hermes import HermesClient


class Transport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def request(self, method: str, url: str, **kwargs: object) -> object:
        self.calls.append({"method": method, "url": url, **kwargs})

        class Response:
            def json(self) -> dict[str, str]:
                return {"run_id": "hermes-1"}

        return Response()


def test_a2a_context_overrides_only_its_hermes_submission() -> None:
    async def scenario() -> None:
        transport = Transport()
        client = HermesClient(
            lab_endpoints={"lab-a": "https://hermes-a.internal"},
            api_key="example-test-key",
            session_id="session-1",
            session_key="default-session",
            transport=transport,
        )

        await client.start_run(
            "lab-a", {"input": "first"}, idempotency_key="first", session_key="a2a-context"
        )
        await client.start_run("lab-a", {"input": "second"}, idempotency_key="second")

        assert transport.calls[0]["headers"]["X-Hermes-Session-Key"] == "a2a-context"
        assert transport.calls[1]["headers"]["X-Hermes-Session-Key"] == "default-session"

    asyncio.run(scenario())
