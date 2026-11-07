CREATE TABLE institutions (
    code text PRIMARY KEY,
    name text NOT NULL,
    active boolean NOT NULL DEFAULT true
);
INSERT INTO institutions VALUES ('akvt', 'Астраханский колледж вычислительной техники (АКВТ)', true);
ALTER TABLE groups ADD COLUMN institution_code text NOT NULL DEFAULT 'akvt' REFERENCES institutions;
ALTER TABLE users ADD COLUMN welcome_seen boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN institution_code text REFERENCES institutions;
UPDATE users u SET welcome_seen = true, institution_code = g.institution_code FROM groups g WHERE u.group_id = g.id;
ALTER TABLE groups ADD UNIQUE (id, institution_code);
ALTER TABLE users ADD CONSTRAINT users_group_institution FOREIGN KEY (group_id, institution_code)
    REFERENCES groups (id, institution_code);
ALTER TABLE users ADD CONSTRAINT users_group_school_required CHECK (group_id IS NULL OR institution_code IS NOT NULL);
ALTER TABLE events ADD COLUMN tags text[] NOT NULL DEFAULT '{}'
    CHECK (tags <@ ARRAY['study','contest','hackathon','culture','sport','important','other']::text[]);
ALTER TABLE events ADD COLUMN report_body text NOT NULL DEFAULT '' CHECK (char_length(report_body) <= 1800);
ALTER TABLE events ADD COLUMN report_link text NOT NULL DEFAULT '' CHECK (char_length(report_link) <= 700);
ALTER TABLE events ADD COLUMN report_published boolean NOT NULL DEFAULT false;
CREATE TABLE event_media (
    event_id bigint REFERENCES events ON DELETE CASCADE,
    section text NOT NULL CHECK (section IN ('event','report')),
    slot smallint CHECK (slot BETWEEN 1 AND 3),
    kind text NOT NULL CHECK (kind IN ('image','video')),
    name text NOT NULL,
    data bytea NOT NULL CHECK (octet_length(data) BETWEEN 1 AND 20971520),
    PRIMARY KEY (event_id, section, slot)
);
CREATE TABLE demo_records (
    kind text NOT NULL CHECK (kind IN ('event','material','homework','user')),
    record_id bigint NOT NULL,
    PRIMARY KEY (kind, record_id)
);
