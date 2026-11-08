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

from app.demo_site_data import load_site
from app.handlers import Deps, build_router
from app.handlers.community import Draft, CommunityHandler
from app.handlers.homework import Draft as HomeworkDraft, HomeworkHandler
from app.handlers.menus import HOME
from app.max_api.media import upload_media, receive_media
from app.max_api.client import MaxClient
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

    def test_video_without_url_uses_authenticated_video_lookup(self):
        client = MaxClient('offline-token', None)
        with patch.object(client, 'request', autospec=True, return_value={'urls':{'mp4_720':'https://omu.okcdn.ru/clip'}}) as request, patch('app.max_api.media.transfer', return_value=b'mp4-fixture'), patch('app.max_api.media.validate_media'):
            self.assertEqual(receive_media(client, {'type':'video','payload':{'token':'video-token'}}),
                             ('video','video.mp4',b'mp4-fixture'))
            request.assert_called_once_with('GET','/videos/video-token',{})

    @unittest.skipUnless(shutil.which('ffprobe') and shutil.which('ffmpeg'), 'Нужны ffmpeg/ffprobe (есть в Docker).')
    def test_real_mp4_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'clip.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=blue:s=32x32:d=0.1','-an','-c:v','mpeg4',str(path)], check=True)
            self.assertEqual(validate_media('video','clip.mp4',path.read_bytes()), 'video/mp4')

    def test_demo_guard_never_reads_working_config(self):
        with patch.dict(os.environ, {'DEMO_DATABASE_URL':'postgresql://demo_app:private@127.0.0.1/production'}, clear=True), self.assertRaises(ValueError):
            demo_database()


@unittest.skipUnless(TEST_URL, 'Нужна отдельная TEST_DATABASE_URL.')
class UXDatabaseTests(unittest.TestCase):
    setUp = fixtures.CommunityTests.setUp
    query = fixtures.CommunityTests.query

    def test_ordinary_bot_rejects_demo_database_before_creating_max_client(self):
        from app.bootstrap import main
        self.query("CREATE TABLE demo_environment (environment text PRIMARY KEY)")
        self.query("INSERT INTO demo_environment VALUES ('podzemelye-demo-v1')")
        with patch('app.bootstrap.load_config',return_value={'MAX_BOT_TOKEN':'synthetic-only'}), \
                patch('app.bootstrap.max_tls_context',return_value=None), \
                patch('app.bootstrap.Database.from_config',return_value=self.store), \
                patch('app.bootstrap.MaxClient') as client, \
                patch('app.bootstrap.run_polling') as poll, self.assertLogs('app.bootstrap'):
            self.assertEqual(main(),1)
        client.assert_not_called()
        poll.assert_not_called()

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
        from app.services.form_drafts import save
        with self.store.connect() as conn:
            save(conn,11,router.handlers)
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

    def test_synthetic_demo_four_weeks_and_one_off_are_isolated(self):
        from app.services.demo_schedule import seed_schedule
        from app.repositories.schedule import ScheduleRepository
        from app.services.today import lessons
        self.query("CREATE TABLE demo_environment (environment text PRIMARY KEY)")
        self.query("INSERT INTO demo_environment VALUES ('podzemelye-demo-v1')")
        future=datetime(datetime.now(SAMARA).year+1,12,28,tzinfo=SAMARA)
        with patch('app.services.demo_schedule.datetime') as clock:
            clock.now.return_value=future
            seed_schedule(self.store)
            seed_schedule(self.store)
        self.assertEqual(self.query('SELECT count(*) FROM schedule_imports'),[(2,)])
        self.query("INSERT INTO users (max_user_id,group_id,institution_code) SELECT 100,id,institution_code FROM groups WHERE name='ДЕМО-11'")
        monday=future.date()
        monday-=timedelta(days=monday.weekday())
        for week in range(4):
            day=monday+timedelta(days=7*week)
            selected,unknown=lessons(ScheduleRepository(self.store).for_user(100,[day]))
            self.assertFalse(unknown)
            self.assertEqual(selected[0][6],'ДЕМО — ЭВМ' if week==1 else 'ДЕМО — Программирование')
            self.assertEqual(selected[1][6],'ДЕМО — Базы данных' if week%2 else 'ДЕМО — Математика')
        outside=ScheduleRepository(self.store).for_user(100,[monday+timedelta(days=28)])
        self.assertIsNone(outside[0][2])
        self.assertEqual(self.query('SELECT count(*) FROM schedule_notifications'),[(0,)])

    def test_demo_seed_idempotence_cleanup_and_no_outbox(self):
        monday = datetime.now(SAMARA).date()
        monday -= timedelta(days=monday.weekday())
        self.store.seed_legacy('fixture',['А-01'],[Lesson('А-01',0,1,'all','Предмет','','','',())],monday,monday+timedelta(days=13),monday)
        self.query("CREATE TABLE demo_environment (environment text PRIMARY KEY)")
        self.query("INSERT INTO demo_environment VALUES ('podzemelye-demo-v1')")
        self.assertTrue(seed(self.store))
        self.assertFalse(seed(self.store))
        self.assertEqual(self.query('SELECT count(*) FROM useful_materials'), [(32,)])
        self.assertEqual(self.query('SELECT count(*) FROM events'), [(4,)])
        self.assertEqual(self.query('SELECT count(*) FROM demo_records'), [(40,)])
        self.assertEqual(self.query('SELECT count(*) FROM useful_files'), [(6,)])
        for table in ('event_notifications','homework_notifications','schedule_notifications'):
            self.assertEqual(self.query('SELECT count(*) FROM ' + table),[(0,)])
        with self.store.connect() as conn:
            clean(conn)
            clean(conn)
        self.assertEqual(self.query('SELECT count(*) FROM users'),[(4,)])
        self.assertEqual(self.query('SELECT count(*) FROM schedule_imports'),[(1,)])
        self.assertEqual(self.query('SELECT count(*) FROM demo_records'),[(0,)])

    def test_demo_useful_cleanup_preserves_unmarked_title_collision(self):
        from app.demo_seed import seed_useful, clean_useful
        from app.demo_seed import FIXTURES
        import json
        self.query("CREATE TABLE demo_environment (environment text PRIMARY KEY)")
        self.query("INSERT INTO demo_environment VALUES ('podzemelye-demo-v1')")
        title = json.loads(FIXTURES.read_text())[0]['title']
        foreign = self.query("INSERT INTO useful_materials (category,title,body,link) VALUES ('curators',%s,'Unmarked record','') RETURNING id", (title,))[0][0]
        with self.store.connect() as conn:
            self.assertEqual(seed_useful(conn), 32)
        with self.store.connect() as conn:
            self.assertEqual(seed_useful(conn), 0)
            clean_useful(conn)
            clean_useful(conn)
        self.assertEqual(self.query('SELECT id FROM useful_materials'), [(foreign,)])
        self.assertEqual(self.query('SELECT count(*) FROM demo_records'), [(0,)])

    def test_demo_useful_concurrent_seeds_are_serialized(self):
        from concurrent.futures import ThreadPoolExecutor
        from app.demo_seed import seed_useful
        self.query("CREATE TABLE demo_environment (environment text PRIMARY KEY)")
        self.query("INSERT INTO demo_environment VALUES ('podzemelye-demo-v1')")
        def apply(_):
            with self.store.connect() as conn:
                return seed_useful(conn)
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertEqual(sorted(executor.map(apply, range(2))), [0, 32])
        self.assertEqual(self.query('SELECT count(*) FROM demo_records'), [(32,)])

    def test_today_deadline_count_respects_personal_and_group_scope(self):
        from app.repositories.homework import HomeworkStore
        now=datetime.now(SAMARA)
        deadline=now+timedelta(hours=1)
        for group,owner in ((self.a,11),(self.a,None),(self.b,12),(self.b,None)):
            self.query("INSERT INTO homework (group_id,owner_id,subject,body,deadline) VALUES (%s,%s,'Предмет','Задание',%s)",(group,owner,deadline))
        self.assertEqual(HomeworkStore(self.store,11,None).due_count(now,deadline+timedelta(hours=1)),2)
        self.assertEqual(HomeworkStore(self.store,12,None).due_count(now,deadline+timedelta(hours=1)),2)
        self.assertEqual(HomeworkStore(self.store,13,None).due_count(now,deadline+timedelta(hours=1)),0)
        self.assertEqual(len(HomeworkStore(self.store,11,None).due_list(now,deadline+timedelta(hours=1))),2)
        self.assertEqual(len(HomeworkStore(self.store,12,None).due_list(now,deadline+timedelta(hours=1))),2)
        self.assertEqual(HomeworkStore(self.store,13,None).due_list(now,deadline+timedelta(hours=1)),[])

    def test_web_views_preserve_homework_scope_and_hide_unpublished_events(self):
        from app.web import ReadViews
        self.query("INSERT INTO homework (group_id,owner_id,subject,body) VALUES (%s,11,'A','personal-A'),(%s,12,'B','personal-B'),(%s,NULL,'A','shared-A'),(%s,NULL,'B','shared-B')",(self.a,self.b,self.a,self.b))
        views=ReadViews(self.store)
        a='\n'.join(views.read(11,'/api/homework')['items'])
        b='\n'.join(views.read(12,'/api/homework')['items'])
        self.assertIn('personal-A',a)
        self.assertIn('shared-A',a)
        self.assertNotIn('personal-B',a)
        self.assertNotIn('shared-B',a)
        self.assertIn('personal-B',b)
        self.assertNotIn('personal-A',b)
        self.super.save_event(Draft('event',title='unpublished-secret',body='draft',starts_at=datetime.now(SAMARA)+timedelta(days=1)))
        self.assertNotIn('unpublished-secret','\n'.join(views.read(11,'/api/events')['items']))

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
        from app.services.form_drafts import save
        with self.store.connect() as conn:
            save(conn,90,router.handlers)
        router.dispatch(callback_update('community:d:' + draft.token + ':prev',user_id=90))
        self.assertEqual(draft.step,1)
        router = build_router(Deps.create(client,self.store,90))
        router.dispatch(callback_update('community:d:old:save',user_id=90))
        self.assertEqual(self.query('SELECT count(*) FROM events'),[(0,)])


    @unittest.skipUnless(os.environ.get('AKVT_TEST_XLS'), 'Нужно реальное XLS для демонстрационного импорта.')
    def test_demo_real_xls_cli_repeat_cleanup(self):
        # Только случайная тестовая схема, рабочий DSN не используется.
        self.query('DELETE FROM users')
        self.query("CREATE TABLE demo_environment (environment text PRIMARY KEY)")
        self.query("INSERT INTO demo_environment VALUES ('podzemelye-demo-v1')")
        with patch('seed_demo.demo_database', return_value=self.store), patch('sys.argv',
                ['seed_demo.py','--demo','--xls',os.environ['AKVT_TEST_XLS']]):
            demo_main()
            demo_main()
        self.assertEqual(self.query('SELECT count(*) FROM schedule_lessons'),[(672,)])
        self.assertEqual(self.query('SELECT count(*) FROM demo_records'),[(40,)])
        self.assertEqual(self.query('SELECT count(*) FROM schedule_notifications'),[(0,)])
        with patch('seed_demo.demo_database', return_value=self.store), patch('sys.argv',['seed_demo.py','--demo','--clean']):
            demo_main()
        self.assertEqual(self.query('SELECT count(*) FROM schedule_lessons'),[(672,)])
        self.assertEqual(self.query('SELECT count(*) FROM demo_records'),[(0,)])


    def test_report_dialog_requires_preview_and_explicit_publication(self):
        event = self.super.save_event(Draft('event', title='Отчёт', body='Событие', starts_at=datetime.now(SAMARA)+timedelta(days=1)))
        self.assertTrue(self.super.transition(event,1,True))
        self.query("UPDATE events SET starts_at = now() - interval '1 day' WHERE id = %s",(event,))
        client = Mock()
        handler = CommunityHandler()
        router = build_router(Deps.create(client,self.store,90),community=handler)
        router.dispatch(callback_update('community:report:'+str(event),user_id=90))
        draft = handler.drafts[90]
        update = message_update('Демо — подтверждённые итоги')
        update['message']['sender']['user_id'] = 90
        update['message']['recipient']['chat_type'] = 'dialog'
        router.dispatch(update)
        def press(action):
            router.dispatch(callback_update('community:d:'+draft.token+':'+action,user_id=90))
        press('skip')
        press('keep')
        self.assertIn('Предпросмотр отчёта',client.answer.call_args.kwargs['text'])
        self.assertFalse(self.student.details(event)[3])
        press('save')
        self.assertTrue(self.student.details(event)[3])
        self.assertIn('Отчёт опубликован',client.answer.call_args.kwargs['text'])

    def test_bot_demo_db_matches_site_content_with_notice_and_refresh_preserves_unmarked(self):
        from app.demo_seed import seed_events, seed_useful, refresh_site_demo
        from app.demo_site_data import load_site
        self.query("CREATE TABLE demo_environment (environment text PRIMARY KEY)")
        self.query("INSERT INTO demo_environment VALUES ('podzemelye-demo-v1')")
        unmarked_event = self.query(
        "INSERT INTO events (title, description, starts_at, place, state) "
        "VALUES ('ДЕМО — студенческий хакатон', 'Чужая запись', now() + interval '5 days', 'Зал', 'published') RETURNING id"
    )[0][0]
        with self.store.connect() as conn:
            self.assertEqual(seed_useful(conn), 32)
            self.assertEqual(seed_useful(conn), 0)
            self.assertEqual(seed_events(conn), 4)
            self.assertEqual(seed_events(conn), 0)
            self.assertEqual(refresh_site_demo(conn), (32, 4))
    # Непомеченное чужое событие сохранилось
        self.assertEqual(self.query("SELECT description FROM events WHERE id = %s", (unmarked_event,)), [('Чужая запись',)])
    # Содержимое совпадает с site.json с учётом отдельного предупреждения DEMO.
        raw_site = load_site()['sections']
        db_useful = dict(self.query("SELECT title, body FROM useful_materials WHERE id IN (SELECT record_id FROM demo_records WHERE kind = 'material')"))
        for card in raw_site['useful']['cards']:
            self.assertEqual(db_useful[card['title']], 'ДЕМО · вымышленные данные. Не справочная информация АКВТ.\n'+card['body'])
        db_study_files = self.query(
            "SELECT m.title, f.name FROM useful_files f JOIN useful_materials m ON m.id = f.material_id "
            "WHERE m.id IN (SELECT record_id FROM demo_records WHERE kind = 'material') ORDER BY m.id"
        )
        self.assertEqual(len(db_study_files), 6)
        self.assertTrue(all(name == 'demo-task.txt' for _, name in db_study_files))
        db_events = {
            row[0]: row[1:]
            for row in self.query(
                "SELECT title, description, report_body, report_published FROM events "
                "WHERE id IN (SELECT record_id FROM demo_records WHERE kind = 'event')"
            )
        }
        for ev in raw_site['events']['cards']:
            self.assertEqual(db_events[ev['title']][0], ev['body'])
        hist = raw_site['events']['history'][0]
        self.assertEqual(db_events[hist['title']], (hist['body'], hist['report_body'], True))
