"""Меню «Расписание»: сегодня, завтра, неделя."""
import logging

import psycopg

from app.handlers.base import Handler
from app.handlers.menus import HOME, MENU
from app.services.schedule_formatter import PERIODS, format_schedule, requested_days

logger = logging.getLogger(__name__)

SCHEDULE_TRIGGERS = {
    "расписание": "open",
    "распис": "open",
    "пары": "open",
    "уроки": "open",
    "сегодня": "today",
    "расписание на сегодня": "today",
    "завтра": "tomorrow",
    "расписание на завтра": "tomorrow",
    "неделя": "week",
    "расписание на неделю": "week",
}


class ScheduleHandler(Handler):
    def on_message(self, ctx):
        if not ctx.private or not ctx.actor or not isinstance(ctx.text, str):
            return False
        cleaned = " ".join(ctx.text.strip().lower().split())
        action = SCHEDULE_TRIGGERS.get(cleaned)
        if not action:
            return False
        self.show(ctx, action, is_message=True)
        return True

    def on_callback(self, ctx):
        if not ctx.payload.startswith("schedule:"):
            return False
        if ctx.chat_type in ("chat", "channel"):
            ctx.client.answer(ctx.callback_id, notification="Открой расписание в личном диалоге с ботом.")
            return True
        self.show(ctx, ctx.payload.removeprefix("schedule:"))
        return True

    @staticmethod
    def show(ctx, action, *, is_message=False):
        client, callback_id, user_id = ctx.client, ctx.callback_id, ctx.actor

        def respond(text=None, *, attachments=None, notification=None):
            if is_message or callback_id is None:
                client.send_message({"user_id": user_id}, text or notification, attachments=attachments)
            else:
                client.answer(callback_id, text=text, notification=notification, attachments=attachments)

        try:
            if action in PERIODS:
                rows = ctx.schedules.for_user(user_id, requested_days(action))
                if rows:
                    respond(text=format_schedule(rows), attachments=MENU)
                    return
            elif action in ("open", "back"):
                group = ctx.users.registered_group(user_id)
                if group:
                    if action == "back":
                        respond(text=f"Твоя группа: {group}.", attachments=HOME)
                    else:
                        respond(text=f"Расписание · {group}", attachments=MENU)
                    return
            else:
                respond(notification="Кнопка недействительна. Отправь /start.")
                return
        except psycopg.Error:
            logger.warning("PostgreSQL: не удалось открыть расписание.")
            respond(notification="Расписание временно недоступно. Попробуй ещё раз.")
            return
        respond(notification="Сначала зарегистрируйся через /start.")