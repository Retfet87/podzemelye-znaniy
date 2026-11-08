"""Диалог «Домашние задания»: списки, создание и правка, роли администраторов, уведомления."""

from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from uuid import uuid4

import psycopg

from app.handlers.base import Handler
from app.handlers.common import MAX_ID, DraftStore, number
from app.handlers.menus import HOME, homework_menu_keyboard
from app.max_api.keyboards import vertical_keyboard
from app.repositories.homework import (
    BODY_LIMIT,
    CATEGORIES,
    FILE_COUNT,
    PAGE_SIZE,
    HomeworkStore,
)
from app.timeutil import format_deadline, parse_deadline

CANCEL = ("Отменить", "cancel")

HW_TRIGGERS = {
    "домашка": "open",
    "дз": "open",
    "домашнее задание": "open",
    "домашние задания": "open",
    "задания": "open",
    "задание": "open",
    "уведомления": "prefs",
}


@dataclass
class Draft:
    group: int | None
    shared: bool
    step: str = "subject"
    task: int | None = None
    revision: int | None = None
    subject: str = ""
    body: str = ""
    deadline: datetime | None = None
    files: list | None = field(default_factory=list)
    retained_names: tuple = ()
    subjects: tuple = ()
    token: str = field(default_factory=lambda: uuid4().hex)
    touched: float = field(default_factory=monotonic)
    date_cursor: str = ""
    date_value: str = ""

    def move(self, step):
        self.step, self.token, self.touched = step, uuid4().hex, monotonic()

    def button(self, label, action):
        return label, f"d:{self.token}:{action}"


class HomeworkHandler(Handler):
    def __init__(self):
        self.drafts = DraftStore("Слишком много открытых черновиков. Попробуй позже.")

    def on_message(self, ctx):
        if not ctx.private or not ctx.actor:
            return False
        return self.handle(ctx.client, ctx.db, ctx.actor, ctx.admin, body=ctx.body)

    def on_callback(self, ctx):
        if not ctx.payload.startswith("hw:"):
            return False
        if not ctx.private:
            ctx.client.answer(
                ctx.callback_id, notification="Открой задания в личном диалоге с ботом."
            )
            return True
        self.handle(
            ctx.client,
            ctx.db,
            ctx.actor,
            ctx.admin,
            payload=ctx.payload[3:],
            callback_id=ctx.callback_id,
        )
        return True

    def handle(
        self,
        client,
        database,
        user,
        admin,
        *,
        body=None,
        payload=None,
        callback_id=None,
    ):
        self.drafts.prune()
        text = (body.get("text") or "") if body else ""
        text = text.strip() if isinstance(text, str) else ""
        lowered = " ".join(text.lower().split())
        command, _, argument = text.partition(" ")
        draft = self.drafts.get(user)
        commands = {"/homework", "/hw_cancel", "/hw_grant", "/hw_revoke"}
        if payload is None and lowered in HW_TRIGGERS and draft is None:
            payload = HW_TRIGGERS[lowered]
        elif (
            payload is None
            and command not in commands
            and (draft is None or command.startswith("/"))
        ):
            return False
        db = HomeworkStore(database, user, admin)

        def reply(text, buttons=(), *, custom_attachments=None):
            attachments = (
                custom_attachments
                if custom_attachments is not None
                else vertical_keyboard(buttons, prefix="hw:")
            )
            if callback_id is None:
                client.send_message({"user_id": user}, text, attachments=attachments)
            else:
                client.answer(callback_id, text=text, attachments=attachments)

        try:
            if payload == "back":
                reply("Главное меню", custom_attachments=HOME)
            elif command == "/hw_cancel" or payload == "cancel":
                self.drafts.pop(user, None)
                reply(
                    "Действие отменено.",
                    custom_attachments=homework_menu_keyboard(has_draft=False),
                )
            elif command in ("/hw_grant", "/hw_revoke"):
                if user != admin:
                    raise ValueError("Команда доступна только супер-администратору.")
                target = number(argument.strip())
                draft = Draft(
                    None, True, step="role_confirm", task=target, body=command[4:]
                )
                self.drafts.put(user, draft)
                self.prompt(reply, draft)
            elif command == "/homework" or payload == "open":
                reply(
                    "Домашние задания",
                    custom_attachments=homework_menu_keyboard(has_draft=bool(draft)),
                )
            elif payload == "resume" and draft:
                if draft.step == "role":
                    self.role_prompt(reply, draft)
                else:
                    self.prompt(reply, draft)
            elif payload in ("prefs", "prefs:home") or (
                payload and payload.startswith("pref:")
            ):
                from_home = payload.endswith(":home")
                clean_payload = payload.removesuffix(":home")
                if clean_payload != "prefs":
                    _, category, flag = clean_payload.split(":")
                    if category not in CATEGORIES or flag not in ("0", "1"):
                        raise ValueError("Кнопка недействительна.")
                    db.set_preference(category, flag == "1")
                prefs = db.preferences()
                reply(
                    "Уведомления о заданиях",
                    [
                        (
                            f"{label} · {'вкл.' if prefs.get(key, True) else 'выкл.'}",
                            f"pref:{key}:{int(not prefs.get(key, True))}"
                            + (":home" if from_home else ""),
                        )
                        for key, label in CATEGORIES.items()
                    ]
                    + ([] if from_home else [("Назад", "open")]),
                )
            elif payload and not payload.startswith("d:"):
                self.navigate(client, db, user, payload, reply)
            elif draft:
                self.advance(client, db, user, draft, text, body, payload, reply)
            else:
                reply("Черновик закрыт. Открой /homework.")
        except ValueError as error:
            reply(
                str(error),
                [("Продолжить черновик", "resume"), CANCEL]
                if user in self.drafts
                else [("Домашние задания", "open")],
            )
        except psycopg.Error:
            reply("База временно недоступна. Попробуй ещё раз.")
        return True

    def navigate(self, client, db, user, payload, reply):
        parts = payload.split(":")
        action = parts[0]
        if action == "role" and len(parts) == 3:
            if db.context["actor"] != db.context["administrator"] or parts[1] not in (
                "grant",
                "revoke",
            ):
                raise ValueError("Команда недоступна.")
            draft = Draft(number(parts[2]), True, step="role", body=parts[1])
            self.drafts.put(user, draft)
            self.role_prompt(reply, draft)
            return
        if (
            action in ("groups", "list", "new")
            and len(parts) in (3, 4)
            and parts[1] in ("g", "p")
        ):
            return self._group_action(db, user, action, parts, reply)
        if action in ("view", "edit", "delete", "file") and len(parts) == (
            4 if action == "file" else 2
        ):
            return self._task_action(client, db, user, action, parts, reply)
        raise ValueError("Кнопка недействительна. Открой /homework.")

    def list_tasks(self, db, reply, shared, group, before):
        group_id, name, manage = group
        scope = "g" if shared else "p"
        rows = db.page(group_id, shared, before)
        buttons = [
            (f"#{task} · {subject}", f"view:{task}")
            for task, subject, _ in rows[:PAGE_SIZE]
        ]
        if len(rows) > PAGE_SIZE:
            buttons.append(
                (
                    "Следующие задания",
                    f"list:{scope}:{group_id}:{rows[PAGE_SIZE - 1][0]}",
                )
            )
        if before != MAX_ID:
            buttons.append(("К началу списка", f"list:{scope}:{group_id}:{MAX_ID}"))
        if manage or (not shared and group_id):
            buttons.append(("Создать задание", f"new:{scope}:{group_id}"))
        if db.context["actor"] == db.context["administrator"] and shared:
            buttons.extend(
                [
                    ("Назначить админа группы", f"role:grant:{group_id}"),
                    ("Снять админа группы", f"role:revoke:{group_id}"),
                ]
            )
        title = f"Общие задания · {name}" if shared else "Личные задания"
        reply(
            title
            if rows
            else f"{title}\nЗаданий пока нет. Личное задание можно добавить из меню заданий.",
            buttons
            + [
                ("Назад", f"groups:{scope}:0")
                if shared and len(db.groups(True)) > 1
                else ("Назад", "open")
            ],
        )

    def start(self, db, user, draft, reply):
        draft.subjects = tuple(
            subject for (subject,) in db.subjects(draft.group, draft.shared)
        )
        if not draft.subjects and not draft.subject:
            raise ValueError("Создание недоступно. Проверь права и расписание группы.")
        self.drafts.put(user, draft)
        self.prompt(reply, draft)

    @staticmethod
    def details(subject, body, deadline, files, title):
        return (
            f"{title}\n{subject[:400]}\nСрок: {format_deadline(deadline)}\n\n{body}\n\nФайлы: "
            + (", ".join(files) if files else "без файлов")
        )

    def prompt(self, reply, draft, offset=0):
        button = draft.button
        if draft.step == "subject":
            buttons = [
                (s, f"d:{draft.token}:subject:{i}")
                for i, s in enumerate(
                    draft.subjects[offset : offset + PAGE_SIZE], offset
                )
            ]
            if offset:
                buttons.append(
                    button("Предыдущие предметы", f"page:{max(0, offset - PAGE_SIZE)}")
                )
            if offset + PAGE_SIZE < len(draft.subjects):
                buttons.append(button("Другие предметы", f"page:{offset + PAGE_SIZE}"))
            if draft.task:
                buttons.append(button("Оставить предмет", "keep"))
            text = "Выбери предмет из расписания группы."
        elif draft.step == "body":
            text, buttons = f"Отправь текст задания (до {BODY_LIMIT} символов).", []
            if draft.task:
                buttons.append(button("Оставить текст", "keep"))
        elif draft.step == "deadline":
            from app.services.date_picker import keyboard

            text, attachments = keyboard(draft, "hw:", optional=True)
            reply(text, custom_attachments=attachments)
            return
        elif draft.step == "files":
            text = "Отправь до трёх файлов PDF, PNG, JPG или TXT, до 5 МБ каждый."
            if draft.files:
                text += "\nДобавлены: " + ", ".join(name for name, _ in draft.files)
            if draft.files is None:
                buttons = [
                    button("Оставить файлы", "done"),
                    button("Без файлов", "skip"),
                ]
            else:
                buttons = [button("Готово" if draft.files else "Пропустить", "done")]
        elif draft.step == "delete":
            text, buttons = (
                f"Удалить задание «{draft.subject}»?",
                [button("Подтвердить удаление", "yes")],
            )
        elif draft.step == "role_confirm":
            label = "Назначить" if draft.body == "grant" else "Снять"
            text = f"{label} администратора группы? MAX ID {draft.task}."
            buttons = [button("Подтвердить", "yes")]
        else:
            files = (
                draft.retained_names
                if draft.files is None
                else [name for name, _ in draft.files]
            )
            title = (
                "Предпросмотр · общее задание"
                if draft.shared
                else "Предпросмотр · личное задание"
            )
            text = self.details(draft.subject, draft.body, draft.deadline, files, title)
            buttons = [button("Сохранить", "yes")]
        reply(
            text,
            buttons
            + (
                [button("⬅️ Назад", "prev")]
                if draft.step in ("body", "deadline", "files", "preview")
                else []
            )
            + [button("Отменить", "cancel")],
        )

    def role_prompt(self, reply, draft):
        reply(
            "Отправь MAX ID зарегистрированного пользователя из выбранной группы.",
            [draft.button("Отменить", "cancel")],
        )

    def advance(self, client, db, user, draft, text, body, payload, reply):
        action = None
        argument = ""
        if payload:
            _, _, remainder = payload.partition(f"d:{draft.token}:")
            if not remainder or not payload.startswith(f"d:{draft.token}:"):
                raise ValueError("Кнопка устарела. Продолжи текущий черновик.")
            action, _, argument = remainder.partition(":")
        if action == "prev":
            steps = ["subject", "body", "deadline", "files", "preview"]
            if draft.step not in steps or steps.index(draft.step) == 0:
                raise ValueError("Вернуться назад нельзя.")
            draft.move(steps[steps.index(draft.step) - 1])
            self.prompt(reply, draft)
            return
        if action == "cancel":
            del self.drafts[user]
            reply(
                "Действие отменено.",
                custom_attachments=homework_menu_keyboard(has_draft=False),
            )
            return

        step = draft.step
        if step == "subject":
            if action == "page":
                self.prompt(reply, draft, number(argument, zero=True))
                return
            if action == "subject":
                index = number(argument, zero=True)
                if index >= len(draft.subjects):
                    raise ValueError("Предмет недоступен.")
                draft.subject = draft.subjects[index]
            elif action != "keep" or not draft.task:
                raise ValueError("Выбери предмет кнопкой.")
            draft.move("body")
        elif step == "body":
            if action is None and 1 <= len(text) <= BODY_LIMIT and "\x00" not in text:
                draft.body = text
            elif action != "keep" or not draft.task:
                raise ValueError(
                    f"Отправь текст задания от 1 до {BODY_LIMIT} символов."
                )
            draft.move("deadline")
        elif step == "deadline":
            from app.services.date_picker import choose

            selected = choose(
                draft, ("dt:" + argument if action == "dt" else action), text=text
            )
            if selected is not None:
                done, value = selected
                if not done:
                    draft.move("deadline")
                    self.prompt(reply, draft)
                    return
                draft.deadline = value
            elif action is None:
                draft.deadline = parse_deadline(text)
            elif action == "skip":
                draft.deadline = None
            elif action != "keep" or not draft.task:
                raise ValueError("Введи срок или выбери «Без срока».")
            draft.move("files")
        elif step == "files":
            if action in ("done", "skip"):
                if action == "skip":
                    draft.files = []
                draft.move("preview")
            elif action is None:
                incoming = body.get("attachments")
                files = draft.files or []
                if not isinstance(incoming, list) or not 1 <= len(
                    incoming
                ) <= FILE_COUNT - len(files):
                    raise ValueError(
                        "Прикрепи до трёх файлов или нажми кнопку под сообщением."
                    )
                draft.files = files + [client.receive_file(file) for file in incoming]
                draft.move("files")
            else:
                raise ValueError("Используй кнопки текущего черновика.")
        elif step == "role" and action is None:
            draft.task = number(text)
            draft.move("role_confirm")
        elif action == "yes" and step in ("delete", "preview", "role_confirm"):
            if step == "role_confirm":
                changed = db.set_admin(
                    draft.task, draft.body == "grant", group=draft.group
                )
                result = (
                    "Роль обновлена."
                    if changed
                    else "Пользователь или роль в этой группе не найдены."
                )
            elif step == "delete":
                result = (
                    "Задание удалено."
                    if db.delete(draft.task, draft.revision)
                    else "Задание уже изменилось или недоступно."
                )
            else:
                saved = db.save(draft)
                result = (
                    "Задание сохранено."
                    if saved
                    else "Задание изменилось, срок истёк или доступ закрыт."
                )
            del self.drafts[user]
            reply(result, custom_attachments=homework_menu_keyboard(has_draft=False))
            return
        else:
            raise ValueError("Используй кнопки текущего черновика.")
        self.prompt(reply, draft)

    def _group_action(self, db, user, action, parts, reply):
        shared = parts[1] == "g"
        groups = db.groups(shared)
        if action == "groups" and len(parts) == 3:
            if not shared and not groups:
                return self.list_tasks(db, reply, False, (0, "", False), MAX_ID)
            if not groups:
                raise ValueError("Сначала зарегистрируйся через /start.")
            if len(groups) == 1:
                return self.list_tasks(db, reply, shared, groups[0], MAX_ID)
            offset = number(parts[2], zero=True)
            buttons = [
                (name, f"list:g:{group}:{MAX_ID}")
                for group, name, _ in groups[offset : offset + PAGE_SIZE]
            ]
            if offset:
                buttons.append(
                    ("Предыдущие группы", f"groups:g:{max(0, offset - PAGE_SIZE)}")
                )
            if offset + PAGE_SIZE < len(groups):
                buttons.append(("Другие группы", f"groups:g:{offset + PAGE_SIZE}"))
            reply("Выбери группу.", buttons + [("Назад", "open")])
            return
        group_id = number(parts[2], zero=not shared)
        if action == "list" and not shared and len(parts) == 4:
            group = groups[0] if groups else (0, "", False)
            return self.list_tasks(db, reply, False, group, number(parts[3]))
        group = next((row for row in groups if row[0] == group_id), None)
        if group is None:
            raise ValueError("Группа недоступна.")
        if action == "list" and len(parts) == 4:
            return self.list_tasks(db, reply, shared, group, number(parts[3]))
        if action == "new" and len(parts) == 3:
            self.start(db, user, Draft(group_id, shared), reply)
            return
        raise ValueError("Кнопка недействительна. Открой /homework.")

    def _task_action(self, client, db, user, action, parts, reply):
        task = number(parts[1])
        row = db.get(task)
        if row is None:
            raise ValueError("Задание недоступно.")
        _, group, shared, subject, body, deadline, revision, editable, name, files = row
        if action == "view":
            buttons = [
                (f"Файл · {file}", f"file:{task}:{revision}:{slot}")
                for slot, file in enumerate(files, 1)
            ]
            if editable:
                buttons.extend(
                    [("Редактировать", f"edit:{task}"), ("Удалить", f"delete:{task}")]
                )
            scope = "g" if shared else "p"
            reply(
                self.details(
                    subject, body, deadline, files, name if shared else "Личное задание"
                ),
                buttons + [("К списку", f"list:{scope}:{group}:{MAX_ID}")],
            )
            return
        if action == "file":
            file = db.file(task, number(parts[2]), number(parts[3]))
            if file is None:
                raise ValueError("Файл недоступен. Открой задание снова.")
            # После загрузки повторно проверяем ревизию и доступ: запись
            # могла измениться, пока MAX принимал файл.
            attachment = client.upload_file(*file)
            if not db.file(task, number(parts[2]), number(parts[3]), check_only=True):
                raise ValueError("Файл недоступен. Открой задание снова.")
            client.send_message({"user_id": user}, file[0], attachments=[attachment])
            reply("Файл отправлен в личный диалог.", [("К заданию", f"view:{task}")])
            return
        if not editable:
            raise ValueError("Изменять это задание нельзя.")
        draft = Draft(
            group,
            shared,
            task=task,
            revision=revision,
            subject=subject,
            body=body,
            deadline=deadline,
            files=None,
            retained_names=tuple(files),
        )
        if action == "delete":
            draft.move("delete")
            self.drafts.put(user, draft)
            self.prompt(reply, draft)
        else:
            self.start(db, user, draft, reply)
        return
