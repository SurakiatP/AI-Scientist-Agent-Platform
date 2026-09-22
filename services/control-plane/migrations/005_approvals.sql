CREATE TABLE IF NOT EXISTS approvals (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    run_id text NOT NULL,
    lab_id text NOT NULL,
    action text NOT NULL CHECK (btrim(action) <> ''),
    effect text NOT NULL CHECK (
        effect IN ('read', 'publish', 'external_write', 'delete',
                   'credential_use', 'network_change', 'unknown')
    ),
    action_fingerprint text NOT NULL CHECK (
        octet_length(action_fingerprint) = 64
        AND action_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    status text NOT NULL CHECK (status IN ('pending', 'approved', 'consumed', 'rejected', 'expired')),
    reason text NOT NULL CHECK (btrim(reason) <> ''),
    policy_rule text NOT NULL CHECK (btrim(policy_rule) <> ''),
    preview jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL CHECK (expires_at > requested_at),
    decided_at timestamptz,
    actor text,
    note text,
    FOREIGN KEY (run_id, lab_id) REFERENCES runs(id, lab_id) ON DELETE CASCADE,
    CHECK (
        (status = 'pending' AND decided_at IS NULL AND actor IS NULL)
        OR (status = 'approved' AND decided_at IS NOT NULL AND actor IS NOT NULL)
        OR (status = 'consumed' AND decided_at IS NOT NULL AND actor IS NOT NULL)
        OR (status = 'rejected' AND decided_at IS NOT NULL AND actor IS NOT NULL)
        OR (status = 'expired' AND decided_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS approvals_active_action_uidx
    ON approvals (lab_id, run_id, action_fingerprint)
    WHERE status IN ('pending', 'approved');

CREATE INDEX IF NOT EXISTS approvals_active_expiry_idx
    ON approvals (lab_id, expires_at)
    WHERE status IN ('pending', 'approved');

ALTER TABLE approvals ENABLE ROW LEVEL SECURITY;
ALTER TABLE approvals FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS approvals_tenant ON approvals;
CREATE POLICY approvals_tenant ON approvals
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));
