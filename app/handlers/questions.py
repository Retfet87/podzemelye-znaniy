"""Контекстные вопросы после команд, активных форм и основных разделов."""

from datetime import datetime, time, timedelta

from app.handlers.base import Handler
from app.handlers.common import MAX_ID
from app.handlers.menus import HOME, MENU
from app.repositories.community import CommunityStore
from app.repositories.homework import HomeworkStore
from app.services.schedule_formatter import format_schedule, requested_days
from app.services.today import lessons, normalise, start_end
from app.timeutil import SAMARA, format_deadline


class QuestionsHandler(Handler):
    def on_message(self, ctx):
        if not ctx.private or not ctx.actor or ctx.deps.today is None:
            return False
        if not isinstance(ctx.text, str) or ctx.text.strip().startswith("/"):
            return False
        group = ctx.users.registered_group(ctx.actor)
        if not group:
            return False

        reply, buttons = self._answer(
            ctx, normalise(ctx.text), group, datetime.now(SAMARA)
        )
        ctx.client.send_message(
            {"user_id": ctx.actor}, reply[:3900], attachments=buttons
        )
        return True

    def _answer(self, ctx, text, group, now):
        if text in ("что у меня сегодня", "что сегодня", "какие пары сегодня"):
            return ctx.deps.today.home(ctx.actor, group, now=now), HOME
        if text in (
            "какое расписание на следующую неделю",
            "какие пары на следующей неделе",
        ):
            rows = ctx.schedules.for_user(
                ctx.actor, requested_days("next_week", now.date())
            )
            return (
                format_schedule(rows) if rows else "Расписание пока не опубликовано."
            ), MENU
        if text in (
            "что завтра",
            "какие завтра пары",
            "какие пары завтра",
            "что у меня завтра",
        ):
            rows = ctx.schedules.for_user(
                ctx.actor, requested_days("tomorrow", now.date())
            )
            return (
                format_schedule(rows) if rows else "Расписание на завтра не загружено."
            ), MENU
        if text in ("во сколько первая пара", "когда первая пара"):
            return self._first_lesson(ctx, now), HOME
        if text in (
            "какие задания на завтра",
            "что задали на завтра",
            "задания на завтра",
        ):
            return self._homework_for_tomorrow(ctx, now), HOME
        if text in (
            "какие ближайшие мероприятия",
            "ближайшие мероприятия",
            "какие мероприятия",
        ):
            events = CommunityStore(ctx.db, ctx.actor, None).events(False, 0)
            lines = [
                f"{title} · {starts.astimezone(SAMARA):%d.%m %H:%M}"
                for _, title, starts, _ in events
            ]
            return "\n".join(lines) or "Мероприятий пока нет.", HOME
        if text in ("кто куратор", "кто мой куратор", "мой куратор"):
            return self._curators(ctx), HOME
        if text in ("что задали", "какие задания", "мои задания"):
            return self._homework(ctx), HOME

        reply = ctx.deps.today.lesson_question(ctx.actor, ctx.text)
        if reply is None:
            reply = (
                "Могу показать расписание и задания. Например: «когда физика», "
                "«где математика», «что завтра» или «что задали»."
            )
        return reply, HOME

    @staticmethod
    def _first_lesson(ctx, now):
        rows = ctx.schedules.for_user(ctx.actor, [now.date()])
        confirmed, uncertain = lessons(rows)
        if uncertain:
            return (
                "Тип недели не подтверждён. Открой расписание, чтобы сверить варианты."
            )
        if not confirmed:
            if rows and rows[0][2] is not None:
                return "Сегодня занятий нет."
            return "Расписание пока не опубликовано."

        first = min(confirmed, key=lambda row: row[4])
        subjects = sorted({row[6] for row in confirmed if row[4] == first[4]})
        return f"Первая пара сегодня в {start_end(first)[0]:%H:%M}: {' / '.join(subjects)}."

    @staticmethod
    def _homework_for_tomorrow(ctx, now):
        start = datetime.combine(now.date() + timedelta(days=1), time.min, SAMARA)
        tasks = HomeworkStore(ctx.db, ctx.actor, None).due_list(
            start, start + timedelta(days=1)
        )
        lines = [
            f"{subject}: {body[:350]} · {format_deadline(deadline)}"
            for _, subject, body, deadline in tasks
        ]
        return "\n".join(lines) or "Заданий со сроком на завтра пока нет."

    @staticmethod
    def _curators(ctx):
        store = CommunityStore(ctx.db, ctx.actor, None)
        cards = []
        for identifier, _ in store.materials("curators", MAX_ID)[:3]:
            record = store.material(identifier)
            if record:
                cards.append(f"{record[1]}\n{record[2]}")
        return (
            "\n\n".join(cards)
            or "Контактов куратора пока нет. Уточни их в учебном заведении."
        )

    @staticmethod
    def _homework(ctx):
        store = HomeworkStore(ctx.db, ctx.actor, None)
        cards = []
        for shared in (False, True):
            for group_id, _, _ in store.groups(shared):
                for task_id, _, _ in store.page(group_id, shared, MAX_ID)[:3]:
                    record = store.get(task_id)
                    if record:
                        cards.append(f"{record[3]}: {record[4]}")
                if not shared:
                    break
        return "\n\n".join(cards) or (
            "Доступных заданий пока нет. Добавить личное можно "
            "в разделе «Домашние задания»."
        )
