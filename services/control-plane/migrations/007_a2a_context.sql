-- Forward: additive and nullable; existing Runs remain valid with context_id = NULL.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS context_id text;

-- Rollback: first deploy code that no longer reads or writes context_id. Keep this
-- column to preserve context IDs for a later forward deploy. If schema removal is
-- required, archive non-NULL context_id values before running:
-- ALTER TABLE runs DROP COLUMN context_id;
