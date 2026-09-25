-- Expand-only: existing credentials keep their IDs, digests, and NULL names.
ALTER TABLE api_credentials ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE a2a_peers ADD COLUMN IF NOT EXISTS name text;

CREATE UNIQUE INDEX IF NOT EXISTS api_credentials_lab_name_uidx
    ON api_credentials (lab_id, name);
CREATE UNIQUE INDEX IF NOT EXISTS a2a_peers_lab_name_uidx
    ON a2a_peers (lab_id, name);
