from pathlib import Path
import os

import pytest


def test_migration_018_drops_not_null_and_adds_model_metadata() -> None:
    sql = (
        Path(__file__).resolve().parents[1] / "migrations/018_metering_unpriced_usage.sql"
    ).read_text().lower()
    compact = " ".join(sql.split())

    assert "alter table metering_usage alter column llm_cost_thb drop not null" in compact
    assert "add column if not exists model text" in compact
    assert "add column if not exists metadata jsonb not null default '{}'::jsonb" in compact
    assert "drop table" not in compact


@pytest.mark.skipif(not os.getenv("SCILAB_TEST_POSTGRES_DSN"), reason="disposable PostgreSQL DSN unavailable")
def test_postgres_null_llm_cost_insert_succeeds_and_row_stays_immutable() -> None:
    import psycopg

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with psycopg.connect(os.environ["SCILAB_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        for path in sorted(migrations.glob("*.sql")):
            conn.execute(path.read_text())

        with conn.transaction():
            conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
            conn.execute("INSERT INTO labs (id, name) VALUES ('lab-a', 'A')")
            conn.execute(
                "INSERT INTO runs (id, lab_id, idempotency_key, state, hermes_run_id, "
                "created_at, updated_at, queued_at, running_since) "
                "VALUES ('run-1', 'lab-a', 'key-1', 'running', 'hermes-1', "
                "now(), now(), now(), now())"
            )
            conn.execute(
                "INSERT INTO metering_usage (usage_id, run_id, lab_id, actor, source, "
                "tokens_in, tokens_out, compute, llm_cost_thb, compute_cost_thb, model, "
                "metadata, recorded_at) VALUES ('usage-1', 'run-1', 'lab-a', 'hermes', "
                "'litellm', 100, 50, 0, NULL, 0, 'unpriced-model', '{}', now())"
            )
            row = conn.execute(
                "SELECT llm_cost_thb, model, metadata FROM metering_usage "
                "WHERE usage_id = 'usage-1'"
            ).fetchone()
        assert row == (None, "unpriced-model", {})

        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            with conn.transaction():
                conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
                conn.execute(
                    "UPDATE metering_usage SET llm_cost_thb = 1 WHERE usage_id = 'usage-1'"
                )
