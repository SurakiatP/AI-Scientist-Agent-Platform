from pathlib import Path
import os

import pytest


def _sql() -> str:
    return (
        Path(__file__).resolve().parents[1] / "migrations/017_approval_execute_effect.sql"
    ).read_text().lower()


def test_migration_017_expands_effect_check_to_include_execute() -> None:
    sql = _sql()
    assert "drop constraint if exists approvals_effect_check" in sql
    assert "add constraint approvals_effect_check" in sql
    assert "'execute'" in sql
    for effect in (
        "read", "publish", "external_write", "delete",
        "credential_use", "network_change", "unknown",
    ):
        assert f"'{effect}'" in sql


def test_migration_017_is_idempotent_text() -> None:
    # "drop ... if exists" before "add constraint" makes re-running the migration
    # file safe (expand-only, no data loss); the executable statement (not the
    # trailing rollback comment) appears exactly once.
    sql = _sql()
    statement = sql.split("-- rollback:")[0]
    assert statement.count("drop constraint if exists approvals_effect_check") == 1
    assert statement.count("add constraint approvals_effect_check") == 1


@pytest.mark.skipif(not os.getenv("SCILAB_TEST_POSTGRES_DSN"), reason="disposable PostgreSQL DSN unavailable")
def test_postgres_approval_effect_check_accepts_execute_and_rejects_unknown_values() -> None:
    import psycopg

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with psycopg.connect(os.environ["SCILAB_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        for path in sorted(migrations.glob("*.sql")):
            conn.execute(path.read_text())
        # Re-applying is idempotent (IF EXISTS drop, then re-add).
        conn.execute((migrations / "017_approval_execute_effect.sql").read_text())

        with conn.transaction():
            conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
            conn.execute("INSERT INTO labs (id, name) VALUES ('lab-a', 'A')")
            conn.execute(
                "INSERT INTO runs (id, lab_id, idempotency_key, state, hermes_run_id, "
                "created_at, updated_at, queued_at, running_since) "
                "VALUES ('run-1', 'lab-a', 'key-1', 'running', 'vendor-1', "
                "now() - interval '1 minute', now() - interval '1 minute', "
                "now() - interval '1 minute', now() - interval '1 minute')"
            )
            conn.execute(
                "INSERT INTO approvals (id, run_id, lab_id, action, effect, "
                "action_fingerprint, status, reason, policy_rule, requested_at, expires_at) "
                "VALUES ('approval-execute', 'run-1', 'lab-a', 'hermes.tool', 'execute', "
                "repeat('a', 64), 'pending', 'review', 'human-review', "
                "now(), now() + interval '24 hours')"
            )
            assert conn.execute(
                "SELECT effect FROM approvals WHERE id = 'approval-execute'"
            ).fetchone() == ("execute",)

        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
                conn.execute(
                    "INSERT INTO approvals (id, run_id, lab_id, action, effect, "
                    "action_fingerprint, status, reason, policy_rule, requested_at, expires_at) "
                    "VALUES ('approval-bad', 'run-1', 'lab-a', 'hermes.tool', 'not-a-real-effect', "
                    "repeat('b', 64), 'pending', 'review', 'human-review', "
                    "now(), now() + interval '24 hours')"
                )
