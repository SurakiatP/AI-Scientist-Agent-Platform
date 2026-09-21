CREATE TABLE IF NOT EXISTS labs (
    id text PRIMARY KEY,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lab_memberships (
    lab_id text NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    subject text NOT NULL,
    role text NOT NULL CHECK (role IN ('owner', 'researcher', 'viewer')),
    PRIMARY KEY (lab_id, subject)
);

CREATE TABLE IF NOT EXISTS api_credentials (
    key_id text PRIMARY KEY,
    lab_id text NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    secret_hash bytea NOT NULL CHECK (octet_length(secret_hash) = 32),
    scopes text[] NOT NULL CHECK (
    cardinality(scopes) > 0
    AND scopes <@ ARRAY['runs:read', 'runs:write', 'runs:approve', 'artifacts:read', 'lab:admin']::text[]
    )
);

CREATE TABLE IF NOT EXISTS a2a_peers (
    peer_name text PRIMARY KEY,
    lab_id text NOT NULL REFERENCES labs(id) ON DELETE CASCADE,
    secret_hash bytea NOT NULL CHECK (octet_length(secret_hash) = 32),
    scopes text[] NOT NULL CHECK (
    cardinality(scopes) > 0
    AND scopes <@ ARRAY['runs:read', 'runs:write', 'runs:approve', 'artifacts:read', 'lab:admin']::text[]
    )
);

ALTER TABLE labs ENABLE ROW LEVEL SECURITY;
ALTER TABLE labs FORCE ROW LEVEL SECURITY;

ALTER TABLE lab_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE lab_memberships FORCE ROW LEVEL SECURITY;
ALTER TABLE api_credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_credentials FORCE ROW LEVEL SECURITY;
ALTER TABLE a2a_peers ENABLE ROW LEVEL SECURITY;
ALTER TABLE a2a_peers FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS labs_tenant ON labs;
CREATE POLICY labs_tenant ON labs
USING (id = current_setting('scilab.current_lab_id', true))
WITH CHECK (id = current_setting('scilab.current_lab_id', true));

DROP POLICY IF EXISTS lab_memberships_tenant ON lab_memberships;
CREATE POLICY lab_memberships_tenant ON lab_memberships
USING (lab_id = current_setting('scilab.current_lab_id', true))
WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));

DROP POLICY IF EXISTS lab_memberships_bootstrap ON lab_memberships;
CREATE POLICY lab_memberships_bootstrap ON lab_memberships
FOR SELECT
USING (
    subject = current_setting('scilab.bootstrap_subject', true)
    AND lab_id = current_setting('scilab.bootstrap_lab_id', true)
);

DROP POLICY IF EXISTS api_credentials_tenant ON api_credentials;
CREATE POLICY api_credentials_tenant ON api_credentials
USING (lab_id = current_setting('scilab.current_lab_id', true))
WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));

DROP POLICY IF EXISTS api_credentials_bootstrap ON api_credentials;
CREATE POLICY api_credentials_bootstrap ON api_credentials
FOR SELECT
USING (
    secret_hash = decode(
        current_setting('scilab.bootstrap_secret_digest', true),
        'hex'
    )
);

DROP POLICY IF EXISTS a2a_peers_tenant ON a2a_peers;
CREATE POLICY a2a_peers_tenant ON a2a_peers
USING (lab_id = current_setting('scilab.current_lab_id', true))
WITH CHECK (lab_id = current_setting('scilab.current_lab_id', true));

DROP POLICY IF EXISTS a2a_peers_bootstrap ON a2a_peers;
CREATE POLICY a2a_peers_bootstrap ON a2a_peers
FOR SELECT
USING (
    secret_hash = decode(
        current_setting('scilab.bootstrap_secret_digest', true),
        'hex'
    )
);
