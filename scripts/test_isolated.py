"""Полный набор только на отдельной *_test БД с bootstrap-owned validation-маркером.

Команда не создаёт маркер и не читает рабочую .env.
"""

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from psycopg.conninfo import conninfo_to_dict

from app import config
from app.db import Database, local_dsn


def main():
    dsn = local_dsn(
        {"VALIDATION_DATABASE_URL": os.environ.get("VALIDATION_DATABASE_URL")},
        "VALIDATION_DATABASE_URL",
    )
    settings = conninfo_to_dict(dsn)
    if not settings["dbname"].endswith("_test") or set(settings) - {
        "host",
        "port",
        "user",
        "password",
        "dbname",
    }:
        raise ValueError("Нужна отдельная *_test БД без переопределений libpq.")
    with Database(dsn).connect() as conn:
        if not conn.execute(
            "SELECT to_regclass('public.validation_environment')"
        ).fetchone()[0]:
            raise ValueError("Нет маркера disposable validation-БД.")
        if conn.execute(
            "SELECT environment FROM public.validation_environment"
        ).fetchall() != [("podzemelye-validation-v1",)]:
            raise ValueError("Неверный маркер validation-БД.")
    original = config.load_config

    def safe_config(*args, **kwargs):
        if args or kwargs or os.environ.get("BOT_CONFIG_MODE") == "compose":
            return original(*args, **kwargs)
        return {}

    environment = {"PATH": os.environ.get("PATH", os.defpath), "TEST_DATABASE_URL": dsn}
    if os.environ.get("VALIDATION_DEMO_DATABASE_URL"):
        environment["VALIDATION_DEMO_DATABASE_URL"] = os.environ[
            "VALIDATION_DEMO_DATABASE_URL"
        ]
    if os.environ.get("AKVT_TEST_XLS"):
        environment["AKVT_TEST_XLS"] = os.environ["AKVT_TEST_XLS"]
    with (
        patch.dict(os.environ, environment, clear=True),
        patch.object(config, "load_config", safe_config),
    ):
        suite = unittest.defaultTestLoader.discover(
            str(Path(__file__).resolve().parents[1] / "tests")
        )
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
