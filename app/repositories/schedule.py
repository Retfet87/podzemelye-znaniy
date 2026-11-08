"""Расписание: выборка занятий для пользователя и сохранение импортированных версий."""


class ScheduleRepository:
    def __init__(self, db):
        self.db = db

    def for_user(self, user_id, days):
        with self.db.connect() as conn:
            return conn.execute(
                """SELECT requested.day, g.name, version.id, version.numerator_monday,
                          lesson.pair, lesson.parity, lesson.subject, lesson.teacher,
                          lesson.room, lesson.note
                   FROM users u JOIN groups g ON g.id = u.group_id
                   CROSS JOIN unnest(%s::date[]) AS requested(day)
                   LEFT JOIN LATERAL (
                       SELECT i.id, i.numerator_monday
                       FROM schedule_groups sg JOIN schedule_imports i ON i.id = sg.import_id
                       WHERE sg.group_id = g.id AND requested.day BETWEEN i.valid_from AND i.valid_to
                       ORDER BY (i.scope = 'date') DESC, i.id DESC LIMIT 1
                   ) version ON true
                   LEFT JOIN schedule_lessons lesson ON lesson.import_id = version.id
                       AND lesson.group_id = g.id
                       AND lesson.weekday = extract(isodow FROM requested.day) - 1
                   WHERE u.max_user_id = %s
                   ORDER BY requested.day, lesson.pair, lesson.parity DESC""",
                (days, user_id),
            ).fetchall()

    @staticmethod
    def save_lessons(cur, version, groups, lessons):
        cur.executemany(
            "INSERT INTO groups (name) VALUES (%s) ON CONFLICT DO NOTHING",
            [(name,) for name in groups],
        )
        cur.execute("SELECT name, id FROM groups WHERE name = ANY(%s)", (list(groups),))
        ids = dict(cur.fetchall())
        cur.executemany(
            "INSERT INTO schedule_groups VALUES (%s, %s)",
            [(version, ids[name]) for name in groups],
        )
        cur.executemany(
            """INSERT INTO schedule_lessons
               (import_id, group_id, weekday, pair, parity, subject, teacher, room, note, raw_rows)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    version,
                    ids[x["group"]],
                    x["weekday"],
                    x["pair"],
                    x["parity"],
                    x["subject"],
                    x["teacher"],
                    x["room"],
                    x["note"],
                    list(x["raw"]),
                )
                for x in lessons
            ],
        )
        return ids
