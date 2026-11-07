from datetime import datetime, timedelta
from io import BytesIO
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from app.handlers import Deps, build_router
from app.handlers.community import Draft, CommunityHandler
from app.handlers.homework import Draft as HomeworkDraft, HomeworkHandler
from app.handlers.menus import HOME
from app.max_api.media import upload_media, receive_media
from app.max_api.keyboards import inline_keyboard
from app.services.media import validate_media
from app.timeutil import SAMARA
from scripts.smoke_test import OfflineMAX
from seed_demo import demo_database, seed, clean, main as demo_main
import test_community as fixtures
from test_db import TEST_URL, remove_community_fixture
from app.db import migrate
from app.services.schedule_parser import Lesson
from test_main import callback_update, message_update


def png():
    output = BytesIO()
    Image.new('RGB', (8, 8), '#246b75').save(output, format='PNG')
    return output.getvalue()


class UXInputTests(unittest.TestCase):
    def test_navigation_keyboard_has_single_home_and_main_has_none(self):
        keyboard = inline_keyboard([[{'type':'callback','text':'🏠 Домой','payload':'nav:home'}]])
        self.assertEqual(sum(b.get('payload') == 'nav:home' for r in keyboard[0]['payload']['buttons'] for b in r), 1)
        self.assertFalse(any(b.get('payload') == 'nav:home' for r in HOME[0]['payload']['buttons'] for b in r))

    def test_image_mime_signature_dimensions_and_name(self):
        self.assertEqual(validate_media('image', 'x.png', png()), 'image/png')
        for name, data in [('x.jpg', png()), ('../x.png', png()), ('x.png', b'not png'), ('x.png', b'x' * (5*1024**2+1))]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_media('image', name, data)
        with self.assertRaises(ValueError):
            validate_media('video', 'x.mp4', b'not mp4')

    def test_max_image_upload_payload_and_receive(self):
        client = Mock()
        client.request.return_value = {'url': 'https://iu.oneme.ru/upload'}
        with patch('app.max_api.media.transfer', return_value=b'{"photos":{"1":{"token":"photo-token"}}}'):
            self.assertEqual(upload_media(client, 'image', 'x.png', png()),
                             {'type':'image','payload':{'photos':{'1':{'token':'photo-token'}}}})
        with patch('app.max_api.media.transfer', return_value=png()):
            self.assertEqual(receive_media(client, {'type':'image','payload':{'url':'https://iu.oneme.ru/a'}}),
                             ('image','photo.png',png()))

    def test_video_token_is_taken_from_upload_response(self):
        client = Mock()
        client.request.return_value = {'url': 'https://omu.okcdn.ru/a', 'token':'video-token'}
        with patch('app.max_api.media.validate_media', return_value='video/mp4'), patch('app.max_api.media.transfer', return_value=b'<retval>1</retval>'):
            self.assertEqual(upload_media(client, 'video', 'x.mp4', b'fixture'),
                             {'type':'video','payload':{'token':'video-token'}})

    @unittest.skipUnless(shutil.which('ffprobe') and shutil.which('ffmpeg'), 'Нужны ffmpeg/ffprobe (есть в Docker).')
    def test_real_mp4_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'clip.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=32x32:d=0.1','-an','-c:v','mpeg4',str(path)], check=True)
            self.assertEqual(validate_media('video','clip.mp4',path.read_bytes()), 'video/mp4')

    def test_demo_guard_never_reads_working_config(self):
        with patch.dict(os.environ, {'TEST_DATABASE_URL':'postgresql://u:private@127.0.0.1/production'}, clear=True), self.assertRaises(ValueError):
            demo_database()


@unittest.skipUnless(TEST_URL, 'Нужна отдельная TEST_DATABASE_URL.')
class UXDatabaseTests(unittest.TestCase):
    setUp = fixtures.CommunityTests.setUp
    query = fixtures.CommunityTests.query

    def test_migration_backfills_old_registration(self):
        with self.store.connect() as conn:
            remove_community_fixture(conn)
        self.assertIn('006_ux.sql', migrate(self.store))
        self.assertEqual(self.query('SELECT institution_code, welcome_seen FROM users WHERE max_user_id = 11'), [('akvt',True)])
        self.assertEqual(self.store.registered_group(11),'А-01')

    def test_school_selection_and_repeat_registration(self):
        self.store.start(100)
        self.assertIsNone(self.store.institution(100))
        self.assertFalse(self.store.choose_institution(100,'invented'))
        self.assertTrue(self.store.choose_institution(100,'akvt'))
        self.assertEqual(self.store.choose_group(100,self.a),('saved','А-01'))
        self.assertFalse(self.store.choose_institution(100,'akvt'))
        self.assertEqual(self.store.start(100),('А-01',))

    def test_home_discards_only_after_actor_bound_confirmation(self):
        client = Mock()
        homework = HomeworkHandler()
        draft = HomeworkDraft(self.a, False, body='Не терять')
        homework.drafts.put(11,draft)
        router = build_router(Deps.create(client,self.store,90),homework=homework)
        router.dispatch(callback_update('nav:home', user_id=11))
        self.assertIn(11,homework.drafts)
        self.assertIn('Несохранённые',client.answer.call_args.kwargs['text'])
        router.dispatch(callback_update('nav:discard:old-token', user_id=11))
        self.assertIn(11,homework.drafts)
        router.dispatch(callback_update('nav:discard:' + draft.token, user_id=12))
        self.assertIn(11,homework.drafts)
        router.dispatch(callback_update('nav:discard:' + draft.token, user_id=11))
        self.assertNotIn(11,homework.drafts)
        self.assertEqual(client.answer.call_args.kwargs['attachments'],HOME)
        self.assertEqual(self.query('SELECT count(*) FROM homework'),[(0,)])

    def test_event_tags_history_report_permissions_and_media(self):
        future = datetime.now(SAMARA) + timedelta(days=2)
        draft = Draft('event',title='Демо событие',body='Описание',starts_at=future,tags=['sport'],media=[('image','x.png',png())])
        event = self.super.save_event(draft)
        self.assertTrue(self.super.transition(event,1,True))
        self.assertEqual(len(self.student.events(False,0,tag='sport')),1)
        self.assertEqual(self.student.events(False,0,tag='hackathon'),[])
        self.assertEqual(self.student.media(event,'event'),[('image','x.png',png())])
        self.query("UPDATE events SET starts_at = now() - interval '1 day' WHERE id = %s",(event,))
        self.assertEqual(self.student.events(False,0),[])
        self.assertEqual(len(self.student.events(False,0,history=True)),1)
        report = Draft('report',id=event,revision=2,body='Демо итоги',media=[('image','x.png',png())])
        self.assertIsNone(self.student.save_report(report))
        self.assertEqual(self.student.media(event,'report'),[])
        self.assertEqual(self.super.save_report(report),event)
        self.assertTrue(self.student.details(event)[3])
        self.assertEqual(len(self.student.media(event,'report')),1)
        self.assertIsNone(self.super.save_report(report))
        self.assertEqual(self.query('SELECT count(*) FROM event_notifications'),[(0,)])

    def test_demo_seed_idempotence_cleanup_and_no_outbox(self):
        monday = datetime.now(SAMARA).date()
        monday -= timedelta(days=monday.weekday())
        self.store.seed_legacy('fixture',['А-01'],[Lesson('А-01',0,1,'all','Предмет','','','',())],monday,monday+timedelta(days=13),monday)
        self.assertTrue(seed(self.store))
        self.assertFalse(seed(self.store))
        self.assertEqual(self.query('SELECT count(*) FROM useful_materials'),[(5,)])
        self.assertEqual(self.query('SELECT count(*) FROM demo_records'),[(12,)])
        for table in ('event_notifications','homework_notifications','schedule_notifications'):
            self.assertEqual(self.query('SELECT count(*) FROM ' + table),[(0,)])
        with self.store.connect() as conn:
            clean(conn)
            clean(conn)
        self.assertEqual(self.query('SELECT count(*) FROM users'),[(4,)])
        self.assertEqual(self.query('SELECT count(*) FROM schedule_imports'),[(1,)])
        self.assertEqual(self.query('SELECT count(*) FROM demo_records'),[(0,)])

    def test_registration_illustration_only_once_and_private_callbacks(self):
        class Client(OfflineMAX):
            count = 0
            def welcome_attachment(self):
                self.count += 1
                return {'type':'image','payload':{'token':'offline'}}
        client = Client()
        router = build_router(Deps.create(client,self.store,90))
        update = message_update()
        update['message']['sender']['user_id'] = 100
        update['message']['recipient']['chat_type'] = 'dialog'
        router.dispatch(update)
        router.dispatch(update)
        self.assertEqual(client.count,1)
        router.dispatch(callback_update('institution:akvt',user_id=100))
        self.assertEqual(self.store.institution(100),'akvt')
        update = callback_update('nav:home',user_id=100)
        update['message']['recipient']['chat_type'] = 'chat'
        before = len(client.messages)
        router.dispatch(update)
        self.assertEqual(len(client.messages),before)

    def test_back_steps_and_stale_form_never_publish(self):
        client = Mock()
        handler = CommunityHandler()
        draft = Draft('event',title='Черновик',step=2)
        handler.drafts.put(90,draft)
        router = build_router(Deps.create(client,self.store,90),community=handler)
        router.dispatch(callback_update('community:d:' + draft.token + ':prev',user_id=90))
        self.assertEqual(draft.step,1)
        router = build_router(Deps.create(client,self.store,90))
        router.dispatch(callback_update('community:d:old:save',user_id=90))
        self.assertEqual(self.query('SELECT count(*) FROM events'),[(0,)])


    @unittest.skipUnless(os.environ.get('AKVT_TEST_XLS'), 'Нужно реальное XLS для демонстрационного импорта.')
    def test_demo_real_xls_cli_repeat_cleanup(self):
        # Only this randomly isolated test schema; the production DSN is never used.
        self.query('DELETE FROM users')
        with patch('seed_demo.demo_database', return_value=self.store), patch('sys.argv',
                ['seed_demo.py','--demo','--xls',os.environ['AKVT_TEST_XLS']]):
            demo_main()
            demo_main()
        self.assertEqual(self.query('SELECT count(*) FROM schedule_lessons'),[(672,)])
        self.assertEqual(self.query('SELECT count(*) FROM demo_records'),[(12,)])
        self.assertEqual(self.query('SELECT count(*) FROM schedule_notifications'),[(0,)])
        with patch('seed_demo.demo_database', return_value=self.store), patch('sys.argv',['seed_demo.py','--demo','--clean']):
            demo_main()
        self.assertEqual(self.query('SELECT count(*) FROM schedule_lessons'),[(672,)])
        self.assertEqual(self.query('SELECT count(*) FROM demo_records'),[(0,)])
