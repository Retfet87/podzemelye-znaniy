from datetime import datetime, timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

SAMARA = ZoneInfo("Europe/Samara")
# Расписание звонков АКВТ на 2026–2027 учебный год.
BELLS = (
    "08:30–10:05", "10:15–11:50", "12:20–13:55",
    "14:10–15:45", "15:55–17:30", "17:40–19:15",
)
DAYS = ("Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье")
PARITY = {"numerator": "числитель", "denominator": "знаменатель"}


def keyboard(*rows):
    return [{"type": "inline_keyboard", "payload": {"buttons": [
        [{"type": "callback", "text": text, "payload": f"schedule:{action}"}
         for text, action in row] for row in rows
    ]}}]


HOME = keyboard((("Расписание", "open"),))
HOME[0]["payload"]["buttons"].append([
    {"type": "callback", "text": "Домашние задания", "payload": "hw:open"},
])
MENU = keyboard((("Сегодня", "today"), ("Завтра", "tomorrow")),
                (("Неделя", "week"), ("Назад", "back")))
PERIODS = {"today", "tomorrow", "week"}


def requested_days(period, today=None):
    today = today or datetime.now(SAMARA).date()
    if period == "week":
        monday = today - timedelta(days=today.weekday())
        return [monday + timedelta(days=i) for i in range(7)]
    return [today + timedelta(days=period == "tomorrow")]


def format_schedule(rows):
    sections = []
    unknown_parity = False
    for day, entries in groupby(rows, key=lambda row: row[0]):
        entries = list(entries)
        _, _, version, anchor, *_ = entries[0]
        title = f"{DAYS[day.weekday()]}, {day:%d.%m.%Y}"
        if version is None:
            sections.append(f"{title}\nРасписание на эту дату не загружено.")
            continue
        current_parity = None
        if anchor is not None:
            weeks = (day - anchor).days // 7
            current_parity = "denominator" if weeks % 2 else "numerator"
        lines = []
        for _, _, _, _, pair, parity, subject, teacher, room, note in entries:
            if pair is None or (current_parity and parity not in ("all", current_parity)):
                continue
            label = ""
            if parity != "all" and current_parity is None:
                unknown_parity = True
                label = f" ({PARITY[parity]})"
            line = (f"{pair}. {BELLS[pair - 1]} — {subject}{label}\n"
                    f"{teacher or 'Преподаватель не указан'} · {room or 'Плавающий кабинет'}")
            if note:
                line += f" · {note}"
            lines.append(line)
        sections.append(title + "\n" + ("\n".join(lines) or "Занятий нет."))
    header = f"Расписание · {rows[0][1]}"
    if unknown_parity:
        header += "\nТип недели не подтверждён. Показаны варианты."
    return header + "\n\n" + "\n\n".join(sections)
