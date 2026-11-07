"""One-command offline journey: TEST_DATABASE_URL=... python -m scripts.smoke_test."""
import os
import sys
import unittest
from datetime import datetime, timedelta
from uuid import uuid4

from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.db import Database, local_dsn, migrate
from app.handlers import Deps, build_router
from app.handlers.community import Draft
from app.notifications.events import prepare
from app.repositories.community import CommunityStore
from app.services.schedule_parser import Lesson
from app.services.schedule_publisher import decide, preview
from app.timeutil import SAMARA


class OfflineMAX:
    """No HTTP transport, token or real recipients: only captured UI responses."""

    def __init__(self):
        self.messages = []
        self.context = None

    def send_message(self, recipient, text, *, attachments=None):
        self.messages.append((text, attachments))

    def answer(self, callback_id, *, text=None, attachments=None, notification=None):
        self.messages.append((text or notification, attachments))

    def welcome_attachment(self):
        return None

    def upload_media(self, kind, name, data):
        return {"type": kind, "payload": {"token": "offline-media"}}

    def upload_file(self, name, data):
        return {"type": "file", "payload": {"token": "offline-file"}}

    def receive_file(self, attachment):
        return "smoke.txt", b"offline attachment"


class Journey(unittest.TestCase):
    def test_student_journey(self):
        # Never read .env or DATABASE_URL. Only an explicitly named test/demo DB.
        dsn = local_dsn({"TEST_DATABASE_URL": os.environ["TEST_DATABASE_URL"]}, "TEST_DATABASE_URL")
        self.assertTrue(conninfo_to_dict(dsn)["dbname"].endswith(("_test", "_demo")))
        schema = "smoke_" + uuid4().hex
        with Database(dsn).connect() as control:
            control.autocommit = True
            control.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            self.addCleanup(self.drop_schema, dsn, schema)
            db = Database(make_conninfo(dsn, options=f"-c search_path={schema}"))
            migrate(db)
            admin, student = 900000090, 900000011
            monday = datetime.now(SAMARA).date()
            monday -= timedelta(days=monday.weekday())
            lesson = Lesson("ДЕМО-01", 0, 1, "all", "Демо-предмет", "", "", "", ())
            token, _, _, _ = preview(db, admin, "offline-smoke", [lesson.group], [lesson],
                                      monday, monday + timedelta(days=13), monday,
                                      scope="base", dry_run=False)
            self.assertEqual(decide(db, admin, admin, token, confirm=True), "applied")
            client = OfflineMAX()
            deps = Deps.create(client, db, admin)
            router = build_router(deps)

            def message(text, attachments=None):
                router.dispatch({"update_type": "message_created", "message": {
                    "sender": {"user_id": student}, "recipient": {"chat_type": "dialog", "chat_id": 1},
                    "body": {"text": text, "attachments": attachments or []}}})

            def callback(payload):
                router.dispatch({"update_type": "message_callback", "callback": {
                    "callback_id": uuid4().hex, "user": {"user_id": student}, "payload": payload},
                    "message": {"recipient": {"chat_type": "dialog"}}})

            def click(label):
                for attachment in client.messages[-1][1] or []:
                    for row in attachment.get("payload", {}).get("buttons", []):
                        for button in row:
                            if button.get("text", "").endswith(label):
                                callback(button["payload"])
                                return
                self.fail(f"Кнопка не найдена: {label}")

            message("/start")
            self.assertIsNone(deps.users.registered_group(student))
            callback("institution:akvt")
            click("ДЕМО")
            click("ДЕМО-01")
            self.assertEqual(deps.users.registered_group(student), "ДЕМО-01")
            click("Расписание")
            click("Неделя")
            self.assertIn("Демо-предмет", client.messages[-1][0])
            callback("hw:open")
            click("Личные задания")
            click("Создать задание")
            click("Демо-предмет")
            message("Проверить сквозной сценарий")
            click("Без срока")
            message("", [{"type": "file"}])
            click("Готово")
            click("Сохранить")
            self.assertIn("Задание сохранено", client.messages[-1][0])
            administrator = CommunityStore(db, admin, admin)
            event = administrator.save_event(Draft(
                "event", title="ДЕМО — smoke", body="Только тестовая база", place="",
                starts_at=datetime.now(SAMARA) + timedelta(hours=23)))
            self.assertTrue(administrator.transition(event, 1, True))
            callback(f"community:join:{event}")
            self.assertIn("Ты записан", client.messages[-1][0])
            callback(f"community:join:{event}")
            with db.connect() as conn:
                prepare(conn)
                prepare(conn)
                self.assertEqual(conn.execute("SELECT count(*) FROM event_participants").fetchone(), (1,))
                self.assertEqual(conn.execute("SELECT user_id, kind, status FROM event_notifications").fetchall(),
                                 [(student, "reminder", "pending")])
            # Fresh DB connections and a fresh router simulate application restart.
            restarted = Database(db.dsn)
            restarted.check_schema()
            build_router(Deps.create(client, restarted, admin)).dispatch({
                "update_type": "message_created", "message": {"sender": {"user_id": student},
                "recipient": {"chat_type": "dialog", "chat_id": 1}, "body": {"text": "/start"}}})
            self.assertIn("ДЕМО-01", client.messages[-1][0])
            with restarted.connect() as conn:
                self.assertEqual(conn.execute("SELECT owner_id, body FROM homework").fetchone(),
                                 (student, "Проверить сквозной сценарий"))
                self.assertEqual(conn.execute("SELECT data FROM homework_files").fetchone(), (b"offline attachment",))
                self.assertEqual(conn.execute("SELECT count(*) FROM event_notifications WHERE sent_at IS NOT NULL").fetchone(), (0,))

    @staticmethod
    def drop_schema(dsn, schema):
        with Database(dsn).connect() as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    try:
        settings = conninfo_to_dict(local_dsn({"TEST_DATABASE_URL": os.environ.get("TEST_DATABASE_URL")},
                                             "TEST_DATABASE_URL"))
        if not settings["dbname"].endswith(("_test", "_demo")):
            raise ValueError
    except (ValueError, KeyError):
        print("Нужна отдельная локальная TEST_DATABASE_URL с именем базы *_test или *_demo.", file=sys.stderr)
        sys.exit(2)
    unittest.main(verbosity=2)
