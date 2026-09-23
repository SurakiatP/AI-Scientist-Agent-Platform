-- Add an optional per-Run budget. NULL preserves the unbounded behavior.
ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS budget_thb numeric CHECK (budget_thb >= 0);

-- Rollback after Run-budget writes stop: ALTER TABLE runs DROP COLUMN budget_thb.
