"""Главное меню, меню расписания и меню домашних заданий."""
import os

from app.max_api.keyboards import callback_button, inline_keyboard
from app.services.validation import validate_public_site_url

_SITE = [[callback_button("🌐 ДЕМО-сайт", "schedule:website")]] if validate_public_site_url(os.environ.get("PUBLIC_DEMO_SITE_URL", "")) else []

HOME = inline_keyboard([
    [callback_button("Сегодня", "schedule:today"), callback_button("Завтра", "schedule:tomorrow")],
    [callback_button("📅 Расписание", "schedule:open")],
    [callback_button("📝 Домашние задания", "hw:open")],
    [callback_button("🎉 Мероприятия", "community:events:0"),
     callback_button("📚 Полезное", "community:useful")],
    [callback_button("🔔 Уведомления заданий", "hw:prefs:home")],
] + _SITE, home=False)

_PERIODS = [
    [callback_button("Сегодня", "schedule:today"), callback_button("Завтра", "schedule:tomorrow")],
    [callback_button("Неделя", "schedule:week")],
    [callback_button("📆 В календарь", "calendar:week")],
]

# Сам экран «Расписание»: «Назад» здесь вела бы на этот же экран, поэтому её нет.
SCHEDULE_MENU = inline_keyboard(_PERIODS)

# Экран с результатом (сегодня / завтра / неделя): «Назад» возвращает в меню расписания.
MENU = inline_keyboard(_PERIODS + [[callback_button("Назад", "schedule:open")]])


def homework_menu_keyboard(*, has_draft=False):
    rows = [
        [callback_button("Общие задания", "hw:groups:g:0"),
         callback_button("Личные задания", "hw:groups:p:0")],
        [callback_button("Уведомления", "hw:prefs")],
    ]
    if has_draft:
        rows.append([callback_button("Продолжить черновик", "hw:resume"),
                     callback_button("Отменить", "hw:cancel")])
    rows.append([callback_button("Назад", "hw:back")])
    return inline_keyboard(rows)
