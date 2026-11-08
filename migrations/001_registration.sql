CREATE TABLE roles (
    code text PRIMARY KEY,
    name text NOT NULL
);

INSERT INTO roles (code, name) VALUES ('student', 'Студент');

CREATE TABLE groups (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name varchar(80) NOT NULL UNIQUE CHECK (btrim(name) <> ''),
    active boolean NOT NULL DEFAULT true
);

CREATE TABLE users (
    max_user_id bigint PRIMARY KEY CHECK (max_user_id > 0),
    role_code text NOT NULL DEFAULT 'student' REFERENCES roles (code),
    group_id bigint REFERENCES groups (id),
    created_at timestamptz NOT NULL DEFAULT now()
);
