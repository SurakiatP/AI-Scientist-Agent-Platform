from pathlib import Path
import os
from datetime import datetime, timezone

import pytest


def test_hermes_approval_migration_keeps_tenant_isolation_and_unique_vendor_request() -> None:
    sql = (Path(__file__).resolve().parents[1] / "migrations/016_hermes_approval.sql").read_text().lower()
    assert "hermes_request_id" in sql
    assert "unique index" in sql
    assert "(lab_id, run_id, hermes_request_id)" in sql
    assert "where hermes_request_id is not null" in sql
    assert "hermes_decision" in sql
    assert "approvals_hermes_stage_check" in sql
    assert "char_length(hermes_request_id) <= 256" in sql


@pytest.mark.skipif(not os.getenv("SCILAB_TEST_POSTGRES_DSN"), reason="disposable PostgreSQL DSN unavailable")
def test_postgres_hermes_approval_rls_and_staged_run() -> None:
    import psycopg

    from scilab.approvals import ApprovalService, ApprovalStateError
    from scilab.events import EventService
    from scilab.identity import Identity

    class Policy:
        def evaluate(self, policy_input):
            assert policy_input["effect"] == "unknown"
            return {"allow": True, "requires_approval": False, "reason": "review",
                    "policy_rule": "human-review"}

    class Events:
        def __init__(self) -> None:
            self.fail = False
            self.published: list[tuple[str, bytes]] = []

        async def publish(self, subject: str, data: bytes) -> None:
            if self.fail:
                raise RuntimeError("NATS unavailable")
            self.published.append((subject, data))

    migrations = Path(__file__).resolve().parents[1] / "migrations"
    with psycopg.connect(os.environ["SCILAB_TEST_POSTGRES_DSN"], autocommit=True) as conn:
        for path in sorted(migrations.glob("*.sql")):
            conn.execute(path.read_text())
        conn.execute((migrations / "016_hermes_approval.sql").read_text())
        conn.execute("CREATE ROLE hermes_approval_probe")
        conn.execute("GRANT ALL ON labs, runs, approvals, run_events, audit_events TO hermes_approval_probe")
        with conn.transaction():
            conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
            conn.execute("INSERT INTO labs (id, name) VALUES ('lab-a', 'A')")
            conn.execute("INSERT INTO runs (id, lab_id, idempotency_key, state, hermes_run_id, "
                         "created_at, updated_at, queued_at, running_since) "
                         "VALUES ('run-1', 'lab-a', 'key-1', 'running', 'vendor-1', "
                         "now() - interval '1 minute', now() - interval '1 minute', "
                         "now() - interval '1 minute', now() - interval '1 minute')")
        conn.execute("SET ROLE hermes_approval_probe")
        actor = Identity("lab-a", "user:a", frozenset({"runs:write", "runs:approve"}))
        bus = Events()
        events = EventService(conn, bus)
        service = ApprovalService(conn, Policy(), events,
                                  clock=lambda: datetime.now(timezone.utc),
                                  id_factory=lambda: "approval-1")
        import asyncio
        approval, run = asyncio.run(service.request_hermes_approval(actor, "run-1", "request-1", {}))
        assert run.state.value == "awaiting_approval"
        assert service.stage_hermes_decision(actor, approval.id, "approve", note="reviewed", run_id="run-1") == "request-1"
        with conn.transaction():
            conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
            before_events = conn.execute(
                "SELECT count(*) FROM run_events WHERE lab_id = 'lab-a' AND run_id = 'run-1'"
            ).fetchone()[0]
        with conn.transaction():
            conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-b', true)")
            assert conn.execute("SELECT count(*) FROM approvals WHERE id = %s", (approval.id,)).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM run_events WHERE run_id = 'run-1'"
            ).fetchone()[0] == 0
        conn.execute("RESET ROLE")
        conn.execute("""
            CREATE FUNCTION fail_run_event_insert() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'event insert failed'; END;
            $$
        """)
        conn.execute("""
            CREATE TRIGGER fail_run_event_insert BEFORE INSERT ON run_events
            FOR EACH ROW EXECUTE FUNCTION fail_run_event_insert()
        """)
        conn.execute("SET ROLE hermes_approval_probe")
        with pytest.raises(psycopg.errors.RaiseException, match="event insert failed"):
            asyncio.run(service.confirm_hermes_decision(actor, approval.id, run_id="run-1"))
        with conn.transaction():
            conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
            assert conn.execute(
                "SELECT status, actor, decided_at, note FROM approvals WHERE id = %s", (approval.id,)
            ).fetchone() == ("pending", None, None, None)
            assert conn.execute("SELECT state FROM runs WHERE id = 'run-1'").fetchone()[0] == "awaiting_approval"
            assert conn.execute(
                "SELECT count(*) FROM run_events WHERE lab_id = 'lab-a' AND run_id = 'run-1'"
            ).fetchone()[0] == before_events

        conn.execute("RESET ROLE")
        conn.execute("DROP TRIGGER fail_run_event_insert ON run_events")
        conn.execute("DROP FUNCTION fail_run_event_insert()")
        conn.execute("SET ROLE hermes_approval_probe")
        bus.fail = True
        confirmer = Identity("lab-a", "user:retry", frozenset({"runs:approve", "runs:read"}))
        confirmed = asyncio.run(service.confirm_hermes_decision(confirmer, approval.id, run_id="run-1"))
        assert confirmed.status == "approved"
        assert confirmed.actor == actor.principal
        assert confirmed.note == "reviewed"
        with conn.transaction():
            conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
            assert conn.execute("SELECT state FROM runs WHERE id = 'run-1'").fetchone()[0] == "running"
            assert conn.execute(
                "SELECT count(*) FROM run_events WHERE lab_id = 'lab-a' AND run_id = 'run-1'"
            ).fetchone()[0] == before_events + 1
        assert service.final_hermes_approval(
            confirmer, approval.id, "approve", run_id="run-1"
        ) == confirmed
        with pytest.raises(ApprovalStateError, match="conflicting Hermes decision"):
            service.final_hermes_approval(confirmer, approval.id, "reject", run_id="run-1")
        replayed = events.replay_events(confirmer, "run-1")
        assert replayed[-1].type == "run.state"
        assert replayed[-1].payload.model_dump(by_alias=True) == {
            "from": "awaiting_approval", "to": "running", "reason": "approval_approved"
        }
        with conn.transaction():
            conn.execute("SELECT set_config('scilab.current_lab_id', 'lab-a', true)")
            with pytest.raises(psycopg.errors.UniqueViolation):
                with conn.transaction():
                    conn.execute("INSERT INTO approvals (id, run_id, lab_id, action, effect, "
                                 "action_fingerprint, status, reason, policy_rule, requested_at, expires_at, "
                                 "hermes_request_id) VALUES ('approval-2', 'run-1', 'lab-a', 'hermes.tool', "
                                 "'unknown', repeat('a', 64), 'pending', 'review', 'human-review', "
                                 "now(), now() + interval '24 hours', 'request-1')")
