ALTER TABLE schedule_imports
    ADD COLUMN scope text NOT NULL DEFAULT 'base' CHECK (scope IN ('base', 'date')),
    ADD COLUMN published_at timestamptz,
    ADD CHECK (scope <> 'date' OR valid_from = valid_to);

CREATE TABLE schedule_previews (
    token uuid PRIMARY KEY,
    owner_id bigint NOT NULL,
    base_head bigint NOT NULL,
    candidate jsonb NOT NULL,
    changes jsonb NOT NULL,
    expires_at timestamptz NOT NULL DEFAULT now() + interval '30 minutes',
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'cancelled', 'applied')),
    import_id bigint UNIQUE REFERENCES schedule_imports
);

CREATE TABLE schedule_changes (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_id bigint NOT NULL REFERENCES schedule_imports,
    group_id bigint NOT NULL REFERENCES groups,
    day date NOT NULL,
    pair smallint NOT NULL CHECK (pair BETWEEN 1 AND 6),
    week text NOT NULL,
    kind text NOT NULL CHECK (kind IN ('added', 'cancelled', 'changed')),
    before_lesson jsonb,
    after_lesson jsonb,
    visible boolean NOT NULL,
    UNIQUE (import_id, group_id, day, pair, week)
);

CREATE TABLE schedule_notifications (
    import_id bigint NOT NULL REFERENCES schedule_imports,
    user_id bigint NOT NULL REFERENCES users (max_user_id),
    group_id bigint NOT NULL REFERENCES groups,
    body text NOT NULL,
    valid_through date NOT NULL,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sending', 'sent', 'failed', 'skipped')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
    next_attempt timestamptz NOT NULL DEFAULT now(),
    last_attempt timestamptz,
    sent_at timestamptz,
    last_error text,
    PRIMARY KEY (import_id, user_id)
);

CREATE INDEX schedule_notifications_due ON schedule_notifications (next_attempt)
    WHERE status IN ('pending', 'sending');

CREATE INDEX schedule_notifications_recipient_open ON schedule_notifications (user_id, import_id)
    WHERE status IN ('pending', 'sending');

CREATE TABLE schedule_notification_rate (
    singleton boolean PRIMARY KEY CHECK (singleton),
    next_send timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schedule_notification_rate (singleton) VALUES (true);

DO $$
DECLARE legacy_constraint text;
BEGIN
    SELECT conname INTO STRICT legacy_constraint FROM pg_constraint
    WHERE conrelid = 'schedule_imports'::regclass AND contype = 'u';
    EXECUTE format('ALTER TABLE schedule_imports DROP CONSTRAINT %I', legacy_constraint);
END $$;
ALTER TABLE schedule_imports ADD COLUMN preview_token uuid UNIQUE REFERENCES schedule_previews;
CREATE UNIQUE INDEX schedule_legacy_source ON schedule_imports
    (source_sha256, valid_from, valid_to, numerator_monday) NULLS NOT DISTINCT
    WHERE preview_token IS NULL;
