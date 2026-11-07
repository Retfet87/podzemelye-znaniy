"""Зависимости приложения и разобранное входящее событие MAX."""
from dataclasses import dataclass

from app.db.database import Database
from app.max_api.client import MaxClient
from app.max_api.updates import as_object, max_user_id
from app.repositories.schedule import ScheduleRepository
from app.repositories.users import UserRepository


@dataclass(frozen=True)
class Deps:
    """Всё, что нужно обработчикам; создаётся один раз при запуске."""

    client: MaxClient
    db: Database
    users: UserRepository
    schedules: ScheduleRepository
    admin: int | None = None

    @classmethod
    def create(cls, client, db, admin=None):
        return cls(client, db, UserRepository(db), ScheduleRepository(db), admin)


@dataclass(frozen=True)
class Context:
    """Одно входящее событие, разобранное один раз.

    kind: "message" | "callback" | "ignored".
    actor — MAX ID человека: автор сообщения или нажавший кнопку (None, если это бот
    или ID некорректен).
    """

    deps: Deps
    update: dict
    kind: str
    actor: int | None
    chat_type: str | None
    body: dict
    callback_id: str | None = None
    payload: str | None = None

    client = property(lambda self: self.deps.client)
    db = property(lambda self: self.deps.db)
    users = property(lambda self: self.deps.users)
    schedules = property(lambda self: self.deps.schedules)
    admin = property(lambda self: self.deps.admin)

    @property
    def private(self):
        return self.chat_type == "dialog"

    @property
    def text(self):
        return self.body.get("text")

    @classmethod
    def parse(cls, deps, update):
        update = as_object(update)
        message = as_object(update.get("message"))
        chat_type = as_object(message.get("recipient")).get("chat_type")
        body = as_object(message.get("body"))
        kind = update.get("update_type")
        if kind == "message_created":
            return cls(deps, update, "message", max_user_id(message.get("sender")), chat_type, body)
        if kind == "message_callback":
            callback = as_object(update.get("callback"))
            callback_id = callback.get("callback_id")
            payload = callback.get("payload")
            actor = max_user_id(callback.get("user"))
            if isinstance(callback_id, str) and callback_id.strip() and actor and isinstance(payload, str):
                return cls(deps, update, "callback", actor, chat_type, body, callback_id, payload)
        return cls(deps, update, "ignored", None, chat_type, body)
