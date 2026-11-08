"""Общие помощники тестов: вызов роутера в стиле старого handle_update."""
from app.handlers import Deps, build_router


def handle_update(client, store, update, administrator=None, homework=None, community=None):
    """store — Mock или тестовая БД, играющая роль и БД, и репозиториев."""
    deps = Deps(client=client, db=store, users=store, schedules=store, admin=administrator)
    build_router(deps, homework=homework, community=community).dispatch(update)
