"""Регистрация: /start и выбор учебной группы."""
import logging
import re

import psycopg

from app.handlers.base import Handler
from app.handlers.menus import HOME
from app.max_api.keyboards import callback_button, inline_keyboard
from app.max_api.updates import as_object, max_user_id, start_message, start_recipient

logger = logging.getLogger(__name__)

WELCOME = (
    "Привет! Я «Подземелье знаний» — учебный бот для студентов "
    "Астраханского колледжа вычислительной техники."
)


def group_prefix(name: str) -> str:
    """Извлекает префикс направления (например, ВЕБ из ВЕБ-11)."""
    match = re.match(r"^([А-ЯЁA-Zа-яёa-z]+)", name.strip())
    return match.group(1).upper() if match else "ДРУГИЕ"


def prefixes_keyboard(groups):
    """Клавиатура первого шага: выбор направления (ВЕБ, ИБ, ИС, ПБ, РУП и т.д.)."""
    prefixes = sorted({group_prefix(name) for _, name in groups})
    buttons = [callback_button(prefix, f"group_prefix:{prefix}") for prefix in prefixes]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return inline_keyboard(rows)


def department_groups_keyboard(prefix: str, groups):
    """Клавиатура второго шага: чистые названия групп + кнопка Назад."""
    filtered = [(gid, name) for gid, name in groups if group_prefix(name) == prefix]
    filtered.sort(key=lambda x: x[1])

    buttons = [callback_button(name, f"group:{gid}") for gid, name in filtered]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([callback_button("Назад", "group_back")])
    return inline_keyboard(rows)


class RegistrationHandler(Handler):
    def on_message(self, ctx):
        message = start_message(ctx.update)
        if message is None:
            return False
        client = ctx.client
        recipient = start_recipient(ctx.update)
        if as_object(message.get("recipient")).get("chat_type") in ("chat", "channel"):
            client.send_message(recipient, f"{WELCOME} Для регистрации отправь /start в личном диалоге со мной.")
            return True
        user_id = max_user_id(message.get("sender"))
        try:
            group = ctx.users.start(user_id)
            groups = [] if group else ctx.users.active_groups()
        except psycopg.Error:
            logger.warning("PostgreSQL: не удалось открыть регистрацию.")
            client.send_message(recipient, "База временно недоступна. Попробуй /start позже.")
            return True
        if group:
            client.send_message(recipient, f"{WELCOME}\nТвоя группа: {group[0]}.", attachments=HOME)
        elif not groups:
            client.send_message(recipient, f"{WELCOME}\nГруппы пока не добавлены. Попробуй /start позже.")
        else:
            client.send_message(
                recipient,
                f"{WELCOME}\nВыбери своё направление:",
                attachments=prefixes_keyboard(groups),
            )
        return True

    def on_callback(self, ctx):
        client, callback_id = ctx.client, ctx.callback_id

        if ctx.payload == "group_back":
            try:
                groups = ctx.users.active_groups()
            except psycopg.Error:
                logger.warning("PostgreSQL: не удалось загрузить список групп.")
                client.answer(callback_id, notification="База временно недоступна. Попробуй ещё раз.")
                return True
            if not groups:
                client.answer(callback_id, notification="Группы пока не добавлены.")
                return True
            client.answer(
                callback_id,
                text=f"{WELCOME}\nВыбери своё направление:",
                attachments=prefixes_keyboard(groups),
            )
            return True

        if ctx.payload.startswith("group_prefix:"):
            prefix = ctx.payload.removeprefix("group_prefix:")
            try:
                groups = ctx.users.active_groups()
            except psycopg.Error:
                logger.warning("PostgreSQL: не удалось загрузить список групп.")
                client.answer(callback_id, notification="База временно недоступна. Попробуй ещё раз.")
                return True
            matching = [g for g in groups if group_prefix(g[1]) == prefix]
            if not matching:
                client.answer(callback_id, notification="Группы этого направления недоступны.")
                return True
            client.answer(
                callback_id,
                text=f"Направление {prefix}. Выбери свою группу:",
                attachments=department_groups_keyboard(prefix, groups),
            )
            return True

        if not ctx.payload.startswith("group:"):
            return False
        raw_id = ctx.payload.removeprefix("group:")
        if not (1 <= len(raw_id) <= 19 and raw_id.isascii() and raw_id.isdecimal()
                and 0 < int(raw_id) < 2**63):
            client.answer(callback_id, notification="Кнопка недействительна. Отправь /start.")
            return True
        try:
            status, name = ctx.users.choose_group(ctx.actor, int(raw_id))
        except psycopg.Error:
            logger.warning("PostgreSQL: не удалось сохранить группу.")
            client.answer(callback_id, notification="База временно недоступна. Попробуй ещё раз.")
            return True
        if status == "unavailable":
            client.answer(callback_id, notification="Группа недоступна. Отправь /start для нового списка.")
        elif status == "existing":
            client.answer(callback_id, text=f"Твоя группа уже сохранена: {name}.", attachments=HOME)
        else:
            client.answer(callback_id, text=f"Регистрация завершена. Твоя группа: {name}.", attachments=HOME)
        return True