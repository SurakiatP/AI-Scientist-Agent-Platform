-- LLM usage for a model with no configured THB rate must still be recorded
-- (compute cost 0, llm_cost_thb unknown) without failing the Run. Track the
-- model name and any extra metadata (e.g. Hermes-reported cost_usd) per row.
ALTER TABLE metering_usage ALTER COLUMN llm_cost_thb DROP NOT NULL;
ALTER TABLE metering_usage ADD COLUMN IF NOT EXISTS model text;
ALTER TABLE metering_usage ADD COLUMN IF NOT EXISTS metadata jsonb NOT NULL DEFAULT '{}'::jsonb;

-- Rollback (only if no NULL llm_cost_thb rows remain):
-- ALTER TABLE metering_usage ALTER COLUMN llm_cost_thb SET NOT NULL;
-- ALTER TABLE metering_usage DROP COLUMN model;
-- ALTER TABLE metering_usage DROP COLUMN metadata;
