"""Подключение к локальной PostgreSQL."""
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo

MIGRATIONS = Path(__file__).resolve().parent.parent.parent / "migrations"


def local_dsn(config, key="DATABASE_URL"):
    """Разрешает loopback либо фиксированный сервис Compose без раскрытия пароля."""
    if key == "DATABASE_URL" and config.get("BOT_CONFIG_MODE") == "compose":
        password = config.get("POSTGRES_PASSWORD")
        if not password:
            raise ValueError("Укажи POSTGRES_PASSWORD для Docker Compose.")
        return make_conninfo(host="postgres", dbname=config.get("POSTGRES_DB", "podzemelye"),
                             user=config.get("POSTGRES_USER", "postgres"), password=password)
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


class Database:
    """Фабрика соединений. Репозитории получают её и сами открывают транзакции."""

    def __init__(self, dsn):
        self.dsn = dsn

    def connect(self):
        host = conninfo_to_dict(self.dsn)["host"]
        options = {"hostaddr": host} if host in ("127.0.0.1", "::1") else {}
        return psycopg.connect(self.dsn, connect_timeout=5, **options)

    def check_schema(self):
        with self.connect() as conn:
            applied = conn.execute("SELECT version FROM schema_migrations").fetchall()
        if set(applied) != {(p.name,) for p in MIGRATIONS.glob("*.sql")}:
            raise ValueError("Схема БД не совпадает с версией проекта. Проверь миграции.")
