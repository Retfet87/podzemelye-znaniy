CREATE TABLE events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
    description text NOT NULL CHECK (char_length(description) <= 1800),
    starts_at timestamptz NOT NULL,
    place text NOT NULL CHECK (char_length(place) <= 300),
    state text NOT NULL CHECK (state IN ('draft', 'proposed', 'published', 'cancelled')),
    proposed_group_id bigint REFERENCES groups,
    revision bigint NOT NULL DEFAULT 1,
    reminder_version bigint NOT NULL DEFAULT 1
);
CREATE INDEX events_upcoming ON events (starts_at, id) WHERE state = 'published';
CREATE TABLE event_participants (
    event_id bigint REFERENCES events ON DELETE CASCADE,
    user_id bigint REFERENCES users (max_user_id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, user_id)
);
CREATE TABLE event_notifications (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id bigint NOT NULL REFERENCES events ON DELETE CASCADE,
    user_id bigint NOT NULL REFERENCES users (max_user_id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('reminder', 'changed', 'cancelled')),
    version bigint NOT NULL,
    valid_until timestamptz NOT NULL,
    body text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'skipped')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
    next_attempt timestamptz NOT NULL DEFAULT now(),
    last_attempt timestamptz,
    sent_at timestamptz,
    last_error text,
    UNIQUE (event_id, user_id, kind, version)
);
CREATE INDEX event_notifications_due ON event_notifications (next_attempt)
    WHERE status IN ('pending', 'sending');
CREATE TABLE useful_materials (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id bigint REFERENCES groups,
    category text NOT NULL CHECK (category IN ('curators', 'teachers', 'rooms', 'study', 'links')),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 120),
    body text NOT NULL CHECK (char_length(body) <= 1800),
    link text NOT NULL CHECK (char_length(link) <= 700),
    revision bigint NOT NULL DEFAULT 1
);
CREATE INDEX useful_materials_list ON useful_materials (category, group_id, id DESC);
CREATE TABLE useful_files (
    material_id bigint REFERENCES useful_materials ON DELETE CASCADE,
    slot smallint CHECK (slot BETWEEN 1 AND 3),
    name text NOT NULL,
    data bytea NOT NULL CHECK (octet_length(data) BETWEEN 1 AND 5242880),
    PRIMARY KEY (material_id, slot)
);
