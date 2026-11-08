CREATE TABLE schedule_imports (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_sha256 text NOT NULL,
    valid_from date NOT NULL,
    valid_to date NOT NULL,
    numerator_monday date,
    CHECK (valid_from <= valid_to),
    CHECK (extract(isodow FROM numerator_monday) = 1),
    UNIQUE NULLS NOT DISTINCT (source_sha256, valid_from, valid_to, numerator_monday)
);

CREATE TABLE schedule_groups (
    import_id bigint REFERENCES schedule_imports,
    group_id bigint REFERENCES groups,
    PRIMARY KEY (import_id, group_id)
);

CREATE INDEX schedule_groups_lookup ON schedule_groups (group_id, import_id DESC);

CREATE TABLE schedule_lessons (
    import_id bigint,
    group_id bigint,
    weekday smallint CHECK (weekday BETWEEN 0 AND 5),
    pair smallint CHECK (pair BETWEEN 1 AND 6),
    parity text CHECK (parity IN ('all', 'numerator', 'denominator')),
    subject text NOT NULL,
    teacher text NOT NULL,
    room text NOT NULL,
    note text NOT NULL,
    raw_rows text[] NOT NULL,
    PRIMARY KEY (import_id, group_id, weekday, pair, parity),
    FOREIGN KEY (import_id, group_id) REFERENCES schedule_groups
);
