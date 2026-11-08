"""Подключение к локальной PostgreSQL."""
from pathlib import Path
from contextlib import contextmanager
from contextvars import ContextVar
import logging

import psycopg
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.pq import TransactionStatus
from psycopg.rows import tuple_row

try:
    from psycopg_pool import ConnectionPool
except ImportError:
    ConnectionPool = None  # Direct connections remain usable before optional rollout.


logger = logging.getLogger(__name__)

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
        self._scope = ContextVar('database_unit_of_work', default=None)
        self._pool = None
        self._reset_callback = self._reset
        host = conninfo_to_dict(dsn)["host"]
        self._options = {"hostaddr": host} if host in ("127.0.0.1", "::1") else {}
        if pool_size:
            if ConnectionPool is None:
                raise ValueError("Для пула установи зависимости проекта (psycopg_pool).")
            self._pool = ConnectionPool(
                dsn, min_size=0, max_size=pool_size, max_waiting=32, timeout=pool_timeout,
                open=False, name="bot-database", check=_pool_check,
                kwargs={"connect_timeout": 5, "prepare_threshold": None, **self._options})

    @classmethod
    def from_config(cls, config):
        value = config.get('DB_POOL_MAX_SIZE', '0')
        if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) > 2:
            raise ValueError('DB_POOL_MAX_SIZE должен быть целым числом от 0 до 16.')
        return cls(local_dsn(config), pool_size=int(value))

    def connect(self):
        if self._scope.get() is not None:
            return self._borrowed_connection()
        if self._pool is not None:
            return self._pooled_connection()
        return psycopg.connect(self.dsn, connect_timeout=5, **self._options)

    @contextmanager
    def _borrowed_connection(self):
        # A savepoint keeps caught repository failures from poisoning the outer
        # draft transaction. Its success never commits that outer transaction.
        conn = self._scope.get()
        with conn.transaction():
            yield conn

    @contextmanager
    def unit_of_work(self):
        """Opt-in atomic form publication + draft consumption in one connection."""
        if self._scope.get() is not None:
            raise RuntimeError('Nested unit of work is unsupported')
        with self.connect() as conn:
            token = self._scope.set(conn)
            try:
                yield conn
            finally:
                self._scope.reset(token)

    @contextmanager
    def _pooled_connection(self):
        self._pool.open()
        conn = self._pool.getconn()
        try:
            # Connection.__exit__ commits/rolls back and recognises pool ownership;
            # explicit commits inside repositories (notification sender) still work.
            with conn:
                yield conn
        finally:
            try:
                if not conn.closed:
                    self._reset_callback(conn)
            except BaseException as error:
                conn.close()
                if not isinstance(error, Exception):
                    raise
                logger.warning('Pool session reset failed; connection discarded')
            finally:
                self._pool.putconn(conn)

    @staticmethod
    def _reset(conn):
        # Synchronous: release session locks BEFORE putconn, not in a queued
        # maintenance task which can be dropped by an immediate pool.close().
        if conn.info.transaction_status != TransactionStatus.IDLE:
            conn.rollback()
        try:
            conn.autocommit = True
            conn.execute('DISCARD ALL', prepare=False)
        finally:
            conn.autocommit = False
        conn.row_factory = tuple_row
        conn.isolation_level = None
        conn.read_only = None
        conn.deferrable = None
        conn.prepare_threshold = None
        # DISCARD ALL removes prepared statements; disable the client cache too.

    def close(self):
        if self._pool is not None:
            self._pool.close(timeout=5)

    def polling_lease(self, token):
        from app.db.polling_lease import PollingLease
        return PollingLease(self.dsn, token)

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
