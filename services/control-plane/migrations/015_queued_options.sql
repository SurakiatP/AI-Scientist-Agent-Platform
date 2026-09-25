-- Legacy queued Runs with stored options cannot be executed. Keep their
-- request_payload for diagnosis and mark that execution never started.
-- Run this cross-Lab data migration with a BYPASSRLS migration role: runs has
-- FORCE ROW LEVEL SECURITY. Validation fails if unseen invalid rows remain.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'runs'::regclass
          AND conname = 'runs_queued_options_empty'
    ) THEN
        ALTER TABLE runs
            ADD CONSTRAINT runs_queued_options_empty
            CHECK (
                state <> 'queued'
                OR request_payload IS NULL
                OR COALESCE(request_payload -> 'options', '{}'::jsonb) = '{}'::jsonb
            ) NOT VALID;
    END IF;
END;
$$;

UPDATE runs
SET state = 'failed',
    reason = 'unsupported_stored_options',
    updated_at = statement_timestamp(),
    worker_claim_token = NULL,
    worker_lease_expires_at = NULL
WHERE state = 'queued'
  AND request_payload IS NOT NULL
  AND COALESCE(request_payload -> 'options', '{}'::jsonb) <> '{}'::jsonb;

ALTER TABLE runs VALIDATE CONSTRAINT runs_queued_options_empty;

-- Rollback schema only: ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_queued_options_empty;
-- Terminalized rows retain request_payload and must not be requeued automatically.
