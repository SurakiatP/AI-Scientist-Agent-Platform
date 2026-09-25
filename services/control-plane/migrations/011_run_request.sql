-- REST request data shares the Run row and its tenant RLS boundary.
ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS request_payload jsonb
        CHECK (request_payload IS NULL OR jsonb_typeof(request_payload) = 'object'),
    ADD COLUMN IF NOT EXISTS actor text
        CHECK (actor IS NULL OR btrim(actor) <> '');

CREATE INDEX IF NOT EXISTS runs_lab_actor_created_idx
    ON runs (lab_id, actor, created_at DESC, id DESC)
    WHERE actor IS NOT NULL;

CREATE INDEX IF NOT EXISTS runs_lab_state_created_idx
    ON runs (lab_id, state, created_at DESC, id DESC);
