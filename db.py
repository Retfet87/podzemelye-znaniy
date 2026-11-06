from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def local_dsn(config, key="DATABASE_URL"):
    """Только локальная БД; не выводим значение строки подключения."""
    dsn = config.get(key)
    if not dsn:
        raise ValueError(f"Укажи {key} в .env.")
    try:
        settings = conninfo_to_dict(dsn)
    except psycopg.Error:
        raise ValueError(f"Некорректная строка подключения {key}.") from None
    if (
        settings.get("host") not in ("127.0.0.1", "::1")
        or "hostaddr" in settings
        or "service" in settings
        or not settings.get("dbname")
    ):
        raise ValueError(f"{key} должен указывать имя базы и локальный адрес 127.0.0.1 или ::1.")
    return dsn


class RegistrationStore:
    def __init__(self, dsn):
        self.dsn = dsn

    def connect(self):
        host = conninfo_to_dict(self.dsn)["host"]
        return psycopg.connect(self.dsn, hostaddr=host, connect_timeout=5)

    def check_schema(self):
        with self.connect() as conn:
            applied = conn.execute("SELECT version FROM schema_migrations").fetchall()
        if set(applied) != {(p.name,) for p in MIGRATIONS.glob("*.sql")}:
            raise ValueError("Схема БД не совпадает с версией проекта. Проверь миграции.")

    def start(self, user_id):
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO users (max_user_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (user_id,),
            )
            return self._group(conn, user_id)

    @staticmethod
    def _group(conn, user_id):
        return conn.execute(
            """SELECT g.name FROM users u JOIN groups g ON g.id = u.group_id
               WHERE u.max_user_id = %s""",
            (user_id,),
        ).fetchone()

    def active_groups(self):
        with self.connect() as conn:
            return conn.execute(
                "SELECT id, name FROM groups WHERE active ORDER BY name"
            ).fetchall()

    def choose_group(self, user_id, group_id):
        with self.connect() as conn:
            chosen = conn.execute(
                """UPDATE users u SET group_id = g.id FROM groups g
                   WHERE u.max_user_id = %s AND u.group_id IS NULL
                     AND g.id = %s AND g.active
                   RETURNING g.name""",
                (user_id, group_id),
            ).fetchone()
            if chosen:
                return "saved", chosen[0]
            existing = self._group(conn, user_id)
            if existing:
                return "existing", existing[0]
            return "unavailable", None

    def registered_group(self, user_id):
        with self.connect() as conn:
            row = self._group(conn, user_id)
            return row[0] if row else None

    def schedule(self, user_id, days):
        with self.connect() as conn:
            return conn.execute(
                """SELECT requested.day, g.name, version.id, version.numerator_monday,
                          lesson.pair, lesson.parity, lesson.subject, lesson.teacher,
                          lesson.room, lesson.note
                   FROM users u JOIN groups g ON g.id = u.group_id
                   CROSS JOIN unnest(%s::date[]) AS requested(day)
                   LEFT JOIN LATERAL (
                       SELECT i.id, i.numerator_monday
                       FROM schedule_groups sg JOIN schedule_imports i ON i.id = sg.import_id
                       WHERE sg.group_id = g.id AND requested.day BETWEEN i.valid_from AND i.valid_to
                       ORDER BY (i.scope = 'date') DESC, i.id DESC LIMIT 1
                   ) version ON true
                   LEFT JOIN schedule_lessons lesson ON lesson.import_id = version.id
                       AND lesson.group_id = g.id
                       AND lesson.weekday = extract(isodow FROM requested.day) - 1
                   WHERE u.max_user_id = %s
                   ORDER BY requested.day, lesson.pair, lesson.parity DESC""",
                (days, user_id),
            ).fetchall()

    @staticmethod
    def save_lessons(cur, version, groups, lessons):
        cur.executemany("INSERT INTO groups (name) VALUES (%s) ON CONFLICT DO NOTHING",
                        [(name,) for name in groups])
        cur.execute("SELECT name, id FROM groups WHERE name = ANY(%s)", (list(groups),))
        ids = dict(cur.fetchall())
        cur.executemany("INSERT INTO schedule_groups VALUES (%s, %s)",
                        [(version, ids[name]) for name in groups])
        cur.executemany(
            """INSERT INTO schedule_lessons
               (import_id, group_id, weekday, pair, parity, subject, teacher, room, note, raw_rows)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(version, ids[x["group"]], x["weekday"], x["pair"], x["parity"], x["subject"],
              x["teacher"], x["room"], x["note"], list(x["raw"])) for x in lessons],
        )
        return ids

    def add_groups(self, names):
        with self.connect() as conn, conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO groups (name) VALUES (%s) ON CONFLICT (name) DO NOTHING",
                [(name,) for name in names],
            )


def migrate(store):
    applied = []
    with store.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(281952764)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version text PRIMARY KEY,
                   applied_at timestamptz NOT NULL DEFAULT now()
               )"""
        )
        seen = set(conn.execute("SELECT version FROM schema_migrations").fetchall())
        paths = sorted(MIGRATIONS.glob("*.sql"))
        if seen - {(path.name,) for path in paths}:
            raise ValueError("В БД есть миграции, которых нет в проекте.")
        for path in paths:
            if (path.name,) not in seen:
                conn.execute(path.read_text(encoding="utf-8"), prepare=False)
                conn.execute("INSERT INTO schema_migrations VALUES (%s)", (path.name,))
                applied.append(path.name)
    return applied
