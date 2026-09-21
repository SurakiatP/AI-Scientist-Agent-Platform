CREATE TABLE IF NOT EXISTS runs (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    lab_id text NOT NULL CHECK (btrim(lab_id) <> '')
        REFERENCES labs(id) ON DELETE CASCADE,
    idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
    state text NOT NULL CHECK (
        state IN ('queued', 'running', 'awaiting_approval', 'completed', 'failed', 'cancelled')
    ),
    reason text,
    retry_count smallint NOT NULL DEFAULT 0 CHECK (retry_count BETWEEN 0 AND 2),
    max_minutes integer NOT NULL DEFAULT 120 CHECK (max_minutes > 0),
    hermes_run_id text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    queued_at timestamptz NOT NULL,
    running_since timestamptz,
    runtime_used interval NOT NULL DEFAULT interval '0 seconds'
        CHECK (runtime_used >= interval '0 seconds'),
    last_heartbeat_at timestamptz,
    approval_expires_at timestamptz,
    UNIQUE (lab_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS runs_lab_created_idx
    ON runs (lab_id, created_at DESC, id DESC);

ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS runs_tenant ON runs;
CREATE POLICY runs_tenant ON runs
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));
