"""Диалоги «Мероприятия» и «Полезное»: списки, запись, предложение, публикация и удаление."""
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from uuid import uuid4

import psycopg

from app.handlers.base import Handler
from app.handlers.common import MAX_ID, DraftStore, number
from app.handlers.menus import HOME
from app.max_api.keyboards import vertical_keyboard
from app.repositories.community import CATEGORIES, PAGE_SIZE, CommunityStore
from app.services.events import event_text
from app.services.validation import validate_link
from app.timeutil import SAMARA, parse_deadline

STATES = {"draft": "Черновик", "proposed": "На рассмотрении",
          "published": "Опубликовано", "cancelled": "Отменено"}
EVENT_FIELDS = ("title", "body", "starts_at", "place")
MATERIAL_FIELDS = ("title", "body", "link", "files")
PROMPTS = {
    "title": "Отправь название (до 120 символов).",
    "body": "Отправь текст (до 1800 символов) или пропусти.",
    "starts_at": "Отправь дату и время в формате ДД.ММ.ГГГГ ЧЧ:ММ (Самара).",
    "place": "Отправь место (до 300 символов) или пропусти.",
    "link": "Отправь HTTPS-ссылку (до 700 символов) или пропусти.",
    "files": "Отправь до трёх PDF, PNG, JPG или TXT как файлы, до 5 МБ каждый.",
}
OPTIONAL = {"body", "place", "link", "files"}
LIMITS = {"title": 120, "body": 1800, "place": 300, "link": 700}

EVENT_TRIGGERS = {"/events", "мероприятия", "мероприятие", "события", "ивенты"}
USEFUL_TRIGGERS = {"/useful", "полезное", "полезные материалы", "материалы", "справка", "преподаватели", "кураторы"}


@dataclass
class Draft:
    kind: str
    id: int | None = None
    revision: int | None = None
    group: int | None = None
    category: str = ""
    title: str = ""
    body: str = ""
    starts_at: datetime | None = None
    place: str = ""
    link: str = ""
    names: tuple = ()
    files: list | None = field(default_factory=list)
    step: int = 0
    action: str = ""
    token: str = field(default_factory=lambda: uuid4().hex)
    touched: float = field(default_factory=monotonic)

    @property
    def fields(self):
        return EVENT_FIELDS if self.kind == "event" else MATERIAL_FIELDS

    def button(self, label, action):
        return label, f"d:{self.token}:{action}"

    def advance(self, *, stay=False):
        self.step += not stay
        self.token = uuid4().hex
        self.touched = monotonic()


class CommunityHandler(Handler):
    def __init__(self):
        self.drafts = DraftStore("Слишком много черновиков. Попробуй позже.")

    def on_message(self, ctx):
        if not ctx.private or not ctx.actor:
            return False
        return self.handle(ctx.client, ctx.db, ctx.actor, ctx.admin, body=ctx.body)

    def on_callback(self, ctx):
        if not ctx.payload.startswith("community:"):
            return False
        if not ctx.private:
            ctx.client.answer(ctx.callback_id, notification="Открой раздел в личном диалоге с ботом.")
            return True
        self.handle(ctx.client, ctx.db, ctx.actor, ctx.admin,
                    payload=ctx.payload.removeprefix("community:"), callback_id=ctx.callback_id)
        return True

    def handle(self, client, database, user, admin, *, body=None, payload=None, callback_id=None):
        self.drafts.prune()
        text = (body or {}).get("text")
        text = text.strip() if isinstance(text, str) else ""
        lowered = " ".join(text.lower().split())
        command = text.split(maxsplit=1)[0] if text else ""
        if payload is None and (command in ("/events", "/useful") or
                                user not in self.drafts and lowered in EVENT_TRIGGERS | USEFUL_TRIGGERS):
            payload = "events:0" if (command == "/events" or lowered in EVENT_TRIGGERS) else "useful"
        elif payload is None and (user not in self.drafts or command.startswith("/")):
            return False

        db = CommunityStore(database, user, admin)

        def reply(message, buttons=(), *, custom_attachments=None):
            attachments = custom_attachments if custom_attachments is not None else vertical_keyboard(buttons, prefix="community:")
            if callback_id is None:
                client.send_message({"user_id": user}, message, attachments=attachments)
            else:
                client.answer(callback_id, text=message, attachments=attachments)

        try:
            if payload is None or payload.startswith("d:"):
                self.advance(client, db, user, text, body or {}, payload, reply)
            elif payload == "resume" and user in self.drafts:
                self.prompt(reply, self.drafts[user])
            else:
                self.navigate(client, db, user, payload, reply)
        except ValueError as error:
            reply(str(error), [("Продолжить черновик", "resume")] if user in self.drafts else [])
        except psycopg.Error:
            reply("База временно недоступна. Попробуй ещё раз.")
        return True

    def navigate(self, client, db, user, payload, reply):
        action, *parts = payload.split(":")
        resume = [("Продолжить черновик", "resume")] if user in self.drafts else []
        if action == "back" and not parts:
            reply("Главное меню", custom_attachments=HOME)
            return
        if action in ("events", "review") and len(parts) == 1:
            offset = number(parts[0], zero=True)
            review = action == "review"
            rows = db.events(review, offset)
            buttons = [(f"{starts.astimezone(SAMARA):%d.%m} · {title}", f"event:{id_}")
                       for id_, title, starts, _ in rows[:PAGE_SIZE]]
            if offset:
                buttons.append(("К началу списка", f"{action}:0"))
            if len(rows) > PAGE_SIZE:
                buttons.append(("Следующая страница", f"{action}:{offset + PAGE_SIZE}"))
            if not review and db.scopes():
                buttons.append(("Создать мероприятие" if db.super else "Предложить мероприятие", "event_new"))
            if db.super and not review:
                buttons.append(("Черновики и предложения", "review:0"))
            if review:
                buttons.append(("Предстоящие", "events:0"))
            title = "Черновики и предложения" if review else "Предстоящие мероприятия"
            back_btn = [] if review else [("Назад", "back")]
            reply(title if rows else title + "\nСписок пуст.", buttons + resume + back_btn)
        elif action == "event_new" and not parts and db.scopes():
            draft = Draft("event")
            self.drafts.put(user, draft)
            self.prompt(reply, draft)
        elif action in ("event", "join", "leave", "event_edit", "publish", "event_cancel") and len(parts) == 1:
            event = number(parts[0])
            if action in ("join", "leave") and not db.attend(event, action == "join"):
                raise ValueError("Запись недоступна.")
            row = db.event(event)
            if row is None:
                raise ValueError("Мероприятие недоступно.")
            title, description, starts_at, place, state, revision, count, joined, future, proposer = row
            if action in ("event_edit", "publish", "event_cancel"):
                if not db.super or not future or state == "cancelled":
                    raise ValueError("Действие недоступно.")
                draft = Draft("event", event, revision, title=title, body=description,
                              starts_at=starts_at, place=place, action=action if action != "event_edit" else "")
                self.drafts.put(user, draft)
                self.prompt(reply, draft)
                return
            buttons = []
            if future and state == "published":
                buttons.append(("Не пойду" if joined else "Пойду", f"{'leave' if joined else 'join'}:{event}"))
            if db.super and future and state != "cancelled":
                buttons.append(("Редактировать", f"event_edit:{event}"))
                if state in ("draft", "proposed"):
                    buttons.append(("Опубликовать", f"publish:{event}"))
                buttons.append(("Отменить мероприятие", f"event_cancel:{event}"))
            reply(event_text(title, description, starts_at, place)
                  + f"\n\n{STATES[state]} · Участников: {count}"
                  + ("\nТы записан." if joined and state == "published" else "")
                  + (f"\nПредложение группы {proposer}" if db.super and state == "proposed" and proposer else ""),
                  buttons + [("К списку", "events:0")])
        elif action == "useful" and not parts:
            reply(
                "Полезное",
                [(label, f"materials:{key}:{MAX_ID}") for key, label in CATEGORIES.items()]
                + resume
                + [("Назад", "back")],
            )
        elif action == "teachers_auto" and len(parts) in (1, 2) and parts[0] in ("group", "all"):
            self.show_teachers(db, parts, reply)
        elif action == "materials" and len(parts) == 2 and parts[0] in CATEGORIES:
            category, raw = parts
            before = number(raw)
            rows = db.materials(category, before)
            buttons = []
            if category == "teachers" and before == MAX_ID:
                buttons.extend([
                    ("Преподаватели группы", "teachers_auto:group:0"),
                    ("Все преподаватели", "teachers_auto:all:0"),
                ])
            buttons.extend([(title, f"material:{item}") for item, title in rows[:PAGE_SIZE]])
            if len(rows) > PAGE_SIZE:
                buttons.append(("Следующая страница", f"materials:{category}:{rows[PAGE_SIZE-1][0]}"))
            if before != MAX_ID:
                buttons.append(("К началу списка", f"materials:{category}:{MAX_ID}"))
            buttons += [(f"Добавить · {name}", f"material_new:{category}:{group or 0}")
                        for group, name in db.scopes()]
            empty_note = ""
            if not rows and category != "teachers":
                empty_note = "\nСписок пуст."
            elif not rows and category == "teachers":
                empty_note = "\nВыбери список из расписания или добавь карточку вручную."
            reply(CATEGORIES[category] + empty_note, buttons + [("К категориям", "useful")])
        elif action == "material_new" and len(parts) == 2 and parts[0] in CATEGORIES:
            group = number(parts[1], zero=True) or None
            if group not in dict(db.scopes()):
                raise ValueError("Добавление недоступно.")
            draft = Draft("material", group=group, category=parts[0])
            self.drafts.put(user, draft)
            self.prompt(reply, draft)
        elif action in ("material", "material_edit", "material_delete", "file") and len(parts) == (3 if action == "file" else 1):
            item = number(parts[0])
            row = db.material(item)
            if row is None:
                raise ValueError("Материал недоступен.")
            category, title, body, link, group, revision, editable, names, scope = row
            if action == "file":
                version, slot = map(number, parts[1:])
                file = db.file(item, version, slot)
                if not file:
                    raise ValueError("Файл недоступен.")
                attachment = client.upload_file(*file)
                if not db.file(item, version, slot, check_only=True):
                    raise ValueError("Файл недоступен.")
                client.send_message({"user_id": user}, file[0], attachments=[attachment])
                reply("Файл отправлен в личный диалог.", [("К материалу", f"material:{item}")])
            elif action in ("material_edit", "material_delete"):
                if not editable:
                    raise ValueError("Изменение недоступно.")
                draft = Draft("material", item, revision, group, category, title, body,
                              link=link, names=tuple(names), files=None,
                              action="material_delete" if action == "material_delete" else "")
                self.drafts.put(user, draft)
                self.prompt(reply, draft)
            else:
                buttons = [(f"Файл · {name}", f"file:{item}:{revision}:{slot}")
                           for slot, name in enumerate(names, 1)]
                if editable:
                    buttons.append(("Редактировать", f"material_edit:{item}"))
                    buttons.append(("Удалить", f"material_delete:{item}"))
                reply(self.material_text(title, body, link, names, scope),
                      buttons + [("К списку", f"materials:{category}:{MAX_ID}")])
        else:
            raise ValueError("Действие недоступно.")

    @staticmethod
    def show_teachers(db, parts, reply):
        mode = parts[0]
        offset = number(parts[1], zero=True) if len(parts) == 2 else 0
        group_only = mode == "group"
        group_name, teachers = db.schedule_teachers(group_only)
        if group_only and group_name is None:
            raise ValueError("Сначала выбери группу через /start.")
        header = f"Преподаватели группы {group_name}" if group_only else "Все преподаватели (из расписания)"
        if not teachers:
            reply(f"{header}\nРасписание пока не загружено или преподаватели не указаны.",
                  [("Назад", f"materials:teachers:{MAX_ID}")])
            return
        page_size = 15
        lines = [header, ""]
        shown = 0
        used = len(header) + 2
        for t_name, subj_str, room_str in teachers[offset:offset + page_size]:
            details = " · ".join(filter(None, (subj_str, room_str)))
            line = f"• {t_name}" + (f" — {details}" if details else "")
            if len(line) > 3400:
                line = line[:3399] + "…"
            if shown and used + len(line) + 1 > 3600:
                break
            lines.append(line)
            used += len(line) + 1
            shown += 1
        nav_buttons = []
        if offset > 0:
            nav_buttons.append(("Предыдущие", f"teachers_auto:{mode}:{max(0, offset - page_size)}"))
        if offset + shown < len(teachers):
            nav_buttons.append(("Следующие", f"teachers_auto:{mode}:{offset + shown}"))
        other_mode = "all" if group_only else "group"
        other_label = "Все преподаватели" if group_only else "Преподаватели группы"
        nav_buttons.append((other_label, f"teachers_auto:{other_mode}:0"))
        nav_buttons.append(("Назад", f"materials:teachers:{MAX_ID}"))
        reply("\n".join(lines), nav_buttons)

    @staticmethod
    def material_text(title, body, link, names, scope="Предпросмотр материала"):
        return "\n\n".join(filter(None, (scope, title, body, link, ", ".join(names))))

    def prompt(self, reply, draft):
        button = draft.button
        if draft.action == "material_delete":
            text = f"Удалить запись «{draft.title}» из раздела «{CATEGORIES.get(draft.category, 'Полезное')}»?"
            buttons = [button("Подтвердить удаление", "save")]
        elif draft.action:
            label = "Опубликовать" if draft.action == "publish" else "Подтвердить отмену"
            text = label + "\n\n" + event_text(draft.title, draft.body, draft.starts_at, draft.place)
            buttons = [button(label, "save")]
        elif draft.step == len(draft.fields):
            if draft.kind == "event":
                text = "Предпросмотр мероприятия\n\n" + event_text(draft.title, draft.body, draft.starts_at, draft.place)
            else:
                names = draft.names if draft.files is None else [name for name, _ in draft.files]
                text = self.material_text(draft.title, draft.body, draft.link, names)
            buttons = [button("Сохранить" if draft.kind == "event" else "Опубликовать материал", "save"),
                       button("Исправить поля", "restart")]
        else:
            key = draft.fields[draft.step]
            text = PROMPTS[key]
            buttons = []
            if key == "files":
                if draft.files:
                    text += "\nДобавлены: " + ", ".join(name for name, _ in draft.files)
                buttons.append(button("Оставить файлы" if draft.files is None else "Готово", "keep"))
            elif draft.id is not None or getattr(draft, key):
                buttons.append(button("Оставить значение", "keep"))
            if key in OPTIONAL:
                buttons.append(button("Пропустить" if key != "files" else "Без файлов", "skip"))
            if draft.kind == "material" and draft.id is not None and draft.step == 0:
                buttons.append(button("Удалить запись", "ask_delete"))
        reply(text, buttons + [button("Отменить действие", "cancel")])

    def advance(self, client, db, user, text, body, payload, reply):
        draft = self.drafts.get(user)
        if draft is None:
            raise ValueError("Черновик закрыт.")
        action = None
        if payload is not None:
            prefix = f"d:{draft.token}:"
            if not payload.startswith(prefix):
                raise ValueError("Кнопка устарела. Продолжи текущий черновик.")
            action = payload.removeprefix(prefix)
        if action == "cancel":
            del self.drafts[user]
            reply("Действие отменено.", [("Мероприятия", "events:0"), ("Полезное", "useful"), ("Назад", "back")])
            return
        if action == "ask_delete" and draft.kind == "material" and draft.id is not None:
            draft.action = "material_delete"
            draft.advance(stay=True)
            self.prompt(reply, draft)
            return
        if action == "save" and (draft.action or draft.step == len(draft.fields)):
            if draft.action == "material_delete":
                deleted = db.delete_material(draft.id, draft.revision)
                category = draft.category
                del self.drafts[user]
                reply(
                    "Запись удалена." if deleted else "Запись уже удалена или недоступна.",
                    [("К списку", f"materials:{category}:{MAX_ID}"), ("Полезное", "useful")],
                )
                return
            if draft.action:
                saved = db.transition(draft.id, draft.revision, draft.action == "publish")
                item = draft.id
            else:
                item = db.save_event(draft) if draft.kind == "event" else db.save_material(draft)
                saved = item is not None
            del self.drafts[user]
            label = ("Мероприятие опубликовано." if draft.action == "publish" else
                     "Мероприятие отменено." if draft.action == "event_cancel" else
                     "Предложение отправлено супер-администратору." if draft.kind == "event" and not db.super
                     else "Сохранено.")
            buttons = [("Мероприятия", "events:0")] if draft.kind == "event" else [("Полезное", "useful")]
            if saved and (db.super or draft.kind == "material"):
                buttons.insert(0, ("Открыть", f"{draft.kind}:{item}"))
            reply(label if saved else "Нет изменений или действие уже недоступно.", buttons)
            return
        if action == "restart" and not draft.action and draft.step == len(draft.fields):
            draft.step = 0
            draft.advance(stay=True)
        elif draft.action or draft.step >= len(draft.fields):
            raise ValueError("Используй кнопки предпросмотра.")
        else:
            key = draft.fields[draft.step]
            if action == "skip" and key in OPTIONAL:
                setattr(draft, key, [] if key == "files" else "")
            elif action == "keep" and (key == "files" or getattr(draft, key) or key in OPTIONAL and draft.id):
                pass
            elif action is not None:
                raise ValueError("Используй кнопки текущего черновика.")
            elif key == "files":
                incoming = body.get("attachments")
                existing = draft.files or []
                if not isinstance(incoming, list) or not 1 <= len(incoming) <= 3 - len(existing):
                    raise ValueError("Прикрепи до трёх файлов.")
                draft.files = existing + [client.receive_file(file) for file in incoming]
                draft.advance(stay=True)
                self.prompt(reply, draft)
                return
            elif key == "starts_at":
                draft.starts_at = parse_deadline(text)
            else:
                if not 1 <= len(text) <= LIMITS[key] or "\x00" in text:
                    raise ValueError(PROMPTS[key])
                if key == "link":
                    validate_link(text)
                setattr(draft, key, text)
            draft.advance()
        self.prompt(reply, draft)