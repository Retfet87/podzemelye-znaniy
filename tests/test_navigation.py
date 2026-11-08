"""Кнопка «Назад» не должна дублировать «🏠 Домой»."""
import unittest

from unittest.mock import Mock
from app.handlers.registration import institution_keyboard, prefixes_keyboard, department_groups_keyboard

from app.handlers.menus import HOME, MENU, SCHEDULE_MENU, homework_menu_keyboard
from app.max_api.keyboards import callback_button, inline_keyboard, vertical_keyboard


def payloads(keyboard):
    return [b["payload"] for row in keyboard[0]["payload"]["buttons"] for b in row]


def labels(keyboard):
    return [b["text"] for row in keyboard[0]["payload"]["buttons"] for b in row]


class BackButtonTests(unittest.TestCase):
    def test_back_to_main_menu_is_dropped_when_home_is_added(self):
        for payload in ("hw:back", "community:back"):
            with self.subTest(payload=payload):
                keyboard = vertical_keyboard([("Что-то", "x"), ("Назад", payload.split(":")[1])],
                                             prefix=payload.split(":")[0] + ":")
                self.assertNotIn(payload, payloads(keyboard))
                self.assertEqual(payloads(keyboard).count("nav:home"), 1)

    def test_homework_menu_has_no_duplicate_back(self):
        for draft in (False, True):
            self.assertNotIn("hw:back", payloads(homework_menu_keyboard(has_draft=draft)))
            self.assertIn("nav:home", payloads(homework_menu_keyboard(has_draft=draft)))

    def test_back_to_a_different_screen_is_kept(self):
        keyboard = vertical_keyboard([("К мероприятию", "event:1")], prefix="community:")
        self.assertIn("community:event:1", payloads(keyboard))
        self.assertIn("⬅️ Назад", labels(keyboard))

    def test_keyboard_of_only_main_menu_back_becomes_just_home(self):
        keyboard = inline_keyboard([[callback_button("Назад", "community:back")]])
        self.assertEqual(payloads(keyboard), ["nav:home"])

    def test_main_menu_unchanged_and_has_no_home(self):
        self.assertNotIn("nav:home", payloads(HOME))
        self.assertFalse(any("Назад" in label for label in labels(HOME)))

    def test_schedule_menu_does_not_point_back_to_itself(self):
        self.assertNotIn("schedule:open", payloads(SCHEDULE_MENU))
        self.assertIn("nav:home", payloads(SCHEDULE_MENU))
        # А на экране с результатом «Назад» ведёт в меню расписания.
        self.assertIn("schedule:open", payloads(MENU))


    def test_registration_home_does_not_point_to_same_screen(self):
        users = Mock()
        users.institutions.return_value = [('akvt', 'АКВТ')]
        self.assertNotIn('nav:home', payloads(institution_keyboard(users)))
        self.assertNotIn('registration:schools', payloads(prefixes_keyboard([(1, 'ИБ-11')])))
        self.assertIn('group_back', payloads(department_groups_keyboard('ИБ', [(1, 'ИБ-11')])))

    def test_keyboard_builder_does_not_mutate_input(self):
        rows = [[callback_button('Назад', 'hw:back')]]
        inline_keyboard(rows)
        self.assertEqual(rows, [[callback_button('Назад', 'hw:back')]])
        self.assertEqual(payloads(inline_keyboard(rows, home=False)), ['hw:back'])

if __name__ == "__main__":
    unittest.main()
