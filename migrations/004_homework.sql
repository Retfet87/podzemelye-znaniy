CREATE TABLE homework_group_admins (
    user_id bigint PRIMARY KEY REFERENCES users (max_user_id) ON DELETE CASCADE,
    group_id bigint NOT NULL REFERENCES groups
);

CREATE TABLE homework (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id bigint NOT NULL REFERENCES groups,
    owner_id bigint REFERENCES users (max_user_id) ON DELETE CASCADE,
    subject text NOT NULL CHECK (btrim(subject) <> ''),
    body text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 3000),
    deadline timestamptz,
    revision bigint NOT NULL DEFAULT 1,
    reminder_version bigint NOT NULL DEFAULT 1
);
CREATE INDEX homework_deadline ON homework (deadline) WHERE deadline IS NOT NULL;

CREATE INDEX homework_shared ON homework (group_id, id DESC) WHERE owner_id IS NULL;
CREATE INDEX homework_personal ON homework (owner_id, id DESC) WHERE owner_id IS NOT NULL;

CREATE TABLE homework_files (
    homework_id bigint REFERENCES homework ON DELETE CASCADE,
    slot smallint CHECK (slot BETWEEN 1 AND 3),
    name text NOT NULL,
    data bytea NOT NULL CHECK (octet_length(data) BETWEEN 1 AND 5242880),
    PRIMARY KEY (homework_id, slot)
);

CREATE TABLE homework_preferences (
    user_id bigint REFERENCES users (max_user_id) ON DELETE CASCADE,
    category text CHECK (category IN ('new', 'group_reminder', 'personal_reminder')),
    enabled boolean NOT NULL,
    PRIMARY KEY (user_id, category)
);

CREATE TABLE homework_notifications (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    homework_id bigint NOT NULL REFERENCES homework ON DELETE CASCADE,
    user_id bigint NOT NULL REFERENCES users (max_user_id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('new', '24h', '3h')),
    version bigint NOT NULL,
    valid_until timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'skipped')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
    next_attempt timestamptz NOT NULL,
    last_attempt timestamptz,
    sent_at timestamptz,
    last_error text,
    UNIQUE (homework_id, user_id, kind, version)
);
CREATE INDEX homework_notifications_due ON homework_notifications (next_attempt)
    WHERE status IN ('pending', 'sending');

-- Super-administrator ID comes only from local configuration. Personal access
-- always requires ownership, even when the caller is the super-administrator.
CREATE FUNCTION homework_access(actor bigint, administrator bigint)
RETURNS TABLE (group_id bigint, group_name varchar, member boolean, can_manage boolean)
LANGUAGE sql STABLE AS $$
    SELECT g.id, g.name, coalesce(u.group_id = g.id, false),
           coalesce(actor = administrator OR a.user_id IS NOT NULL, false)
    FROM groups g
    LEFT JOIN users u ON u.max_user_id = actor
    LEFT JOIN homework_group_admins a ON a.user_id = u.max_user_id AND a.group_id = u.group_id
    WHERE g.active AND (u.group_id = g.id OR actor = administrator)
$$;
