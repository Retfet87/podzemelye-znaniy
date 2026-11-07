from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Event
import time
import unittest
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg.pq import TransactionStatus
from psycopg_pool import PoolTimeout

from app.db import Database, migrate
from app.demo import DemoDatabase
from app.db.locks import NOTIFICATION_WORKER_LOCK, HOMEWORK_LOCK, EVENT_LOCK
from app.handlers.community import Draft
from app.max_api.client import MaxError
from app.notifications.worker import deliver_one
from app.repositories.community import CommunityStore
from app.repositories.users import UserRepository
from app.repositories.homework import HomeworkStore
from app.handlers.homework import Draft as HomeworkDraft
from app.services.schedule_parser import Lesson
from app.services.schedule_publisher import preview, decide
from app.timeutil import SAMARA
from test_db import TEST_URL


class PoolConfigTests(unittest.TestCase):
    def test_opt_in_and_invalid_limits(self):
        config = {'DATABASE_URL':'postgresql://test@127.0.0.1/example_test'}
        self.assertIsNone(Database.from_config(config)._pool)
        for value in ('-1','17','1.5','secret','999'):
            with self.subTest(value=value), self.assertRaises(ValueError) as failure:
                Database.from_config(config | {'DB_POOL_MAX_SIZE':value})
            self.assertNotIn('secret',str(failure.exception))


    def test_demo_adapter_cannot_accidentally_enable_pool(self):
        with self.assertRaises(ValueError):
            DemoDatabase('postgresql://test@127.0.0.1/podzemelye_demo', pool_size=1)


@unittest.skipUnless(TEST_URL, 'Нужна отдельная тестовая PostgreSQL.')
class PoolTests(unittest.TestCase):
    def setUp(self):
        self.name = 'test_pool_' + uuid4().hex
        self.control = self.enterContext(Database(TEST_URL).connect())
        self.control.autocommit = True
        self.control.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.name)))
        self.addCleanup(self.control.execute, sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.name)))
        self.dsn = make_conninfo(TEST_URL, options=f'-c search_path={self.name},public',
                                 application_name=self.name)
        self.raw = Database(self.dsn)
        migrate(self.raw)
        self.pool = self.enterContext(Database(self.dsn, pool_size=1, pool_timeout=.15))
        with self.raw.connect() as conn:
            conn.execute('CREATE TABLE records (id integer PRIMARY KEY)')

    def assert_unlocked(self):
        with self.pool.connect() as conn:
            self.assertEqual(conn.info.transaction_status, TransactionStatus.IDLE)
            self.assertEqual(conn.execute("SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'").fetchone(), (0,))
        with self.raw.connect() as conn:
            for key in (NOTIFICATION_WORKER_LOCK, HOMEWORK_LOCK, EVENT_LOCK):
                self.assertTrue(conn.execute('SELECT pg_try_advisory_lock(%s)',(key,)).fetchone()[0])
                conn.execute('SELECT pg_advisory_unlock(%s)',(key,))

    def test_commit_rollback_and_sql_error_reuse(self):
        with self.pool.connect() as conn:
            pid = conn.info.backend_pid
            conn.execute('INSERT INTO records VALUES (1)')
        with self.assertRaises(RuntimeError):
            with self.pool.connect() as conn:
                conn.execute('INSERT INTO records VALUES (2)')
                raise RuntimeError('rollback')
        with self.assertRaises(psycopg.errors.DivisionByZero):
            with self.pool.connect() as conn:
                conn.execute('SELECT 1/0')
        with self.pool.connect() as conn:
            self.assertEqual(conn.info.backend_pid,pid)
            self.assertEqual(conn.info.transaction_status,TransactionStatus.IDLE)
            self.assertEqual(conn.execute('SELECT id FROM records').fetchall(),[(1,)])

    def test_session_locks_released_after_success_and_exception_across_commit(self):
        for fail in (False,True):
            try:
                with self.pool.connect() as conn:
                    for key in (NOTIFICATION_WORKER_LOCK,HOMEWORK_LOCK,EVENT_LOCK):
                        conn.execute('SELECT pg_advisory_lock(%s)',(key,))
                        conn.execute('SELECT pg_advisory_lock(%s)',(key,))
                    conn.commit()
                    if fail:
                        raise RuntimeError('after commit')
            except RuntimeError:
                pass
            self.assert_unlocked()

    def test_session_settings_and_temp_tables_do_not_leak(self):
        with self.pool.connect() as conn:
            original=conn.execute('SHOW search_path').fetchone()
            conn.execute('CREATE TEMP TABLE private_temp(id int)')
            conn.execute('SET search_path=pg_catalog')
        with self.pool.connect() as conn:
            self.assertEqual(conn.execute('SHOW search_path').fetchone(),original)
            self.assertIsNone(conn.execute("SELECT to_regclass('pg_temp.private_temp')").fetchone()[0])
            self.assertIsNone(conn.prepare_threshold)

    def test_closed_connection_is_replaced(self):
        with self.pool.connect() as conn:
            pid=conn.info.backend_pid
            conn.close()
        with self.pool.connect() as conn:
            self.assertNotEqual(conn.info.backend_pid,pid)
            self.assertEqual(conn.execute('SELECT 1').fetchone(),(1,))

    def test_timeout_does_not_leak_borrowed_connections(self):
        with self.pool.connect() as conn:
            with self.assertRaises(PoolTimeout):
                with self.pool.connect():
                    self.fail('pool exceeded its bound')
            conn.execute('SELECT 1')
        self.assert_unlocked()

    def test_reset_failure_discards_session_and_its_locks(self):
        reset_attempted = Event()
        def fail(conn):
            reset_attempted.set()
            raise RuntimeError('reset failed')
        with patch.object(Database, '_reset', staticmethod(fail)):
            db = self.enterContext(Database(self.dsn, pool_size=1))
        with db.connect() as conn:
            pid = conn.info.backend_pid
            conn.execute('SELECT pg_advisory_lock(%s)', (EVENT_LOCK,))
        self.assertTrue(reset_attempted.wait(2))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not self.control.execute('SELECT count(*) FROM pg_stat_activity WHERE pid=%s', (pid,)).fetchone()[0]:
                break
            time.sleep(.01)
        self.assertEqual(self.control.execute('SELECT count(*) FROM pg_stat_activity WHERE pid=%s', (pid,)).fetchone()[0], 0)
        self.assert_unlocked()

    def fixture_event(self):
        users=UserRepository(self.raw)
        users.add_groups(['POOL-01'])
        gid=users.active_groups()[0][0]
        users.start(11)
        users.choose_group(11,gid)
        store=CommunityStore(self.raw,90,90)
        event=store.save_event(Draft('event',title='Тест',body='',starts_at=datetime.now(SAMARA)+timedelta(hours=23)))
        self.assertTrue(store.transition(event,1,True))
        self.assertTrue(CommunityStore(self.raw,11,90).attend(event,True))

    def test_notification_send_holds_lock_through_commit_and_releases_it(self):
        self.fixture_event()
        sent=[]
        def send(recipient,body):
            with self.raw.connect() as conn:
                self.assertFalse(conn.execute('SELECT pg_try_advisory_lock(%s)',(EVENT_LOCK,)).fetchone()[0])
            sent.append(body)
        self.assertTrue(deliver_one(self.pool,send,MaxError,homework=False,schedule=False,events=True))
        self.assertEqual(len(sent),1)
        self.assert_unlocked()
        self.assertFalse(deliver_one(self.pool,send,MaxError,homework=False,schedule=False,events=True))
        self.assert_unlocked()

    def test_unexpected_send_failure_after_commit_does_not_leak_locks(self):
        self.fixture_event()
        def fail(*args):
            raise RuntimeError('sender failed')
        with self.assertRaises(RuntimeError):
            deliver_one(self.pool,fail,MaxError,homework=False,schedule=False,events=True)
        self.assert_unlocked()
        with self.raw.connect() as conn:
            self.assertEqual(conn.execute('SELECT status FROM event_notifications').fetchall(),[('sending',)])

    def test_two_worker_pools_do_not_send_same_job(self):
        self.fixture_event()
        other=self.enterContext(Database(self.dsn,pool_size=1))
        entered=Event()
        release=Event()
        sent=[]
        def send(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test timeout')
            sent.append(1)
        with ThreadPoolExecutor(max_workers=2) as executor:
            first=executor.submit(deliver_one,self.pool,send,MaxError,homework=False,schedule=False,events=True)
            try:
                self.assertTrue(entered.wait(3))
                self.assertFalse(deliver_one(other,send,MaxError,homework=False,schedule=False,events=True))
            finally:
                release.set()
            self.assertTrue(first.result(timeout=3))
        self.assertEqual(sent,[1])
        self.assert_unlocked()

    def test_concurrency_bound_and_shutdown(self):
        db=Database(self.dsn,pool_size=4)
        def read(_):
            with db.connect() as conn:
                conn.execute('SELECT pg_sleep(0.002)')
                return conn.info.backend_pid
        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                pids=set(executor.map(read,range(100)))
            self.assertLessEqual(len(pids),4)
        finally:
            db.close()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            count=self.control.execute('SELECT count(*) FROM pg_stat_activity WHERE pid=ANY(%s)',(list(pids),)).fetchone()[0]
            if not count:
                break
            time.sleep(.01)
        self.assertEqual(count,0)


    def test_migration_and_repository_writes_use_pool_without_nested_checkouts(self):
        extra=self.name+'_fresh'
        self.control.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(extra)))
        self.addCleanup(self.control.execute,sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(extra)))
        db=self.enterContext(Database(make_conninfo(self.dsn,options=f'-c search_path={extra},public'),pool_size=1))
        self.assertEqual(len(migrate(db)),6)
        self.assertEqual(migrate(db),[])
        db.check_schema()
        monday=datetime.now(SAMARA).date()
        monday-=timedelta(days=monday.weekday())
        lesson=Lesson('POOL-01',0,1,'all','Предмет','','','',())
        token,*_=preview(db,90,'pool-fixture',['POOL-01'],[lesson],monday,monday+timedelta(days=13),monday,
                         scope='base',dry_run=False)
        self.assertEqual(decide(db,90,90,token,confirm=True),'applied')
        users=UserRepository(db)
        gid=users.active_groups()[0][0]
        users.start(11)
        self.assertEqual(users.choose_group(11,gid),('saved','POOL-01'))
        tasks=HomeworkStore(db,11,90)
        task=tasks.save(HomeworkDraft(gid,False,subject='Предмет',body='Задание',files=[('a.txt',b'attachment')]))
        self.assertIsNotNone(tasks.get(task))
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT data FROM homework_files WHERE homework_id=%s',(task,)).fetchone(),(b'attachment',))
        material=CommunityStore(db,90,90).save_material(Draft('material',category='study',title='Пример',body='Описание',files=[('a.txt',b'material')]))
        self.assertEqual(CommunityStore(db,11,90).file(material,1,1),('a.txt',b'material'))

    def test_terminated_backend_is_replaced_before_next_query(self):
        with self.pool.connect() as conn:
            pid=conn.info.backend_pid
        self.assertTrue(self.control.execute('SELECT pg_terminate_backend(%s)',(pid,)).fetchone()[0])
        with self.pool.connect() as conn:
            self.assertNotEqual(conn.info.backend_pid,pid)
            self.assertEqual(conn.execute('SELECT 1').fetchone(),(1,))
