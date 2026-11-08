import unittest
from datetime import date, datetime, timedelta
from unittest.mock import Mock

from test_main import callback_update

from app.handlers import Deps, build_router
from app.services.calendar_export import calendar, event_calendar, schedule_calendar
from app.services.file_validation import validate_data
from app.timeutil import SAMARA


class CalendarTests(unittest.TestCase):
    def row(self, **kw):
        values = dict(day=date(2026,11,9),group='А-01',version=1,anchor=date(2026,11,9),pair=1,
                      parity='all',subject='История',teacher='Педагог',room='101',note='')
        values.update(kw)
        return tuple(values.values())

    def test_parity_bells_stable_uid_after_substitution(self):
        rows = [self.row(parity='numerator'), self.row(parity='denominator',subject='ЭВМ')]
        text = schedule_calendar(rows).decode()
        self.assertEqual(text.count('BEGIN:VEVENT'),1)
        self.assertIn('DTSTART;TZID=Europe/Samara:20261109T083000',text)
        self.assertIn('DTEND;TZID=Europe/Samara:20261109T100500',text)
        other = schedule_calendar([self.row(subject='ЭВМ',room='202')]).decode()
        uid = lambda t: next(x for x in t.splitlines() if x.startswith('UID:'))
        self.assertEqual(uid(text),uid(other))
        following = schedule_calendar([self.row(day=date(2026,11,16),parity='denominator',subject='ЭВМ')]).decode()
        self.assertIn('ЭВМ',following)

    def test_escaping_folding_unicode_no_property_injection(self):
        now = datetime(2026,11,9,12,tzinfo=SAMARA)
        data = calendar([('public', 'Я'*100+';x,y\\z\nATTENDEE:secret',now,None,'а\rб','текст')])
        validate_data('calendar.ics',data)
        self.assertTrue(data.endswith(b'END:VCALENDAR\r\n'))
        self.assertTrue(all(len(line)<=75 for line in data.split(b'\r\n')))
        unfolded = data.decode().replace('\r\n ','')
        self.assertIn(r'\;x\,y\\z\nATTENDEE:secret',unfolded)
        self.assertNotIn('\r\nATTENDEE:',unfolded)

    def test_empty_unloaded_unknown_parity_refuse_misleading_export(self):
        for rows in ([],[self.row(pair=None)],[self.row(version=None)],[self.row(anchor=None,parity='numerator')]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                schedule_calendar(rows)

    def test_event_has_no_attendees_or_invented_duration_and_stable_uid(self):
        row = ('Событие','Описание',datetime.now(SAMARA),'Зал','published',1,99,True,True,'Личные данные')
        data = event_calendar(1,row).decode()
        self.assertNotIn('Личные данные',data)
        self.assertNotIn('DTEND',data)
        with self.assertRaises(ValueError):
            event_calendar(1,row[:4]+('draft',)+row[5:])

    def test_buttons_scope_to_actor_and_block_public_chat_and_bad_week(self):
        client,db,users,schedules=Mock(),Mock(),Mock(),Mock()
        schedules.for_user.return_value=[self.row()]
        router=build_router(Deps(client,db,users,schedules))
        monday=date.today()-timedelta(days=date.today().weekday())
        router.dispatch(callback_update(f'calendar:export:{monday}',user_id=42))
        self.assertEqual(schedules.for_user.call_args.args[0],42)
        self.assertEqual(client.upload_file.call_count,1)
        schedules.reset_mock();client.reset_mock()
        update=callback_update('calendar:week',user_id=42)
        update['message']['recipient']['chat_type']='chat'
        router.dispatch(update)
        router.dispatch(callback_update('calendar:export:2026-11-10',user_id=42))
        schedules.for_user.assert_not_called();client.upload_file.assert_not_called()

    def test_lost_polling_lease_prevents_processing_received_batch(self):
        from app.polling import run_polling
        client, router, guard = Mock(), Mock(), Mock()
        client.get_updates.return_value=([{'update_type':'message_created'}],1)
        guard.side_effect=[None,RuntimeError('lease lost')]
        with self.assertRaises(RuntimeError):run_polling(client,router,guard=guard)
        router.dispatch.assert_not_called()
