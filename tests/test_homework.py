from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from psycopg import sql
from psycopg.conninfo import make_conninfo

from db import migrate
from homework import HomeworkStore, SAMARA, parse_deadline
from homework_dialog import Draft, HomeworkDialog, MAX_ID
from homework_files import FILE_LIMIT, cdn_target, receive_file, transfer, upload_file, validate_data
from main import MaxClient, MaxError, handle_update
from schedule_notifications import deliver_one
from test_db import TEST_URL, LegacyFixtureStore, remove_homework_fixture
from test_main import message_update
from test_updates import lesson


class HomeworkInputTests(unittest.TestCase):
    def test_deadline_uses_samara_and_rejects_past_and_invalid_dates(self):
        now = datetime(2026, 11, 6, 17, 30, tzinfo=timezone.utc)
        self.assertEqual(parse_deadline("06.11.2026 22:00", now).isoformat(), "2026-11-06T22:00:00+04:00")
        for text in ("06.11.2026 21:30", "31.02.2026 22:00", "tomorrow"):
            with self.assertRaises(ValueError):
                parse_deadline(text, now)

    def test_file_validation_and_cdn_only(self):
        for name, data in (("../x.txt", b"x"), ("x.exe", b"x"), ("x.pdf", b"bad"),
                           ("x.txt", b"\xff"), ("x.txt", b""), ("x.txt", b"x" * (FILE_LIMIT + 1)),
                           ("a\r\nb.txt", b"x")):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_data(name, data)
        validate_data("Задание.txt", "Текст".encode())
        self.assertEqual(cdn_target("https://fu.oneme.ru/file?q=signed"), ("fu.oneme.ru", "/file?q=signed"))
        for url in ("https://127.0.0.1/file", "https://fu.oneme.ru.evil.org/file",
                    "http://fu.oneme.ru/file", "https://u@fu.oneme.ru/file", "https://fu.oneme.ru:123/file"):
            with self.assertRaises(ValueError):
                cdn_target(url)

    def test_attachment_transport_validates_size_disables_redirects_and_uploads_bytes(self):
        attachment = {"type": "file", "payload": {
            "url": "https://fu.oneme.ru/file", "filename": "a.txt", "size": 3,
        }}
        with patch("homework_files.transfer", return_value=b"abc") as download:
            self.assertEqual(receive_file(None, attachment), ("a.txt", b"abc"))
            self.assertEqual(download.call_args.kwargs["limit"], FILE_LIMIT)
            attachment["payload"]["size"] = FILE_LIMIT + 1
            with self.assertRaises(ValueError):
                receive_file(None, attachment)
            download.assert_called_once()
            actual_max = {"type": "file", "filename": "a.txt", "size": 3,
                          "payload": {"url": "https://fd.oneme.ru/file", "token": "test"}}
            self.assertEqual(receive_file(None, actual_max), ("a.txt", b"abc"))
            self.assertEqual(download.call_count, 2)
        with patch("homework_files.http.client.HTTPSConnection") as connect:
            response = connect.return_value.getresponse.return_value
            response.status = 302
            with self.assertRaises(ValueError):
                transfer(None, "https://fu.oneme.ru/file", "GET", limit=1)
            response.read.assert_not_called()
            connect.return_value.close.assert_called_once()
        client = Mock()
        client.request.return_value = {"url": "https://fu.oneme.ru/upload"}
        with patch("homework_files.transfer", return_value=b'{"token":"ready"}') as upload:
            self.assertEqual(upload_file(client, "a.txt", b"abc"),
                             {"type": "file", "payload": {"token": "ready"}})
            self.assertIn(b'filename="a.txt"', upload.call_args.kwargs["body"])
            self.assertIn(b"\r\nabc\r\n", upload.call_args.kwargs["body"])

    def test_only_attachment_processing_error_retries_message(self):
        client = MaxClient("test", None)
        recipient = {"user_id": 10}
        attachment = [{"type": "file", "payload": {"token": "test"}}]
        with patch.object(client, "request", side_effect=[
            MaxError("processing", status=400, attachment_pending=True), {"message": {}},
        ]) as request, patch("main.time.sleep") as sleep:
            client.send_message(recipient, "file.txt", attachments=attachment)
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(1)
        with patch.object(client, "request", side_effect=MaxError("timeout", retryable=True)) as request:
            with self.assertRaises(MaxError):
                client.send_message(recipient, "file.txt", attachments=attachment)
            request.assert_called_once()


@unittest.skipUnless(TEST_URL, "Укажи отдельную TEST_DATABASE_URL.")
class HomeworkTests(unittest.TestCase):
    def setUp(self):
        name = "test_homework_" + uuid4().hex
        schema = sql.Identifier(name)
        conn = self.enterContext(LegacyFixtureStore(TEST_URL).connect())
        conn.autocommit = True
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        self.addCleanup(conn.execute, sql.SQL("DROP SCHEMA {} CASCADE").format(schema))
        self.store = LegacyFixtureStore(make_conninfo(TEST_URL, options=f"-c search_path={name}"))
        migrate(self.store)
        today = datetime.now(SAMARA).date()
        self.store.seed_legacy("test", ["А-01", "А-02"], [
            lesson(), lesson("А-02", subject="Физика"),
        ], today, today + timedelta(days=30), None)
        self.a, self.b = [group for group, _ in self.store.active_groups()]
        for user, group in ((10, self.a), (11, self.a), (12, self.b)):
            self.store.start(user)
            self.store.choose_group(user, group)
        self.super = self.db(90)
        self.admin = self.db(10)
        self.member = self.db(11)
        self.outsider = self.db(12)
        self.client = Mock()
        self.dialog = HomeworkDialog()

    def db(self, user):
        return HomeworkStore(self.store, user, 90)

    def query(self, query, args=()):
        with self.store.connect() as conn:
            cursor = conn.execute(query, args)
            return cursor.fetchall() if cursor.description else None

    def draft(self, shared=True, *, hours=None, **fields):
        defaults = dict(group=self.a, shared=shared, subject="Математика", body="Страница 42",
                        deadline=datetime.now(SAMARA) + timedelta(hours=hours) if hours else None)
        return Draft(**(defaults | fields))

    def test_stage_four_upgrade_preserves_data_and_runs_once(self):
        with self.store.connect() as conn:
            remove_homework_fixture(conn)
        self.assertEqual(migrate(self.store), ["004_homework.sql", "005_community.sql"])
        self.assertEqual(migrate(self.store), [])
        self.assertEqual(self.store.registered_group(10), "А-01")
        self.assertEqual(self.member.subjects(self.a, False), [("Математика",)])

    def test_shared_permissions_include_files_revision_and_revocation(self):
        self.assertIsNone(self.admin.save(self.draft()))
        self.assertFalse(self.admin.set_admin(10, True))
        self.assertFalse(self.super.set_admin(10, True, group=self.b))
        self.assertTrue(self.super.set_admin(10, True, group=self.a))
        self.assertIsNone(self.admin.save(self.draft(group=self.b, subject="Физика")))
        self.assertIsNone(self.admin.save(self.draft(subject="Физика")))
        task = self.admin.save(self.draft(files=[("a.txt", b"abc")]))
        self.assertIsNotNone(task)
        self.assertEqual(self.member.get(task)[4], "Страница 42")
        self.assertEqual(self.member.file(task, 1, 1), ("a.txt", b"abc"))
        self.assertIsNone(self.outsider.get(task))
        self.assertIsNone(self.outsider.file(task, 1, 1))
        edit = self.draft(task=task, revision=1, body="Страница 43", files=None)
        self.assertIsNone(self.member.save(edit))
        self.assertFalse(self.outsider.delete(task, 1))
        self.assertEqual(self.admin.save(edit), task)
        self.assertIsNone(self.admin.save(edit))
        self.assertIsNone(self.member.file(task, 1, 1))
        self.assertEqual(self.member.file(task, 2, 1), ("a.txt", b"abc"))
        self.assertTrue(self.super.set_admin(10, False, group=self.a))
        self.assertFalse(self.admin.delete(task, 2))
        self.assertTrue(self.super.delete(task, 2))
        self.assertEqual(self.query("SELECT count(*) FROM homework_files"), [(0,)])

    def test_personal_access_excludes_super_admin_and_survives_group_deactivation(self):
        task = self.member.save(self.draft(False, files=[("notes.txt", b"mine")]))
        self.assertIsNotNone(task)
        self.query("UPDATE groups SET active = false WHERE id = %s", (self.a,))
        self.assertEqual(self.member.page(0, False, MAX_ID)[0][0], task)
        self.assertEqual(self.member.file(task, 1, 1), ("notes.txt", b"mine"))
        for db in (self.admin, self.outsider, self.super):
            self.assertIsNone(db.get(task))
            self.assertIsNone(db.file(task, 1, 1))
            self.assertFalse(db.delete(task, 1))
        self.assertTrue(self.member.delete(task, 1))

    def test_pagination_has_no_duplicates_and_does_not_list_personal_tasks(self):
        self.super.set_admin(10, True)
        for _ in range(11):
            self.admin.save(self.draft())
        self.member.save(self.draft(False))
        first = self.member.page(self.a, True, MAX_ID)
        self.assertEqual(len(first), 9)
        second = self.member.page(self.a, True, first[7][0])
        self.assertEqual(len(second), 3)
        self.assertFalse(set(first[:8]) & set(second))
        self.assertEqual(self.outsider.page(self.a, True, MAX_ID), [])

    def test_notifications_categories_recipient_windows_and_deduplication(self):
        shared = self.super.save(self.draft(hours=20))
        private = self.member.save(self.draft(False, hours=2))
        expired = self.member.save(self.draft(False, hours=1))
        self.query("UPDATE homework SET deadline = now() - interval '1 minute' WHERE id = %s", (expired,))
        self.member.set_preference("new", False)
        self.admin.set_preference("group_reminder", False)
        self.member.set_preference("personal_reminder", False)
        send = Mock()

        def drain():
            for _ in range(10):
                self.query("UPDATE schedule_notification_rate SET next_send = now()")
                if not deliver_one(self.store, send, MaxError, schedule=False):
                    return
            self.fail("Очередь не опустела")

        drain()
        drain()
        self.assertEqual([c.args[0] for c in send.call_args_list], [{"user_id": 10}, {"user_id": 11}])
        self.assertEqual(self.query("""
            SELECT homework_id, user_id, kind, status FROM homework_notifications
            ORDER BY homework_id, user_id, kind
        """), [(shared, 10, "24h", "skipped"), (shared, 10, "new", "sent"),
               (shared, 11, "24h", "sent"), (shared, 11, "new", "skipped"),
               (private, 11, "3h", "skipped")])
        self.query("UPDATE homework SET deadline = now() + interval '2 hours' WHERE id = %s", (shared,))
        drain()
        self.assertEqual(self.query("SELECT user_id, status FROM homework_notifications WHERE homework_id = %s "
                                    "AND kind = '3h' ORDER BY user_id", (shared,)), [(10, "skipped"), (11, "sent")])

    def test_retry_never_sends_old_deadline_and_new_deadline_has_new_key(self):
        task = self.member.save(self.draft(False, hours=4))
        send = Mock(side_effect=MaxError("wait", retryable=True, status=429, retry_after=60))
        self.assertTrue(deliver_one(self.store, send, MaxError, schedule=False))
        self.assertEqual(self.query("SELECT kind, version, status, attempts FROM homework_notifications"),
                         [("24h", 1, "pending", 1)])
        self.assertEqual(self.member.save(self.draft(False, task=task, revision=1, hours=2)), task)
        self.query("UPDATE homework_notifications SET next_attempt = now() - interval '1 second'")
        self.query("UPDATE schedule_notification_rate SET next_send = now()")
        send.reset_mock(side_effect=True)
        self.assertTrue(deliver_one(self.store, send, MaxError, schedule=False))
        send.assert_not_called()
        self.assertTrue(deliver_one(self.store, send, MaxError, schedule=False))
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], {"user_id": 11})
        self.assertEqual(self.query("SELECT kind, version, status FROM homework_notifications ORDER BY id"),
                         [("24h", 1, "skipped"), ("3h", 2, "sent")])

    def event(self, user, *, text="", payload=None, chat_type="dialog", attachments=None):
        if payload is None:
            update = message_update(text)
            message = update["message"]
            message["sender"]["user_id"] = user
            message["body"]["attachments"] = attachments
        else:
            message = {}
            update = {"update_type": "message_callback", "message": message, "callback": {
                "callback_id": "test", "user": {"user_id": user}, "payload": "hw:" + payload,
            }}
        message["recipient"] = {"chat_type": chat_type}
        handle_update(self.client, self.store, update, 90, self.dialog)

    def click(self, user, action):
        self.event(user, payload=f"d:{self.dialog.drafts[user].token}:{action}")

    def test_dialog_create_edit_attachment_confirm_delete_and_cancel(self):
        self.event(11, text="/homework")
        self.event(11, payload=f"new:p:{self.a}")
        stale = self.dialog.drafts[11].token
        self.click(11, "subject:0")
        self.event(11, payload=f"d:{stale}:subject:0")
        self.assertIn("устарела", self.client.answer.call_args.kwargs["text"])
        self.event(11, text="Текст")
        self.event(11, text="прошлое")
        self.assertEqual(self.dialog.drafts[11].step, "deadline")
        self.click(11, "skip")
        self.click(11, "done")
        self.assertEqual(self.member.page(self.a, False, MAX_ID), [])
        self.click(11, "yes")
        task = self.member.page(self.a, False, MAX_ID)[0][0]
        self.event(11, payload=f"edit:{task}")
        self.click(11, "keep")
        self.click(11, "keep")
        self.click(11, "keep")
        self.client.receive_homework_file.return_value = ("readme.txt", b"abc")
        self.event(11, attachments=[{"type": "file"}])
        self.click(11, "done")
        self.click(11, "yes")
        self.assertEqual(self.member.file(task, 2, 1), ("readme.txt", b"abc"))
        self.event(12, payload=f"file:{task}:2:1")
        self.client.upload_homework_file.assert_not_called()
        self.event(11, payload=f"file:{task}:2:1")
        self.client.upload_homework_file.assert_called_once_with("readme.txt", b"abc")
        self.event(11, payload=f"delete:{task}")
        self.click(11, "cancel")
        self.assertIsNotNone(self.member.get(task))
        self.event(11, payload=f"delete:{task}")
        self.click(11, "yes")
        self.assertIsNone(self.member.get(task))

    def test_dialog_role_requires_private_super_admin_and_confirmation(self):
        self.event(10, text="/hw_grant 10")
        self.event(90, text="/hw_grant 10", chat_type="chat")
        self.assertFalse(self.admin.groups(True)[0][2])
        self.event(90, text="/hw_grant 10")
        self.assertFalse(self.admin.groups(True)[0][2])
        self.click(90, "yes")
        self.assertTrue(self.admin.groups(True)[0][2])
        self.event(90, payload=f"role:revoke:{self.a}")
        self.event(90, text="10")
        self.click(90, "yes")
        self.assertFalse(self.admin.groups(True)[0][2])
