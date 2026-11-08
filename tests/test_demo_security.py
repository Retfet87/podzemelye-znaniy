"""Isolation boundaries: no production fallback, unauthorised output or notifications."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, MagicMock, patch

from app.demo import (DemoClient, DemoDatabase, DemoPolicy, DemoRouter, REQUEST,
                      LABEL, demo_database, verify_demo)
from app.demo_seed import FIXTURES
from app.demo_site_data import events as site_events, parse_site_subtitle, read_asset_bytes, map_tags, useful as site_useful
from app.handlers import Deps
from app.max_api.client import MaxClient
from app.notifications.worker import deliver_one
from test_main import callback_update, message_update

DSN = 'postgresql://demo_app:demo-secret@127.0.0.1/podzemelye_demo'


class DemoSecurityTests(unittest.TestCase):
    def setUp(self):
        self.folder = self.enterContext(tempfile.TemporaryDirectory())
        self.switch = Path(self.folder) / 'enabled'
        self.switch.write_text('1\n')
        self.policy = DemoPolicy(frozenset({10}), self.switch, True)

    def test_production_settings_never_supply_demo_dsn(self):
        for env in ({'DATABASE_URL': DSN}, {'TEST_DATABASE_URL': DSN}, {}):
            with self.subTest(env=list(env)), patch.dict(os.environ, env, clear=True), \
                    patch('pathlib.Path.read_text', side_effect=AssertionError('No .env reads')), \
                    patch('app.db.database.psycopg.connect') as connect:
                with self.assertRaises(ValueError):
                    demo_database()
                connect.assert_not_called()

    def test_reject_wrong_source_and_libpq_overrides_without_connecting(self):
        for dsn in (DSN.replace('podzemelye_demo', 'production'), DSN.replace('demo_app', 'postgres'),
                    DSN.replace('127.0.0.1', 'production-host'), DSN + '?hostaddr=192.0.2.1',
                    DSN + '?service=production', DSN + '?options=-csearch_path=public', 'bad-secret'):
            with self.subTest(dsn=dsn), patch.dict(os.environ, {'DEMO_DATABASE_URL': dsn}, clear=True), \
                    patch('app.db.database.psycopg.connect') as connect:
                with self.assertRaises(ValueError) as failure:
                    demo_database()
                self.assertNotIn('demo-secret', str(failure.exception))
                connect.assert_not_called()

    def test_only_explicit_demo_credentials_are_selected(self):
        with patch.dict(os.environ, {'DEMO_DATABASE_URL': DSN, 'DATABASE_URL': 'never-read'}, clear=True):
            self.assertIsInstance(demo_database(), DemoDatabase)

    def test_marker_missing_or_invalid_blocks_connection(self):
        for found, rows in ((None, []), ('demo_environment', [('production',)])):
            conn = Mock()
            conn.execute.return_value.fetchone.return_value = (found,)
            conn.execute.return_value.fetchall.return_value = rows
            with patch('app.db.database.psycopg.connect', return_value=conn):
                with self.assertRaises(ValueError):
                    DemoDatabase(DSN).connect()
                conn.close.assert_called_once()

    def test_hot_switch_fails_closed_and_does_not_cache_permission(self):
        self.assertTrue(self.policy.permits(10))
        for data in ('0\n', '1', 'garbage', ''):
            self.switch.write_text(data)
            self.assertFalse(self.policy.permits(10))
        self.switch.unlink()
        self.assertFalse(self.policy.permits(10))
        self.switch.write_text('1\n')
        self.assertFalse(self.policy.permits(11))
        self.assertFalse(DemoPolicy(self.policy.users, self.switch, False).permits(10))

    def test_default_runtime_configuration_is_disabled(self):
        with patch.dict(os.environ, {'DEMO_ALLOWED_USER_IDS': '10'}, clear=True):
            self.assertFalse(DemoPolicy.from_env().enabled)
        for ids in ('', '0', '-1', '10,', '10, 11', '*', str(2**63)):
            with patch.dict(os.environ, {'DEMO_ALLOWED_USER_IDS': ids}, clear=True):
                with self.assertRaises(ValueError):
                    DemoPolicy.from_env()

    def test_router_blocks_other_accounts_group_chats_and_disabled_requests(self):
        router = Mock()
        router.deps = Deps(Mock(), Mock(), Mock(), Mock())
        gate = DemoRouter(router, self.policy)
        for update in (callback_update('nav:home', user_id=11), message_update('/start')):
            gate.dispatch(update)
        update = callback_update('nav:home', user_id=10)
        update['message']['recipient']['chat_type'] = 'chat'
        gate.dispatch(update)
        self.switch.write_text('0\n')
        gate.dispatch(callback_update('nav:home', user_id=10))
        router.dispatch.assert_not_called()

    def test_authorised_context_is_cleared_even_after_handler_failure(self):
        router = Mock()
        router.deps = Deps(Mock(), Mock(), Mock(), Mock())
        router.dispatch.side_effect = RuntimeError('handler failed')
        gate = DemoRouter(router, self.policy)
        with self.assertRaises(RuntimeError):
            gate.dispatch(callback_update('nav:home', user_id=10))
        self.assertIsNone(REQUEST.get())

    def test_transport_never_sends_outside_current_actor_or_callback(self):
        client = DemoClient('test-only', None, self.policy)
        with patch.object(MaxClient, 'request') as http:
            with self.assertRaises(ValueError):
                client.request('POST', '/messages', {'user_id': 10}, payload={'text': 'hello'})
            token = REQUEST.set((10, 'click'))
            try:
                for path, params in (('/messages', {'user_id': 11}), ('/messages', {'chat_id': 10}),
                                     ('/answers', {'callback_id': 'foreign'}), ('/subscriptions', {})):
                    with self.assertRaises(ValueError):
                        client.request('POST', path, params, payload={'text': 'hello'})
                self.switch.write_text('0\n')
                with self.assertRaises(ValueError):
                    client.request('POST', '/messages', {'user_id': 10}, payload={'text': 'hello'})
                http.assert_not_called()
            finally:
                REQUEST.reset(token)

    def test_every_message_and_callback_has_demo_banner_and_length_limit(self):
        client = DemoClient('test-only', None, self.policy)
        token = REQUEST.set((10, 'click'))
        try:
            with patch.object(MaxClient, 'request') as http:
                client.request('POST', '/messages', {'user_id': 10}, payload={'text': 'x' * 4000})
                text = http.call_args.kwargs['payload']['text']
                self.assertTrue(text.startswith(LABEL))
                self.assertLessEqual(len(text), 4000)
                client.request('POST', '/answers', {'callback_id': 'click'}, payload={'message': {'text': 'card'}})
                self.assertTrue(http.call_args.kwargs['payload']['message']['text'].startswith(LABEL))
        finally:
            REQUEST.reset(token)

    def test_private_start_chat_reply_is_bound_to_authorised_actor(self):
        client = DemoClient('test-only', None, self.policy)
        token = REQUEST.set((10, None))
        try:
            with patch.object(MaxClient, 'request', return_value={'message': {}}) as http:
                client.send_message({'chat_id': 123}, 'welcome')
                self.assertEqual(http.call_args.args[2], {'user_id': 10})
                self.assertTrue(http.call_args.kwargs['payload']['text'].startswith(LABEL))
        finally:
            REQUEST.reset(token)

    def test_demo_worker_is_disabled_even_when_all_categories_are_enabled(self):
        store = MagicMock()
        conn = store.connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.return_value = ('demo_environment',)
        send = Mock()
        self.assertFalse(deliver_one(store, send, RuntimeError, homework=True, schedule=True, events=True))
        send.assert_not_called()
        self.assertEqual(conn.execute.call_count, 1)

    def test_all_32_useful_fixtures_are_preserved_and_labelled(self):
        cards = json.loads(FIXTURES.read_text(encoding='utf-8'))
        site_cards = site_useful()
        self.assertEqual(len(cards), 32)
        self.assertEqual(len(site_cards), 32)
        self.assertEqual(len({c['key'] for c in cards}), 32)
        self.assertEqual({c['category'] for c in cards}, {'curators', 'teachers', 'rooms', 'study', 'links'})
        for c, sc in zip(cards, site_cards):
            self.assertTrue(c['title'].startswith('ДЕМО'))
            self.assertEqual((c['key'], c['category'], c['title'], c['link']),
                             (sc['key'], sc['category'], sc['title'], sc['link']))
            import re
            def plain(value):
                return re.sub(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]', '', value).split()
            self.assertEqual(plain(c['body']), plain(sc['body']))
            self.assertNotIn('+7 (900)', c['body'])
            self.assertLessEqual(len(c['body']), 1800)
            self.assertLessEqual(len(c['title']), 120)

    def test_site_events_parser_tags_dates_and_asset_bounds(self):
        ev = site_events()
        self.assertEqual(len(ev), 4)
        self.assertEqual([e['report_published'] for e in ev], [False, False, False, True])
        dt, place = parse_site_subtitle('12 ноября · 14:00 · ДЕМО-площадка')
        self.assertEqual(dt.isoformat(), '2026-11-12T14:00:00+04:00')
        self.assertEqual(place, 'ДЕМО-площадка')
        hist_dt, hist_place = parse_site_subtitle('6 ноября · ДЕМО-лаборатория', is_history=True)
        self.assertEqual(hist_dt.isoformat(), '2026-11-06T12:00:00+04:00')
        self.assertEqual(hist_place, 'ДЕМО-лаборатория')
        with self.assertRaises(ValueError):
            map_tags(['НеизвестныйТег'])
        for bad_path in ('../secrets.txt', '/etc/passwd', './assets/../x.png', 'unknown.png'):
            with self.assertRaises(ValueError):
                read_asset_bytes(bad_path)

    def test_demo_entrypoint_cannot_start_polling_by_default(self):
        from demo_bot import main
        with patch.dict(os.environ, {'MAX_BOT_TOKEN': 'working-token'}, clear=True), \
                patch('demo_bot.run_polling') as poll:
            with self.assertRaises(ValueError):
                main()
            poll.assert_not_called()