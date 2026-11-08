"""Мероприятия и полезные материалы: хранение и права доступа."""

from collections import defaultdict

from app.db.locks import EVENT_LOCK
from app.services.events import event_text
from app.services.file_validation import validate_data
from app.services.validation import validate_link
from app.services.media import validate_media

TAGS = {
    "study": "🎓 Учёба",
    "contest": "🏆 Конкурсы",
    "hackathon": "💻 Хакатоны",
    "culture": "🎭 Культура",
    "sport": "⚽ Спорт",
    "important": "📢 Важное",
    "other": "🎉 Другое",
}

CATEGORIES = {
    "curators": "Кураторы",
    "teachers": "Преподаватели",
    "rooms": "Кабинеты",
    "study": "Учебные материалы",
    "links": "Полезные ссылки",
}
PAGE_SIZE = 8
MEMBER = """EXISTS (SELECT 1 FROM users u JOIN groups g ON g.id = u.group_id
                      WHERE u.max_user_id = %(actor)s AND g.active)"""
ADMIN = "coalesce(%(actor)s = %(admin)s, false)"
GROUP_ADMIN = """EXISTS (SELECT 1 FROM homework_access(%(actor)s, %(admin)s) a
                           WHERE a.group_id = {group} AND a.member AND a.can_manage)"""
PROPOSER = """EXISTS (SELECT 1 FROM homework_access(%(actor)s, %(admin)s) a
                        WHERE a.member AND a.can_manage)"""


class CommunityStore:
    def __init__(self, store, actor, administrator):
        self.store = store
        self.params = {"actor": actor, "admin": administrator}
        self.super = actor == administrator

    def scopes(self):
        with self.store.connect() as conn:
            groups = conn.execute(
                """SELECT group_id, group_name FROM homework_access(%(actor)s, %(admin)s)
                   WHERE member AND can_manage""",
                self.params,
            ).fetchall()
            return ([(None, "Для всех групп")] if self.super else []) + groups

    def events(self, review, offset, *, history=False, tag=None):
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT id, title, starts_at, state FROM events
                    WHERE CASE WHEN %(review)s THEN {ADMIN} AND state IN ('draft', 'proposed')
                    ELSE ({ADMIN} OR {MEMBER}) AND state = 'published' AND ((starts_at <= now()) = %(history)s) END
                    AND (%(tag)s::text IS NULL OR %(tag)s = ANY(tags))
                    ORDER BY starts_at, id LIMIT {PAGE_SIZE + 1} OFFSET %(offset)s""",
                self.params
                | {"review": review, "offset": offset, "history": history, "tag": tag},
            ).fetchall()

    def event(self, event):
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT title, description, starts_at, place, state, revision,
                    (SELECT count(*) FROM event_participants p WHERE p.event_id = e.id),
                    EXISTS (SELECT 1 FROM event_participants p WHERE p.event_id = e.id
                            AND p.user_id = %(actor)s), starts_at > now(),
                    (SELECT name FROM groups WHERE id = e.proposed_group_id)
                    FROM events e WHERE id = %(id)s AND ({ADMIN} OR
                    ({MEMBER} AND (state = 'published' OR (state = 'cancelled' AND EXISTS (
                        SELECT 1 FROM event_participants p WHERE p.event_id = e.id
                        AND p.user_id = %(actor)s)))))""",
                self.params | {"id": event},
            ).fetchone()

    @staticmethod
    def event_lock(conn):
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (EVENT_LOCK,))

    def attend(self, event, join):
        with self.store.connect() as conn:
            self.event_lock(conn)
            args = self.params | {"id": event}
            if not conn.execute(
                f"""SELECT 1 FROM events WHERE id = %(id)s AND state = 'published'
                    AND starts_at > now() AND {MEMBER}""",
                args,
            ).fetchone():
                return False
            if join:
                conn.execute(
                    """INSERT INTO event_participants VALUES (%(id)s, %(actor)s)
                       ON CONFLICT DO NOTHING""",
                    args,
                )
            else:
                conn.execute(
                    "DELETE FROM event_participants WHERE event_id = %(id)s AND user_id = %(actor)s",
                    args,
                )
            return True

    def save_event(self, draft):
        if not set(draft.tags) <= TAGS.keys():
            raise ValueError("Выбери теги из списка.")
        self.validate_media_list(draft.media)
        args = (
            self.params
            | vars(draft)
            | {
                "tags": sorted(set(draft.tags)),
                "media_changed": draft.media is not None,
            }
        )
        with self.store.connect() as conn:
            self.event_lock(conn)
            if draft.id is None:
                row = conn.execute(
                    f"""INSERT INTO events (title, description, starts_at, place, state, proposed_group_id, tags)
                        SELECT %(title)s, %(body)s, %(starts_at)s, %(place)s,
                        CASE WHEN {ADMIN} THEN 'draft' ELSE 'proposed' END,
                        CASE WHEN NOT {ADMIN} THEN (SELECT group_id FROM users WHERE max_user_id = %(actor)s) END, %(tags)s
                        WHERE ({ADMIN} OR {PROPOSER}) AND %(starts_at)s > now()
                        RETURNING id""",
                    args,
                ).fetchone()
            else:
                row = conn.execute(
                    f"""UPDATE events SET title = %(title)s, description = %(body)s,
                        place = %(place)s, starts_at = %(starts_at)s, tags = %(tags)s, revision = revision + 1,
                        reminder_version = reminder_version + (starts_at <> %(starts_at)s)::integer
                        WHERE id = %(id)s AND revision = %(revision)s AND {ADMIN}
                          AND state <> 'cancelled' AND starts_at > now() AND %(starts_at)s > now()
                          AND (%(media_changed)s OR (title, description, starts_at, place, tags) IS DISTINCT FROM
                              (%(title)s, %(body)s, %(starts_at)s, %(place)s, %(tags)s))
                        RETURNING id, state""",
                    args,
                ).fetchone()
                if row and row[1] == "published":
                    self.enqueue_event(conn, row[0], "changed")
            if row and draft.media is not None:
                self.replace_media(conn, row[0], "event", draft.media)
            return row[0] if row else None

    def transition(self, event, revision, publish):
        with self.store.connect() as conn:
            self.event_lock(conn)
            row = conn.execute(
                f"""UPDATE events SET state = %(target)s, revision = revision + 1
                    WHERE id = %(id)s AND revision = %(revision)s AND {ADMIN}
                      AND starts_at > now() AND state = ANY(%(source)s) RETURNING id""",
                self.params
                | {
                    "id": event,
                    "revision": revision,
                    "target": "published" if publish else "cancelled",
                    "source": ["draft", "proposed"]
                    if publish
                    else ["draft", "proposed", "published"],
                },
            ).fetchone()
            if row and not publish:
                self.enqueue_event(conn, event, "cancelled")
            return bool(row)

    @staticmethod
    def enqueue_event(conn, event, kind):
        title, description, starts_at, place = conn.execute(
            "SELECT title, description, starts_at, place FROM events WHERE id = %s",
            (event,),
        ).fetchone()
        label = (
            "Мероприятие отменено" if kind == "cancelled" else "Мероприятие изменилось"
        )
        body = label + "\n" + event_text(title, description, starts_at, place)
        conn.execute(
            """INSERT INTO event_notifications (event_id, user_id, kind, version, valid_until, body)
               SELECT e.id, p.user_id, %s, e.revision, now() + interval '1 day', %s
               FROM events e JOIN event_participants p ON p.event_id = e.id WHERE e.id = %s
               ON CONFLICT DO NOTHING""",
            (kind, body, event),
        )

    @staticmethod
    def visible_material():
        return f"""({ADMIN} OR EXISTS (
            SELECT 1 FROM homework_access(%(actor)s, %(admin)s) a
            WHERE a.member AND (m.group_id IS NULL OR a.group_id = m.group_id)))"""

    @staticmethod
    def editable_material(group):
        return f"({ADMIN} OR {GROUP_ADMIN.format(group=group)})"

    def materials(self, category, before):
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT id, title FROM useful_materials m WHERE category = %(category)s
                    AND id < %(before)s AND {self.visible_material()}
                    ORDER BY id DESC LIMIT {PAGE_SIZE + 1}""",
                self.params | {"category": category, "before": before},
            ).fetchall()

    def material(self, item):
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT category, title, body, link, group_id, revision,
                    {self.editable_material("m.group_id")},
                    ARRAY(SELECT name FROM useful_files WHERE material_id = m.id ORDER BY slot),
                    coalesce((SELECT name FROM groups WHERE id = m.group_id), 'Для всех групп')
                    FROM useful_materials m WHERE id = %(id)s AND {self.visible_material()}""",
                self.params | {"id": item},
            ).fetchone()

    def file(self, item, revision, slot, *, check_only=False):
        fields = "1" if check_only else "f.name, f.data"
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT {fields} FROM useful_files f JOIN useful_materials m ON m.id = f.material_id
                    WHERE m.id = %(id)s AND m.revision = %(revision)s AND f.slot = %(slot)s
                      AND {self.visible_material()}""",
                self.params | {"id": item, "revision": revision, "slot": slot},
            ).fetchone()

    def save_material(self, draft):
        validate_link(draft.link)
        if draft.files is not None:
            if len(draft.files) > 3:
                raise ValueError("Можно прикрепить до трёх файлов.")
            for name, data in draft.files:
                validate_data(name, data)
        if not (
            draft.body
            or draft.link
            or draft.files
            or (draft.files is None and draft.names)
        ):
            raise ValueError("Добавь текст, ссылку или файл.")
        args = self.params | vars(draft)
        with self.store.connect() as conn:
            if draft.id is None:
                row = conn.execute(
                    f"""INSERT INTO useful_materials (group_id, category, title, body, link)
                        SELECT %(group)s, %(category)s, %(title)s, %(body)s, %(link)s
                        WHERE {self.editable_material("%(group)s::bigint")} RETURNING id""",
                    args,
                ).fetchone()
            else:
                row = conn.execute(
                    f"""UPDATE useful_materials m SET title = %(title)s, body = %(body)s,
                        link = %(link)s, revision = revision + 1
                        WHERE id = %(id)s AND revision = %(revision)s
                          AND {self.editable_material("m.group_id")} RETURNING id""",
                    args,
                ).fetchone()
            if row and draft.files is not None:
                conn.execute("DELETE FROM useful_files WHERE material_id = %s", row)
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO useful_files VALUES (%s, %s, %s, %s)",
                        [
                            (row[0], slot, name, data)
                            for slot, (name, data) in enumerate(draft.files, 1)
                        ],
                    )
            return row[0] if row else None

    def delete_material(self, item, revision):
        with self.store.connect() as conn:
            return bool(
                conn.execute(
                    f"""DELETE FROM useful_materials m
                    WHERE id = %(id)s AND revision = %(revision)s
                      AND {self.editable_material("m.group_id")}""",
                    self.params | {"id": item, "revision": revision},
                ).rowcount
            )

    def schedule_teachers(self, group_only: bool):
        """Собирает преподавателей, их предметы и кабинеты из актуального расписания XLS."""
        with self.store.connect() as conn:
            user_group = conn.execute(
                """SELECT g.id, g.name FROM users u JOIN groups g ON g.id = u.group_id
                   WHERE u.max_user_id = %(actor)s AND g.active""",
                self.params,
            ).fetchone()
            if not user_group and not self.super:
                return None, []

            rows = conn.execute(
                """WITH selected_groups AS (
                       SELECT id FROM groups WHERE active AND (%s::bigint IS NULL OR id = %s)
                   ), versions AS (
                       SELECT g.id AS group_id,
                              (SELECT i.id FROM schedule_groups sg
                               JOIN schedule_imports i ON i.id = sg.import_id
                               WHERE sg.group_id = g.id AND i.scope = 'base'
                                 AND i.valid_from <= (now() AT TIME ZONE 'Europe/Samara')::date
                                 AND i.valid_to >= (now() AT TIME ZONE 'Europe/Samara')::date
                               ORDER BY i.id DESC LIMIT 1) AS import_id
                       FROM selected_groups g
                       UNION
                       SELECT sg.group_id, i.id FROM schedule_groups sg
                       JOIN schedule_imports i ON i.id = sg.import_id
                       JOIN selected_groups g ON g.id = sg.group_id
                       WHERE i.scope = 'date' AND i.published_at IS NOT NULL
                         AND i.valid_from = (now() AT TIME ZONE 'Europe/Samara')::date
                         AND i.id = (SELECT max(newer.id) FROM schedule_imports newer
                                     JOIN schedule_groups ng ON ng.import_id = newer.id
                                     WHERE ng.group_id = sg.group_id AND newer.scope = 'date'
                                       AND newer.valid_from = i.valid_from)
                   )
                   SELECT DISTINCT l.teacher, l.subject, l.room FROM schedule_lessons l
                   JOIN versions v ON v.import_id = l.import_id AND v.group_id = l.group_id
                   WHERE btrim(l.teacher) <> ''""",
                ((user_group[0] if group_only and user_group else None),) * 2,
            ).fetchall()

        subjects_by_teacher = defaultdict(set)
        rooms_by_teacher = defaultdict(set)
        for raw_teacher, raw_subject, raw_room in rows:
            teachers = [t.strip() for t in raw_teacher.split(" / ") if t.strip()]
            rooms = [r.strip() for r in (raw_room or "").split(" / ") if r.strip()]
            subject = (raw_subject or "").strip()
            for idx, t_name in enumerate(teachers):
                if subject and subject != "Предмет не указан":
                    subjects_by_teacher[t_name].add(subject)
                if idx < len(rooms):
                    rooms_by_teacher[t_name].add(rooms[idx])
                elif rooms:
                    rooms_by_teacher[t_name].update(rooms)

        result = []
        for t_name in sorted(subjects_by_teacher.keys() | rooms_by_teacher.keys()):
            subj_str = ", ".join(sorted(subjects_by_teacher[t_name]))
            room_str = ", ".join(sorted(rooms_by_teacher[t_name]))
            result.append((t_name, subj_str, room_str))
        return (user_group[1] if user_group else None), result

    @staticmethod
    def validate_media_list(media):
        if media is not None:
            if len(media) > 3:
                raise ValueError("До трёх фото или видео.")
            for kind, name, data in media:
                validate_media(kind, name, data)

    @staticmethod
    def replace_media(conn, event, section, media):
        conn.execute(
            "DELETE FROM event_media WHERE event_id = %s AND section = %s",
            (event, section),
        )
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO event_media VALUES (%s,%s,%s,%s,%s,%s)",
                [
                    (event, section, i, kind, name, data)
                    for i, (kind, name, data) in enumerate(media, 1)
                ],
            )

    def details(self, event):
        if self.event(event) is None:
            return None
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT tags, report_body, report_link, report_published FROM events WHERE id = %s",
                (event,),
            ).fetchone()

    def media(self, event, section):
        details = self.details(event)
        if (
            not details
            or section not in ("event", "report")
            or (section == "report" and not (self.super or details[3]))
        ):
            return []
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT kind,name,data FROM event_media WHERE event_id = %s AND section = %s ORDER BY slot",
                (event, section),
            ).fetchall()

    def save_report(self, draft):
        validate_link(draft.link)
        self.validate_media_list(draft.media)
        if not draft.body.strip():
            raise ValueError("Добавь краткие итоги мероприятия.")
        with self.store.connect() as conn:
            self.event_lock(conn)
            row = conn.execute(
                f"""UPDATE events SET report_body = %(body)s, report_link = %(link)s,
                    report_published = true, revision = revision + 1
                    WHERE id = %(id)s AND revision = %(revision)s AND starts_at <= now()
                      AND state = 'published' AND {ADMIN} RETURNING id""",
                self.params | vars(draft),
            ).fetchone()
            if row and draft.media is not None:
                self.replace_media(conn, row[0], "report", draft.media)
            return row[0] if row else None
