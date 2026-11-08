"""Локальный статус без пользовательских данных, токенов и публичного порта."""

import json
import logging
import math
import os
from pathlib import Path
import stat
import tempfile
from threading import Lock
import time

logger = logging.getLogger(__name__)
CRITICAL = {
    "startup_configuration",
    "startup_tls",
    "startup_database",
    "max_authentication",
    "max_api_failure",
    "main_loop_failure",
    "notification_worker_failure",
    "health_write_failure",
}


class HealthState:
    def __init__(self, path, *, clock=time.time):
        self.path = Path(path).expanduser()
        self.clock = clock
        self.lock = Lock()
        parent = self.path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or parent.st_mode & 0o077
        ):
            raise ValueError(
                "Health-каталог должен принадлежать оператору и иметь права 0700."
            )
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
            ):
                raise ValueError(
                    "Health-файл должен быть обычным закрытым файлом оператора."
                )
        now = clock()
        self.data = {
            "version": 1,
            "pid": os.getpid(),
            "started_at": now,
            "updated_at": now,
            "stopped": False,
            "last_max_success": None,
            "last_max_failure": None,
            "max_error": None,
            "last_db_check": None,
            "database_available": None,
            "critical_error": None,
            "handler_errors": 0,
        }
        if not self._save():
            raise ValueError("Не удалось создать закрытый health-файл.")

    @classmethod
    def from_config(cls, config):
        return cls(config["BOT_HEALTH_FILE"]) if config.get("BOT_HEALTH_FILE") else None

    def _save(self):
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".health-", dir=self.path.parent)
            with os.fdopen(fd, "w") as stream:
                json.dump(self.data, stream)
                stream.write("\n")
            os.replace(temporary, self.path)
            return True
        except OSError:
            self.data["critical_error"] = "health_write_failure"
            logger.error("Private health snapshot could not be written")
            return False
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _update(self, **values):
        with self.lock:
            self.data.update(values, updated_at=self.clock())
            self._save()

    def max_success(self):
        self._update(last_max_success=self.clock(), max_error=None)

    def max_failure(self, status):
        code = (
            f"http_{status}"
            if type(status) is int and 100 <= status <= 599
            else "transport"
        )
        self._update(last_max_failure=self.clock(), max_error=code)

    def critical(self, code):
        if code not in CRITICAL:
            raise ValueError("Недопустимая категория health-ошибки.")
        self._update(critical_error=code)

    def handler_error(self):
        with self.lock:
            self.data["handler_errors"] += 1
            self.data["updated_at"] = self.clock()
            self._save()

    def probe_database(self, db):
        try:
            with db.connect() as conn:
                available = conn.execute("SELECT 1").fetchone() == (1,)
        except Exception:
            available = False
        self._update(last_db_check=self.clock(), database_available=available)
        return available

    def monitor(self, db, stop, *, interval=30):
        while not stop.is_set():
            self.probe_database(db)
            stop.wait(interval)

    def stopped(self):
        self._update(stopped=True)


def read_status(path, *, now=None, max_age=120):
    """Статус читает только владелец обычного файла 0600; симлинки запрещены."""
    if not math.isfinite(max_age) or max_age <= 0:
        raise ValueError("max_age должен быть положительным.")
    fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
        ):
            raise ValueError("Нет доступа к закрытому health-файлу оператора.")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("Неверный health-файл.")
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or type(data.get("pid")) is not int
        or data["pid"] <= 0
        or data.get("critical_error") not in CRITICAL | {None}
        or type(data.get("stopped")) is not bool
        or type(data.get("handler_errors")) is not int
        or data.get("database_available") not in (True, False, None)
    ):
        raise ValueError("Неверный health-файл.")
    for key in (
        "started_at",
        "updated_at",
        "last_max_success",
        "last_max_failure",
        "last_db_check",
    ):
        if data.get(key) is not None and (
            type(data[key]) not in (int, float) or not math.isfinite(data[key])
        ):
            raise ValueError("Неверный health-файл.")
    if data.get("updated_at") is None:
        raise ValueError("Неверный health-файл.")
    try:
        os.kill(data["pid"], 0)
        alive = True
    except ProcessLookupError:
        alive = False
    except PermissionError:
        alive = None
    now = time.time() if now is None else now

    def age(key):
        return None if data.get(key) is None else max(0, round(now - data[key], 3))

    result = {
        key: data.get(key)
        for key in (
            "pid",
            "started_at",
            "last_max_success",
            "last_max_failure",
            "last_db_check",
            "database_available",
            "critical_error",
            "handler_errors",
            "stopped",
        )
    }
    result.update(
        process_alive=alive,
        snapshot_age_seconds=age("updated_at"),
        max_success_age_seconds=age("last_max_success"),
        database_check_age_seconds=age("last_db_check"),
    )
    result["healthy"] = bool(
        alive
        and not data["stopped"]
        and not data["critical_error"]
        and data["database_available"] is True
        and age("last_db_check") is not None
        and age("last_db_check") <= max_age
        and age("last_max_success") is not None
        and age("last_max_success") <= max_age
        and age("updated_at") <= max_age
    )
    return result
