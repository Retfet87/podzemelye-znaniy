from datetime import datetime,date,timedelta
import unittest
from unittest.mock import Mock,patch

from app.handlers import Deps,build_router
from app.handlers.homework import HomeworkHandler,Draft
from app.services.today import TodayService,schedule_today
from app.timeutil import SAMARA
from test_main import message_update,callback_update

DAY=date(2026,11,2)
NOW=datetime(2026,11,2,9,0,tzinfo=SAMARA)


def row(pair=1,subject='Физика',parity='all',anchor=DAY,day=DAY):
    return (day,'ИБ-11',1,anchor,pair,parity,subject,'Преподаватель','101','')


class TodayTests(unittest.TestCase):
    def test_no_data_and_no_classes_are_distinct(self):
        self.assertIn('не загружено',schedule_today([],NOW))
        self.assertIn('не загружено',schedule_today([(DAY,'ИБ',None,None,None,None,None,None,None,None)],NOW))
        self.assertIn('занятий нет',schedule_today([(DAY,'ИБ',1,DAY,None,None,None,None,None,None)],NOW))

    def test_current_next_and_finished_lessons(self):
        rows=[row(),row(2,'Математика')]
        self.assertIn('Сейчас: Физика',schedule_today(rows,NOW))
        self.assertIn('Ближайшая: Математика',schedule_today(rows,NOW.replace(hour=10,minute=10)))
        self.assertIn('закончились',schedule_today(rows,NOW.replace(hour=20)))

    def test_unknown_parity_is_never_guessed(self):
        self.assertIn('не подтверждён',schedule_today([row(parity='numerator',anchor=None)],NOW))
        self.assertIn('занятий нет',schedule_today([row(parity='denominator')],NOW))

    def test_home_uses_same_actor_and_local_deadline_window(self):
        db,schedules=Mock(),Mock()
        schedules.for_user.return_value=[row()]
        with patch('app.services.today.HomeworkStore') as store:
            store.return_value.due_count.return_value=2
            text=TodayService(db,schedules).home(11,'ИБ-11',now=NOW)
            schedules.for_user.assert_called_once_with(11,[DAY])
            store.assert_called_once_with(db,11,None)
            start,end=store.return_value.due_count.call_args.args
            self.assertEqual(start,NOW)
            self.assertEqual(end,datetime(2026,11,4,tzinfo=SAMARA))
            self.assertIn('2 заданий',text)
            self.assertIn('ИБ-11',text)

    def test_lesson_queries_handle_ambiguity_missing_data_and_future_days(self):
        schedules=Mock()
        service=TodayService(Mock(),schedules)
        schedules.for_user.return_value=[row(day=DAY+timedelta(days=1))]
        self.assertIn('03.11 в 08:30',service.lesson_question(11,'КОГДА у меня физика?',now=NOW))
        self.assertIn('101',service.lesson_question(11,'где физика',now=NOW))
        schedules.for_user.return_value=[row(subject='Физика'),row(2,'Физика практикум')]
        self.assertIn('Уточни',service.lesson_question(11,'когда физика',now=NOW))
        schedules.for_user.return_value=[]
        self.assertIn('не нашлось',service.lesson_question(11,'где физика',now=NOW))
        self.assertIsNone(service.lesson_question(11,'привет',now=NOW))
        schedules.for_user.return_value=[row(parity='numerator',anchor=None),row(day=DAY+timedelta(days=7))]
        self.assertIn('не подтверждён',service.lesson_question(11,'когда физика',now=NOW))

    def test_main_start_and_home_use_personal_service(self):
        client,store,today=Mock(),Mock(),Mock()
        store.start.return_value=('ИБ-11',)
        store.registered_group.return_value='ИБ-11'
        today.home.return_value='Личная сводка'
        router=build_router(Deps(client,store,store,store,None,today))
        update=message_update('/start')
        update['message']['recipient']['chat_type']='dialog'
        router.dispatch(update)
        self.assertEqual(client.send_message.call_args.args[1],'Личная сводка')
        router.dispatch(callback_update('nav:home'))
        self.assertEqual(client.answer.call_args.kwargs['text'],'Личная сводка')
        today.home.assert_called_with(10,'ИБ-11')

    def test_active_form_text_is_not_interpreted_as_a_question(self):
        client,store,today=Mock(),Mock(),Mock()
        homework=HomeworkHandler()
        draft=Draft(1,False,step='body',subject='Предмет')
        homework.drafts.put(10,draft)
        router=build_router(Deps(client,store,store,store,None,today),homework=homework)
        update=message_update('когда физика')
        update['message']['recipient']['chat_type']='dialog'
        router.dispatch(update)
        self.assertEqual(draft.body,'когда физика')
        today.lesson_question.assert_not_called()

    def test_unrecognised_private_text_has_help_without_external_service(self):
        client,store,today=Mock(),Mock(),Mock()
        store.registered_group.return_value='ИБ-11'
        today.lesson_question.return_value=None
        router=build_router(Deps(client,store,store,store,None,today))
        update=message_update('непонятный вопрос')
        update['message']['recipient']['chat_type']='dialog'
        router.dispatch(update)
        self.assertIn('Могу показать',client.send_message.call_args.args[1])
