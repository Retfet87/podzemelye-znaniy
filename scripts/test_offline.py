"""Offline-проверки: python -m scripts.test_offline.

PostgreSQL-сценарии пропускаются и требуют отдельного запуска на тестовой базе.
"""

import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from app import config


def main():
    root = Path(__file__).resolve().parents[1]
    original = config.load_config

    def safe_config(*args, **kwargs):
        # Явные временные файлы и Compose-конфигурацию тестов по-прежнему читаем.
        if args or kwargs or os.environ.get("BOT_CONFIG_MODE") == "compose":
            return original(*args, **kwargs)
        return {}

    with (
        patch.dict(
            os.environ, {"PATH": os.environ.get("PATH", os.defpath)}, clear=True
        ),
        patch.object(config, "load_config", safe_config),
    ):
        suite = unittest.defaultTestLoader.discover(str(root / "tests"))
        result = unittest.TextTestRunner(verbosity=1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.dont_write_bytecode = True
    raise SystemExit(main())
