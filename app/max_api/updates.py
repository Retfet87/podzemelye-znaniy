"""Разбор событий MAX: безопасное чтение вложенных полей и команды /start."""


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
