import asyncio
import json
from typing import Any

from scilab.orchestration import ResearchCycle


class CapturingHermesClient:
    def __init__(self) -> None:
        self.request: dict[str, Any] | None = None

    async def start_run(
        self,
        lab_id: str,
        request: dict[str, Any],
        *,
        idempotency_key: str,
    ) -> str:
        self.request = request
        return "run-1"


def test_run_carries_selected_inputs_and_skill_pack_in_hermes_input() -> None:
    client = CapturingHermesClient()
    cycle = ResearchCycle(
        client,
        lab_id="lab-a",
        pi_provider="provider-a",
        reviewer_provider="provider-b",
    )

    run_id = asyncio.run(
        cycle.run(
            "Compare these sources.",
            idempotency_key="idem-1",
            inputs=["artifact-1", "https://data.example.org/paper.csv"],
            skill_packs=["general-research"],
        )
    )

    assert run_id == "run-1"
    assert client.request is not None
    specification = json.loads(client.request["input"])
    assert specification["inputs"] == [
        "artifact-1",
        "https://data.example.org/paper.csv",
    ]
    assert specification["skill_packs"] == ["general-research"]
    assert [stage["stage"] for stage in specification["stages"]] == [
        "plan",
        "gather",
        "analyze",
        "critique",
        "report",
    ]
