from datetime import datetime, date
from dataclasses import asdict
import unittest
from unittest.mock import Mock, patch

from app.handlers.homework import Draft, HomeworkHandler
from app.handlers.community import Draft as EventDraft, CommunityHandler
from app.services.date_picker import choose, keyboard
from app.services.form_drafts import encode, decode
from app.services.schedule_formatter import requested_days
from app.timeutil import SAMARA


class DatePickerTests(unittest.TestCase):
    def setUp(self):
        self.draft=Draft(1,False,step='deadline')
        self.now=datetime(2028,2,28,14,tzinfo=SAMARA)

    def test_quick_dates_cross_leap_month_and_preserve_timezone(self):
        for offset,expected in ((0,date(2028,2,28)),(1,date(2028,2,29)),(2,date(2028,3,1)),(7,date(2028,3,6))):
            choose(self.draft,'dt:offset:'+str(offset),now=self.now)
            self.assertEqual(self.draft.date_value,expected.isoformat())
            done,value=choose(self.draft,'dt:time:18:00',now=self.now)
            self.assertTrue(done);self.assertEqual(value.date(),expected);self.assertEqual(value.utcoffset().total_seconds(),14400)

    def test_calendar_month_year_navigation_and_leap_day(self):
        choose(self.draft,'dt:month:2028-02',now=self.now)
        text,markup=keyboard(self.draft,'hw:')
        self.assertIn('2028',text)
        self.assertIn('2028-02-29',str(markup))
        choose(self.draft,'dt:month:2028-12',now=self.now)
        self.assertIn('2029-01',str(keyboard(self.draft,'hw:')[1]))
        choose(self.draft,'dt:month:2029-01',now=self.now)
        self.assertIn('2028-12',str(keyboard(self.draft,'hw:')[1]))

    def test_invalid_and_past_dates_and_times_are_rejected(self):
        for action in ('dt:day:2027-02-29','dt:day:2028-02-27','dt:month:9999-01','dt:offset:500'):
            with self.assertRaises(ValueError):choose(self.draft,action,now=self.now)
        choose(self.draft,'dt:offset:0',now=self.now)
        for clock in ('09:00','25:00','bad'):
            with self.assertRaises(ValueError):choose(self.draft,'dt:time:'+clock,now=self.now)
        self.assertEqual(self.draft.date_value,'2028-02-28')

    def test_custom_clock_and_reset(self):
        choose(self.draft,'dt:offset:1',now=self.now);choose(self.draft,'dt:clock',now=self.now)
        done,value=choose(self.draft,None,text='16:45',now=self.now)
        self.assertTrue(done);self.assertEqual(value.hour,16)
        choose(self.draft,'dt:reset',now=self.now);self.assertEqual(self.draft.date_value,'')

    def test_date_picker_state_roundtrips_and_old_payload_still_loads(self):
        choose(self.draft,'dt:offset:1',now=self.now)
        payload={k:encode(v) for k,v in asdict(self.draft).items()}
        restored=Draft(**{k:decode(v) for k,v in payload.items()})
        self.assertEqual(restored.date_value,self.draft.date_value)
        self.assertEqual(Draft(1,False).date_value,'')
        self.assertEqual(EventDraft('event').date_cursor,'')

    def test_homework_nonce_rotates_and_duplicate_cannot_advance(self):
        h=HomeworkHandler();h.drafts[1]=self.draft;old=self.draft.token
        with patch('app.services.date_picker.datetime') as clock:
            clock.now.return_value=self.now
            h.advance(Mock(),Mock(),1,self.draft,'',{},f'd:{old}:dt:offset:1',Mock())
        self.assertNotEqual(old,self.draft.token);self.assertEqual(self.draft.step,'deadline')
        with self.assertRaisesRegex(ValueError,'устарела'):
            h.advance(Mock(),Mock(),1,self.draft,'',{},f'd:{old}:dt:offset:1',Mock())

    def test_event_picker_does_not_publish_and_rejects_stale_callback(self):
        h=CommunityHandler();d=EventDraft('event',step=2);h.drafts[1]=d;db=Mock();old=d.token
        h.advance(Mock(),db,1,'',{},f'd:{old}:dt:offset:1',Mock())
        self.assertEqual(d.step,2);db.save_event.assert_not_called()
        with self.assertRaisesRegex(ValueError,'устарела'):
            h.advance(Mock(),db,1,'',{},f'd:{old}:dt:offset:1',Mock())

    def test_no_deadline_only_for_optional_homework(self):
        self.assertIn('Без срока',str(keyboard(self.draft,'hw:',optional=True)))
        self.assertNotIn('Без срока',str(keyboard(EventDraft('event'),'community:')))

    def test_next_week_on_sunday_and_new_year(self):
        for today,monday in ((date(2026,11,8),date(2026,11,9)),(date(2026,12,31),date(2027,1,4)),(date(2028,2,28),date(2028,3,6))):
            days=requested_days('next_week',today)
            self.assertEqual(days[0],monday);self.assertEqual(len(days),7)
