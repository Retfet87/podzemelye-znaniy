"""Separate, explicitly gated demo bot; no production config or notifications."""
import logging
import os
import ssl

from app.demo import DemoClient, DemoPolicy, DemoRouter, demo_database
from app.handlers import Deps, build_router
from app.polling import run_polling


def main():
    if os.environ.get('DEMO_POLLING_ALLOWED') != '1':
        raise ValueError('Demo polling выключен. Нужны отдельный тестовый токен и явное разрешение запуска.')
    token = os.environ.get('DEMO_BOT_TOKEN', '')
    if not token or not token.isascii() or any(c.isspace() or not c.isprintable() for c in token):
        raise ValueError('Нужен отдельный DEMO_BOT_TOKEN.')
    policy = DemoPolicy.from_env()
    if not policy.enabled:
        raise ValueError('ДЕМО выключено.')
    db = demo_database()
    db.check_schema()
    context = ssl.create_default_context()
    context.load_verify_locations(cafile='certificates/russian_trusted_root_ca.pem')
    client = DemoClient(token, context, policy)
    # No administrator publication commands and no notification thread.
    router = DemoRouter(build_router(Deps.create(client, db)), policy)
    run_polling(client, router)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from None
