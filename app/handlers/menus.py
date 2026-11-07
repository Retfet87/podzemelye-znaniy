"""Главное меню, меню расписания и меню домашних заданий."""
from app.max_api.keyboards import callback_button, inline_keyboard

HOME = inline_keyboard([
    [callback_button("Расписание", "schedule:open")],
    [callback_button("Домашние задания", "hw:open")],
    [callback_button("Мероприятия", "community:events:0"),
     callback_button("Полезное", "community:useful")],
])

MENU = inline_keyboard([
    [callback_button("Сегодня", "schedule:today"), callback_button("Завтра", "schedule:tomorrow")],
    [callback_button("Неделя", "schedule:week")],
    [callback_button("Назад", "schedule:back")],
])


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