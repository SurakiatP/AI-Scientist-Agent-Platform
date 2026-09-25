-- Durable idempotency-aware Run admission reservations, scoped by Lab.
CREATE TABLE IF NOT EXISTS run_admission_attempts (
    lab_id text NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    idempotency_key text NOT NULL CHECK (btrim(idempotency_key) <> ''),
    admitted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (lab_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS run_admission_attempts_window_idx
    ON run_admission_attempts (lab_id, admitted_at);

ALTER TABLE run_admission_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_admission_attempts FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS run_admission_attempts_tenant ON run_admission_attempts;
CREATE POLICY run_admission_attempts_tenant ON run_admission_attempts
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));

-- Rollback only after admission writers stop: DROP TABLE run_admission_attempts;
