-- Private form payloads, including attachments, share the database backup policy.
CREATE TABLE form_drafts (
    user_id bigint PRIMARY KEY CHECK (user_id > 0),
    flow text NOT NULL CHECK (flow IN ('homework', 'community')),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    expires_at timestamptz NOT NULL,
    CHECK (pg_column_size(payload) <= 90000000)
);
CREATE INDEX form_drafts_expiry ON form_drafts (expires_at);
