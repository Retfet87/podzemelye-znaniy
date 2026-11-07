"""Точка сборки: читает настройки, создаёт зависимости и запускает бота."""
import logging
from threading import Event, Thread

import psycopg

from app.config import admin_id, load_config, max_tls_context
from app.db import Database, local_dsn
from app.handlers import Deps, build_router
from app.max_api.client import MaxClient, MaxError
from app.notifications.worker import work_notifications
from app.polling import run_polling

logger = logging.getLogger(__name__)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config()
    except (OSError, UnicodeError):
        logger.error("Не удалось прочитать .env. Проверь права доступа и кодировку UTF-8.")
        return 1

    token = config.get("MAX_BOT_TOKEN")
    if not token:
        logger.error("MAX_BOT_TOKEN отсутствует. Добавь токен в файл .env и запусти бота снова.")
        return 1
    if not token.isascii() or any(char.isspace() or not char.isprintable() for char in token):
        logger.error("MAX_BOT_TOKEN содержит недопустимые символы. Проверь значение в .env.")
        return 1

    try:
        context = max_tls_context(config)
    except (OSError, ValueError):
        logger.error("Не удалось загрузить доверенные TLS-сертификаты. Проверь MAX_CA_FILE.")
        return 1

    try:
        administrator = admin_id(config)
        db = Database(local_dsn(config))
        db.check_schema()
    except ValueError as error:
        logger.error("%s", error)
        return 1
    except psycopg.Error:
        logger.error("PostgreSQL недоступен или схема не готова. Проверь DATABASE_URL и запусти manage.py migrate.")
        return 1

    logger.info("Запускаю локальный опрос MAX. Остановка — Ctrl+C.")
    client = MaxClient(token, context)
    router = build_router(Deps.create(client, db, administrator))
    stop = Event()
    worker = None
    schedule_enabled = config.get("SCHEDULE_NOTIFICATIONS_ENABLED") == "1"
    homework_enabled = config.get("HOMEWORK_NOTIFICATIONS_ENABLED") == "1"
    events_enabled = config.get("EVENT_NOTIFICATIONS_ENABLED") == "1"
    if schedule_enabled or homework_enabled or events_enabled:
        worker = Thread(target=work_notifications, args=(db, client.send_message, MaxError, stop),
                        kwargs={"schedule": schedule_enabled, "homework": homework_enabled, "events": events_enabled},
                        name="notifications", daemon=True)
        worker.start()
    try:
        run_polling(client, router)
    except MaxError as error:
        logger.error("%s", error)
        return 1
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    finally:
        stop.set()
        if worker:
            worker.join(timeout=20)
    return 0
