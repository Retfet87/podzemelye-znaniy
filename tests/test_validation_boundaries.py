"""Real PostgreSQL and loopback HTTP boundaries. No MAX HTTP transport is used."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
import http.client
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch
from uuid import uuid4
from wsgiref.simple_server import make_server

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo,conninfo_to_dict

from app.db import migrate
from app.demo import DemoDatabase,DemoPolicy,demo_database
from app.demo_seed import seed_useful,clean_useful,FIXTURES
from app.notifications.worker import deliver_one
from app.repositories.homework import HomeworkStore
from app.timeutil import SAMARA
from app.web import MiniApp,ReadViews
from mini_app import QuietHandler
from seed_demo import seed,clean
from test_db import TEST_URL
import test_community as fixtures
from test_web import signed,TOKEN

DEMO_URL=os.environ.get('VALIDATION_DEMO_DATABASE_URL')


@unittest.skipUnless(DEMO_URL,'Нужна отдельная provisioned VALIDATION_DEMO_DATABASE_URL.')
class DemoRoleTests(unittest.TestCase):
    def setUp(self):
        # The separate validation database marker must already be bootstrap-owned.
        with patch.dict(os.environ,{'DEMO_DATABASE_URL':DEMO_URL},clear=True):
            self.control=demo_database()
        values=conninfo_to_dict(DEMO_URL)
        self.assertEqual((values['dbname'],values['user']),('podzemelye_demo','demo_app'))
        # demo_app is intentionally not allowed to CREATE SCHEMA; use a unique
        # table namespace via public table transactions instead of elevated setup.
        # A dedicated fresh demo database is reserved for this class only.
        self.db=self.control
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT environment FROM public.validation_environment').fetchall(),[('podzemelye-validation-v1',)])
        migrate(self.db)
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM demo_records').fetchone(),(0,))
        self.addCleanup(self.remove_own_records)
        self.foreign_ids=[]

    def remove_own_records(self):
        with self.db.connect() as conn:
            clean(conn)
            for item in self.foreign_ids:
                conn.execute('DELETE FROM useful_materials WHERE id=%s',(item,))
            conn.execute("DELETE FROM schedule_lessons WHERE import_id IN (SELECT id FROM schedule_imports WHERE source_sha256='validation-fixture')")
            conn.execute("DELETE FROM schedule_groups WHERE import_id IN (SELECT id FROM schedule_imports WHERE source_sha256='validation-fixture')")
            conn.execute("DELETE FROM schedule_imports WHERE source_sha256='validation-fixture'")
            conn.execute("DELETE FROM groups WHERE name='VALIDATION-DEMO-01'")

    def test_role_cannot_modify_marker_escalate_or_connect_other_databases(self):
        for statement in ("UPDATE public.demo_environment SET environment='forged'",'DROP TABLE public.demo_environment','CREATE ROLE forbidden_role'):
            with self.subTest(statement=statement),self.assertRaises(psycopg.errors.InsufficientPrivilege):
                with self.db.connect() as conn:
                    conn.execute(statement)
        with self.db.connect() as conn:
            conn.commit()
            conn.autocommit=True
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute('CREATE DATABASE forbidden_database')
        for name in ('postgres','validation_test'):
            with self.subTest(database=name),self.assertRaises(psycopg.OperationalError):
                with psycopg.connect(make_conninfo(DEMO_URL,dbname=name)):
                    pass

    def test_seed_and_cleanup_preserve_foreign_material_attachment_and_ids(self):
        card=json.loads(FIXTURES.read_text())[0]
        with self.db.connect() as conn:
            foreign=conn.execute("INSERT INTO useful_materials(category,title,body,link) VALUES ('curators',%s,'Unmarked foreign record','') RETURNING id",(card['title'],)).fetchone()[0]
            self.foreign_ids.append(foreign)
            conn.execute("INSERT INTO useful_files VALUES (%s,1,'foreign.txt',%s)",(foreign,b'foreign-attachment'))
            self.assertEqual(seed_useful(conn),32)
        with self.db.connect() as conn:
            before=conn.execute('SELECT * FROM demo_records ORDER BY kind,record_id').fetchall()
            self.assertEqual(seed_useful(conn),0)
            self.assertEqual(conn.execute('SELECT * FROM demo_records ORDER BY kind,record_id').fetchall(),before)
            self.assertEqual(conn.execute("SELECT count(*) FROM useful_materials WHERE title LIKE 'ДЕМО%' AND body LIKE 'ДЕМО%'").fetchone(),(32,))
            clean_useful(conn)
            clean_useful(conn)
            self.assertEqual(conn.execute('SELECT title,body FROM useful_materials WHERE id=%s',(foreign,)).fetchone(),(card['title'],'Unmarked foreign record'))
            self.assertEqual(bytes(conn.execute('SELECT data FROM useful_files WHERE material_id=%s',(foreign,)).fetchone()[0]),b'foreign-attachment')

    def test_mid_seed_failure_rolls_back_records_markers_and_files(self):
        from app.services.validation import validate_link
        attempts=0
        def fail_late(value):
            nonlocal attempts
            attempts+=1
            if attempts==21:
                raise ValueError('injected fixture failure')
            return validate_link(value)
        with self.assertRaisesRegex(ValueError,'injected'):
            with self.db.connect() as conn,patch('app.demo_seed.validate_link',side_effect=fail_late):
                seed_useful(conn)
        with self.db.connect() as conn:
            for table in ('useful_materials','useful_files','demo_records'):
                self.assertEqual(conn.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone(),(0,))

    def test_two_concurrent_seeds_have_one_stable_set(self):
        def apply(_):
            with self.db.connect() as conn:
                return seed_useful(conn)
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(apply,range(2))),[0,32])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM demo_records').fetchone(),(32,))
            self.assertEqual(conn.execute('SELECT count(*) FROM demo_fixture_keys').fetchone(),(32,))

    def test_full_seed_no_delivery_even_after_rsvp_and_misconfigured_worker(self):
        now=datetime.now(SAMARA).date()
        monday=now-timedelta(days=now.weekday())
        with self.db.connect() as conn:
            group=conn.execute("INSERT INTO groups(name) VALUES ('VALIDATION-DEMO-01') RETURNING id").fetchone()[0]
            version=conn.execute("INSERT INTO schedule_imports(source_sha256,valid_from,valid_to,numerator_monday) VALUES ('validation-fixture',%s,%s,%s) RETURNING id",(monday,monday+timedelta(days=13),monday)).fetchone()[0]
            conn.execute('INSERT INTO schedule_groups VALUES (%s,%s)',(version,group))
            conn.execute("INSERT INTO schedule_lessons VALUES (%s,%s,0,1,'all','ДЕМО-предмет','','','',ARRAY[]::text[])",(version,group))
        self.assertTrue(seed(self.db))
        self.assertFalse(seed(self.db))
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM demo_records').fetchone(),(39,))
            for table in ('event_notifications','homework_notifications','schedule_notifications'):
                self.assertEqual(conn.execute(sql.SQL('SELECT count(*) FROM {}').format(sql.Identifier(table))).fetchone(),(0,))
        from app.repositories.community import CommunityStore
        from seed_demo import DEMO_USER
        with self.db.connect() as conn:
            event=conn.execute("SELECT id FROM events WHERE starts_at>now() ORDER BY id LIMIT 1").fetchone()[0]
        self.assertTrue(CommunityStore(self.db,DEMO_USER,None).attend(event,True))
        def forbidden_sender(*args):
            self.fail('Demo worker attempted external delivery')
        self.assertFalse(deliver_one(self.db,forbidden_sender,RuntimeError,events=True,homework=True,schedule=True))
        with self.db.connect() as conn:
            clean(conn)
            self.assertEqual(conn.execute('SELECT count(*) FROM schedule_lessons').fetchone(),(1,))


@unittest.skipUnless(TEST_URL,'Нужна disposable TEST_DATABASE_URL.')
class LiveHTTPTests(unittest.TestCase):
    setUp=fixtures.CommunityTests.setUp
    query=fixtures.CommunityTests.query

    def start_server(self,policy=None):
        app=MiniApp(ReadViews(self.store),TOKEN,demo_policy=policy)
        server=make_server('127.0.0.1',0,app,handler_class=QuietHandler)
        thread=Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        thread.start()
        def stop():
            server.shutdown();server.server_close();thread.join(timeout=5)
        self.addCleanup(stop)
        self.port=server.server_port

    def request(self,path='/api/homework',actor=11,raw=None,method='GET'):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=5)
        try:
            conn.request(method,path,headers={'X-Max-Init-Data':signed(actor=actor) if raw is None else raw})
            response=conn.getresponse()
            return response.status,dict(response.getheaders()),response.read()
        finally:
            conn.close()

    def test_direct_http_auth_scope_and_private_file_non_exposure(self):
        for group,owner,body in ((self.a,11,'private-A'),(self.a,None,'shared-A'),(self.b,12,'private-B'),(self.b,None,'shared-B')):
            task=self.query("INSERT INTO homework(group_id,owner_id,subject,body) VALUES (%s,%s,'Subject',%s) RETURNING id",(group,owner,body))[0][0]
            self.query("INSERT INTO homework_files VALUES (%s,1,'private.txt',%s)",(task,b'private-file-sentinel'))
        self.start_server()
        self.assertEqual(self.request(raw='')[0],401)
        self.assertEqual(self.request(raw=signed().replace('test-launch','forged'))[0],401)
        self.assertEqual(self.request(raw=signed(stamp=1))[0],401)
        for actor,own,foreign in ((11,'private-A','private-B'),(12,'private-B','private-A')):
            status,headers,body=self.request(actor=actor)
            self.assertEqual(status,200)
            self.assertIn(own.encode(),body)
            self.assertNotIn(foreign.encode(),body)
            self.assertNotIn(b'private-file-sentinel',body)
            self.assertEqual(headers['Cache-Control'],'no-store')
        self.assertEqual(self.request('/api/homework?user_id=12')[0],400)
        for path in ('/api/files/1','/.env','/fixtures/demo/useful.json'):
            self.assertEqual(self.request(path)[0],404)
        self.assertEqual(self.request(method='POST')[0],405)
        _,_,body=self.request(actor=13)
        self.assertIsNone(json.loads(body)['group'])

    def test_demo_hot_disable_and_foreign_actor_http(self):
        folder=self.enterContext(tempfile.TemporaryDirectory())
        switch=Path(folder)/'enabled';switch.write_text('1\n')
        before=self.query('SELECT max_user_id,group_id FROM users ORDER BY max_user_id')
        self.start_server(DemoPolicy(frozenset({11}),switch,True))
        status,_,body=self.request()
        self.assertEqual(status,200)
        self.assertTrue(json.loads(body)['demo'])
        self.assertEqual(self.request(actor=12)[0],403)
        switch.write_text('0\n')
        status,_,body=self.request()
        self.assertEqual(status,403)
        self.assertNotIn('А-01'.encode(),body)
        self.assertNotIn(b'items',body)
        self.assertEqual(self.query('SELECT max_user_id,group_id FROM users ORDER BY max_user_id'),before)

    def test_static_assets_have_no_server_secret_and_no_file_endpoint(self):
        self.start_server()
        for path in ('/','/app.js','/style.css'):
            status,_,body=self.request(path,raw='')
            self.assertEqual(status,200)
            self.assertNotIn(TOKEN.encode(),body)
        self.assertEqual(self.request('/api/schedule',raw='')[0],401)
        self.assertEqual(self.request('/api/events',raw='')[0],401)


@unittest.skipUnless(TEST_URL,'Нужна disposable TEST_DATABASE_URL.')
class SchemaCopyUpgradeTests(unittest.TestCase):
    def test_main_schema_copy_upgrade_preserves_existing_data_and_files(self):
        from app.db import Database
        from app.db.database import MIGRATIONS
        schema='validation_main_'+uuid4().hex
        with Database(TEST_URL).connect() as conn:
            conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        def cleanup():
            with Database(TEST_URL).connect() as conn:
                conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))
        self.addCleanup(cleanup)
        db=Database(make_conninfo(TEST_URL,options=f'-c search_path={schema}'))
        # Files 001–005 were verified identical to main=459cc1c during audit.
        with db.connect() as conn:
            conn.execute('CREATE TABLE schema_migrations(version text PRIMARY KEY,applied_at timestamptz NOT NULL DEFAULT now())')
            for path in sorted(MIGRATIONS.glob('*.sql')):
                if path.name >= '006':
                    break
                conn.execute(path.read_text(),prepare=False)
                conn.execute('INSERT INTO schema_migrations(version) VALUES (%s)',(path.name,))
            gid=conn.execute("INSERT INTO groups(name) VALUES ('SCHEMA-COPY-01') RETURNING id").fetchone()[0]
            conn.execute('INSERT INTO users(max_user_id,group_id) VALUES (123456,%s)',(gid,))
            task=conn.execute("INSERT INTO homework(group_id,owner_id,subject,body) VALUES (%s,123456,'Subject','Preserve body') RETURNING id",(gid,)).fetchone()[0]
            conn.execute("INSERT INTO homework_files VALUES (%s,1,'keep.txt',%s)",(task,b'keep-old-homework'))
            material=conn.execute("INSERT INTO useful_materials(category,title,body,link) VALUES ('study','Keep title','Keep body','') RETURNING id").fetchone()[0]
            conn.execute("INSERT INTO useful_files VALUES (%s,1,'keep.txt',%s)",(material,b'keep-old-material'))
        self.assertEqual(migrate(db),['006_ux.sql','007_form_drafts.sql'])
        self.assertEqual(migrate(db),[])
        db.check_schema()
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT max_user_id,group_id,institution_code,welcome_seen FROM users').fetchone(),(123456,gid,'akvt',True))
            self.assertEqual(conn.execute('SELECT name FROM groups').fetchone(),('SCHEMA-COPY-01',))
            self.assertEqual(bytes(conn.execute('SELECT data FROM homework_files').fetchone()[0]),b'keep-old-homework')
            self.assertEqual(bytes(conn.execute('SELECT data FROM useful_files').fetchone()[0]),b'keep-old-material')
            self.assertEqual(conn.execute('SELECT body FROM homework').fetchone(),('Preserve body',))
