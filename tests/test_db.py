import os
from dataclasses import asdict
from datetime import date
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from psycopg import sql
from psycopg.conninfo import make_conninfo

from psycopg.errors import UniqueViolation
from db import RegistrationStore, local_dsn, migrate
from main import load_config
from schedule_import import Lesson, parse_xls

TEST_URL = os.environ.get("TEST_DATABASE_URL") or load_config().get("TEST_DATABASE_URL")


class DatabaseConfigTests(unittest.TestCase):
    def test_only_explicit_loopback_is_allowed(self):
        allowed = "postgresql://user:secret@127.0.0.1/podzemelye"
        self.assertEqual(local_dsn({"DATABASE_URL": allowed}), allowed)
        for value in ("", "postgresql://user:secret@remote/podzemelye",
                      allowed + "?hostaddr=192.0.2.1", allowed + "?service=remote"):
            with self.assertRaises(ValueError) as failure:
                local_dsn({"DATABASE_URL": value})
            self.assertNotIn("secret", str(failure.exception))


class LegacyFixtureStore(RegistrationStore):
    def seed_legacy(self, digest, groups, lessons, valid_from, valid_to, numerator_monday):
        with self.connect() as conn, conn.cursor() as cur:
            conn.execute("SELECT pg_advisory_xact_lock(281952765)")
            row = conn.execute(
                """INSERT INTO schedule_imports
                       (source_sha256, valid_from, valid_to, numerator_monday)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT DO NOTHING RETURNING id""",
                (digest, valid_from, valid_to, numerator_monday),
            ).fetchone()
            if row is None:
                return False
            version = row[0]
            self.save_lessons(cur, version, groups, [asdict(lesson) for lesson in lessons])
            return True

@unittest.skipUnless(TEST_URL, "Укажи TEST_DATABASE_URL для проверки на PostgreSQL.")
class PostgreSQLTests(unittest.TestCase):
    def setUp(self):
        dsn = local_dsn({"TEST_DATABASE_URL": TEST_URL}, "TEST_DATABASE_URL")
        schema_name = "test_registration_" + uuid4().hex
        schema = sql.Identifier(schema_name)
        conn = self.enterContext(RegistrationStore(dsn).connect())
        conn.autocommit = True
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(schema))
        self.addCleanup(conn.execute, sql.SQL("DROP SCHEMA {} CASCADE").format(schema))
        self.store = LegacyFixtureStore(make_conninfo(dsn, options=f"-c search_path={schema_name}"))
        self.assertEqual(migrate(self.store), ["001_registration.sql", "002_schedule.sql", "003_schedule_updates.sql"])
        self.assertEqual(migrate(self.store), [])
        self.store.check_schema()
        self.store.add_groups(["А-01", "А-02"])
        self.store.add_groups(["А-01"])
        self.groups = {name: id_ for id_, name in self.store.active_groups()}

    def test_registration_persists_and_cannot_be_overwritten(self):
        first = self.groups["А-01"]
        second = self.groups["А-02"]
        self.assertEqual(self.store.choose_group(100, first), ("unavailable", None))
        self.assertIsNone(self.store.start(100))
        self.assertIsNone(self.store.start(100))
        self.assertEqual(self.store.choose_group(100, first), ("saved", "А-01"))
        reopened = RegistrationStore(self.store.dsn)
        self.assertEqual(reopened.start(100), ("А-01",))
        self.assertEqual(reopened.choose_group(100, second), ("existing", "А-01"))
        with reopened.connect() as conn:
            self.assertEqual(conn.execute("SELECT max_user_id, role_code FROM users").fetchall(),
                             [(100, "student")])

    def test_migration_preserves_registration_from_stage_two(self):
        with self.store.connect() as conn:
            conn.execute("DROP TABLE schedule_notification_rate, schedule_notifications, "
                         "schedule_changes, schedule_previews, schedule_lessons, "
                         "schedule_groups, schedule_imports")
            conn.execute("DELETE FROM schema_migrations WHERE version <> '001_registration.sql'")
        self.store.start(123)
        self.store.choose_group(123, self.groups["А-01"])
        self.assertEqual(migrate(self.store), ["002_schedule.sql", "003_schedule_updates.sql"])
        self.assertEqual(self.store.start(123), ("А-01",))

    def test_import_is_idempotent_group_scoped_and_atomic(self):
        day = date(2026, 11, 2)
        lesson = Lesson("А-01", 0, 1, "all", "Физика", "", "", "", ("Физика",))
        self.store.start(123)
        self.store.choose_group(123, self.groups["А-01"])
        self.store.start(124)
        self.store.choose_group(124, self.groups["А-02"])
        args = ("first", ["А-01"], [lesson], day, day, None)
        self.assertTrue(self.store.seed_legacy(*args))
        self.assertFalse(self.store.seed_legacy(*args))
        self.assertEqual(self.store.schedule(123, [day])[0][6], "Физика")
        self.assertIsNone(self.store.schedule(124, [day])[0][2])
        with self.assertRaises(UniqueViolation):
            self.store.seed_legacy("bad", ["А-01", "НОВ-11"], [lesson, lesson], day, day, None)
        self.assertNotIn("НОВ-11", dict((name, id_) for id_, name in self.store.active_groups()))
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT source_sha256 FROM schedule_imports").fetchall(),
                             [("first",)])
        self.assertTrue(self.store.seed_legacy("replacement", ["А-01"], [], day, day, None))
        self.assertIsNone(self.store.schedule(123, [day])[0][4])
        self.assertEqual(self.store.start(123), ("А-01",))

    @unittest.skipUnless(os.environ.get("AKVT_TEST_XLS"), "Укажи AKVT_TEST_XLS для проверки реального XLS.")
    def test_import_real_xls(self):
        digest, names, lessons = parse_xls(Path(os.environ["AKVT_TEST_XLS"]))
        day = date(2026, 11, 2)
        self.assertTrue(self.store.seed_legacy(digest, names, lessons, day, day, None))
        self.assertFalse(self.store.seed_legacy(digest, names, lessons, day, day, None))
        groups = {name: id_ for id_, name in self.store.active_groups()}
        self.store.start(123)
        self.store.choose_group(123, groups["ИБ-11"])
        rows = self.store.schedule(123, [day])
        self.assertEqual({row[6] for row in rows if row[4] == 4}, {"Биология", "Литература"})
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM schedule_lessons").fetchone(), (672,))

    def test_inactive_or_unknown_group_is_not_saved(self):
        first = self.groups["А-01"]
        with self.store.connect() as conn:
            conn.execute("UPDATE groups SET active = false WHERE id = %s", (first,))
        self.store.start(100)
        self.assertEqual(self.store.choose_group(100, first), ("unavailable", None))
        self.assertEqual(self.store.choose_group(100, 999), ("unavailable", None))
        self.assertIsNone(self.store.start(100))
        self.assertNotIn((first, "А-01"), self.store.active_groups())

    def test_simultaneous_clicks_save_only_one_group(self):
        self.store.start(100)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda group: self.store.choose_group(100, group),
                                    self.groups.values()))
        self.assertEqual({status for status, _ in results}, {"saved", "existing"})
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(self.store.start(100), (results[0][1],))
