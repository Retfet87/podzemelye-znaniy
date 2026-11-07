"""Create explicitly labelled demonstration drafts only in a separate test database."""
import argparse
from datetime import datetime, timedelta

from psycopg.conninfo import conninfo_to_dict

from app.config import load_config
from app.db import Database, local_dsn
from app.timeutil import SAMARA


def main():
    parser = argparse.ArgumentParser(description="Демонстрационные данные в отдельной тестовой БД.")
    parser.add_argument("--demo", action="store_true", required=True)
    parser.parse_args()
    config = load_config()
    dsn = local_dsn(config, "TEST_DATABASE_URL")
    settings = conninfo_to_dict(dsn)
    if not settings["dbname"].endswith(("_test", "_demo")):
        parser.error("TEST_DATABASE_URL должен указывать отдельную БД с суффиксом _test или _demo.")
    if config.get("DATABASE_URL") == dsn:
        parser.error("Тестовая база должна отличаться от рабочей.")
    db = Database(dsn)
    db.check_schema()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO events (title, description, starts_at, place, state)
               SELECT %s, %s, %s, '', 'draft'
               WHERE NOT EXISTS (SELECT 1 FROM events WHERE title = %s)""",
            ("ДЕМО — проверка мероприятия", "Учебный пример для демонстрации.",
             datetime.now(SAMARA) + timedelta(days=3), "ДЕМО — проверка мероприятия"),
        )
    print("Демонстрационный черновик добавлен в отдельную тестовую базу.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
