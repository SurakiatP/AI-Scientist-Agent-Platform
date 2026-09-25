from __future__ import annotations

import calendar
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from scilab.db import SET_TENANT_SQL, TENANT_SETTING
from scilab.identity import Identity
from scilab.metering import MeteringService
from scilab.tenancy import require_scope


class UsagePeriodService:
    def __init__(self, metering: MeteringService) -> None:
        self.metering = metering

    def get(self, identity: Identity, period: str) -> dict[str, Any]:
        if period not in {"daily", "monthly"}:
            raise HTTPException(status_code=422, detail="period must be daily or monthly")

        now = self.metering.clock().astimezone(timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "monthly":
            start = start.replace(day=1)
        end = start + timedelta(
            days=1 if period == "daily" else calendar.monthrange(start.year, start.month)[1]
        )

        require_scope(identity, "runs:read")
        with self.metering.connection.transaction():
            with self.metering.connection.cursor() as cursor:
                cursor.execute(SET_TENANT_SQL, (TENANT_SETTING, identity.lab_id))
                cursor.execute(
                    "SELECT COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0), "
                    "COALESCE(SUM(compute), 0), COALESCE(SUM(llm_cost_thb), 0), "
                    "COALESCE(SUM(compute_cost_thb), 0) FROM metering_usage "
                    "WHERE lab_id = %s AND recorded_at >= %s AND recorded_at < %s",
                    (identity.lab_id, start, end),
                )
                totals = cursor.fetchone() or (0, 0, 0, 0, 0)

        return {
            "period": period,
            "window": {"start": start.isoformat(), "end": end.isoformat()},
            "tokens_in": int(totals[0]),
            "tokens_out": int(totals[1]),
            "compute": float(totals[2]),
            "llm_cost_thb": float(totals[3]),
            "compute_cost_thb": float(totals[4]),
        }
