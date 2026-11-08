"""Один poller на токен и базу; его отдельная сессия не возвращается в пул."""

from hashlib import sha256

import psycopg
from psycopg.conninfo import conninfo_to_dict


class PollingLease:
    def __init__(self, dsn, token):
        self.dsn = dsn
        self.key = int.from_bytes(
            sha256(token.encode()).digest()[:4], "big", signed=True
        )
        self.conn = None

    def open(self):
        host = conninfo_to_dict(self.dsn).get("host")
        options = {"hostaddr": host} if host in ("127.0.0.1", "::1") else {}
        self.conn = psycopg.connect(
            self.dsn, autocommit=True, connect_timeout=5, **options
        )
        if not self.conn.execute(
            "SELECT pg_try_advisory_lock(%s,%s)", (17181017, self.key)
        ).fetchone()[0]:
            self.close()
            raise ValueError(
                "Другой процесс уже опрашивает этот бот в данной базе. Второй запуск запрещён."
            )
        return self

    def check(self):
        # После потери сессии нельзя продолжать без гарантии единственного poller.
        self.conn.execute("SELECT 1")

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
