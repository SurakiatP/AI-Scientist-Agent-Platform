import asyncio
import json

import pytest

from scilab.contracts import ResearchResult
from scilab.orchestration import ResearchCycle


class FakeHermesClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def start_run(
        self,
        lab_id: str,
        request: dict[str, object],
        *,
        idempotency_key: str,
    ) -> str:
        self.calls.append(
            {
                "lab_id": lab_id,
                "request": request,
                "idempotency_key": idempotency_key,
            }
        )
        return "run-1"


def research_cycle(client: FakeHermesClient) -> ResearchCycle:
    return ResearchCycle(
        client,
        lab_id="lab-a",
        pi_provider="provider-a",
        reviewer_provider="provider-b",
    )


def test_cycle_submits_one_pi_run_with_ordered_five_stage_plan() -> None:
    client = FakeHermesClient()
    cycle = research_cycle(client)

    run_id = asyncio.run(
        cycle.run("Investigate the research question.", idempotency_key="idem-1")
    )

    assert run_id == "run-1"
    assert len(client.calls) == 1
    assert client.calls[0]["lab_id"] == "lab-a"
    assert client.calls[0]["idempotency_key"] == "idem-1"
    request = client.calls[0]["request"]
    assert isinstance(request, dict)
    assert list(request) == ["input"]
    specification = json.loads(request["input"])
    assert specification["goal"] == "Investigate the research question."
    assert [stage["stage"] for stage in specification["stages"]] == [
        "plan",
        "gather",
        "analyze",
        "critique",
        "report",
    ]


def test_roles_aliases_and_output_schema_match_tor_delegation_contract() -> None:
    request = research_cycle(FakeHermesClient()).request("Investigate the question.")
    stages = json.loads(request["input"])["stages"]
    delegations = [
        delegation
        for stage in stages
        for delegation in stage.get("delegations", [])
    ]

    assert [(item["role"], item["model_alias"]) for item in delegations] == [
        ("Literature", "sci-longctx"),
        ("Data Scientist", "sci-specialist"),
        ("Domain Specialist", "sci-specialist"),
        ("Reviewer", "sci-reviewer"),
        ("Writer", "sci-specialist"),
    ]
    expected_schema = ResearchResult.model_json_schema()
    assert all(item["tool"] == "delegate_task" for item in delegations)
    assert all(item["output_schema"] == expected_schema for item in delegations)
    assert stages[0]["role"] == "PI"
    assert stages[0]["model_alias"] == "sci-pi-frontier"


def test_repair_failure_and_concurrency_are_sent_to_hermes_once() -> None:
    request = research_cycle(FakeHermesClient()).request("Investigate the question.")
    specification = json.loads(request["input"])

    assert specification["delegation"] == {
        "default_concurrency": 10,
        "schema_repair_attempts": 1,
        "on_invalid_after_repair": {
            "schema_valid": False,
            "include_raw_summary": True,
        },
    }


def test_reviewer_provider_must_differ_from_pi_provider() -> None:
    with pytest.raises(ValueError, match="Reviewer provider"):
        ResearchCycle(
            FakeHermesClient(),
            lab_id="lab-a",
            pi_provider="provider-a",
            reviewer_provider="provider-a",
        )


def test_critique_ceiling_is_two_rounds() -> None:
    cycle = research_cycle(FakeHermesClient())

    assert cycle.critique(rounds=4)["max_rounds"] == 2
