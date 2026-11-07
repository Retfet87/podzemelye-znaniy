"""Обработчики событий MAX.

Порядок в списке важен для сообщений: сначала команды администратора, затем
активные диалоги (мероприятия, задания), и только потом /start.
Кнопки (callback) различаются по префиксу payload, поэтому порядок для них неважен.
"""
from app.handlers.admin import AdminHandler
from app.handlers.community import CommunityHandler
from app.handlers.context import Context, Deps
from app.handlers.homework import HomeworkHandler
from app.handlers.registration import RegistrationHandler
from app.handlers.router import Router
from app.handlers.schedule import ScheduleHandler


def build_router(deps, *, homework=None, community=None):
    return Router(deps, [
        AdminHandler(),
        community or CommunityHandler(),
        homework or HomeworkHandler(),
        RegistrationHandler(),
        ScheduleHandler(),
    ])


__all__ = ["Context", "Deps", "Router", "build_router"]
