CREATE TABLE IF NOT EXISTS artifacts (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    run_id text NOT NULL,
    lab_id text NOT NULL,
    kind text NOT NULL CHECK (btrim(kind) <> ''),
    uri text NOT NULL CHECK (btrim(uri) <> ''),
    sha256 text NOT NULL CHECK (octet_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$'),
    bytes bigint NOT NULL CHECK (bytes >= 0),
    produced_by_step integer NOT NULL CHECK (produced_by_step >= 0),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    UNIQUE (id, lab_id),
    UNIQUE (lab_id, run_id, uri),
    FOREIGN KEY (run_id, lab_id) REFERENCES runs(id, lab_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS artifacts_lab_run_idx ON artifacts (lab_id, run_id, id);

CREATE TABLE IF NOT EXISTS run_manifests (
    run_id text NOT NULL,
    lab_id text NOT NULL,
    content jsonb NOT NULL,
    sha256 text NOT NULL CHECK (octet_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$'),
    artifact_id text NOT NULL,
    sealed_at timestamptz NOT NULL,
    PRIMARY KEY (lab_id, run_id),
    FOREIGN KEY (artifact_id, lab_id) REFERENCES artifacts(id, lab_id),
    FOREIGN KEY (run_id, lab_id) REFERENCES runs(id, lab_id) ON DELETE CASCADE
);

ALTER TABLE artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifacts FORCE ROW LEVEL SECURITY;
ALTER TABLE run_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_manifests FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS artifacts_tenant ON artifacts;
CREATE POLICY artifacts_tenant ON artifacts
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));

DROP POLICY IF EXISTS run_manifests_tenant ON run_manifests;
CREATE POLICY run_manifests_tenant ON run_manifests
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));

CREATE OR REPLACE FUNCTION prevent_run_manifest_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'sealed run manifests are immutable';
END;
$$;

DROP TRIGGER IF EXISTS run_manifests_immutable ON run_manifests;
CREATE TRIGGER run_manifests_immutable
BEFORE UPDATE OR DELETE ON run_manifests
FOR EACH ROW EXECUTE FUNCTION prevent_run_manifest_mutation();
