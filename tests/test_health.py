from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from threading import Event
import tempfile
import unittest
from unittest.mock import Mock, patch

from app.health import HealthState, read_status
from app.handlers import Deps
from app.handlers.router import Router
from app.max_api.client import MaxError
from app.notifications.worker import work_notifications
from app.polling import run_polling
from scripts.health import main as health_main
from test_main import message_update


class HealthTests(unittest.TestCase):
    def setUp(self):
        folder=self.enterContext(tempfile.TemporaryDirectory())
        self.path=Path(folder)/'health.json'
        self.health=HealthState(self.path,clock=lambda:100.)

    def good_probe(self):
        db=Mock()
        db.connect.return_value.__enter__=Mock(return_value=Mock())
        db.connect.return_value.__exit__=Mock(return_value=False)
        db.connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value=(1,)
        self.assertTrue(self.health.probe_database(db))

    def test_private_snapshot_freshness_and_shutdown(self):
        self.good_probe()
        self.health.max_success()
        self.assertEqual(self.path.stat().st_mode & 0o777,0o600)
        self.assertTrue(read_status(self.path,now=110)['healthy'])
        self.assertFalse(read_status(self.path,now=221)['healthy'])
        self.health.stopped()
        self.assertFalse(read_status(self.path,now=110)['healthy'])

    def test_database_failure_is_visible_and_recovers(self):
        self.good_probe()
        self.health.max_success()
        db=Mock()
        db.connect.side_effect=RuntimeError('dsn password SECRET')
        self.assertFalse(self.health.probe_database(db))
        self.assertFalse(read_status(self.path,now=100)['database_available'])
        self.good_probe()
        self.assertTrue(read_status(self.path,now=100)['healthy'])
        self.assertNotIn('SECRET',self.path.read_text())

    def test_successful_empty_poll_updates_health(self):
        client=Mock()
        client.get_updates.side_effect=[([],9),KeyboardInterrupt]
        with self.assertRaises(KeyboardInterrupt):
            run_polling(client,Mock(),health=self.health)
        self.assertEqual(json.loads(self.path.read_text())['last_max_success'],100.)

    def test_fatal_max_error_is_recorded_without_token(self):
        client=Mock()
        client.get_updates.side_effect=MaxError('SECRET',status=401)
        with self.assertRaises(MaxError):
            run_polling(client,Mock(),health=self.health)
        data=json.loads(self.path.read_text())
        self.assertEqual(data['critical_error'],'max_authentication')
        self.assertEqual(data['max_error'],'http_401')
        self.assertNotIn('SECRET',self.path.read_text())

    def test_one_handler_failure_does_not_stop_next_user_or_mark_critical(self):
        class Handler:
            drafts=None
            def __init__(self):
                self.calls=0
            def on_message(self,ctx):
                self.calls+=1
                if self.calls==1:
                    raise RuntimeError('SECRET')
                return True
        client=Mock()
        handler=Handler()
        router=Router(Deps(client,Mock(),Mock(),Mock(),health=self.health),[handler])
        update=message_update('ordinary text')
        update['message']['recipient']['chat_type']='dialog'
        with self.assertLogs('app',level='ERROR'):
            router.dispatch(update)
        update['message']['sender']['user_id']=12
        router.dispatch(update)
        data=json.loads(self.path.read_text())
        self.assertEqual((handler.calls,data['handler_errors']),(2,1))
        self.assertIsNone(data['critical_error'])
        self.assertNotIn('SECRET',self.path.read_text())

    def test_worker_failure_is_visible_instead_of_silent_thread_death(self):
        with patch('app.notifications.worker.deliver_one',side_effect=RuntimeError('SECRET')),self.assertLogs('app',level='ERROR'):
            work_notifications(Mock(),Mock(),MaxError,Event(),health=self.health)
        self.assertEqual(read_status(self.path,now=100)['critical_error'],'notification_worker_failure')
        self.assertNotIn('SECRET',self.path.read_text())

    def test_unsafe_permissions_symlinks_and_foreign_owner_rejected(self):
        self.path.chmod(0o644)
        with self.assertRaises(ValueError):
            read_status(self.path)
        self.path.chmod(0o600)
        link=self.path.with_name('link')
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            read_status(link)
        with patch('app.health.os.getuid',return_value=self.path.stat().st_uid+1),self.assertRaises(ValueError):
            read_status(self.path)
        with self.assertRaises(ValueError):
            HealthState(link)

    def test_dead_process_and_cli_fail_closed(self):
        self.good_probe()
        self.health.max_success()
        with patch('app.health.os.kill',side_effect=ProcessLookupError):
            self.assertFalse(read_status(self.path,now=100)['healthy'])
        self.path.write_text('SECRET-not-json')
        out=StringIO()
        with patch('sys.argv',['health',str(self.path)]),redirect_stdout(out):
            self.assertEqual(health_main(),2)
        self.assertNotIn('SECRET',out.getvalue())

    def test_snapshot_io_error_does_not_break_polling_state(self):
        with patch('app.health.os.replace',side_effect=OSError('disk full SECRET')),self.assertLogs('app',level='ERROR'):
            self.health.max_success()
        self.assertEqual(self.health.data['critical_error'],'health_write_failure')
        self.assertEqual(list(self.path.parent.glob('.health-*')),[])

    def test_unknown_critical_category_is_not_persisted(self):
        with self.assertRaises(ValueError):
            self.health.critical('SECRET')
        self.assertNotIn('SECRET',self.path.read_text())


class BootstrapCleanupTests(unittest.TestCase):
    def test_pool_closed_after_interrupt_and_fatal_max(self):
        from app.bootstrap import main
        for error, code in ((KeyboardInterrupt(), 0), (MaxError('authentication', status=401), 1)):
            with self.subTest(error=type(error).__name__), patch('app.bootstrap.load_config',return_value={'MAX_BOT_TOKEN':'synthetic'}), patch('app.bootstrap.max_tls_context'), patch('app.bootstrap.Database.from_config') as factory, patch('app.bootstrap.MaxClient'), patch('app.bootstrap.run_polling',side_effect=error), self.assertLogs('app',level='INFO'):
                self.assertEqual(main(),code)
                factory.return_value.close.assert_called_once()

    def test_pool_closed_if_client_initialization_fails(self):
        from app.bootstrap import main
        with patch('app.bootstrap.load_config',return_value={'MAX_BOT_TOKEN':'synthetic'}), patch('app.bootstrap.max_tls_context'), patch('app.bootstrap.Database.from_config') as factory, patch('app.bootstrap.MaxClient',side_effect=RuntimeError('PRIVATE')), self.assertLogs('app',level='ERROR') as logs:
            self.assertEqual(main(),1)
            factory.return_value.close.assert_called_once()
        self.assertNotIn('PRIVATE',' '.join(logs.output))
