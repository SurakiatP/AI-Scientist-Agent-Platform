CREATE TABLE IF NOT EXISTS uploaded_inputs (
    id text PRIMARY KEY CHECK (btrim(id) <> ''),
    lab_id text NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    name text NOT NULL CHECK (btrim(name) <> ''),
    media_type text NOT NULL CHECK (btrim(media_type) <> ''),
    bucket_name text NOT NULL CHECK (btrim(bucket_name) <> ''),
    object_key text NOT NULL CHECK (btrim(object_key) <> ''),
    sha256 text NOT NULL CHECK (
        octet_length(sha256) = 64 AND sha256 ~ '^[0-9a-f]{64}$'
    ),
    bytes bigint NOT NULL CHECK (bytes >= 0),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'ready')),
    created_at timestamptz NOT NULL,
    UNIQUE (lab_id, object_key)
);

CREATE INDEX IF NOT EXISTS uploaded_inputs_lab_created_idx
    ON uploaded_inputs (lab_id, created_at DESC);

ALTER TABLE uploaded_inputs ENABLE ROW LEVEL SECURITY;
ALTER TABLE uploaded_inputs FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS uploaded_inputs_tenant ON uploaded_inputs;
CREATE POLICY uploaded_inputs_tenant ON uploaded_inputs
    USING (lab_id = current_setting('scilab.current_lab_id', true))
    WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));
