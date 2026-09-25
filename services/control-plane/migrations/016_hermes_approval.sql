ALTER TABLE approvals ADD COLUMN IF NOT EXISTS hermes_request_id text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS hermes_decision text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS hermes_decision_actor text;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS hermes_decided_at timestamptz;
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS hermes_decision_note text;

CREATE UNIQUE INDEX IF NOT EXISTS approvals_hermes_request_uidx
    ON approvals (lab_id, run_id, hermes_request_id)
    WHERE hermes_request_id IS NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'approvals_hermes_stage_check'
                   AND conrelid = 'approvals'::regclass) THEN
        ALTER TABLE approvals ADD CONSTRAINT approvals_hermes_stage_check CHECK (
            (hermes_request_id IS NULL OR
                (btrim(hermes_request_id) <> '' AND char_length(hermes_request_id) <= 256))
            AND ((hermes_decision IS NULL AND hermes_decision_actor IS NULL
                  AND hermes_decided_at IS NULL AND hermes_decision_note IS NULL)
                 OR (hermes_request_id IS NOT NULL
                     AND hermes_decision IN ('approve', 'reject')
                     AND hermes_decision_actor IS NOT NULL
                     AND btrim(hermes_decision_actor) <> ''
                     AND hermes_decided_at IS NOT NULL))
        );
    END IF;
END $$;
