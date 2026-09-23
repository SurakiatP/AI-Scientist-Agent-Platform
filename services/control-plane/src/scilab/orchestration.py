from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .contracts import ResearchResult


class ResearchCycle:
    def __init__(
        self,
        client: Any,
        lab_id: str,
        pi_provider: str,
        reviewer_provider: str,
    ) -> None:
        if pi_provider == reviewer_provider:
            raise ValueError("Reviewer provider must differ from PI provider")
        self.client = client
        self.lab_id = lab_id
        self.pi_provider = pi_provider
        self.reviewer_provider = reviewer_provider

    def plan(self) -> dict[str, Any]:
        return {
            "stage": "plan",
            "role": "PI",
            "model_alias": "sci-pi-frontier",
            "provider": self.pi_provider,
        }

    def gather(self) -> dict[str, Any]:
        return {
            "stage": "gather",
            "delegations": [
                self._delegation("Literature", "sci-longctx", self.pi_provider),
            ],
        }

    def analyze(self) -> dict[str, Any]:
        return {
            "stage": "analyze",
            "delegations": [
                self._delegation("Data Scientist", "sci-specialist", self.pi_provider),
                self._delegation("Domain Specialist", "sci-specialist", self.pi_provider),
            ],
        }

    def critique(self, rounds: int = 1) -> dict[str, Any]:
        return {
            "stage": "critique",
            "max_rounds": min(rounds, 2),
            "delegations": [
                self._delegation("Reviewer", "sci-reviewer", self.reviewer_provider),
            ],
        }

    def report(self) -> dict[str, Any]:
        return {
            "stage": "report",
            "delegations": [
                self._delegation("Writer", "sci-specialist", self.pi_provider),
            ],
        }

    def request(
        self,
        question: str,
        *,
        inputs: Sequence[str] = (),
        skill_packs: Sequence[str] = (),
    ) -> dict[str, Any]:
        specification = {
            "goal": question,
            "stages": [
                self.plan(),
                self.gather(),
                self.analyze(),
                self.critique(),
                self.report(),
            ],
            "delegation": {
                "default_concurrency": 10,
                "schema_repair_attempts": 1,
                "on_invalid_after_repair": {
                    "schema_valid": False,
                    "include_raw_summary": True,
                },
            },
        }
        if inputs:
            specification["inputs"] = list(inputs)
        if skill_packs:
            specification["skill_packs"] = list(skill_packs)
        return {"input": json.dumps(specification, separators=(",", ":"))}

    async def run(
        self,
        question: str,
        *,
        idempotency_key: str,
        session_key: str | None = None,
        inputs: Sequence[str] = (),
        skill_packs: Sequence[str] = (),
    ) -> str:
        options = {"session_key": session_key} if session_key is not None else {}
        return await self.client.start_run(
            self.lab_id,
            self.request(question, inputs=inputs, skill_packs=skill_packs),
            idempotency_key=idempotency_key,
            **options,
        )

    @staticmethod
    def _delegation(role: str, model_alias: str, provider: str) -> dict[str, Any]:
        return {
            "role": role,
            "tool": "delegate_task",
            "model_alias": model_alias,
            "provider": provider,
            "output_schema": ResearchResult.model_json_schema(),
        }
