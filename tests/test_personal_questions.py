from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from app.handlers.questions import QuestionsHandler
from app.timeutil import SAMARA


class PersonalQuestionsTests(unittest.TestCase):
    def context(self,text):
        today=Mock();today.home.return_value='Твоя группа: А-01';today.lesson_question.return_value=None
        return SimpleNamespace(private=True,actor=10,text=text,users=Mock(registered_group=Mock(return_value='А-01')),
                               db=Mock(),client=Mock(),schedules=Mock(),deps=SimpleNamespace(today=today))

    def test_today_uses_actual_actor(self):
        ctx=self.context('Что у меня сегодня?');QuestionsHandler().on_message(ctx)
        self.assertEqual(ctx.deps.today.home.call_args.args,(10,'А-01'))
        self.assertIn('А-01',ctx.client.send_message.call_args.args[1])

    def test_next_week_calls_scoped_repository_with_next_seven_days(self):
        ctx=self.context('Какое расписание на следующую неделю?');ctx.schedules.for_user.return_value=[]
        QuestionsHandler().on_message(ctx)
        actor,days=ctx.schedules.for_user.call_args.args
        self.assertEqual(actor,10);self.assertEqual(len(days),7);self.assertEqual(days[0].weekday(),0)
        self.assertIn('не опубликовано',ctx.client.send_message.call_args.args[1])

    def test_tomorrow_phrase(self):
        ctx=self.context('Какие пары завтра?');ctx.schedules.for_user.return_value=[]
        QuestionsHandler().on_message(ctx);self.assertEqual(ctx.schedules.for_user.call_args.args[0],10)

    def test_first_lesson_does_not_guess_unknown_parity(self):
        ctx=self.context('Во сколько первая пара?')
        day=datetime.now(SAMARA).date();ctx.schedules.for_user.return_value=[(day,'А-01',1,None,1,'numerator','Математика','','','')]
        QuestionsHandler().on_message(ctx)
        self.assertIn('не подтверждён',ctx.client.send_message.call_args.args[1])

    def test_tomorrow_homework_uses_scope_and_half_open_day(self):
        ctx=self.context('Какие задания на завтра?')
        with patch('app.handlers.questions.HomeworkStore') as store:
            store.return_value.due_list.return_value=[];QuestionsHandler().on_message(ctx)
            store.assert_called_once_with(ctx.db,10,None)
            start,end=store.return_value.due_list.call_args.args
            self.assertEqual((end-start).total_seconds(),86400);self.assertEqual(start.hour,0)

    def test_nearest_events_are_published_views_only(self):
        ctx=self.context('Какие ближайшие мероприятия?')
        with patch('app.handlers.questions.CommunityStore') as store:
            store.return_value.events.return_value=[];QuestionsHandler().on_message(ctx)
            store.assert_called_once_with(ctx.db,10,None);store.return_value.events.assert_called_once_with(False,0)
            self.assertIn('пока нет',ctx.client.send_message.call_args.args[1])

    def test_unknown_question_offers_buttons_without_fabrication(self):
        ctx=self.context('Какая стипендия будет через десять лет?');QuestionsHandler().on_message(ctx)
        self.assertIn('Могу показать',ctx.client.send_message.call_args.args[1])
        self.assertTrue(ctx.client.send_message.call_args.kwargs['attachments'])
