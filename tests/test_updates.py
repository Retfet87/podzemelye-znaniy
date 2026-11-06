from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from db import migrate, local_dsn
from main import MaxError, handle_update
from schedule_import import Lesson, parse_xls
from schedule_notifications import deliver_one
from schedule_updates import admin_id, compare, day_slots, decide, preview
from schedule_view import SAMARA
from test_db import TEST_URL, LegacyFixtureStore, remove_homework_fixture
from test_main import message_update


def lesson(group="А-01", **changes):
    row = Lesson(group, 0, 1, "all", "Математика", "Иванов И.И.", "ауд. 12", "", ())
    return replace(row, **changes)


class UpdateTests(unittest.TestCase):
    def test_formats_order_and_raw_rows_do_not_change_schedule(self):
        day = date(2026, 11, 2)
        old = [lesson(), lesson(pair=2)]
        new = [lesson(pair=2, room="АУД.12"), lesson(subject="  математика ",
               teacher="Иванов И. И", raw=("другое оформление",))]
        self.assertEqual(list(compare("А-01", day,
                                     day_slots(map(asdict, old), day, None),
                                     day_slots(map(asdict, new), day, None), True)), [])

    def test_parity_anchor_moves_lesson_to_other_dates(self):
        day = date(2026, 11, 2)
        rows = [asdict(lesson(parity="numerator"))]
        changes = list(compare("А-01", day, day_slots(rows, day, day),
                               day_slots(rows, day, day - timedelta(days=7)), True))
        self.assertEqual([c["kind"] for c in changes], ["cancelled"])

    def test_admin_from_env_and_only_authenticated_private_sender(self):
        self.assertIsNone(admin_id({}))
        self.assertEqual(admin_id({"MAX_SUPER_ADMIN_ID": "10"}), 10)
        with self.assertRaisesRegex(ValueError, "^Некорректная настройка MAX_SUPER_ADMIN_ID\\.$"):
            admin_id({"MAX_SUPER_ADMIN_ID": "secret"})
        token = uuid4()
        update = message_update(f"/schedule_confirm {token}")
        client, store = Mock(), Mock()
        with patch("main.decide", return_value="applied") as operation:
            for chat_type, admin in (("chat", 10), ("channel", 10), ("dialog", None), ("dialog", 11)):
                update["message"]["recipient"]["chat_type"] = chat_type
                handle_update(client, store, update, admin)
            operation.assert_not_called()
            client.send_message.assert_not_called()
            handle_update(client, store, update, 10)
            operation.assert_called_once_with(store, 10, 10, token, confirm=True)
            self.assertEqual(client.send_message.call_args.args[0], {"user_id": 10})


@unittest.skipUnless(TEST_URL, "Укажи TEST_DATABASE_URL.")
class PublicationTests(unittest.TestCase):
    def setUp(self):
        dsn = local_dsn({"TEST_DATABASE_URL": TEST_URL}, "TEST_DATABASE_URL")
        name = "test_updates_" + uuid4().hex
        conn = self.enterContext(LegacyFixtureStore(dsn).connect())
        conn.autocommit = True
        schema = sql.Identifier(name)
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        self.addCleanup(conn.execute, sql.SQL("DROP SCHEMA {} CASCADE").format(schema))
        self.store = LegacyFixtureStore(make_conninfo(dsn, options=f"-c search_path={name}"))
        migrate(self.store)
        now = datetime.now(SAMARA).date()
        self.day = now + timedelta(days=(7 - now.weekday()) % 7 + 7)
        self.end = self.day + timedelta(days=13)
        self.names = ["А-01", "А-02"]
        self.rows = [lesson(), lesson("А-02")]
        self.store.seed_legacy("old", self.names, self.rows, self.day, self.end, None)
        for user, (group, _) in enumerate(self.store.active_groups(), 101):
            self.store.start(user)
            self.store.choose_group(user, group)
        self.store.start(103)

    def query(self, query, args=()):
        with self.store.connect() as conn:
            result = conn.execute(query, args)
            return result.fetchall() if result.description else None

    def draft(self, rows=None, *, scope="base", dry_run=False, expected=0):
        return preview(self.store, 10, "new", self.names,
                       [lesson(room="ауд. 13"), self.rows[1]] if rows is None else rows,
                       self.day, self.day if scope == "date" else self.end, None, scope=scope,
                       selected=["А-01"] if scope == "date" else [],
                       expected_cancellations=expected, dry_run=dry_run)

    def decision(self, token, confirm=True, actor=10):
        return decide(self.store, 10, actor, token, confirm=confirm)

    def test_dry_run_cancel_and_permissions(self):
        self.assertIsNone(self.draft(dry_run=True)[0])
        self.assertEqual(self.query("SELECT count(*) FROM schedule_previews"), [(0,)])
        token = self.draft()[0]
        self.assertEqual(self.decision(token, actor=11), "forbidden")
        self.assertEqual(self.decision(token, confirm=False), "cancelled")
        self.assertEqual(self.decision(token), "cancelled")
        self.assertEqual(self.query("SELECT count(*) FROM schedule_imports"), [(1,)])
        self.assertEqual(self.query("SELECT count(*) FROM schedule_notifications"), [(0,)])

    def test_concurrent_confirmation_stale_and_repeat_import(self):
        token, changes, _, _ = self.draft()
        other = self.draft()[0]
        self.assertEqual({c["kind"] for c in changes}, {"changed"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(list(pool.map(self.decision, [token, token])), ["applied", "applied"])
        self.assertEqual(self.decision(other), "stale")
        self.assertEqual(self.draft()[:2], (None, []))
        self.assertEqual(self.query("SELECT user_id, status FROM schedule_notifications"), [(101, "pending")])
        self.assertEqual(self.query("SELECT count(DISTINCT id) FROM schedule_changes"), [(2,)])
        self.assertEqual(self.store.registered_group(101), "А-01")

    def test_atomic_rollback_on_outbox_failure(self):
        token = self.draft()[0]
        self.query("ALTER TABLE schedule_notifications ADD CHECK (body = '')")
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.decision(token)
        self.assertEqual(self.query("SELECT count(*) FROM schedule_imports"), [(1,)])
        self.assertEqual(self.query("SELECT count(*) FROM schedule_changes"), [(0,)])
        self.assertEqual(self.query("SELECT status FROM schedule_previews"), [("pending",)])
        self.assertEqual(self.store.schedule(101, [self.day])[0][8], "ауд. 12")

    def test_date_isolation_and_priority_over_later_base(self):
        token = self.draft(scope="date")[0]
        self.assertEqual(self.decision(token), "applied")
        self.assertEqual(self.store.schedule(101, [self.day, self.day + timedelta(days=7)])[0][8], "ауд. 13")
        self.assertEqual(self.store.schedule(101, [self.day + timedelta(days=7)])[0][8], "ауд. 12")
        token, changes, _, _ = self.draft([lesson(room="ауд. 14"), self.rows[1]])
        self.assertEqual([c["visible"] for c in changes], [False, True])
        self.assertEqual(self.decision(token), "applied")
        self.assertEqual(self.store.schedule(101, [self.day])[0][8], "ауд. 13")
        self.assertEqual(self.store.schedule(102, [self.day])[0][8], "ауд. 12")

    def test_rejects_roster_loss_and_requires_exact_cancellation_count(self):
        with self.assertRaisesRegex(ValueError, "Состав групп"):
            preview(self.store, 10, "partial", ["А-01"], [lesson()], self.day, self.end,
                    None, scope="base")
        token, changes, _, cancellations = self.draft([self.rows[1]], scope="date")
        self.assertIsNone(token)
        self.assertEqual(cancellations, 1)
        self.assertEqual(changes[0]["kind"], "cancelled")
        token = self.draft([self.rows[1]], scope="date", expected=1)[0]
        self.assertEqual(self.decision(token), "applied")
        self.assertIsNone(self.store.schedule(101, [self.day])[0][4])

    def test_date_split_uses_confirmed_week_on_repeat(self):
        self.store.seed_legacy("known", self.names, self.rows, self.day, self.end, self.day)
        rows = [lesson(parity="numerator", room="ауд. 13"),
                lesson(parity="denominator", room="ауд. 20"), self.rows[1]]
        token, changes, _, _ = self.draft(rows, scope="date")
        self.assertEqual(len(changes), 1)
        self.assertEqual(self.decision(token), "applied")
        self.assertEqual(self.draft(rows, scope="date")[:2], (None, []))

    def test_upgrade_preserves_an_existing_stage_three_import(self):
        with self.store.connect() as conn:
            remove_homework_fixture(conn)
            conn.execute("DROP TABLE schedule_notification_rate, schedule_notifications, schedule_changes")
            conn.execute("ALTER TABLE schedule_imports DROP COLUMN preview_token, "
                         "DROP COLUMN scope, DROP COLUMN published_at")
            conn.execute("DROP TABLE schedule_previews")
            conn.execute("ALTER TABLE schedule_imports ADD UNIQUE NULLS NOT DISTINCT "
                         "(source_sha256, valid_from, valid_to, numerator_monday)")
            conn.execute("DELETE FROM schema_migrations WHERE version = %s",
                         ("003_schedule_updates.sql",))
        self.assertEqual(migrate(self.store), ["003_schedule_updates.sql", "004_homework.sql", "005_community.sql"])
        self.assertEqual(self.store.registered_group(101), "А-01")
        self.assertEqual(self.store.schedule(101, [self.day])[0][8], "ауд. 12")
        self.assertEqual(self.query("SELECT scope FROM schedule_imports"), [("base",)])

    def test_expired_preview(self):
        token = self.draft()[0]
        self.query("UPDATE schedule_previews SET expires_at = now() - interval '1 second'")
        self.assertEqual(self.decision(token), "expired")

    def test_queue_retry_rate_and_deduplication(self):
        self.decision(self.draft()[0])
        send = Mock(side_effect=[MaxError("429", status=429, retryable=True, retry_after=70), None])
        self.assertTrue(deliver_one(self.store, send, MaxError))
        self.assertEqual(self.query("SELECT status, attempts, last_error FROM schedule_notifications"),
                         [("pending", 1, "http_429")])
        self.assertEqual(self.query("SELECT extract(epoch FROM next_attempt - last_attempt) >= 70 "
                                    "FROM schedule_notifications"), [(True,)])
        self.assertFalse(deliver_one(self.store, send, MaxError))
        self.query("UPDATE schedule_notifications SET next_attempt = now()")
        self.query("UPDATE schedule_notification_rate SET next_send = now()")
        self.assertTrue(deliver_one(self.store, send, MaxError))
        self.assertFalse(deliver_one(self.store, send, MaxError))
        self.assertEqual([call.args[0] for call in send.call_args_list], [{"user_id": 101}] * 2)
        self.assertEqual(self.query("SELECT status, attempts, sent_at IS NOT NULL "
                                    "FROM schedule_notifications"), [("sent", 2, True)])

    def test_queue_terminal_failure_and_abandoned_attempt(self):
        self.decision(self.draft()[0])
        send = Mock(side_effect=MaxError("400", status=400))
        self.assertTrue(deliver_one(self.store, send, MaxError))
        self.assertEqual(self.query("SELECT status, last_error FROM schedule_notifications"),
                         [("failed", "http_400")])
        self.query("UPDATE schedule_notifications SET status = 'sending', attempts = 5, next_attempt = now()")
        self.assertFalse(deliver_one(self.store, send, MaxError))
        self.assertEqual(self.query("SELECT status, last_error FROM schedule_notifications"),
                         [("failed", "unconfirmed")])

    def test_queue_rechecks_group_before_sending(self):
        self.decision(self.draft()[0])
        self.query("UPDATE users SET group_id = NULL WHERE max_user_id = 101")
        send = Mock()
        self.assertTrue(deliver_one(self.store, send, MaxError))
        send.assert_not_called()
        self.assertEqual(self.query("SELECT status FROM schedule_notifications"), [("skipped",)])

    def test_queue_does_not_send_expired_schedule(self):
        self.decision(self.draft()[0])
        self.query("UPDATE schedule_notifications SET valid_through = '2000-01-01'")
        send = Mock()
        self.assertFalse(deliver_one(self.store, send, MaxError))
        send.assert_not_called()
        self.assertEqual(self.query("SELECT status, last_error FROM schedule_notifications"),
                         [("skipped", "expired")])

    @unittest.skipUnless(os.environ.get("AKVT_TEST_XLS"), "Укажи AKVT_TEST_XLS.")
    def test_real_xls_bootstrap_and_unchanged_preview(self):
        digest, groups, rows = parse_xls(os.environ["AKVT_TEST_XLS"])
        self.query("DELETE FROM schedule_lessons; DELETE FROM schedule_groups; DELETE FROM schedule_imports")
        args = (self.store, 10, digest, groups, rows, self.day, self.end, self.day)
        token, changes, _, _ = preview(*args, scope="base", dry_run=False)
        self.assertEqual({c["kind"] for c in changes}, {"added"})
        self.assertEqual(self.decision(token), "applied")
        self.assertEqual(self.query("SELECT count(*) FROM schedule_lessons"), [(672,)])
        self.assertEqual(preview(*args, scope="base", dry_run=False)[:2], (None, []))
