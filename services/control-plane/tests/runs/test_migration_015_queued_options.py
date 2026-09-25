from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.rows import dict_row

from scilab.identity import Identity
from scilab.runs.service import RunService
from scilab.runs.state import RunStateError
from scilab.runs.worker import RunWorker

MIGRATIONS = Path(__file__).parents[2] / "migrations"
MIGRATION_015 = MIGRATIONS / "015_queued_options.sql"
OLD_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_postgres_worker_claim_is_lab_scoped_after_migration(postgres) -> None:
    postgres.execute("INSERT INTO labs (id, name) VALUES ('lab-a', 'A'), ('lab-b', 'B')")
    apply_migration(postgres)
    payload = {"goal": "Research", "inputs": [], "skill_packs": [], "options": {}}
    seed_run(postgres, "run-a", "lab-a", "queued", payload)
    seed_run(postgres, "run-b", "lab-b", "queued", payload)

    worker = RunWorker(postgres)
    claim = worker.claim_next(Identity("lab-a", "service:worker", frozenset({"runs:write"})))

    assert claim is not None
    assert claim.run.id == "run-a"
    assert claim.run.lab_id == "lab-a"
    assert claim.token is not None
    assert postgres.execute("SELECT worker_claim_token FROM runs WHERE id = 'run-b'").fetchone()["worker_claim_token"] is None


@pytest.fixture
def postgres():
    url = os.environ.get("SCILAB_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set SCILAB_TEST_POSTGRES_URL to a disposable PostgreSQL database")
    schema = f"test_queued_options_{uuid4().hex}"
    with psycopg.connect(url, autocommit=True, row_factory=dict_row) as connection:
        if not connection.execute(
            "SELECT rolsuper OR rolbypassrls AS can_migrate "
            "FROM pg_roles WHERE rolname = current_user"
        ).fetchone()["can_migrate"]:
            pytest.skip("cross-Lab cleanup requires a BYPASSRLS migration role")
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection.execute(
            sql.SQL("SET search_path TO {}, public").format(sql.Identifier(schema))
        )
        try:
            for number in range(1, 15):
                path = next(MIGRATIONS.glob(f"{number:03d}_*.sql"))
                connection.execute(path.read_text())
            yield connection
        finally:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def seed_run(
    connection: psycopg.Connection,
    run_id: str,
    lab_id: str,
    state: str,
    payload: dict,
    *,
    reason: str | None = None,
    claimed: bool = False,
) -> None:
    connection.execute(
        "INSERT INTO runs (id, lab_id, idempotency_key, state, reason, "
        "created_at, updated_at, queued_at, request_payload, actor, "
        "worker_claim_token, worker_lease_expires_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)",
        (
            run_id,
            lab_id,
            run_id,
            state,
            reason,
            OLD_TIME,
            OLD_TIME,
            OLD_TIME,
            json.dumps(payload),
            "user:researcher",
            uuid4() if claimed else None,
            datetime(2027, 1, 1, tzinfo=timezone.utc) if claimed else None,
        ),
    )


def apply_migration(connection: psycopg.Connection) -> None:
    connection.execute(MIGRATION_015.read_text())


def test_legacy_queued_options_fail_without_losing_request_or_retry_history(postgres) -> None:
    postgres.execute("INSERT INTO labs (id, name) VALUES ('lab-a', 'A'), ('lab-b', 'B')")
    old_request = {"goal": "legacy", "options": {"seed": 7}, "context": {"keep": True}}
    seed_run(postgres, "bad-a", "lab-a", "queued", old_request, claimed=True)
    seed_run(postgres, "bad-b", "lab-b", "queued", {"goal": "other", "options": [1]})
    seed_run(postgres, "good", "lab-a", "queued", {"goal": "valid", "options": {}})
    seed_run(postgres, "missing", "lab-a", "queued", {"goal": "legacy valid"})
    seed_run(
        postgres, "historical", "lab-a", "failed", {"options": {"seed": 8}}, reason="error"
    )

    apply_migration(postgres)
    rows = {
        row["id"]: row
        for row in postgres.execute(
            "SELECT id, state, reason, retry_count, updated_at, request_payload, "
            "worker_claim_token, worker_lease_expires_at FROM runs"
        ).fetchall()
    }

    for run_id in ("bad-a", "bad-b"):
        assert rows[run_id]["state"] == "failed"
        assert rows[run_id]["reason"] == "unsupported_stored_options"
        assert rows[run_id]["retry_count"] == 0
        assert rows[run_id]["updated_at"] > OLD_TIME
        assert rows[run_id]["worker_claim_token"] is None
        assert rows[run_id]["worker_lease_expires_at"] is None
    assert rows["bad-a"]["request_payload"] == old_request
    assert rows["good"]["state"] == rows["missing"]["state"] == "queued"
    assert rows["historical"]["reason"] == "error"
    assert rows["historical"]["updated_at"] == OLD_TIME

    worker = RunService(postgres)
    identity = Identity("lab-a", "user:researcher", frozenset({"runs:read", "runs:write"}))
    assert worker.get(identity, "bad-a").reason == "unsupported_stored_options"
    with pytest.raises(RunStateError, match="unsupported stored options"):
        worker.retry(identity, "bad-a")
    assert postgres.execute(
        "SELECT state, retry_count FROM runs WHERE id = 'bad-a'"
    ).fetchone() == {"state": "failed", "retry_count": 0}


def test_constraint_rejects_new_and_requeued_invalid_options(postgres) -> None:
    postgres.execute("INSERT INTO labs (id, name) VALUES ('lab-a', 'A')")
    seed_run(postgres, "historical", "lab-a", "failed", {"options": {"seed": 7}})

    apply_migration(postgres)
    apply_migration(postgres)

    with pytest.raises(errors.CheckViolation):
        seed_run(postgres, "new-invalid", "lab-a", "queued", {"options": {"seed": 7}})
    with pytest.raises(errors.CheckViolation):
        postgres.execute("UPDATE runs SET state = 'queued' WHERE id = 'historical'")
    seed_run(postgres, "new-valid", "lab-a", "queued", {"options": {}})
    assert postgres.execute(
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid = 'runs'::regclass"
    ).fetchone() == {"relrowsecurity": True, "relforcerowsecurity": True}
    assert postgres.execute(
        "SELECT policyname FROM pg_policies "
        "WHERE schemaname = current_schema() AND tablename = 'runs'"
    ).fetchall() == [{"policyname": "runs_tenant"}]
    assert postgres.execute(
        "SELECT convalidated FROM pg_constraint "
        "WHERE conrelid = 'runs'::regclass AND conname = 'runs_queued_options_empty'"
    ).fetchone() == {"convalidated": True}
