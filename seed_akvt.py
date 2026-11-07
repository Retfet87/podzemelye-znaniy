"""Скрипт заполнения БД базовыми материалами АКВТ (без кураторов) и демо-мероприятиями."""
from datetime import datetime, timedelta
from app.config import load_config
from app.db import Database, local_dsn
from app.timeutil import SAMARA

MATERIALS = [
    # Кабинеты и подразделения
    (
        "rooms",
        "Корпуса, кабинеты и ключевые подразделения АКВТ",
        "📍 Главный корпус: г. Астрахань, Смоляной переулок, д. 2\n"
        "📍 Второй корпус: г. Астрахань, ул. Космонавта Комарова, 55\n"
        "• Приёмная директора / Секретариат — 2 этаж\n"
        "• Учебная часть (справки об обучении) — пн–пт с 08:30 до 17:00\n"
        "• Библиотека и читальный зал — 1 этаж\n\n"
        "⚠️ Обратите внимание: занятия, выделенные в расписании пометкой «Другой корпус», "
        "проводятся во втором учебном корпусе колледжа.",
        "",
    ),
    # Учебные материалы
    (
        "study",
        "Методические указания по курсовому проектированию и ОПД",
        "Требования к оформлению отчётов по лабораторным работам, практикам и индивидуальным проектам (по ГОСТ 7.32):\n"
        "• Шрифт: Times New Roman, 14 пт, межстрочный интервал 1,5.\n"
        "• Поля: левое — 30 мм, правое — 15 мм, верхнее и нижнее — 20 мм.\n"
        "• Обязательно наличие титульного листа установленного образца АКВТ.",
        "https://www.akvt.ru/",
    ),
    # Полезные ссылки
    (
        "links",
        "Официальные ресурсы АКВТ для студентов",
        "• Официальный сайт колледжа и раздел расписания занятий.\n"
        "• Портал дистанционного обучения ГБПОУ АО «АКВТ».\n"
        "• Электронная библиотечная система колледжа.",
        "https://www.akvt.ru/students/raspisanie-zanyatij/",
    ),
]


def main():
    config = load_config()
    db = Database(local_dsn(config))
    db.check_schema()

    now = datetime.now(SAMARA)
    event_1_time = (now + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
    event_2_time = (now + timedelta(days=3)).replace(hour=15, minute=30, second=0, microsecond=0)

    with db.connect() as conn:
        for category, title, body, link in MATERIALS:
            exists = conn.execute(
                "SELECT id FROM useful_materials WHERE category = %s AND title = %s AND group_id IS NULL",
                (category, title),
            ).fetchone()
            if not exists:
                conn.execute(
                    """INSERT INTO useful_materials (group_id, category, title, body, link)
                       VALUES (NULL, %s, %s, %s, %s)""",
                    (category, title, body, link),
                )

        events = [
            (
                "Хакатон АКВТ «Подземелье знаний»",
                "Финальная защита студенческих IT-проектов и чат-ботов для мессенджера MAX. "
                "Приглашаются участники команд, студенты всех курсов и преподаватели!",
                event_1_time,
                "Актовый зал АКВТ / ауд. 312",
                "published",
            ),
            (
                "Мастер-класс по кибербезопасности и CTF",
                "Разбор практических кейсов по защите веб-приложений, анализу трафика и подготовке к соревнованиям.",
                event_2_time,
                "Лаборатория ИБ (ауд. 408)",
                "published",
            ),
        ]
        for title, desc, starts_at, place, state in events:
            exists = conn.execute("SELECT id FROM events WHERE title = %s", (title,)).fetchone()
            if not exists:
                conn.execute(
                    """INSERT INTO events (title, description, starts_at, place, state)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (title, desc, starts_at, place, state),
                )

    print("База наполнена материалами АКВТ (без кураторов) и мероприятиями!")


if __name__ == "__main__":
    main()