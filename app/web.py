"""WSGI только для чтения; каждый API-запрос проверяет подпись MAX."""

from datetime import datetime
import json
import logging
from pathlib import Path

import psycopg

from app.repositories.users import UserRepository
from app.repositories.schedule import ScheduleRepository
from app.repositories.homework import HomeworkStore
from app.repositories.community import CATEGORIES, TAGS, CommunityStore
from app.services.schedule_formatter import format_schedule, requested_days
from app.services.today import TodayService, schedule_today
from app.timeutil import SAMARA, format_deadline
from app.web_auth import InvalidLaunch, verify_init_data

logger = logging.getLogger(__name__)
STATIC = Path(__file__).resolve().parents[1] / "web"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
ENDPOINTS = {"/api/schedule", "/api/homework", "/api/events", "/api/useful"}


class ReadViews:
    def __init__(self, db):
        self.db = db
        self.users = UserRepository(db)

    def read(self, actor, path):
        group = self.users.registered_group(actor)
        if not group:
            return {
                "group": None,
                "summary": "Сначала выбери учебное заведение и группу в личном чате с ботом.",
                "items": ["Сначала выбери группу в личном чате с ботом."],
                "cards": [],
            }

        schedules = ScheduleRepository(self.db)
        now = datetime.now(SAMARA)
        today_rows = schedules.for_user(actor, [now.date()])
        summary_text = TodayService(self.db, schedules).home(
            actor, group, now=now, rows=today_rows
        )
        today_status = schedule_today(today_rows, now)

        items = []
        cards = []
        extra = {}

        if path == "/api/schedule":
            rows = schedules.for_user(actor, requested_days("week"))
            if rows:
                formatted = format_schedule(rows)
                items = [formatted]
                sections = formatted.split("\n\n")
                header = sections[0] if sections else f"Расписание · {group}"
                for block in sections[1:]:
                    lines = block.splitlines()
                    day_title = lines[0] if lines else "День недели"
                    day_body = (
                        "\n".join(lines[1:]) if len(lines) > 1 else "Занятий нет."
                    )
                    cards.append(
                        {
                            "id": day_title,
                            "title": day_title,
                            "subtitle": group,
                            "badge": "Сегодня"
                            if f"{now:%d.%m.%Y}" in day_title
                            else "",
                            "preview": day_body,
                            "body": day_body,
                            "meta": header,
                        }
                    )
            else:
                items = ["Расписание пока не загружено."]

        elif path == "/api/homework":
            store = HomeworkStore(self.db, actor, None)
            for shared in (True, False):
                for gid, _, _ in store.groups(shared):
                    for task, _, _ in store.page(gid, shared, 2**63 - 1)[:8]:
                        record = store.get(task)
                        if record:
                            (
                                task_id,
                                _,
                                common,
                                subject,
                                body,
                                deadline,
                                _,
                                _,
                                group_name,
                                file_names,
                            ) = record
                            kind_label = "Общее" if common else "Личное"
                            deadline_str = (
                                format_deadline(deadline) if deadline else "не указан"
                            )
                            items.append(
                                f"{kind_label} · {subject}\n{body}\nСрок: {deadline_str}"
                            )
                            cards.append(
                                {
                                    "id": f"hw-{task_id}",
                                    "title": subject,
                                    "subtitle": f"Срок: {deadline_str}",
                                    "badge": kind_label,
                                    "preview": body[:140]
                                    + ("…" if len(body) > 140 else ""),
                                    "body": body,
                                    "meta": f"{kind_label} задание · {group_name}",
                                    "files": list(file_names or ()),
                                }
                            )
                    if not shared:
                        break  # Личный список уже охватывает все доступные пользователю группы.

        elif path == "/api/events":
            store = CommunityStore(self.db, actor, None)
            for identifier, _, _, _ in store.events(False, 0)[:8]:
                record = store.event(identifier)
                if record:
                    title, body, starts, place, state, _, count, joined, _, _ = record
                    details = store.details(identifier) or ([], "", "", False)
                    tags_list = [TAGS[t] for t in details[0] if t in TAGS]
                    when_str = format_deadline(starts)
                    place_str = place or "Место не указано"
                    items.append(f"{title}\n{when_str} · {place_str}\n{body}")
                    cards.append(
                        {
                            "id": f"event-{identifier}",
                            "title": title,
                            "subtitle": f"{when_str} · {place_str}",
                            "badge": "Ты записан" if joined else f"Участников: {count}",
                            "preview": (body[:140] + ("…" if len(body) > 140 else ""))
                            if body
                            else "Описание не указано.",
                            "body": body or "Описание не указано.",
                            "meta": f"Статус: Опубликовано · Участников: {count}"
                            + (" · Ты в списке участников" if joined else ""),
                            "tags": tags_list,
                        }
                    )

            history_cards = []
            for identifier, _, _, _ in store.events(False, 0, history=True)[:8]:
                record = store.event(identifier)
                if record:
                    title, body, starts, place, _, _, count, joined, _, _ = record
                    details = store.details(identifier) or ([], "", "", False)
                    tags_list = [TAGS[t] for t in details[0] if t in TAGS]
                    report_body = details[1] if details[3] else ""
                    report_link = details[2] if details[3] else ""
                    when_str = format_deadline(starts)
                    place_str = place or "Место не указано"
                    history_cards.append(
                        {
                            "id": f"history-{identifier}",
                            "title": title,
                            "subtitle": f"{when_str} · {place_str}",
                            "badge": "Итоги опубликованы"
                            if details[3]
                            else "Завершено",
                            "preview": (
                                report_body or body or "Мероприятие завершено."
                            )[:140],
                            "body": body or "Описание не указано.",
                            "meta": f"Прошедшее мероприятие · Участников: {count}",
                            "tags": tags_list,
                            "report_published": bool(details[3]),
                            "report_body": report_body,
                            "report_link": report_link,
                        }
                    )
            extra["history"] = history_cards

        elif path == "/api/useful":
            store = CommunityStore(self.db, actor, None)
            categories_meta = [{"key": k, "label": v} for k, v in CATEGORIES.items()]
            _, auto_teachers = store.schedule_teachers(True)
            if auto_teachers:
                teacher_lines = [
                    f"• {t_name}"
                    + (
                        f" — {' · '.join(filter(None, (subj, room)))}"
                        if (subj or room)
                        else ""
                    )
                    for t_name, subj, room in auto_teachers
                ]
                auto_body = "\n".join(teacher_lines)
                items.append(f"Преподаватели группы {group}\n{auto_body}")
                cards.append(
                    {
                        "id": "useful-auto-teachers",
                        "category": "teachers",
                        "category_label": CATEGORIES["teachers"],
                        "title": f"Преподаватели группы {group}",
                        "subtitle": "Автоматический список из расписания",
                        "badge": "Расписание",
                        "preview": "\n".join(teacher_lines[:3]),
                        "body": auto_body,
                        "meta": f"Категория: {CATEGORIES['teachers']} · {group}",
                        "link": "",
                        "files": [],
                    }
                )
            for cat_key, cat_label in CATEGORIES.items():
                for item_id, _ in store.materials(cat_key, 2**63 - 1)[:8]:
                    record = store.material(item_id)
                    if record:
                        _, title, body, link, _, _, _, file_names, scope = record
                        items.append(f"{cat_label} · {title}\n{body}")
                        cards.append(
                            {
                                "id": f"useful-{item_id}",
                                "category": cat_key,
                                "category_label": cat_label,
                                "title": title,
                                "subtitle": scope,
                                "badge": cat_label,
                                "preview": (
                                    body[:140] + ("…" if len(body) > 140 else "")
                                )
                                if body
                                else (link or "Вложение"),
                                "body": body or "Текстовое описание отсутствует.",
                                "meta": f"{cat_label} · {scope}",
                                "link": link or "",
                                "files": list(file_names or ()),
                            }
                        )
            extra["categories"] = categories_meta

        return {
            "group": group,
            "today_status": today_status,
            "summary": summary_text,
            "items": items
            or ["Пока ничего нет. Новые записи появятся здесь после публикации."],
            "cards": cards,
            **extra,
        }


class MiniApp:
    def __init__(self, views, bot_token, *, demo_policy=None):
        self.views, self.bot_token, self.demo_policy = views, bot_token, demo_policy

    def __call__(self, environ, start_response):
        headers = [
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            ("Referrer-Policy", "no-referrer"),
            (
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' https://st.max.ru; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'",
            ),
        ]
        status = "200 OK"
        kind = "application/json; charset=utf-8"
        try:
            path = environ.get("PATH_INFO", "/")
            if environ.get("REQUEST_METHOD") != "GET":
                status = "405 Method Not Allowed"
                result = {"error": "Доступно только чтение."}
            elif environ.get("QUERY_STRING"):
                status = "400 Bad Request"
                result = {"error": "Параметры запроса не поддерживаются."}
            elif path in ASSETS:
                filename, kind = ASSETS[path]
                result = (STATIC / filename).read_bytes()
            elif path not in ENDPOINTS:
                status = "404 Not Found"
                result = {"error": "Раздел не найден."}
            else:
                actor = verify_init_data(
                    environ.get("HTTP_X_MAX_INIT_DATA", ""), self.bot_token
                )
                if self.demo_policy is not None and not self.demo_policy.permits(actor):
                    status = "403 Forbidden"
                    result = {"error": "ДЕМО выключено или доступ запрещён."}
                else:
                    result = self.views.read(actor, path)
                    # Повторно проверяем выключатель перед выдачей уже прочитанных личных данных.
                    if self.demo_policy is not None:
                        if not self.demo_policy.permits(actor):
                            status = "403 Forbidden"
                            result = {"error": "ДЕМО выключено."}
                        else:
                            result = result | {"demo": True}
        except InvalidLaunch:
            status = "401 Unauthorized"
            result = {"error": "Открой приложение заново из MAX."}
        except psycopg.Error:
            logger.warning("Mini-app database unavailable")
            status = "503 Service Unavailable"
            result = {"error": "Данные временно недоступны. Попробуй позже."}
        except Exception:
            logger.exception("Mini-app request failed")
            status = "500 Internal Server Error"
            result = {"error": "Не удалось открыть данные. Попробуй позже."}
        body = (
            result
            if isinstance(result, bytes)
            else json.dumps(result, ensure_ascii=False).encode()
        )
        start_response(
            status,
            headers + [("Content-Type", kind), ("Content-Length", str(len(body)))],
        )
        return [body]
