CREATE UNIQUE INDEX IF NOT EXISTS runs_id_lab_uidx
    ON runs (id, lab_id);

CREATE TABLE IF NOT EXISTS run_events (
    event_id text PRIMARY KEY
        CHECK (event_id ~ '^[0-9A-HJKMNP-TV-Z]{26}$'),
    run_id text NOT NULL,
    lab_id text NOT NULL,
    ts timestamptz NOT NULL,
    type text NOT NULL CHECK (
        type IN (
            'run.state', 'tool.started', 'tool.progress', 'tool.finished',
            'delegation.started', 'delegation.finished', 'approval.required',
            'artifact.registered', 'cost.updated', 'run.completed', 'run.failed'
        )
    ),
    seq bigint NOT NULL CHECK (seq > 0),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    source text NOT NULL CHECK (source IN ('hermes', 'run-service', 'policy', 'sandbox')),
    UNIQUE (lab_id, run_id, seq),
    FOREIGN KEY (run_id, lab_id)
        REFERENCES runs (id, lab_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS run_events_replay_idx
    ON run_events (lab_id, run_id, seq);

ALTER TABLE run_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS run_events_tenant ON run_events;
CREATE POLICY run_events_tenant ON run_events
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));
