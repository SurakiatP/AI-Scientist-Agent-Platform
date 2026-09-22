from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def parse_tor_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    if isinstance(value, str):
        if "T" not in value and "t" not in value:
            raise ValueError("must be an ISO 8601 date-time")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError as exc:
            raise ValueError("must be an ISO 8601 date-time") from exc
    elif not isinstance(value, datetime):
        raise TypeError("must be a datetime or ISO 8601 date-time string")
    if parsed.tzinfo is None:
        raise ValueError("must include a timezone")
    return parsed


class TimestampModel(StrictModel):
    @field_validator("ts", "expires_at", mode="before", check_fields=False)
    @classmethod
    def validate_tor_datetime(cls, value: object) -> datetime:
        return parse_tor_datetime(value)


EventType = Literal[
    "run.state",
    "tool.started",
    "tool.progress",
    "tool.finished",
    "delegation.started",
    "delegation.finished",
    "approval.required",
    "artifact.registered",
    "cost.updated",
    "run.completed",
    "run.failed",
]
EventSource = Literal["hermes", "run-service", "policy", "sandbox"]
ManifestSource = Literal["web", "rest", "a2a", "mcp"]


class RunStatePayload(StrictModel):
    from_state: str = Field(alias="from")
    to: str
    reason: str


class ToolPayload(StrictModel):
    tool: str
    args_redacted: dict[str, Any]
    duration_ms: int
    ok: bool
    error: str | None = None


class DelegationPayload(StrictModel):
    delegation_id: str
    role: str
    goal: str
    child_count: int
    schema_valid: bool | None = None


class ApprovalPayload(TimestampModel):
    approval_id: str
    action: str
    reason: str
    policy_rule: str
    expires_at: datetime
    preview: dict[str, Any]


class ArtifactPayload(StrictModel):
    artifact_id: str
    kind: str
    uri: str
    sha256: str
    bytes: int
    produced_by_step: int


class CostPayload(StrictModel):
    tokens_in: int
    tokens_out: int
    llm_cost_thb: float
    compute_cost_thb: float
    budget_remaining_thb: float | None


class CompletedPayload(StrictModel):
    summary: str
    report_artifact_id: str
    manifest_artifact_id: str
    claims_count: int


EventPayload = (
    dict[str, Any]
    | RunStatePayload
    | ToolPayload
    | DelegationPayload
    | ApprovalPayload
    | ArtifactPayload
    | CostPayload
    | CompletedPayload
)

EVENT_PAYLOAD_MODELS = {
    "run.state": RunStatePayload,
    "tool.started": ToolPayload,
    "tool.finished": ToolPayload,
    "delegation.started": DelegationPayload,
    "delegation.finished": DelegationPayload,
    "approval.required": ApprovalPayload,
    "artifact.registered": ArtifactPayload,
    "cost.updated": CostPayload,
    "run.completed": CompletedPayload,
}


class RunEvent(TimestampModel):
    event_id: str
    run_id: str
    lab_id: str
    ts: datetime
    type: EventType
    seq: int
    payload: Annotated[EventPayload, Field(union_mode="left_to_right")]
    source: EventSource

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema: Any, handler: Any) -> dict[str, Any]:
        schema = handler(core_schema)
        payload_refs = schema["properties"]["payload"]["anyOf"]
        schema["allOf"] = [
            {
                "if": {
                    "properties": {"type": {"const": event_type}},
                    "required": ["type"],
                },
                "then": {
                    "properties": {
                        "payload": next(
                            payload_ref
                            for payload_ref in payload_refs
                            if payload_model.__name__ in payload_ref.get("$ref", "")
                        )
                    },
                    "required": ["payload"],
                },
            }
            for event_type, payload_model in EVENT_PAYLOAD_MODELS.items()
        ]
        return schema

    @model_validator(mode="after")
    def validate_payload_for_event_type(self) -> "RunEvent":
        payload_model = EVENT_PAYLOAD_MODELS.get(self.type)
        if payload_model is not None:
            self.payload = payload_model.model_validate(self.payload)
        elif not isinstance(self.payload, dict):
            raise TypeError("generic event payload must be an object")
        return self


class HermesManifest(StrictModel):
    image: str
    config_sha256: str
    model_aliases: "ModelAliases"


class ModelAliases(StrictModel):
    pi: str
    child: str


class ManifestInput(StrictModel):
    artifact_id: str
    sha256: str
    name: str


class ManifestStep(StrictModel):
    n: int
    role: str
    delegation_id: str
    commands_log: str
    outputs: list[str]


class ManifestClaim(StrictModel):
    id: str
    text: str
    evidence: list[str]
    confidence: float = Field(ge=0, le=1)


class ManifestCost(StrictModel):
    tokens_in: int
    tokens_out: int
    llm_thb: float
    compute_thb: float


class RunManifest(StrictModel):
    run_id: str
    lab_id: str
    actor: str
    source: ManifestSource
    goal: str
    skill_packs: list[str]
    hermes: HermesManifest
    skills_image: str
    sandbox_image: str
    inputs: list[ManifestInput]
    steps: list[ManifestStep]
    claims: list[ManifestClaim]
    cost: ManifestCost
    sealed_at: datetime
    manifest_sha256: str

    @field_validator("sealed_at", mode="before")
    @classmethod
    def validate_sealed_at(cls, value: object) -> datetime:
        return parse_tor_datetime(value)


class ResearchClaim(StrictModel):
    text: str
    evidence: list[str]
    confidence: float = Field(ge=0, le=1)


class ResearchResult(StrictModel):
    claims: list[ResearchClaim]
    artifacts: list[str]
    caveats: list[str]
    next_steps: list[str] = Field(default_factory=list)
