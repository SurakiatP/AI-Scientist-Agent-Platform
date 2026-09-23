import asyncio

from scilab.orchestration import ResearchCycle


class HermesClient:
    def __init__(self) -> None:
        self.session_keys: list[str | None] = []

    async def start_run(
        self,
        lab_id: str,
        request: dict[str, object],
        *,
        idempotency_key: str,
        session_key: str | None = None,
    ) -> str:
        self.session_keys.append(session_key)
        return "hermes-run-1"


def test_research_cycle_forwards_a2a_context_id_as_hermes_session_key() -> None:
    client = HermesClient()
    cycle = ResearchCycle(
        client,
        lab_id="lab-a",
        pi_provider="provider-a",
        reviewer_provider="provider-b",
    )

    run_id = asyncio.run(
        cycle.run(
            "Investigate the question.",
            idempotency_key="a2a-idempotency-key",
            session_key="a2a-context-1",
        )
    )

    assert run_id == "hermes-run-1"
    assert client.session_keys == ["a2a-context-1"]
