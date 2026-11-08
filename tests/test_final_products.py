"""Acceptance of imported team UI, strict API and independent public fixtures."""
import json
import hashlib
import re
from pathlib import Path
import tempfile
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta

from app.handlers import Deps, build_router
from app.handlers.community import Draft as CommunityDraft
from app.handlers.homework import Draft as HomeworkDraft
from app.repositories.community import CommunityStore
from app.repositories.homework import HomeworkStore
from app.services.schedule_parser import Lesson
from app.services.validation import validate_public_site_url
from app.timeutil import SAMARA
from app.web import MiniApp, ReadViews
from scripts.build_demo_site import build, dataset, ROOT
import test_db as fixtures
from test_main import callback_update
from test_web import call, signed, TOKEN


class PublicProductTests(unittest.TestCase):
    def test_unauthenticated_local_and_forwarded_requests_never_get_private_api(self):
        views=Mock()
        views.resolve_local_actor.return_value=123
        for path in ('/api/schedule','/api/homework','/api/events','/api/useful'):
            status,_,body=call(MiniApp(views,TOKEN),path,raw='',REMOTE_ADDR='127.0.0.1',HTTP_X_FORWARDED_FOR='192.0.2.2')
            self.assertEqual(status,'401 Unauthorized')
            self.assertNotIn(b'123',body)
        views.read.assert_not_called()
        views.resolve_local_actor.assert_not_called()
        from mini_app import main
        with patch.dict('os.environ',{'WEB_MODE':'local'},clear=True),patch('mini_app.make_server') as serve:
            with self.assertRaises(ValueError):main()
            serve.assert_not_called()

    def test_public_url_never_falls_back_to_localhost_or_credentials(self):
        for url in ('','http://example.com','https://localhost','https://127.0.0.1','https://[::1]',
                    'https://user:secret@example.org','https://example.org?token=x','https://example.local',
                    'https://192.168.1.1','javascript:alert(1)','https://127.1','https://localhost.','https://0177.0.0.1'):
            self.assertIsNone(validate_public_site_url(url))
        self.assertEqual(validate_public_site_url('https://example.org/demo/'),'https://example.org/demo/')
        client=Mock()
        deps=Deps(client,Mock(),Mock(),Mock())
        router=build_router(deps)
        with patch.dict('os.environ',{},clear=True):router.dispatch(callback_update('schedule:website'))
        self.assertNotIn('127.0.0.1',client.answer.call_args.kwargs['text'])
        with patch.dict('os.environ',{'PUBLIC_DEMO_SITE_URL':'https://example.org/demo/'}):router.dispatch(callback_update('schedule:website'))
        self.assertIn('https://example.org/demo/',str(client.answer.call_args.kwargs['attachments']))

    def test_public_build_has_all_four_sections_32_cards_media_and_no_max_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            output=build(Path(tmp)/'public')
            data=json.loads((output/'data/demo.json').read_text())
            self.assertEqual(len(data['sections']['useful']['cards']),32)
            self.assertTrue(all(data['sections'][s]['cards'] for s in data['sections']))
            self.assertTrue(data['sections']['events']['history'])
            html=(output/'index.html').read_text();js=(output/'app.js').read_text()
            for marker in ('max-web-app','initData','WebApp','127.0.0.1','X-Max-Init-Data','/api/'):
                self.assertNotIn(marker,html+js)
            self.assertIn('Content-Security-Policy',html)
            self.assertIn('AbortController',js)
            self.assertIn('requestVersion',js)
            self.assertNotIn('innerHTML',js)
            self.assertIn('ftyp',(output/'assets/demo-flower.mp4').read_bytes()[:32].decode('latin1'))
            self.assertTrue((output/'assets/demo-week.ics').read_bytes().endswith(b'END:VCALENDAR\r\n'))
            self.assertFalse(list(output.rglob('.env*')))
            self.assertEqual(len(list(output.rglob('*.json'))),1)

    def test_public_dataset_validation_rejects_private_fields_even_in_optimized_python(self):
        from scripts import build_demo_site as builder
        data=dataset()
        data['sections']['homework']['cards'][0]['max_user_id']=123
        with patch.object(builder.json,'loads',return_value=data),self.assertRaises(ValueError):
            builder.dataset()
        code = '''from scripts import build_demo_site as b
from unittest.mock import patch
d=b.dataset()
d['sections']['homework']['cards'][0]['max_user_id']=123
with patch.object(b.json,'loads',return_value=d):
 try:b.dataset()
 except ValueError:raise SystemExit(0)
 raise SystemExit(1)
'''
        result=subprocess.run([sys.executable,'-O','-c',code],cwd=ROOT,env={'PATH':os.defpath},capture_output=True)
        self.assertEqual(result.returncode,0)

    def test_release_public_ui_and_json_have_no_decorative_emoji(self):
        emoji=re.compile('[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]')
        with tempfile.TemporaryDirectory() as tmp:
            output=build(Path(tmp)/'site')
            for name in ('index.html','app.js','data/demo.json'):
                self.assertIsNone(emoji.search((output/name).read_text()),name)
            html=(output/'index.html').read_text()
            for phrase in ('без лишних поисков','Меньше поисков','ХАКАТОН 2026'):
                self.assertNotIn(phrase,html)

    def test_release_site_build_is_byte_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            first=build(Path(tmp)/'one');second=build(Path(tmp)/'two')
            hashes=lambda root: {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
            self.assertEqual(hashes(first),hashes(second))
            calendar=(first/'assets/demo-week.ics').read_text()
            self.assertEqual(calendar.count('BEGIN:VEVENT'),12)
            self.assertIn('DTSTAMP:20261108T000000Z',calendar)

    def test_build_refuses_unknown_output_files_and_source_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp);(p/'private.env').write_text('not-public')
            with self.assertRaises(ValueError):build(p)
        with self.assertRaises(ValueError):build(ROOT/'demo-site')
        self.assertTrue(dataset()['demo'])


@unittest.skipUnless(fixtures.TEST_URL,'Requires isolated PostgreSQL')
class FinalAPITests(unittest.TestCase):
    def setUp(self):
        fixtures.PostgreSQLTests.setUp(self)
        for actor,name in ((101,'А-01'),(102,'А-02')):
            self.store.start(actor);self.store.choose_group(actor,self.groups[name])
        today=datetime.now(SAMARA).date()
        self.store.seed_legacy('final-api',['А-01','А-02'],[Lesson(name,0,1,'all','Программирование','','','',()) for name in self.groups],today,today+timedelta(days=14),None)
        self.super=CommunityStore(self.store,90,90)

    def get(self,path,actor=101):
        status,_,body=call(MiniApp(ReadViews(self.store),TOKEN),path,raw=signed(actor))
        self.assertEqual(status,'200 OK')
        return json.loads(body)

    def test_new_cards_keep_private_homework_and_group_materials_isolated(self):
        for actor,name in ((101,'А-01'),(102,'А-02')):
            store=HomeworkStore(self.store,actor,None)
            self.assertTrue(store.save(HomeworkDraft(self.groups[name],False,subject='Программирование',body=f'PRIVATE-{actor}',files=[('demo.txt',b'private attachment')])))
            self.super.save_material(CommunityDraft('material',group=self.groups[name],category='study',title=f'MATERIAL-{actor}',body=f'GROUP-{actor}'))
        payload=self.get('/api/homework')
        self.assertIn('PRIVATE-101',str(payload['cards']))
        self.assertNotIn('PRIVATE-102',str(payload))
        self.assertNotIn('private attachment',str(payload))
        payload=self.get('/api/useful')
        self.assertEqual(len(payload['categories']),5)
        self.assertIn('MATERIAL-101',str(payload['cards']))
        self.assertNotIn('MATERIAL-102',str(payload))
        self.assertTrue(payload['today_status'])
        self.assertIn('cards',self.get('/api/schedule'))

    def test_history_hides_unpublished_reports_and_drafts(self):
        start=datetime.now(SAMARA)+timedelta(days=3)
        hidden=self.super.save_event(CommunityDraft('event',title='HIDDEN-DRAFT',starts_at=start))
        public=self.super.save_event(CommunityDraft('event',title='PUBLIC-HISTORY',starts_at=start))
        self.assertTrue(self.super.transition(public,1,True))
        with self.store.connect() as conn:
            conn.execute("UPDATE events SET starts_at=now()-interval '1 day', report_body='PRIVATE-REPORT' WHERE id=%s",(public,))
        payload=self.get('/api/events')
        self.assertNotIn('HIDDEN-DRAFT',str(payload));self.assertNotIn('PRIVATE-REPORT',str(payload))
        self.assertEqual(payload['history'][0]['title'],'PUBLIC-HISTORY')
        with self.store.connect() as conn:conn.execute('UPDATE events SET report_published=true WHERE id=%s',(public,))
        self.assertIn('PRIVATE-REPORT',str(self.get('/api/events')['history']))
        self.assertIsNotNone(hidden)

    def test_notification_back_from_home_preserves_context_after_toggle(self):
        client=Mock();router=build_router(Deps.create(client,self.store,90))
        router.dispatch(callback_update('hw:prefs:home',user_id=101))
        buttons=client.answer.call_args.kwargs['attachments']
        self.assertNotIn("'payload': 'hw:open'",str(buttons))
        payload=next(b['payload'] for row in buttons[0]['payload']['buttons'] for b in row if b['payload'].startswith('hw:pref:'))
        self.assertTrue(payload.endswith(':home'))
        router.dispatch(callback_update(payload,user_id=101))
        self.assertNotIn("'payload': 'hw:open'",str(client.answer.call_args.kwargs['attachments']))
        router.dispatch(callback_update('hw:prefs',user_id=101))
        self.assertIn("'payload': 'hw:open'",str(client.answer.call_args.kwargs['attachments']))
