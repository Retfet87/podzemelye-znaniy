"""Подключение к локальной PostgreSQL."""
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.pq import TransactionStatus

try:
    from psycopg_pool import ConnectionPool
except ImportError:
    ConnectionPool = None  # Direct connections remain usable before optional rollout.


_pool_check = ConnectionPool.check_connection if ConnectionPool else None

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

    def __init__(self, dsn, *, pool_size=0, pool_timeout=5):
        if type(pool_size) is not int or not 0 <= pool_size <= 16:
            raise ValueError("Размер пула должен быть от 0 до 16.")
        self.dsn = dsn
        self._pool = None
        host = conninfo_to_dict(dsn)["host"]
        self._options = {"hostaddr": host} if host in ("127.0.0.1", "::1") else {}
        if pool_size:
            if ConnectionPool is None:
                raise ValueError("Для пула установи зависимости проекта (psycopg_pool).")
            self._pool = ConnectionPool(
                dsn, min_size=0, max_size=pool_size, max_waiting=32, timeout=pool_timeout,
                open=False, name="bot-database", check=_pool_check,
                reset=self._reset, kwargs={"connect_timeout": 5, "prepare_threshold": None, **self._options})

    @classmethod
    def from_config(cls, config):
        value = config.get('DB_POOL_MAX_SIZE', '0')
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 2:
            raise ValueError('DB_POOL_MAX_SIZE должен быть целым числом от 0 до 16.')
        return cls(local_dsn(config), pool_size=int(value))

    def connect(self):
        if self._pool is not None:
            self._pool.open()
            return self._pool.connection()
        return psycopg.connect(self.dsn, connect_timeout=5, **self._options)

    @staticmethod
    def _reset(conn):
        # Pool commits/rolls back before reset. Session locks survive commit;
        # DISCARD ALL must finish before this connection is available again.
        if conn.info.transaction_status != TransactionStatus.IDLE:
            conn.rollback()
        autocommit = conn.autocommit
        try:
            conn.autocommit = True
            conn.execute('DISCARD ALL', prepare=False)
        finally:
            conn.autocommit = autocommit
        # Pooled connections disable auto-prepare: DISCARD ALL also removes
        # server prepared statements, so retaining a client cache is unsafe.

    def close(self):
        if self._pool is not None:
            self._pool.close(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def reject_demo(self):
        """Normal bot/web entrypoints must never bypass the dedicated demo gates."""
        with self.connect() as conn:
            if conn.execute("SELECT to_regclass('demo_environment')").fetchone()[0]:
                raise ValueError('Demo-БД разрешена только через отдельный защищённый demo-вход.')

    def check_schema(self):
        with self.connect() as conn:
            applied = conn.execute("SELECT version FROM schema_migrations").fetchall()
        if set(applied) != {(p.name,) for p in MIGRATIONS.glob("*.sql")}:
            raise ValueError("Схема БД не совпадает с версией проекта. Проверь миграции.")
