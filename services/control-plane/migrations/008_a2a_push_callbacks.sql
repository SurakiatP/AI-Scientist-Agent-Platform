-- Expand-only Wave 13 migration. Existing peers and Runs remain valid.
ALTER TABLE a2a_peers ADD COLUMN IF NOT EXISTS push_secret_ref text;

CREATE UNIQUE INDEX IF NOT EXISTS runs_lab_id_id_unique_idx ON runs (lab_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS a2a_peers_lab_name_unique_idx ON a2a_peers (lab_id, peer_name);

CREATE TABLE IF NOT EXISTS a2a_push_callbacks (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    lab_id text NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    run_id text NOT NULL,
    peer_name text NOT NULL,
    url text NOT NULL CHECK (url LIKE 'https://%'),
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (lab_id, run_id) REFERENCES runs(lab_id, id) ON DELETE CASCADE,
    FOREIGN KEY (lab_id, peer_name) REFERENCES a2a_peers(lab_id, peer_name) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS a2a_push_callbacks_run_idx
    ON a2a_push_callbacks (lab_id, run_id);

ALTER TABLE a2a_push_callbacks ENABLE ROW LEVEL SECURITY;
ALTER TABLE a2a_push_callbacks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS a2a_push_callbacks_tenant ON a2a_push_callbacks;
CREATE POLICY a2a_push_callbacks_tenant ON a2a_push_callbacks
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));

-- Rollback path (manual, only after callback writes stop): drop this table,
-- then the two unique indexes and the nullable peer push_secret_ref column.
-- Do not contract during mixed-version rollout; older code ignores both additions.
