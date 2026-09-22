CREATE TABLE IF NOT EXISTS lab_budgets (
    lab_id text PRIMARY KEY CHECK (btrim(lab_id) <> ''),
    budget_thb numeric NOT NULL CHECK (budget_thb >= 0),
    configured_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS metering_usage (
    usage_id text PRIMARY KEY CHECK (btrim(usage_id) <> ''),
    run_id text NOT NULL,
    lab_id text NOT NULL,
    actor text NOT NULL CHECK (btrim(actor) <> ''),
    source text NOT NULL CHECK (btrim(source) <> ''),
    tokens_in bigint NOT NULL CHECK (tokens_in >= 0),
    tokens_out bigint NOT NULL CHECK (tokens_out >= 0),
    compute double precision NOT NULL CHECK (compute >= 0),
    llm_cost_thb numeric NOT NULL CHECK (llm_cost_thb >= 0),
    compute_cost_thb numeric NOT NULL CHECK (compute_cost_thb >= 0),
    recorded_at timestamptz NOT NULL,
    FOREIGN KEY (run_id, lab_id) REFERENCES runs(id, lab_id)
);

CREATE INDEX IF NOT EXISTS metering_usage_dimensions_idx
    ON metering_usage (lab_id, run_id, actor, source, recorded_at);

CREATE TABLE IF NOT EXISTS audit_events (
    audit_id text PRIMARY KEY CHECK (btrim(audit_id) <> ''),
    run_id text NOT NULL,
    lab_id text NOT NULL,
    actor text NOT NULL CHECK (btrim(actor) <> ''),
    source text NOT NULL CHECK (btrim(source) <> ''),
    action text NOT NULL CHECK (btrim(action) <> ''),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    FOREIGN KEY (run_id, lab_id) REFERENCES runs(id, lab_id)
);

CREATE INDEX IF NOT EXISTS audit_events_dimensions_idx
    ON audit_events (lab_id, run_id, actor, source, created_at);

CREATE TABLE IF NOT EXISTS metering_event_outbox (
    usage_id text PRIMARY KEY,
    run_id text NOT NULL,
    lab_id text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    delivered_at timestamptz,
    FOREIGN KEY (usage_id) REFERENCES metering_usage(usage_id),
    FOREIGN KEY (run_id, lab_id) REFERENCES runs(id, lab_id)
);

CREATE INDEX IF NOT EXISTS metering_event_outbox_pending_idx
    ON metering_event_outbox (lab_id, created_at)
    WHERE delivered_at IS NULL;

ALTER TABLE lab_budgets ENABLE ROW LEVEL SECURITY;
ALTER TABLE lab_budgets FORCE ROW LEVEL SECURITY;
ALTER TABLE metering_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE metering_usage FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
ALTER TABLE metering_event_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE metering_event_outbox FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS lab_budgets_tenant ON lab_budgets;
CREATE POLICY lab_budgets_tenant ON lab_budgets
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));

DROP POLICY IF EXISTS metering_usage_tenant ON metering_usage;
CREATE POLICY metering_usage_tenant ON metering_usage
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));

DROP POLICY IF EXISTS audit_events_tenant ON audit_events;
CREATE POLICY audit_events_tenant ON audit_events
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));

DROP POLICY IF EXISTS metering_event_outbox_tenant ON metering_event_outbox;
CREATE POLICY metering_event_outbox_tenant ON metering_event_outbox
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));

CREATE OR REPLACE FUNCTION prevent_metering_audit_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'metering and audit records are append-only';
END;
$$;

DROP TRIGGER IF EXISTS metering_usage_immutable ON metering_usage;
CREATE TRIGGER metering_usage_immutable
    BEFORE UPDATE OR DELETE ON metering_usage
    FOR EACH ROW EXECUTE FUNCTION prevent_metering_audit_mutation();

DROP TRIGGER IF EXISTS audit_events_immutable ON audit_events;
CREATE TRIGGER audit_events_immutable
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION prevent_metering_audit_mutation();
