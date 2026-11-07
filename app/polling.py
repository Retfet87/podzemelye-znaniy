"""Long Polling: получение событий MAX и передача их роутеру."""
import logging
import time

from app.max_api.client import MaxError

logger = logging.getLogger(__name__)


def run_polling(client, router):
    marker = None
    retry_delay = 2
    connected = False

    while True:
        try:
            updates, next_marker = client.get_updates(marker)
        except MaxError as error:
            if not error.retryable:
                if error.status in (400, 409):
                    logger.error("Проверь, что у бота нет активной подписки Webhook.")
                raise
            logger.warning("%s Повтор опроса через %s с.", error, retry_delay)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30)
            continue

        if not connected or retry_delay > 2:
            logger.info("MAX отвечает. Long Polling запущен.")
        connected = True
        retry_delay = 2

        for update in updates:
            try:
                router.dispatch(update)
            except MaxError as error:
                if error.status in (None, 401) and not error.retryable:
                    raise
                # При обрыве связи сервер мог уже принять POST.
                logger.warning("Ответ на событие не подтверждён. %s", error)

        if next_marker is not None:
            marker = next_marker
