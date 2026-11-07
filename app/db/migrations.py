"""Применение SQL-миграций из папки migrations/."""
from app.db.database import MIGRATIONS
from app.db.locks import MIGRATION_LOCK


def migrate(db):
    applied = []
    with db.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
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
