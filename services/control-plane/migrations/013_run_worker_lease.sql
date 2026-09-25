ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS worker_claim_token uuid,
    ADD COLUMN IF NOT EXISTS worker_lease_expires_at timestamptz;

CREATE INDEX IF NOT EXISTS runs_worker_claim_idx
    ON runs (lab_id, queued_at, id)
    WHERE state = 'queued' AND request_payload IS NOT NULL;
