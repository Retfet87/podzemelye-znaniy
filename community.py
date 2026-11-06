"""Persistence and authorization for events and useful materials."""
from urllib.parse import urlsplit

from homework import format_deadline
from homework_files import validate_data

CATEGORIES = {
    "curators": "Кураторы", "teachers": "Преподаватели", "rooms": "Кабинеты",
    "study": "Учебные материалы", "links": "Полезные ссылки",
}
EVENT_LOCK = 281952768
PAGE_SIZE = 8
MEMBER = """EXISTS (SELECT 1 FROM users u JOIN groups g ON g.id = u.group_id
                      WHERE u.max_user_id = %(actor)s AND g.active)"""
ADMIN = "coalesce(%(actor)s = %(admin)s, false)"
GROUP_ADMIN = """EXISTS (SELECT 1 FROM homework_access(%(actor)s, %(admin)s) a
                          WHERE a.group_id = {group} AND a.member AND a.can_manage)"""
PROPOSER = """EXISTS (SELECT 1 FROM homework_access(%(actor)s, %(admin)s) a
                       WHERE a.member AND a.can_manage)"""


def validate_link(link):
    if not link:
        return
    try:
        url = urlsplit(link)
        if (len(link) <= 700 and url.scheme == "https" and url.hostname
                and url.username is None and url.port in (None, 443)
                and not any(c.isspace() or ord(c) < 32 or c in '<>\\' for c in link)):
            return
    except ValueError:
        pass
    raise ValueError("Укажи HTTPS-ссылку без пробелов и данных для входа.")


def event_text(title, description, starts_at, place):
    return (f"{title}\n{format_deadline(starts_at)}\nМесто: {place or 'не указано'}"
            + (f"\n\n{description}" if description else ""))


class CommunityStore:
    def __init__(self, store, actor, administrator):
        self.store = store
        self.params = {"actor": actor, "admin": administrator}
        self.super = actor == administrator

    def scopes(self):
        with self.store.connect() as conn:
            groups = conn.execute(
                """SELECT group_id, group_name FROM homework_access(%(actor)s, %(admin)s)
                   WHERE member AND can_manage""", self.params,
            ).fetchall()
            return ([(None, "Для всех групп")] if self.super else []) + groups

    def events(self, review, offset):
        with self.store.connect() as conn:
            return conn.execute(
                f"""SELECT id, title, starts_at, state FROM events
                    WHERE CASE WHEN %(review)s THEN {ADMIN} AND state IN ('draft', 'proposed')
                    ELSE ({ADMIN} OR {MEMBER}) AND state = 'published' AND starts_at > now() END
                    ORDER BY starts_at, id LIMIT {PAGE_SIZE + 1} OFFSET %(offset)s""",
                self.params | {"review": review, "offset": offset},
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
                        AND p.user_id = %(actor)s)))))""", self.params | {"id": event},
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
                    AND starts_at > now() AND {MEMBER}""", args,
            ).fetchone():
                return False
            if join:
                conn.execute(
                    """INSERT INTO event_participants VALUES (%(id)s, %(actor)s)
                       ON CONFLICT DO NOTHING""", args,
                )
            else:
                conn.execute(
                    "DELETE FROM event_participants WHERE event_id = %(id)s AND user_id = %(actor)s", args,
                )
            return True

    def save_event(self, draft):
        args = self.params | vars(draft)
        with self.store.connect() as conn:
            self.event_lock(conn)
            if draft.id is None:
                row = conn.execute(
                    f"""INSERT INTO events (title, description, starts_at, place, state, proposed_group_id)
                        SELECT %(title)s, %(body)s, %(starts_at)s, %(place)s,
                        CASE WHEN {ADMIN} THEN 'draft' ELSE 'proposed' END,
                        CASE WHEN NOT {ADMIN} THEN (SELECT group_id FROM users WHERE max_user_id = %(actor)s) END
                        WHERE ({ADMIN} OR {PROPOSER}) AND %(starts_at)s > now()
                        RETURNING id""", args,
                ).fetchone()
            else:
                row = conn.execute(
                    f"""UPDATE events SET title = %(title)s, description = %(body)s,
                        place = %(place)s, starts_at = %(starts_at)s, revision = revision + 1,
                        reminder_version = reminder_version + (starts_at <> %(starts_at)s)::integer
                        WHERE id = %(id)s AND revision = %(revision)s AND {ADMIN}
                          AND state <> 'cancelled' AND starts_at > now() AND %(starts_at)s > now()
                          AND (title, description, starts_at, place) IS DISTINCT FROM
                              (%(title)s, %(body)s, %(starts_at)s, %(place)s)
                        RETURNING id, state""", args,
                ).fetchone()
                if row and row[1] == "published":
                    self.enqueue_event(conn, row[0], "changed")
            return row[0] if row else None

    def transition(self, event, revision, publish):
        with self.store.connect() as conn:
            self.event_lock(conn)
            row = conn.execute(
                f"""UPDATE events SET state = %(target)s, revision = revision + 1
                    WHERE id = %(id)s AND revision = %(revision)s AND {ADMIN}
                      AND starts_at > now() AND state = ANY(%(source)s) RETURNING id""",
                self.params | {"id": event, "revision": revision,
                               "target": "published" if publish else "cancelled",
                               "source": ["draft", "proposed"] if publish else ["draft", "proposed", "published"]},
            ).fetchone()
            if row and not publish:
                self.enqueue_event(conn, event, "cancelled")
            return bool(row)

    @staticmethod
    def enqueue_event(conn, event, kind):
        title, description, starts_at, place = conn.execute(
            "SELECT title, description, starts_at, place FROM events WHERE id = %s", (event,),
        ).fetchone()
        label = "Мероприятие отменено" if kind == "cancelled" else "Мероприятие изменилось"
        body = label + "\n" + event_text(title, description, starts_at, place)
        conn.execute(
            """INSERT INTO event_notifications (event_id, user_id, kind, version, valid_until, body)
               SELECT e.id, p.user_id, %s, e.revision, now() + interval '1 day', %s
               FROM events e JOIN event_participants p ON p.event_id = e.id WHERE e.id = %s
               ON CONFLICT DO NOTHING""", (kind, body, event),
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
                    {self.editable_material('m.group_id')},
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
        if not (draft.body or draft.link or draft.files or (draft.files is None and draft.names)):
            raise ValueError("Добавь текст, ссылку или файл.")
        args = self.params | vars(draft)
        with self.store.connect() as conn:
            if draft.id is None:
                row = conn.execute(
                    f"""INSERT INTO useful_materials (group_id, category, title, body, link)
                        SELECT %(group)s, %(category)s, %(title)s, %(body)s, %(link)s
                        WHERE {self.editable_material('%(group)s::bigint')} RETURNING id""", args,
                ).fetchone()
            else:
                row = conn.execute(
                    f"""UPDATE useful_materials m SET title = %(title)s, body = %(body)s,
                        link = %(link)s, revision = revision + 1
                        WHERE id = %(id)s AND revision = %(revision)s
                          AND {self.editable_material('m.group_id')} RETURNING id""", args,
                ).fetchone()
            if row and draft.files is not None:
                conn.execute("DELETE FROM useful_files WHERE material_id = %s", row)
                with conn.cursor() as cur:
                    cur.executemany("INSERT INTO useful_files VALUES (%s, %s, %s, %s)", [
                        (row[0], slot, name, data) for slot, (name, data) in enumerate(draft.files, 1)
                    ])
            return row[0] if row else None
