"""Условные четыре недели только в заранее изолированной DEMO-базе."""

from datetime import datetime, timedelta
from hashlib import sha256

from app.demo import verify_demo
from app.services.schedule_parser import Lesson
from app.services.schedule_publisher import preview, decide
from app.timeutil import SAMARA


def seed_schedule(db):
    owner = 900000090
    digest = sha256(b"demo-four-weeks-v1").hexdigest()
    replacement = sha256(b"demo-one-date-v1").hexdigest()
    today = datetime.now(SAMARA).date()
    monday = today - timedelta(days=today.weekday())

    def lesson(day, pair, parity, subject, room="ДЕМО-аудитория"):
        return Lesson(
            "ДЕМО-11",
            day,
            pair,
            parity,
            "ДЕМО — " + subject,
            "ДЕМО — преподаватель",
            room,
            "Условный учебный пример, не официальное расписание",
            ("ДЕМО",),
        )

    rows = [lesson(d, 1, "all", "Программирование") for d in range(5)]
    rows += [
        lesson(d, 2, p, s)
        for d in range(5)
        for p, s in [("numerator", "Математика"), ("denominator", "Базы данных")]
    ]
    with db.unit_of_work() as conn:
        verify_demo(conn)
        conn.execute("SELECT pg_advisory_xact_lock(7210196)")
        if conn.execute(
            "SELECT 1 FROM schedule_imports WHERE source_sha256 NOT IN (%s,%s)",
            (digest, replacement),
        ).fetchone():
            raise ValueError(
                "Условное расписание требует пустую DEMO-базу либо свой предыдущий набор; чужие импорты не заменяются."
            )
        if conn.execute(
            "SELECT 1 FROM schedule_imports WHERE source_sha256=%s AND valid_from=%s AND valid_to=%s",
            (digest, monday, monday + timedelta(days=27)),
        ).fetchone():
            return
        token, _, _, _ = preview(
            db,
            owner,
            digest,
            {"ДЕМО-11"},
            rows,
            monday,
            monday + timedelta(days=27),
            monday,
            scope="base",
            dry_run=False,
        )
        if token:
            decide(db, owner, owner, token, confirm=True)
        day = monday + timedelta(days=7)
        changed = [
            lesson(0, 1, "all", "ЭВМ", ""),
            lesson(0, 2, "numerator", "Математика"),
            lesson(0, 2, "denominator", "Базы данных"),
        ]
        token, _, _, _ = preview(
            db,
            owner,
            replacement,
            {"ДЕМО-11"},
            changed,
            day,
            day,
            monday,
            scope="date",
            selected=("ДЕМО-11",),
            dry_run=False,
        )
        if token:
            decide(db, owner, owner, token, confirm=True)
