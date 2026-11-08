import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from contextlib import nullcontext, redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.db import Database, migrate
from scripts import mac_runtime as mac
from test_db import TEST_URL


CONFIG = {'MAX_BOT_TOKEN':'offline-test-token','DATABASE_URL':'postgresql://test:private@127.0.0.1/example_test'}


class MacLaunchTests(unittest.TestCase):
    def test_machine_lock_blocks_second_checkout_and_releases(self):
        token = 'test-' + uuid4().hex
        with mac.machine_lock(token):
            with self.assertRaisesRegex(ValueError,'уже запущен'):
                with mac.machine_lock(token):
                    self.fail('Second lock must fail')
        with mac.machine_lock(token):
            pass

    def test_legacy_process_detection_without_exposing_arguments(self):
        output = '123 /bin/python /bin/python /old/main.py secret\n124 /bin/python /bin/python -m demo_bot\n125 /bin/python /bin/python unrelated.py\n'
        with patch.object(mac.subprocess,'run',return_value=Mock(stdout=output)):
            self.assertEqual(mac.local_pollers(),[123,124])

    def test_process_inventory_failure_is_closed(self):
        with patch.object(mac.subprocess,'run',side_effect=subprocess.CalledProcessError(1,'ps')):
            with self.assertRaises(subprocess.CalledProcessError):mac.local_pollers()

    def test_missing_token_stops_without_network(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(mac,'PROJECT_DIR',Path(folder)), patch.dict(mac.os.environ,{},clear=True):
            p=Path(folder)/'.env';p.write_text('DATABASE_URL=private');p.chmod(0o600)
            with self.assertRaisesRegex(ValueError,'MAX_BOT_TOKEN'):mac.configuration()

    def test_readable_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(mac,'PROJECT_DIR',Path(folder)), patch.dict(mac.os.environ,{},clear=True):
            p=Path(folder)/'.env';p.write_text('MAX_BOT_TOKEN=private');p.chmod(0o644)
            with self.assertRaisesRegex(ValueError,'600'):mac.configuration()

    def test_invalid_utf8_does_not_echo_file_contents(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(mac,'PROJECT_DIR',Path(folder)), patch.dict(mac.os.environ,{},clear=True):
            p=Path(folder)/'.env';p.write_bytes(b'MAX_BOT_TOKEN=private\xff');p.chmod(0o600)
            with self.assertRaises(ValueError) as caught:mac.configuration()
            self.assertNotIn('private',str(caught.exception))

    def test_start_without_backup_never_creates_bot(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(mac,'PROJECT_DIR',Path(folder)), patch.object(mac,'configuration',return_value=CONFIG), patch.object(mac,'preflight'), patch.object(mac,'machine_lock',return_value=nullcontext()), patch('app.bootstrap.main') as bot, patch.object(mac.sys,'argv',['launcher','start']), redirect_stderr(io.StringIO()):
            self.assertEqual(mac.main(),1);bot.assert_not_called()

    def test_stop_and_restart_of_guarded_entrypoint_without_max(self):
        with patch.object(mac,'configuration',return_value=CONFIG), patch.object(mac,'preflight'), patch.object(mac,'require_backup'), patch('app.bootstrap.main',return_value=0) as bot, patch.object(mac.sys,'argv',['launcher','start']), redirect_stdout(io.StringIO()):
            self.assertEqual(mac.main(),0);self.assertEqual(mac.main(),0)
            self.assertEqual(bot.call_count,2)

    def test_preflight_never_runs_bot_or_creates_backup(self):
        with patch.object(mac,'configuration',return_value=CONFIG), patch.object(mac,'preflight') as check, patch('app.bootstrap.main') as bot, patch.object(mac,'backup') as backup, patch.object(mac.sys,'argv',['launcher','preflight']):
            self.assertEqual(mac.main(),0);check.assert_called_once();bot.assert_not_called();backup.assert_not_called()

    def test_backup_integrity_age_and_database_scope(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(mac,'PROJECT_DIR',Path(folder)):
            d=Path(folder)/'backups/mac';d.mkdir(parents=True);p=d/'backup.dump';p.write_bytes(b'archive')
            data={'file':p.name,'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'created':time.time(),'database':mac.backup_identity(CONFIG)}
            manifest=d/'backup.dump.json';manifest.write_text(json.dumps(data));mac.require_backup(CONFIG)
            p.write_bytes(b'corrupt')
            with self.assertRaises(ValueError):mac.require_backup(CONFIG)
            p.write_bytes(b'archive')
            for bad in ({'created':time.time()-90000},{'database':'other'},{'file':'../outside'}):
                manifest.write_text(json.dumps(data|bad))
                with self.assertRaises(ValueError):mac.require_backup(CONFIG)

    def test_unavailable_postgresql_is_redacted_and_no_bot_started(self):
        config=CONFIG|{'DATABASE_URL':'postgresql://test:never-print-this@127.0.0.1:1/unavailable_test'}
        output=io.StringIO()
        db=Mock();db.connect.side_effect=psycopg.OperationalError('never-print-this')
        with patch.object(mac.Database,'from_config',return_value=db), patch.object(mac,'configuration',return_value=config), patch.object(mac,'preflight',side_effect=lambda c:mac.database_check(c)), patch.object(mac.sys,'argv',['launcher','start']), patch('app.bootstrap.main') as bot, redirect_stderr(output):
            self.assertEqual(mac.main(),1);bot.assert_not_called()
        self.assertNotIn('never-print-this',output.getvalue());self.assertNotIn('postgresql://',output.getvalue())


@unittest.skipUnless(TEST_URL,'Нужна отдельная тестовая PostgreSQL.')
class MacDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.name='test_mac_'+uuid4().hex
        self.control=self.enterContext(Database(TEST_URL).connect());self.control.autocommit=True
        self.control.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.name)))
        self.addCleanup(self.control.execute,sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.name)))
        self.db=Database(make_conninfo(TEST_URL,options=f'-c search_path={self.name},public'))
        migrate(self.db)
        self.config=CONFIG|{'DATABASE_URL':self.db.dsn}
        self.enterContext(patch.object(mac.Database,'from_config',return_value=self.db))
        self.enterContext(redirect_stdout(io.StringIO()))

    def test_current_schema_and_read_only_checks(self):
        mac.database_check(self.config)
        with self.db.connect() as conn:self.assertEqual(conn.execute('SELECT count(*) FROM users').fetchone(),(0,))

    def test_missing_migration_blocks_start_but_allows_backup(self):
        with self.db.connect() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version='007_form_drafts.sql'")
            conn.execute('DROP TABLE form_drafts')
        with self.assertRaisesRegex(ValueError,'007_form_drafts.sql'):mac.database_check(self.config)
        mac.database_check(self.config,allow_pending=True)
        with self.db.connect() as conn:self.assertIsNone(conn.execute("SELECT to_regclass('form_drafts')").fetchone()[0])

    def test_live_polling_lease_blocks_preflight(self):
        lease=self.db.polling_lease(self.config['MAX_BOT_TOKEN']);lease.open();self.addCleanup(lease.close)
        with self.assertRaisesRegex(ValueError,'poller'):mac.database_check(self.config)

    def test_missing_table_is_not_hidden_by_version_rows(self):
        with self.db.connect() as conn:conn.execute('DROP TABLE form_drafts')
        with self.assertRaisesRegex(ValueError,'form_drafts'):mac.database_check(self.config)
