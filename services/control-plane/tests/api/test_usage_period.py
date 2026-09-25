from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import pytest
from fastapi import HTTPException

from scilab.api.usage_period import UsagePeriodService
from scilab.identity import Identity
from scilab.metering import MeteringService


class _Cursor:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection
        self.result: tuple[int, int, float, float, float] | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...]) -> None:
        compact = " ".join(sql.split()).lower()
        if compact.startswith("select set_config"):
            self.connection.current_lab_id = params[1]
            return

        assert "where lab_id = %s and recorded_at >= %s and recorded_at < %s" in compact
        lab_id, start, end = params
        rows = [
            row
            for row in self.connection.rows
            if row["lab_id"] == self.connection.current_lab_id == lab_id
            and start <= row["recorded_at"] < end
        ]
        self.result = (
            sum(row["tokens_in"] for row in rows),
            sum(row["tokens_out"] for row in rows),
            sum(row["compute"] for row in rows),
            sum(row["llm_cost_thb"] for row in rows),
            sum(row["compute_cost_thb"] for row in rows),
        )

    def fetchone(self) -> tuple[int, int, float, float, float] | None:
        return self.result


class _Connection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.current_lab_id: str | None = None

    @contextmanager
    def transaction(self):
        previous_lab_id = self.current_lab_id
        try:
            yield
        finally:
            self.current_lab_id = previous_lab_id

    @contextmanager
    def cursor(self):
        yield _Cursor(self)


def _identity(lab_id: str = "lab-a") -> Identity:
    return Identity(lab_id, "user:alice", frozenset({"runs:read"}))


def _row(lab_id: str, recorded_at: datetime, **totals: int | float) -> dict[str, Any]:
    return {
        "lab_id": lab_id,
        "recorded_at": recorded_at,
        "tokens_in": totals.get("tokens_in", 0),
        "tokens_out": totals.get("tokens_out", 0),
        "compute": totals.get("compute", 0.0),
        "llm_cost_thb": totals.get("llm_cost_thb", 0.0),
        "compute_cost_thb": totals.get("compute_cost_thb", 0.0),
    }


def test_daily_usage_includes_start_and_excludes_next_utc_day() -> None:
    start = datetime(2026, 9, 24, tzinfo=timezone.utc)
    connection = _Connection(
        [
            _row("lab-a", start, tokens_in=2, tokens_out=3, llm_cost_thb=0.5),
            _row("lab-a", datetime(2026, 9, 24, 12, tzinfo=timezone.utc), tokens_in=4),
            _row("lab-a", datetime(2026, 9, 25, tzinfo=timezone.utc), tokens_in=100),
        ]
    )
    metering = MeteringService(
        connection,
        clock=lambda: datetime(2026, 9, 24, 16, tzinfo=timezone.utc),
    )

    result = UsagePeriodService(metering).get(_identity(), "daily")

    assert result == {
        "period": "daily",
        "window": {
            "start": "2026-09-24T00:00:00+00:00",
            "end": "2026-09-25T00:00:00+00:00",
        },
        "tokens_in": 6,
        "tokens_out": 3,
        "compute": 0.0,
        "llm_cost_thb": 0.5,
        "compute_cost_thb": 0.0,
    }


def test_monthly_usage_uses_the_current_utc_month() -> None:
    connection = _Connection(
        [
            _row("lab-a", datetime(2026, 2, 1, tzinfo=timezone.utc), tokens_in=3),
            _row("lab-a", datetime(2026, 2, 28, 23, 59, tzinfo=timezone.utc), tokens_in=4),
            _row("lab-a", datetime(2026, 3, 1, tzinfo=timezone.utc), tokens_in=100),
        ]
    )
    metering = MeteringService(
        connection,
        clock=lambda: datetime.fromisoformat("2026-03-01T00:30:00+02:00"),
    )

    result = UsagePeriodService(metering).get(_identity(), "monthly")

    assert result["window"] == {
        "start": "2026-02-01T00:00:00+00:00",
        "end": "2026-03-01T00:00:00+00:00",
    }
    assert result["tokens_in"] == 7


def test_usage_totals_include_only_the_authenticated_labs_rows() -> None:
    recorded_at = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    connection = _Connection(
        [
            _row("lab-a", recorded_at, tokens_in=5),
            _row("lab-b", recorded_at, tokens_in=500),
        ]
    )
    metering = MeteringService(
        connection,
        clock=lambda: datetime(2026, 9, 24, 16, tzinfo=timezone.utc),
    )

    result = UsagePeriodService(metering).get(_identity("lab-a"), "daily")

    assert result["tokens_in"] == 5


def test_invalid_period_returns_422() -> None:
    metering = MeteringService(
        _Connection([]),
        clock=lambda: datetime(2026, 9, 24, tzinfo=timezone.utc),
    )

    with pytest.raises(HTTPException) as error:
        UsagePeriodService(metering).get(_identity(), "all-time")

    assert error.value.status_code == 422


def test_usage_without_rows_returns_period_window_and_zero_totals() -> None:
    metering = MeteringService(
        _Connection([]),
        clock=lambda: datetime(2026, 9, 24, 16, tzinfo=timezone.utc),
    )

    result = UsagePeriodService(metering).get(_identity(), "daily")

    assert result == {
        "period": "daily",
        "window": {
            "start": "2026-09-24T00:00:00+00:00",
            "end": "2026-09-25T00:00:00+00:00",
        },
        "tokens_in": 0,
        "tokens_out": 0,
        "compute": 0.0,
        "llm_cost_thb": 0.0,
        "compute_cost_thb": 0.0,
    }
