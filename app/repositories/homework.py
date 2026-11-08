"""Домашние задания: хранение, права доступа и настройки уведомлений."""

from app.db.locks import HOMEWORK_LOCK
from app.services.file_validation import validate_data

PAGE_SIZE = 8
BODY_LIMIT = 3000
FILE_COUNT = 3
CATEGORIES = {
    "new": "Новые общие задания",
    "group_reminder": "Сроки общих заданий",
    "personal_reminder": "Сроки личных заданий",
}

VISIBLE = """(h.owner_id = %(actor)s OR
              (h.owner_id IS NULL AND (a.member OR a.can_manage)))"""
EDITABLE = """(h.owner_id = %(actor)s OR (h.owner_id IS NULL AND a.can_manage))"""
ACCESS = """homework_access(%(actor)s, %(administrator)s) a"""
SCOPED = f"""LEFT JOIN {ACCESS} ON a.group_id = h.group_id"""
MANAGES = f"""EXISTS (SELECT 1 FROM {ACCESS} WHERE a.group_id = h.group_id AND a.can_manage)"""
# Предметы берём из последней базы и ещё действующих замен на конкретные даты.
SUBJECTS = """
    SELECT DISTINCT l.subject
    FROM schedule_lessons l JOIN schedule_imports i ON i.id = l.import_id
    WHERE l.group_id = a.group_id AND btrim(l.subject) <> ''
      AND (i.id = (
          SELECT sg.import_id
          FROM schedule_groups sg JOIN schedule_imports base ON base.id = sg.import_id
          WHERE sg.group_id = a.group_id AND base.scope = 'base'
          ORDER BY (base.valid_from <= (now() AT TIME ZONE 'Europe/Samara')::date) DESC,
                   CASE WHEN base.valid_from <= (now() AT TIME ZONE 'Europe/Samara')::date
                        THEN base.valid_from END DESC,
                   base.valid_from ASC, base.id DESC
          LIMIT 1
      ) OR (i.scope = 'date' AND i.published_at IS NOT NULL
            AND i.valid_to >= (now() AT TIME ZONE 'Europe/Samara')::date
            AND i.id = (SELECT max(sg.import_id)
                        FROM schedule_groups sg JOIN schedule_imports d ON d.id = sg.import_id
                        WHERE sg.group_id = a.group_id AND d.scope = 'date'
                          AND d.published_at IS NOT NULL AND d.valid_from = i.valid_from)))
"""


class HomeworkStore:
    def __init__(self, store, actor, administrator):
        self.store = store
        self.context = {"actor": actor, "administrator": administrator}

    def due_count(self, now, until):
        """Сроки своей группы и личных заданий в полуинтервале; без чужих админских групп."""
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT count(*) FROM homework h {SCOPED}
                    WHERE {VISIBLE} AND (h.owner_id = %(actor)s OR a.member)
                      AND h.deadline >= %(now)s AND h.deadline < %(until)s""",
                self.context | {"now": now, "until": until},
            ).fetchone()[0]

    def groups(self, shared):
        with self.store.connect() as conn:
            rows = conn.execute(
                f"""SELECT group_id, group_name, can_manage, member FROM {ACCESS}
                    WHERE member OR (%(shared)s AND can_manage)
                    ORDER BY member DESC, group_name, group_id""",
                self.context | {"shared": shared},
            ).fetchall()
            # Если пользователь зарегистрирован в группе (member=True),
            # для просмотра заданий используем сразу его группу без лишнего выбора.
            member_rows = [
                (gid, name, can_manage)
                for gid, name, can_manage, is_member in rows
                if is_member
            ]
            if member_rows and not shared:
                return member_rows
            return [(gid, name, can_manage) for gid, name, can_manage, _ in rows]

    def subjects(self, group, shared):
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT subject FROM {ACCESS} CROSS JOIN LATERAL ({SUBJECTS}) s
                    WHERE group_id = %(group)s AND CASE WHEN %(shared)s
                    THEN can_manage ELSE member END ORDER BY subject""",
                self.context | {"group": group, "shared": shared},
            ).fetchall()

    def page(self, group, shared, before):
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT h.id, h.subject, h.deadline FROM homework h
                    {SCOPED}
                    WHERE {VISIBLE} AND (NOT %(shared)s OR h.group_id = %(group)s)
                      AND (h.owner_id IS NULL) = %(shared)s AND h.id < %(before)s
                    ORDER BY h.id DESC LIMIT {PAGE_SIZE + 1}""",
                self.context | {"group": group, "shared": shared, "before": before},
            ).fetchall()

    def due_list(self, start, end):
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT h.id,h.subject,h.body,h.deadline FROM homework h
                {SCOPED} WHERE {VISIBLE} AND h.deadline >= %(start)s AND h.deadline < %(end)s
                ORDER BY h.deadline,h.id LIMIT 10""",
                self.context | {"start": start, "end": end},
            ).fetchall()

    def get(self, task):
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT h.id, h.group_id, h.owner_id IS NULL, h.subject, h.body, h.deadline,
                           h.revision, {EDITABLE}, g.name,
                           ARRAY(SELECT name FROM homework_files f WHERE f.homework_id = h.id ORDER BY slot)
                    FROM homework h JOIN groups g ON g.id = h.group_id {SCOPED}
                    WHERE h.id = %(task)s AND {VISIBLE}""",
                self.context | {"task": task},
            ).fetchone()

    def file(self, task, revision, slot, *, check_only=False):
        fields = "1" if check_only else "f.name, f.data"
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT {fields} FROM homework_files f JOIN homework h ON h.id = f.homework_id
                    {SCOPED}
                    WHERE h.id = %(task)s AND h.revision = %(revision)s AND f.slot = %(slot)s AND {VISIBLE}""",
                self.context | {"task": task, "revision": revision, "slot": slot},
            ).fetchone()

    def set_admin(self, target, grant, group=None):
        args = self.context | {"target": target, "group": group}
        with self.store.connect() as conn:
            self.lock(conn)
            if grant:
                result = conn.execute(
                    """INSERT INTO homework_group_admins (user_id, group_id)
                       SELECT u.max_user_id, g.id FROM users u JOIN groups g ON g.id = u.group_id
                       WHERE u.max_user_id = %(target)s AND g.active AND g.id = coalesce(%(group)s, g.id)
                         AND %(actor)s = %(administrator)s
                       ON CONFLICT (user_id) DO UPDATE SET group_id = excluded.group_id""",
                    args,
                )
            else:
                result = conn.execute(
                    """DELETE FROM homework_group_admins
                       WHERE user_id = %(target)s AND group_id = coalesce(%(group)s, group_id)
                         AND %(actor)s = %(administrator)s""",
                    args,
                )
            return bool(result.rowcount)

    def preferences(self):
        with self.store.connect() as conn:
            return dict(
                conn.execute(
                    "SELECT category, enabled FROM homework_preferences WHERE user_id = %(actor)s",
                    self.context,
                ).fetchall()
            )

    def set_preference(self, category, enabled):
        with self.store.connect() as conn:
            self.lock(conn)
            conn.execute(
                """INSERT INTO homework_preferences (user_id, category, enabled)
                   SELECT max_user_id, %(category)s, %(enabled)s FROM users
                   WHERE max_user_id = %(actor)s AND group_id IS NOT NULL
                   ON CONFLICT (user_id, category) DO UPDATE SET enabled = excluded.enabled""",
                self.context | {"category": category, "enabled": enabled},
            )

    @staticmethod
    def lock(conn):
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (HOMEWORK_LOCK,))

    def save(self, draft):
        if draft.files is not None:
            if len(draft.files) > FILE_COUNT:
                raise ValueError("Можно прикрепить не более трёх файлов.")
            for name, data in draft.files:
                validate_data(name, data)
        args = self.context | {
            "group": draft.group,
            "shared": draft.shared,
            "task": draft.task,
            "revision": draft.revision,
            "subject": draft.subject,
            "body": draft.body,
            "deadline": draft.deadline,
        }
        with self.store.connect() as conn:
            self.lock(conn)
            if draft.task is None:
                row = conn.execute(
                    f"""INSERT INTO homework (group_id, owner_id, subject, body, deadline)
                        SELECT a.group_id, CASE WHEN %(shared)s THEN NULL ELSE %(actor)s::bigint END,
                               %(subject)s, %(body)s, %(deadline)s
                        FROM {ACCESS} WHERE a.group_id = %(group)s
                          AND CASE WHEN %(shared)s THEN a.can_manage ELSE a.member END
                          AND %(subject)s IN ({SUBJECTS})
                          AND coalesce(%(deadline)s::timestamptz > now(), true)
                        RETURNING id""",
                    args,
                ).fetchone()
            else:
                row = conn.execute(
                    f"""UPDATE homework h SET subject = %(subject)s, body = %(body)s,
                            deadline = %(deadline)s, revision = revision + 1,
                            reminder_version = reminder_version +
                                (h.deadline IS DISTINCT FROM %(deadline)s)::integer
                        WHERE h.id = %(task)s AND h.revision = %(revision)s
                          AND (h.owner_id = %(actor)s OR (h.owner_id IS NULL AND {MANAGES}))
                          AND (h.subject = %(subject)s OR EXISTS (
                              SELECT 1 FROM {ACCESS}
                              WHERE a.group_id = h.group_id AND %(subject)s IN ({SUBJECTS})
                          ))
                          AND (h.deadline IS NOT DISTINCT FROM %(deadline)s
                               OR coalesce(%(deadline)s::timestamptz > now(), true))
                        RETURNING h.id""",
                    args,
                ).fetchone()
            if not row:
                return None
            task = row[0]
            if draft.files is not None:
                conn.execute(
                    "DELETE FROM homework_files WHERE homework_id = %s", (task,)
                )
                with conn.cursor() as cursor:
                    cursor.executemany(
                        "INSERT INTO homework_files VALUES (%s, %s, %s, %s)",
                        [
                            (task, slot, name, data)
                            for slot, (name, data) in enumerate(draft.files, 1)
                        ],
                    )
            if draft.shared and draft.task is None:
                conn.execute(
                    """INSERT INTO homework_notifications
                       (homework_id, user_id, kind, version, next_attempt, valid_until)
                       SELECT h.id, u.max_user_id, 'new', 1, now(),
                              least(now() + interval '1 day', h.deadline)
                       FROM homework h JOIN users u ON u.group_id = h.group_id
                       WHERE h.id = %s ON CONFLICT DO NOTHING""",
                    (task,),
                )
            return task

    def delete(self, task, revision):
        with self.store.connect() as conn:
            self.lock(conn)
            return bool(
                conn.execute(
                    f"""DELETE FROM homework h WHERE h.id = %(task)s AND h.revision = %(revision)s
                      AND (h.owner_id = %(actor)s OR (h.owner_id IS NULL AND {MANAGES}))""",
                    self.context | {"task": task, "revision": revision},
                ).rowcount
            )
