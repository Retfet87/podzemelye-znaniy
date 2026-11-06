"""MAX dialogue for stages of an event or material publication."""
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from uuid import uuid4

import psycopg

from community import CATEGORIES, PAGE_SIZE, CommunityStore, event_text, validate_link
from homework import SAMARA, parse_deadline
from homework_dialog import DRAFT_TTL, MAX_DRAFTS, MAX_ID, keyboard, number

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


class CommunityDialog:
    def __init__(self):
        self.drafts = {}

    def start(self, user, draft):
        if user not in self.drafts and len(self.drafts) >= MAX_DRAFTS:
            raise ValueError("Слишком много черновиков. Попробуй позже.")
        self.drafts[user] = draft

    def handle(self, client, store, user, admin, *, body=None, payload=None, callback_id=None):
        for owner, draft in list(self.drafts.items()):
            if monotonic() - draft.touched >= DRAFT_TTL:
                del self.drafts[owner]
        text = (body or {}).get("text")
        text = text.strip() if isinstance(text, str) else ""
        command = text.split(maxsplit=1)[0] if text else ""
        if payload is None and command in ("/events", "/useful"):
            payload = "events:0" if command == "/events" else "useful"
        elif payload is None and (user not in self.drafts or command.startswith("/")):
            return False

        db = CommunityStore(store, user, admin)

        def reply(message, buttons=()):
            attachments = keyboard(buttons, prefix="community:")
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
            reply(title if rows else title + "\nСписок пуст.", buttons + resume)
        elif action == "event_new" and not parts and db.scopes():
            draft = Draft("event")
            self.start(user, draft)
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
                self.start(user, draft)
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
            reply("Полезное", [(label, f"materials:{key}:{MAX_ID}") for key, label in CATEGORIES.items()] + resume)
        elif action == "materials" and len(parts) == 2 and parts[0] in CATEGORIES:
            category, raw = parts
            before = number(raw)
            rows = db.materials(category, before)
            buttons = [(title, f"material:{item}") for item, title in rows[:PAGE_SIZE]]
            if len(rows) > PAGE_SIZE:
                buttons.append(("Следующая страница", f"materials:{category}:{rows[PAGE_SIZE-1][0]}"))
            if before != MAX_ID:
                buttons.append(("К началу списка", f"materials:{category}:{MAX_ID}"))
            buttons += [(f"Добавить · {name}", f"material_new:{category}:{group or 0}")
                        for group, name in db.scopes()]
            reply(CATEGORIES[category] + ("" if rows else "\nСписок пуст."), buttons + [("К категориям", "useful")])
        elif action == "material_new" and len(parts) == 2 and parts[0] in CATEGORIES:
            group = number(parts[1], zero=True) or None
            if group not in dict(db.scopes()):
                raise ValueError("Добавление недоступно.")
            draft = Draft("material", group=group, category=parts[0])
            self.start(user, draft)
            self.prompt(reply, draft)
        elif action in ("material", "material_edit", "file") and len(parts) == (3 if action == "file" else 1):
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
                attachment = client.upload_homework_file(*file)
                if not db.file(item, version, slot, check_only=True):
                    raise ValueError("Файл недоступен.")
                client.send_message({"user_id": user}, file[0], attachments=[attachment])
                reply("Файл отправлен в личный диалог.", [("К материалу", f"material:{item}")])
            elif action == "material_edit":
                if not editable:
                    raise ValueError("Редактирование недоступно.")
                draft = Draft("material", item, revision, group, category, title, body,
                              link=link, names=tuple(names), files=None)
                self.start(user, draft)
                self.prompt(reply, draft)
            else:
                buttons = [(f"Файл · {name}", f"file:{item}:{revision}:{slot}")
                           for slot, name in enumerate(names, 1)]
                if editable:
                    buttons.append(("Редактировать", f"material_edit:{item}"))
                reply(self.material_text(title, body, link, names, scope),
                      buttons + [("К списку", f"materials:{category}:{MAX_ID}")])
        else:
            raise ValueError("Действие недоступно.")

    @staticmethod
    def material_text(title, body, link, names, scope="Предпросмотр материала"):
        return "\n\n".join(filter(None, (scope, title, body, link, ", ".join(names))))

    def prompt(self, reply, draft):
        button = draft.button
        if draft.action:
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
            reply("Действие отменено.", [("Мероприятия", "events:0"), ("Полезное", "useful")])
            return
        if action == "save" and (draft.action or draft.step == len(draft.fields)):
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
                draft.files = existing + [client.receive_homework_file(file) for file in incoming]
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
