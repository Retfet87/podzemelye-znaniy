from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import unittest
from unittest.mock import Mock
from uuid import uuid4

from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.db import migrate
from app.handlers.community import CommunityHandler, Draft
from app.max_api.client import MaxError
from app.notifications.worker import deliver_one
from app.repositories.community import CommunityStore
from app.repositories.homework import HomeworkStore
from app.services.schedule_parser import Lesson
from app.services.validation import validate_link
from app.timeutil import SAMARA
from helpers import handle_update
from test_db import LegacyFixtureStore, TEST_URL, remove_community_fixture
from test_main import callback_update, message_update


class LinkTests(unittest.TestCase):
    def test_safe_links_and_optional_link(self):
        for value in ("", "https://akvt.ru/students/", "https://example.org/a?q=x"):
            validate_link(value)
        for value in ("javascript:alert(1)", "http://example.org", "https://a:b@example.org",
                      "https://example.org/a\n", "https://example.org\\x", "https://example.org:12"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_link(value)


@unittest.skipUnless(TEST_URL, "Укажи отдельную TEST_DATABASE_URL.")
class CommunityTests(unittest.TestCase):
    def setUp(self):
        schema_name = "test_community_" + uuid4().hex
        schema = sql.Identifier(schema_name)
        conn = self.enterContext(LegacyFixtureStore(TEST_URL).connect())
        conn.autocommit = True
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        self.addCleanup(conn.execute, sql.SQL("DROP SCHEMA {} CASCADE").format(schema))
        self.store = LegacyFixtureStore(make_conninfo(TEST_URL, options=f"-c search_path={schema_name}"))
        migrate(self.store)
        self.store.add_groups(["А-01", "Б-02"])
        self.a, self.b = [group for group, _ in self.store.active_groups()]
        for user, group in ((10, self.a), (11, self.a), (12, self.b), (90, self.a)):
            self.store.start(user)
            self.store.choose_group(user, group)
        HomeworkStore(self.store, 90, 90).set_admin(10, True, self.a)
        self.super, self.group_admin, self.student, self.outsider, self.unregistered = (
            CommunityStore(self.store, user, 90) for user in (90, 10, 11, 12, 13)
        )

    def query(self, query, params=()):
        with self.store.connect() as conn:
            cursor = conn.execute(query, params)
            return cursor.fetchall() if cursor.description else None

    def draft_event(self, **overrides):
        defaults = dict(kind="event", title="Олимпиада", body="Описание", place="Зал",
                        starts_at=datetime.now(SAMARA) + timedelta(hours=30))
        return Draft(**(defaults | overrides))

    def publish(self, hours=30):
        event = self.super.save_event(self.draft_event(starts_at=datetime.now(SAMARA) + timedelta(hours=hours)))
        self.assertTrue(self.super.transition(event, 1, True))
        return event

    def dispatch(self, send):
        return deliver_one(self.store, send, MaxError, events=True, homework=False, schedule=False)

    def unthrottle(self):
        self.query("UPDATE schedule_notification_rate SET next_send = now()")

    def test_teacher_directory_requires_membership(self):
        self.assertEqual(self.unregistered.schedule_teachers(False), (None, []))

    def test_material_deletion_checks_revision_before_removing_files(self):
        draft = Draft("material", group=self.a, category="study", title="Файл",
                      body="Описание", files=[("file.txt", b"test")])
        item = self.group_admin.save_material(draft)
        self.assertFalse(self.group_admin.delete_material(item, 0))
        self.assertEqual(self.student.file(item, 1, 1), ("file.txt", b"test"))
        self.assertTrue(self.group_admin.delete_material(item, 1))
        self.assertIsNone(self.student.material(item))

    def test_upgrade_preserves_registration_and_previous_schema(self):
        with self.store.connect() as conn:
            remove_community_fixture(conn)
        self.assertEqual(migrate(self.store), ["005_community.sql", "006_ux.sql"])
        self.assertEqual(migrate(self.store), [])
        self.assertEqual(self.store.registered_group(11), "А-01")

    def test_proposals_review_publish_and_stale_edit(self):
        self.assertIsNone(self.student.save_event(self.draft_event()))
        event = self.group_admin.save_event(self.draft_event())
        self.assertIsNone(self.student.event(event))
        self.assertEqual(self.super.event(event)[-1], "А-01")
        self.assertEqual(self.group_admin.events(True, 0), [])
        self.assertEqual(self.super.events(True, 0)[0][0], event)
        self.assertFalse(self.group_admin.transition(event, 1, True))
        self.assertTrue(self.super.transition(event, 1, True))
        self.assertFalse(self.super.transition(event, 1, True))
        row = self.super.event(event)
        edit = self.draft_event(id=event, revision=row[5], starts_at=row[2], place="Новый зал")
        self.assertIsNone(self.group_admin.save_event(edit))
        self.assertEqual(self.super.save_event(edit), event)
        self.assertIsNone(self.super.save_event(edit))
        self.assertEqual(self.student.event(event)[3], "Новый зал")

    def test_concurrent_rsvp_is_unique_and_leave_idempotent(self):
        event = self.publish()
        self.assertFalse(self.unregistered.attend(event, True))
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(lambda _: self.student.attend(event, True), range(2))), [True, True])
        self.assertEqual(self.student.event(event)[6:8], (1, True))
        self.assertTrue(self.student.attend(event, False))
        self.assertTrue(self.student.attend(event, False))
        self.assertEqual(self.student.event(event)[6:8], (0, False))

    def test_reminder_only_to_attendee_once_and_reschedule(self):
        event = self.publish(hours=25)
        self.student.attend(event, True)
        send = Mock()
        self.assertFalse(self.dispatch(send))
        self.query("UPDATE events SET starts_at = now() + interval '23 hours'")
        self.assertTrue(self.dispatch(send))
        self.unthrottle()
        self.assertFalse(self.dispatch(send))
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], {"user_id": 11})
        self.assertIn("Напоминание", send.call_args.args[1])
        self.assertEqual(self.query("SELECT kind, status, attempts FROM event_notifications"),
                         [("reminder", "sent", 1)])

    def test_edit_cancel_and_no_notifications_for_student_who_left(self):
        event = self.publish()
        self.student.attend(event, True)
        self.group_admin.attend(event, True)
        row = self.super.event(event)
        self.super.save_event(self.draft_event(id=event, revision=row[5], starts_at=row[2], place="Другой зал"))
        self.group_admin.attend(event, False)
        send = Mock()
        while self.dispatch(send):
            self.unthrottle()
        self.assertEqual(send.call_args.args[0], {"user_id": 11})
        self.assertIn("Другой зал", send.call_args.args[1])
        self.assertTrue(self.super.transition(event, row[5] + 1, False))
        self.assertFalse(self.super.transition(event, row[5] + 1, False))
        self.assertFalse(self.student.attend(event, True))
        self.assertIsNone(self.outsider.event(event))
        self.assertEqual(self.student.event(event)[4], "cancelled")
        self.assertTrue(self.dispatch(send))
        self.assertEqual(send.call_count, 2)
        self.assertEqual(send.call_args.args[0], {"user_id": 11})
        self.assertIn("отменено", send.call_args.args[1])

    def test_material_scope_stale_revision_file_link_and_delete(self):
        draft = Draft("material", group=self.a, category="study", title="Учебник",
                      body="Описание", link="https://example.org/book", files=[("book.txt", b"file")])
        self.assertIsNone(self.student.save_material(draft))
        material = self.group_admin.save_material(draft)
        self.assertIsNotNone(material)
        self.assertIsNone(self.outsider.material(material))
        self.assertIsNone(self.outsider.file(material, 1, 1))
        self.assertEqual(self.student.file(material, 1, 1), ("book.txt", b"file"))
        draft.id, draft.revision, draft.files, draft.names = material, 1, None, ("book.txt",)
        self.assertIsNone(self.student.save_material(draft))
        draft.title = "Изменённый учебник"
        self.assertEqual(self.group_admin.save_material(draft), material)
        self.assertIsNone(self.group_admin.save_material(draft))
        self.assertIsNone(self.student.file(material, 1, 1))
        self.assertEqual(self.student.file(material, 2, 1), ("book.txt", b"file"))
        self.assertFalse(self.student.delete_material(material, 2))
        self.assertTrue(self.group_admin.delete_material(material, 2))
        self.assertIsNone(self.student.material(material))
        shared = Draft("material", category="links", title="Колледж", link="https://akvt.ru")
        self.assertIsNone(self.group_admin.save_material(shared))
        item = self.super.save_material(shared)
        self.assertEqual(self.outsider.material(item)[3], "https://akvt.ru")

    def test_schedule_teachers_for_group_and_all(self):
        today = datetime.now(SAMARA).date()
        self.store.seed_legacy("teachers", ["А-01", "Б-02"], [
            Lesson("А-01", 0, 1, "all", "Физика", "Яблокова М.В.", "ауд.401", "", ()),
            Lesson("Б-02", 0, 2, "all", "ОПД", "Исхакова А.Р.", "ауд.232", "", ()),
        ], today, today, None)
        group_name, group_teachers = self.student.schedule_teachers(True)
        self.assertEqual(group_name, "А-01")
        self.assertEqual(group_teachers, [("Яблокова М.В.", "Физика", "ауд.401")])
        _, all_teachers = self.student.schedule_teachers(False)
        self.assertEqual(len(all_teachers), 2)

    def test_dialog_preview_and_private_callbacks(self):
        client = Mock()
        dialog = CommunityHandler()
        update = callback_update("community:event_new", user_id=90)
        update["message"]["recipient"] = {"chat_type": "chat"}
        handle_update(client, self.store, update, 90, community=dialog)
        self.assertNotIn(90, dialog.drafts)
        update["message"]["recipient"]["chat_type"] = "dialog"
        handle_update(client, self.store, update, 90, community=dialog)
        draft = dialog.drafts[90]

        def input_text(text):
            message = message_update(text)
            message["message"]["sender"]["user_id"] = 90
            message["message"]["recipient"]["chat_type"] = "dialog"
            handle_update(client, self.store, message, 90, community=dialog)

        def press(action):
            update["callback"]["payload"] = "community:" + draft.button("", action)[1]
            handle_update(client, self.store, update, 90, community=dialog)

        input_text("Олимпиада")
        press("skip")
        input_text((datetime.now(SAMARA) + timedelta(days=3)).strftime("%d.%m.%Y %H:%M"))
        press("skip")
        press("keep")  # tags
        press("keep")  # media
        self.assertIn("Предпросмотр", client.answer.call_args.kwargs["text"])
        self.assertEqual(self.query("SELECT count(*) FROM events"), [(0,)])
        press("save")
        self.assertEqual(self.query("SELECT state FROM events"), [("draft",)])
