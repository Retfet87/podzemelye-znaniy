"""Регистрация: /start и выбор учебной группы."""
import logging
import re

import psycopg

from app.handlers.base import Handler
from app.handlers.common import number
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


def prefixes_keyboard(groups, offset=0):
    """Клавиатура первого шага: выбор направления (ВЕБ, ИБ, ИС, ПБ, РУП и т.д.)."""
    prefixes = sorted({group_prefix(name) for _, name in groups})
    buttons = [callback_button(prefix, f"group_prefix:{prefix}") for prefix in prefixes[offset:offset + 40]]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    if offset:
        rows.append([callback_button("Предыдущие направления", f"group_prefix_page:{max(0, offset - 40)}")])
    if offset + 40 < len(prefixes):
        rows.append([callback_button("Другие направления", f"group_prefix_page:{offset + 40}")])
    return inline_keyboard(rows)


def department_groups_keyboard(prefix: str, groups, offset=0):
    """Клавиатура второго шага: чистые названия групп + кнопка Назад."""
    filtered = [(gid, name) for gid, name in groups if group_prefix(name) == prefix]
    filtered.sort(key=lambda x: x[1])

    buttons = [callback_button(name, f"group:{gid}") for gid, name in filtered[offset:offset + 40]]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    if offset:
        rows.append([callback_button("Предыдущие группы", f"group_page:{prefix}:{max(0, offset - 40)}")])
    if offset + 40 < len(filtered):
        rows.append([callback_button("Другие группы", f"group_page:{prefix}:{offset + 40}")])
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

        selection = (ctx.payload == "group_back" or ctx.payload.startswith(
            ("group:", "group_prefix:", "group_prefix_page:", "group_page:")))
        if not selection:
            return False
        if not ctx.private:
            client.answer(callback_id, notification="Выбери группу в личном диалоге с ботом.")
            return True
        if not ctx.payload.startswith("group:"):
            try:
                groups = ctx.users.active_groups()
                if ctx.payload == "group_back" or ctx.payload.startswith("group_prefix_page:"):
                    offset = 0 if ctx.payload == "group_back" else number(
                        ctx.payload.removeprefix("group_prefix_page:"), zero=True)
                    attachments = prefixes_keyboard(groups, offset)
                    text = f"{WELCOME}\nВыбери своё направление:"
                else:
                    parts = ctx.payload.split(":")
                    if len(parts) != (3 if parts[0] == "group_page" else 2):
                        raise ValueError("Кнопка недействительна.")
                    prefix = parts[1]
                    offset = number(parts[2], zero=True) if parts[0] == "group_page" and len(parts) == 3 else 0
                    if not any(group_prefix(name) == prefix for _, name in groups):
                        raise ValueError("Группы этого направления недоступны.")
                    attachments = department_groups_keyboard(prefix, groups, offset)
                    text = f"Направление {prefix}. Выбери свою группу:"
                if not groups:
                    raise ValueError("Группы пока не добавлены.")
            except ValueError as error:
                client.answer(callback_id, notification=str(error))
                return True
            except psycopg.Error:
                logger.warning("PostgreSQL: не удалось загрузить список групп.")
                client.answer(callback_id, notification="База временно недоступна. Попробуй ещё раз.")
                return True
            client.answer(callback_id, text=text, attachments=attachments)
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