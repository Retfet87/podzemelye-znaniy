"""Точка сборки: читает настройки, создаёт зависимости и запускает бота."""
import logging
from threading import Event, Thread

import psycopg

from app.config import admin_id, load_config, max_tls_context
from app.db import Database
from app.health import HealthState
from app.handlers import Deps, build_router
from app.max_api.client import MaxClient, MaxError
from app.notifications.worker import work_notifications
from app.polling import run_polling

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    health = None
    try:
        config = load_config()
        health = HealthState.from_config(config)
    except (OSError, UnicodeError, ValueError):
        logger.error("Не удалось прочитать .env. Проверь права доступа и кодировку UTF-8.")
        return 1

    token = config.get("MAX_BOT_TOKEN")
    if not token:
        if health is not None:
            health.critical('startup_configuration')
        logger.error("MAX_BOT_TOKEN отсутствует. Добавь токен в файл .env и запусти бота снова.")
        return 1
    if not token.isascii() or any(char.isspace() or not char.isprintable() for char in token):
        if health is not None:
            health.critical('startup_configuration')
        logger.error("MAX_BOT_TOKEN содержит недопустимые символы. Проверь значение в .env.")
        return 1

    try:
        context = max_tls_context(config)
    except (OSError, ValueError):
        if health is not None:
            health.critical('startup_tls')
        logger.error("Не удалось загрузить доверенные TLS-сертификаты. Проверь MAX_CA_FILE.")
        return 1

    db = None
    try:
        administrator = admin_id(config)
        db = Database.from_config(config)
        db.check_schema()
        db.reject_demo()
    except ValueError as error:
        if health is not None:
            health.critical('startup_database')
        if db is not None:
            db.close()
        logger.error("%s", error)
        return 1
    except psycopg.Error:
        if health is not None:
            health.critical('startup_database')
        if db is not None:
            db.close()
        logger.error("PostgreSQL недоступен или схема не готова. Проверь DATABASE_URL и запусти manage.py migrate.")
        return 1

    stop = Event()
    worker = None
    monitor = None
    lease = None
    try:
        lease = db.polling_lease(token)
        lease.open()
        logger.info("Запускаю локальный опрос MAX. Остановка — Ctrl+C.")
        client = MaxClient(token, context)
        router = build_router(Deps.create(client, db, administrator, health=health))
        if health is not None:
            monitor = Thread(target=health.monitor, args=(db, stop), name="health", daemon=True)
            monitor.start()
        schedule_enabled = config.get("SCHEDULE_NOTIFICATIONS_ENABLED") == "1"
        homework_enabled = config.get("HOMEWORK_NOTIFICATIONS_ENABLED") == "1"
        events_enabled = config.get("EVENT_NOTIFICATIONS_ENABLED") == "1"
        if schedule_enabled or homework_enabled or events_enabled:
            worker = Thread(target=work_notifications, args=(db, client.send_message, MaxError, stop),
                            kwargs={"schedule": schedule_enabled, "homework": homework_enabled,
                                    "events": events_enabled, "health": health},
                            name="notifications", daemon=True)
            worker.start()
        if health is None:
            run_polling(client, router, guard=lease.check)
        else:
            run_polling(client, router, health=health, guard=lease.check)
    except ValueError as error:
        logger.error('%s', error)
        return 1
    except MaxError as error:
        if health is not None:
            health.critical("max_authentication" if error.status == 401 else "max_api_failure")
        logger.error("%s", error)
        return 1
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    except Exception:
        if health is not None:
            health.critical('main_loop_failure')
        logger.error('Main polling loop stopped after unexpected failure')
        return 1
    finally:
        stop.set()
        if worker is not None and worker.is_alive():
            worker.join(timeout=20)
        if monitor is not None and monitor.is_alive():
            monitor.join(timeout=6)
        if lease is not None:
            lease.close()
        db.close()
        if health is not None:
            health.stopped()
    return 0
