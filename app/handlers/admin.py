"""Команды супер-администратора: подтверждение и отмена публикации расписания."""
import logging
from uuid import UUID

import psycopg

from app.handlers.base import Handler
from app.services.schedule_publisher import decide

logger = logging.getLogger(__name__)

ADMIN_COMMANDS = {"/schedule_confirm": True, "/schedule_cancel": False}
ADMIN_RESULTS = {
    "applied": "Расписание опубликовано.",
    "cancelled": "Предпросмотр отменён.",
    "expired": "Срок подтверждения истёк. Создай новый предпросмотр.",
    "stale": "Расписание изменилось. Создай новый предпросмотр.",
    "missing": "Предпросмотр не найден.",
    "forbidden": "Команда недоступна.",
}


class AdminHandler(Handler):
    def on_message(self, ctx):
        administrator = ctx.admin
        if administrator is None or ctx.actor != administrator or not ctx.private:
            return False
        text = ctx.text
        args = text.split(maxsplit=2) if isinstance(text, str) else []
        if not args or args[0] not in ADMIN_COMMANDS:
            return False
        try:
            if len(args) != 2 or len(args[1]) != 36:
                raise ValueError
            token = UUID(args[1])
            status = decide(ctx.db, administrator, ctx.actor, token,
                            confirm=ADMIN_COMMANDS[args[0]])
            reply = ADMIN_RESULTS[status]
        except ValueError:
            reply = "Укажи код предпросмотра из локальной команды."
        except psycopg.Error:
            logger.warning("PostgreSQL: не удалось выполнить команду публикации.")
            reply = "База временно недоступна. Повтори команду позже."
        ctx.client.send_message({"user_id": administrator}, reply)
        return True
