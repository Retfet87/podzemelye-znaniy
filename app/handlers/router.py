"""Роутер: разбирает событие и отдаёт его первому подходящему обработчику."""

import logging

from app.handlers.context import Context
from app.max_api.client import MaxError
from app.handlers.menus import HOME
from app.handlers.registration import WELCOME, institution_keyboard
from app.max_api.keyboards import inline_keyboard, callback_button
from app.db.database import Database
from app.services import form_drafts
import psycopg


logger = logging.getLogger(__name__)


class Router:
    def __init__(self, deps, handlers):
        self.deps = deps
        self.handlers = list(handlers)

    def dispatch(self, update):
        try:
            ctx = Context.parse(self.deps, update)
            # Реальные формы сохраняются в общей транзакции; подставные зависимости
            # позволяют проверить маршрутизацию без PostgreSQL.
            if isinstance(self.deps.db, Database) and ctx.private and ctx.actor:
                with self.deps.db.unit_of_work() as conn:
                    locked = conn.execute(
                        "SELECT pg_try_advisory_xact_lock(%s)", (-ctx.actor,)
                    ).fetchone()[0]
                    if not locked:
                        self._error_reply(
                            update,
                            "Предыдущее действие ещё выполняется. Повтори через несколько секунд.",
                        )
                        return
                    form_drafts.load(conn, ctx.actor, self.handlers)
                    self._dispatch(ctx)
                    form_drafts.save(conn, ctx.actor, self.handlers)
            else:
                self._dispatch(ctx)
        except MaxError:
            # Повторы и фатальные ошибки авторизации обрабатывает polling.
            raise
        except psycopg.Error:
            if self.deps.health is not None:
                self.deps.health.handler_error()
            logger.warning("Database unavailable while handling an update")
            self._error_reply(update, "База временно недоступна. Попробуй ещё раз.")
        except Exception:
            if self.deps.health is not None:
                self.deps.health.handler_error()
            # Текст события, вложения и токены не должны попадать в журнал.
            logger.exception("Unexpected handler failure")
            self._error_reply(
                update, "Что-то пошло не так. Попробуй ещё раз или нажми «Домой»."
            )

    def _error_reply(self, update, text):
        try:
            ctx = Context.parse(self.deps, update)
            if ctx.private and ctx.actor:
                if ctx.kind == "callback":
                    ctx.client.answer(
                        ctx.callback_id, text=text, attachments=inline_keyboard([])
                    )
                else:
                    ctx.client.send_message(
                        {"user_id": ctx.actor}, text, attachments=inline_keyboard([])
                    )
        except MaxError:
            raise
        except Exception:
            # Неудачный ответ об ошибке не должен зациклить обработку или остановить polling.
            logger.exception("Unable to send handler error response")

    def _dispatch(self, ctx):
        if ctx.kind == "ignored":
            return
        if self.navigate(ctx):
            return
        method = "on_message" if ctx.kind == "message" else "on_callback"
        handlers = self.handlers
        if ctx.kind == "message" and not (
            isinstance(ctx.text, str) and ctx.text.strip().startswith("/")
        ):
            active = [
                handler
                for handler in handlers
                if handler.drafts is not None and ctx.actor in handler.drafts
            ]
            handlers = active + [
                handler for handler in handlers if handler not in active
            ]
        for handler in handlers:
            if getattr(handler, method)(ctx):
                self._keep_single_draft(handler, ctx.actor)
                return
        if ctx.kind == "callback" and ctx.private:
            ctx.client.answer(
                ctx.callback_id,
                text="Эта кнопка устарела. Открой нужный раздел из меню.",
                attachments=inline_keyboard([]),
            )

    def navigate(self, ctx):
        if not ctx.private or not ctx.actor:
            return False
        active = [
            (handler, handler.drafts[ctx.actor])
            for handler in self.handlers
            if handler.drafts is not None and ctx.actor in handler.drafts
        ]
        callback = ctx.kind == "callback"
        navigation = callback and ctx.payload.startswith("nav:")
        switching_form = callback and ctx.payload.startswith(
            (
                "hw:new:",
                "hw:edit:",
                "hw:role:",
                "community:event_new",
                "community:event_edit:",
                "community:material_new:",
                "community:material_edit:",
                "community:report:",
            )
        )
        home = navigation or (switching_form and bool(active))
        start = (
            ctx.kind == "message"
            and isinstance(ctx.text, str)
            and ctx.text.strip() == "/start"
        )
        if not (home or start) or (start and not home and not active):
            return False

        def reply(text, attachments):
            if ctx.kind == "callback":
                ctx.client.answer(ctx.callback_id, text=text, attachments=attachments)
            else:
                ctx.client.send_message(
                    {"user_id": ctx.actor}, text, attachments=attachments
                )

        if active:
            handler, draft = active[0]
            if ctx.payload != "nav:discard:" + draft.token:
                prefix = (
                    "hw:"
                    if type(handler).__name__ == "HomeworkHandler"
                    else "community:"
                )
                reply(
                    "Выйти из формы? Несохранённые данные будут удалены. Ничего не опубликуется.",
                    inline_keyboard(
                        [
                            [callback_button("Остаться в форме", prefix + "resume")],
                            [
                                callback_button(
                                    "Выйти без сохранения", "nav:discard:" + draft.token
                                )
                            ],
                        ]
                    ),
                )
                return True
            for handler, _ in active:
                handler.drafts.pop(ctx.actor, None)
        try:
            group = ctx.users.registered_group(ctx.actor)
            if group:
                text = (
                    ctx.deps.today.home(ctx.actor, group)
                    if ctx.deps.today
                    else f"🏰 Подземелье знаний\nТвоя группа: {group}\n\nКуда отправимся?"
                )
                reply(text, HOME)
            else:
                ctx.users.start(ctx.actor)
                reply(WELCOME, institution_keyboard(ctx.users))
        except psycopg.Error:
            reply("База временно недоступна. Попробуй ещё раз.", inline_keyboard([]))
        return True

    def _keep_single_draft(self, owner, actor):
        """У человека может быть открыт только один черновик: новый диалог закрывает чужой."""
        if owner.drafts is None or actor not in owner.drafts:
            return
        for other in self.handlers:
            if other is not owner and other.drafts is not None:
                other.drafts.pop(actor, None)
