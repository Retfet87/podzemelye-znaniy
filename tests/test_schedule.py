import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import psycopg

from helpers import handle_update
from app.handlers.menus import HOME, MENU, SCHEDULE_MENU
from app.services.schedule_formatter import format_schedule, requested_days
from app.services.schedule_parser import parse_block, parse_xls
from test_main import callback_update, message_update

MONDAY = date(2026, 11, 2)


class ParserTests(unittest.TestCase):
    def test_unsplit_lesson_with_multiple_teachers_and_rooms(self):
        lesson, = parse_block(
            ["Иностранный язык", "Алексеева Е.Н ауд.318", "Мурзагалиева Р.В ауд.419", ""],
            False, "ИБ-11", 0, 3,
        )
        self.assertEqual(lesson.parity, "all")
        self.assertEqual(lesson.teacher, "Алексеева Е.Н / Мурзагалиева Р.В")
        self.assertEqual(lesson.room, "ауд.318 / ауд.419")

    def test_empty_upper_half_is_not_an_all_weeks_lesson(self):
        lesson, = parse_block(["", "", "Эко.основы", "Булынина С.А ауд.119"],
                              True, "ПБ-22", 3, 1)
        self.assertEqual((lesson.parity, lesson.subject, lesson.room),
                         ("denominator", "Эко.основы", "ауд.119"))

    def test_split_by_two_subjects_and_gray_fill_other_building(self):
        first, second = parse_block(
            ["Физика", "Яблокова М.В ауд.401", "ОПД", "Исхакова А.Р ауд.232"],
            False, "ВЕБ-11", 0, 4, gray_flags=(True, True, False, False),
        )
        self.assertEqual((first.parity, first.subject, first.teacher, first.room, first.note),
                         ("numerator", "Физика", "Яблокова М.В", "ауд.401", "Другой корпус"))
        self.assertEqual((second.parity, second.subject, second.teacher, second.room, second.note),
                         ("denominator", "ОПД", "Исхакова А.Р", "ауд.232", ""))

    def test_invalid_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.xls"
            path.write_text("<html>Ошибка</html>")
            with self.assertRaisesRegex(ValueError, "XLS"):
                parse_xls(path)

    @unittest.skipUnless(os.environ.get("AKVT_TEST_XLS"), "Укажи AKVT_TEST_XLS для проверки реального XLS.")
    def test_original_workbook(self):
        digest, names, lessons = parse_xls(os.environ["AKVT_TEST_XLS"])
        self.assertEqual(digest, "8c38df8a729e63274bf3bdae7376ea82258fb915f1822bcaca16e5540a254f93")
        self.assertEqual(len(names), 37)
        self.assertEqual(len(lessons), 672)
        pair = [x for x in lessons if (x.group, x.weekday, x.pair) == ("ИБ-11", 0, 4)]
        self.assertEqual([(x.parity, x.subject, x.room) for x in pair],
                         [("numerator", "Биология", "ауд.119"),
                          ("denominator", "Литература", "ауд.408")])


class ScheduleUITests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.store = Mock()

    def test_open_and_back_and_text_triggers(self):
        self.store.registered_group.return_value = "ИБ-11"
        handle_update(self.client, self.store, callback_update("schedule:open"))
        self.client.answer.assert_called_once_with("click", text="Расписание · ИБ-11", attachments=SCHEDULE_MENU)
        self.client.reset_mock()
        handle_update(self.client, self.store, callback_update("schedule:back"))
        self.client.answer.assert_called_once_with("click", text="Твоя группа: ИБ-11.", attachments=HOME)
        self.client.reset_mock()
        msg = message_update("расписание")
        msg["message"]["recipient"]["chat_type"] = "dialog"
        handle_update(self.client, self.store, msg)
        self.client.send_message.assert_called_once_with(
            {"user_id": 10}, "Расписание · ИБ-11", attachments=SCHEDULE_MENU
        )

    @patch("app.handlers.schedule.requested_days", return_value=[MONDAY])
    def test_period_uses_clicker_and_keeps_navigation(self, days):
        self.store.for_user.return_value = [
            (MONDAY, "ИБ-11", 1, None, 2, "all", "Математика", "Маслова А.С", "", ""),
        ]
        for action in ("today", "tomorrow", "week"):
            with self.subTest(action=action):
                handle_update(self.client, self.store, callback_update(f"schedule:{action}"))
                days.assert_called_with(action)
                self.store.for_user.assert_called_with(10, [MONDAY])
                kwargs = self.client.answer.call_args.kwargs
                self.assertEqual(kwargs["attachments"], MENU)
                for expected in ("10:15–11:50", "Математика", "Маслова А.С", "Плавающий кабинет"):
                    self.assertIn(expected, kwargs["text"])

    def test_unregistered_and_database_error(self):
        self.store.for_user.return_value = []
        handle_update(self.client, self.store, callback_update("schedule:today"))
        self.assertIn("зарегистрируйся", self.client.answer.call_args.kwargs["notification"])
        self.store.for_user.side_effect = psycopg.OperationalError()
        with self.assertLogs("app", level="WARNING"):
            handle_update(self.client, self.store, callback_update("schedule:today"))
        self.assertIn("временно недоступно", self.client.answer.call_args.kwargs["notification"])

    def test_empty_day_and_unknown_schedule(self):
        rows = [(MONDAY, "ИБ-11", 1, None, None, None, None, None, None, None)]
        self.assertIn("Занятий нет", format_schedule(rows))
        rows = [(MONDAY, "ИБ-11", None, None, None, None, None, None, None, None)]
        self.assertIn("пока не опубликовано", format_schedule(rows))

    def test_parity_uses_anchor_and_exposes_uncertainty_without_it(self):
        def rows(anchor):
            return [(MONDAY, "ИБ-11", 1, anchor, 1, parity, subject, "", "", "")
                    for parity, subject in (("numerator", "Биология"), ("denominator", "Литература"))]

        known = format_schedule(rows(date(2026, 10, 26)))
        self.assertIn("Литература", known)
        self.assertNotIn("Биология", known)
        unknown = format_schedule(rows(None))
        self.assertIn("Биология (числитель)", unknown)
        self.assertIn("Литература (знаменатель)", unknown)

    def test_calendar_week_and_tomorrow_across_boundary(self):
        sunday = date(2026, 11, 8)
        self.assertEqual(requested_days("tomorrow", sunday), [date(2026, 11, 9)])
        week = requested_days("week", sunday)
        self.assertEqual((len(week), week[0], week[-1]), (7, MONDAY, sunday))
