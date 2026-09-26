-- Add "execute" to the approvals.effect allowlist (Hermes dangerous-shell /
-- execute_code approval gates). Expand-only: drop and re-add the CHECK
-- constraint auto-named by 005_approvals.sql (approvals_effect_check).
ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_effect_check;

ALTER TABLE approvals ADD CONSTRAINT approvals_effect_check CHECK (
    effect IN ('read', 'publish', 'external_write', 'delete',
               'credential_use', 'network_change', 'execute', 'unknown')
);

-- Rollback: ALTER TABLE approvals DROP CONSTRAINT IF EXISTS approvals_effect_check;
--   then re-run 005_approvals.sql's original CHECK if 'execute' rows do not exist.
