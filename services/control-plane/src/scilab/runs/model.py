from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class Run:
    id: str
    lab_id: str
    idempotency_key: str
    state: RunState
    reason: str | None
    retry_count: int
    max_minutes: int
    hermes_run_id: str | None
    created_at: datetime
    updated_at: datetime
    queued_at: datetime
    running_since: datetime | None
    runtime_used: timedelta
    last_heartbeat_at: datetime | None
    approval_expires_at: datetime | None
    context_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("id", "lab_id", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-blank")
        object.__setattr__(self, "state", RunState(self.state))
        if self.hermes_run_id is not None and (
            not isinstance(self.hermes_run_id, str) or not self.hermes_run_id.strip()
        ):
            raise ValueError("hermes_run_id must be non-blank when provided")
        if self.context_id is not None and (
            not isinstance(self.context_id, str) or not self.context_id.strip()
        ):
            raise ValueError("context_id must be non-blank when provided")
        if self.retry_count < 0 or self.retry_count > 2:
            raise ValueError("retry_count must be between 0 and 2")
        if self.max_minutes <= 0:
            raise ValueError("max_minutes must be positive")
        if self.runtime_used < timedelta(0):
            raise ValueError("runtime_used cannot be negative")
