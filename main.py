import http.client
import json
import logging
import ssl
import time
from threading import Event, Thread
from uuid import UUID
from pathlib import Path
from urllib.parse import urlencode

import psycopg

from db import RegistrationStore, local_dsn
from schedule_notifications import work_notifications
from schedule_updates import admin_id, decide
from schedule_view import HOME, MENU, PERIODS, format_schedule, requested_days

PROJECT_DIR = Path(__file__).resolve().parent
API_HOST = "platform-api2.max.ru"
POLL_TIMEOUT = 30
WELCOME = (
    "Привет! Я «Подземелье знаний» — учебный бот для студентов "
    "Астраханского колледжа вычислительной техники."
)
logger = logging.getLogger(__name__)


def load_config(path=PROJECT_DIR / ".env"):
    """Читает простые строки КЛЮЧ=значение из локального файла."""
    if not path.exists():
        return {}

    config = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            config[key.strip()] = value.strip()
    return config


class MaxError(Exception):
    def __init__(self, message, *, retryable=False, status=None, retry_after=None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after


class MaxClient:
    def __init__(self, token, context):
        self.token = token
        self.context = context

    def request(self, method, path, params, *, payload=None, timeout=15):
        target = f"{path}?{urlencode(params)}"
        headers = {"Authorization": self.token, "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        connection = http.client.HTTPSConnection(
            API_HOST, timeout=timeout, context=self.context
        )
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            if not 200 <= status < 300:
                if status in (401, 403):
                    raise MaxError(
                        f"MAX отклонил доступ (HTTP {status}). Проверь MAX_BOT_TOKEN и права бота.",
                        status=status,
                    )
                retry_after = response.getheader("Retry-After") if status == 429 else None
                retry_after = (int(retry_after) if isinstance(retry_after, str)
                               and retry_after.isascii() and retry_after.isdecimal()
                               and len(retry_after) <= 9 else None)
                raise MaxError(
                    f"MAX вернул HTTP {status} для {method} {path}.",
                    retryable=status in (408, 429) or 500 <= status < 600,
                    status=status, retry_after=retry_after,
                )
            result = json.loads(response.read())
        except ssl.SSLCertVerificationError:
            raise MaxError(
                "Не удалось проверить TLS-сертификат MAX. Проверь MAX_CA_FILE и доверенные сертификаты."
            ) from None
        except (OSError, http.client.HTTPException):
            raise MaxError(
                f"Ошибка соединения с MAX при {method} {path}.", retryable=True
            ) from None
        except (ValueError, UnicodeError):
            raise MaxError(f"Некорректные данные для {method} {path}.") from None
        finally:
            connection.close()

        if not isinstance(result, dict):
            raise MaxError(f"Неожиданный ответ MAX для {method} {path}.")
        return result

    def get_updates(self, marker):
        params = {"timeout": POLL_TIMEOUT, "types": "message_created,message_callback"}
        if marker is not None:
            params["marker"] = marker
        result = self.request("GET", "/updates", params, timeout=POLL_TIMEOUT + 10)
        updates = result.get("updates")
        next_marker = result.get("marker")
        if not isinstance(updates, list) or (
            next_marker is not None and type(next_marker) is not int
        ):
            raise MaxError("MAX вернул обновления в неожиданном формате.")
        return updates, next_marker

    def send_message(self, recipient, text, *, attachments=None):
        payload = {"text": text}
        if attachments is not None:
            payload["attachments"] = attachments
        result = self.request("POST", "/messages", recipient, payload=payload)
        if not isinstance(result.get("message"), dict):
            raise MaxError("MAX не подтвердил отправку сообщения.", retryable=True)

    def answer(self, callback_id, *, text=None, notification=None, attachments=None):
        payload = (
            {"message": {"text": text, "attachments": attachments or []}}
            if text is not None else {"notification": notification}
        )
        self.request("POST", "/answers", {"callback_id": callback_id}, payload=payload)


def as_object(value):
    return value if isinstance(value, dict) else {}


def max_user_id(user):
    user = as_object(user)
    value = user.get("user_id")
    if not user.get("is_bot") and type(value) is int and 0 < value < 2**63:
        return value
    return None


def start_message(update):
    update = as_object(update)
    if update.get("update_type") != "message_created":
        return None
    message = as_object(update.get("message"))
    text = as_object(message.get("body")).get("text")
    if not isinstance(text, str) or text.strip().split(maxsplit=1)[:1] != ["/start"]:
        return None
    if max_user_id(message.get("sender")) is None:
        return None
    return message


def start_recipient(update):
    message = start_message(update)
    if message is None:
        return None
    chat_id = as_object(message.get("recipient")).get("chat_id")
    if type(chat_id) is int:
        return {"chat_id": chat_id}
    return {"user_id": max_user_id(message.get("sender"))}


ADMIN_COMMANDS = {"/schedule_confirm": True, "/schedule_cancel": False}
ADMIN_RESULTS = {
    "applied": "Расписание опубликовано.",
    "cancelled": "Предпросмотр отменён.",
    "expired": "Срок подтверждения истёк. Создай новый предпросмотр.",
    "stale": "Расписание изменилось. Создай новый предпросмотр.",
    "missing": "Предпросмотр не найден.",
    "forbidden": "Команда недоступна.",
}


def handle_admin(client, store, update, administrator):
    update = as_object(update)
    if update.get("update_type") != "message_created" or administrator is None:
        return False
    message = as_object(update.get("message"))
    if (max_user_id(message.get("sender")) != administrator
            or as_object(message.get("recipient")).get("chat_type") != "dialog"):
        return False
    text = as_object(message.get("body")).get("text")
    args = text.split(maxsplit=2) if isinstance(text, str) else []
    if not args or args[0] not in ADMIN_COMMANDS:
        return False
    try:
        if len(args) != 2 or len(args[1]) != 36:
            raise ValueError
        token = UUID(args[1])
        status = decide(store, administrator, max_user_id(message.get("sender")), token,
                        confirm=ADMIN_COMMANDS[args[0]])
        reply = ADMIN_RESULTS[status]
    except ValueError:
        reply = "Укажи код предпросмотра из локальной команды."
    except psycopg.Error:
        logger.warning("PostgreSQL: не удалось выполнить команду публикации.")
        reply = "База временно недоступна. Повтори команду позже."
    client.send_message({"user_id": administrator}, reply)
    return True


def handle_update(client, store, update, administrator=None):
    if handle_admin(client, store, update, administrator):
        return
    message = start_message(update)
    if message is not None:
        recipient = start_recipient(update)
        if as_object(message.get("recipient")).get("chat_type") in ("chat", "channel"):
            client.send_message(recipient, f"{WELCOME} Для регистрации отправь /start в личном диалоге со мной.")
            return
        user_id = max_user_id(message.get("sender"))
        try:
            group = store.start(user_id)
            groups = [] if group else store.active_groups()
        except psycopg.Error:
            logger.warning("PostgreSQL: не удалось открыть регистрацию.")
            client.send_message(recipient, "База временно недоступна. Попробуй /start позже.")
            return
        if group:
            client.send_message(recipient, f"{WELCOME}\nТвоя группа: {group[0]}.", attachments=HOME)
        elif not groups:
            client.send_message(recipient, f"{WELCOME}\nГруппы пока не добавлены. Попробуй /start позже.")
        else:
            buttons = [
                {"type": "callback", "text": name, "payload": f"group:{group_id}"}
                for group_id, name in groups
            ]
            # До 30 строк в одном сообщении; разбиваем длинный список групп.
            for offset in range(0, len(buttons), 60):
                rows = [buttons[i:i+2] for i in range(offset, min(offset + 60, len(buttons)), 2)]
                text = f"{WELCOME}\nВыбери свою группу:" if offset == 0 else "Другие группы:"
                client.send_message(recipient, text, attachments=[
                    {"type": "inline_keyboard", "payload": {"buttons": rows}},
                ])
        return

    update = as_object(update)
    if update.get("update_type") != "message_callback":
        return
    callback = as_object(update.get("callback"))
    callback_id = callback.get("callback_id")
    payload = callback.get("payload")
    user_id = max_user_id(callback.get("user"))
    if not (isinstance(callback_id, str) and callback_id.strip() and user_id):
        return
    if not isinstance(payload, str):
        return
    if payload.startswith("schedule:"):
        message = as_object(update.get("message"))
        if as_object(message.get("recipient")).get("chat_type") in ("chat", "channel"):
            client.answer(callback_id, notification="Открой расписание в личном диалоге с ботом.")
            return
        handle_schedule(client, store, callback_id, user_id, payload.removeprefix("schedule:"))
        return
    if not payload.startswith("group:"):
        return
    raw_id = payload.removeprefix("group:")
    if not (1 <= len(raw_id) <= 19 and raw_id.isascii() and raw_id.isdecimal()
            and 0 < int(raw_id) < 2**63):
        client.answer(callback_id, notification="Кнопка недействительна. Отправь /start.")
        return
    try:
        status, name = store.choose_group(user_id, int(raw_id))
    except psycopg.Error:
        logger.warning("PostgreSQL: не удалось сохранить группу.")
        client.answer(callback_id, notification="База временно недоступна. Попробуй ещё раз.")
        return
    if status == "unavailable":
        client.answer(callback_id, notification="Группа недоступна. Отправь /start для нового списка.")
    elif status == "existing":
        client.answer(callback_id, text=f"Твоя группа уже сохранена: {name}.", attachments=HOME)
    else:
        client.answer(callback_id, text=f"Регистрация завершена. Твоя группа: {name}.", attachments=HOME)


def handle_schedule(client, store, callback_id, user_id, action):
    try:
        if action in PERIODS:
            rows = store.schedule(user_id, requested_days(action))
            if rows:
                client.answer(callback_id, text=format_schedule(rows), attachments=MENU)
                return
        elif action in ("open", "back"):
            group = store.registered_group(user_id)
            if group:
                if action == "back":
                    client.answer(callback_id, text=f"Твоя группа: {group}.", attachments=HOME)
                else:
                    client.answer(callback_id, text=f"Расписание · {group}", attachments=MENU)
                return
        else:
            client.answer(callback_id, notification="Кнопка недействительна. Отправь /start.")
            return
    except psycopg.Error:
        logger.warning("PostgreSQL: не удалось открыть расписание.")
        client.answer(callback_id, notification="Расписание временно недоступно. Попробуй ещё раз.")
        return
    client.answer(callback_id, notification="Сначала зарегистрируйся через /start.")


def run_polling(client, store, administrator=None):
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
                handle_update(client, store, update, administrator)
            except MaxError as error:
                if error.status in (None, 401, 403) and not error.retryable:
                    raise
                # При обрыве связи сервер мог уже принять POST.
                logger.warning("Ответ на событие не подтверждён. %s", error)

        if next_marker is not None:
            marker = next_marker


def max_tls_context(config):
    context = ssl.create_default_context()
    ca_file = config.get("MAX_CA_FILE")
    if ca_file:
        context.load_verify_locations(cafile=PROJECT_DIR / Path(ca_file).expanduser())
    return context


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
        store = RegistrationStore(local_dsn(config))
        store.check_schema()
    except ValueError as error:
        logger.error("%s", error)
        return 1
    except psycopg.Error:
        logger.error("PostgreSQL недоступен или схема не готова. Проверь DATABASE_URL и запусти manage.py migrate.")
        return 1

    logger.info("Запускаю локальный опрос MAX. Остановка — Ctrl+C.")
    client = MaxClient(token, context)
    stop = Event()
    worker = None
    if config.get("SCHEDULE_NOTIFICATIONS_ENABLED") == "1":
        worker = Thread(target=work_notifications, args=(store, client.send_message, MaxError, stop),
                        name="schedule-notifications", daemon=True)
        worker.start()
    try:
        run_polling(client, store, administrator)
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


if __name__ == "__main__":
    raise SystemExit(main())
