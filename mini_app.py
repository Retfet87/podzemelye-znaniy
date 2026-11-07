"""Disabled-by-default local web server. No poller, migrations or real MAX requests."""
import logging
import os
from wsgiref.simple_server import WSGIRequestHandler, make_server

from app.db import Database,local_dsn
from app.demo import DemoPolicy,demo_database
from app.web import MiniApp,ReadViews


class QuietHandler(WSGIRequestHandler):
    # Never put auth headers or user-controlled query strings in access logs.
    def log_message(self,*args):
        pass


def main():
    mode=os.environ.get('WEB_MODE','off')
    if mode not in ('demo','real'):
        raise ValueError('Web-сервер выключен. Задай WEB_MODE для отдельного разрешённого окружения.')
    token=os.environ.get('WEB_MAX_BOT_TOKEN','')
    if not token or not token.isascii() or any(c.isspace() or not c.isprintable() for c in token):
        raise ValueError('Нужен явный WEB_MAX_BOT_TOKEN для проверки подписи, не для polling.')
    policy=DemoPolicy.from_env() if mode=='demo' else None
    db=demo_database() if mode=='demo' else Database(local_dsn({'WEB_DATABASE_URL':os.environ.get('WEB_DATABASE_URL')},'WEB_DATABASE_URL'))
    db.check_schema()
    if mode=='real':
        with db.connect() as conn:
            if conn.execute("SELECT to_regclass('demo_environment')").fetchone()[0]:
                raise ValueError('Demo-БД запрещена в real web-режиме.')
    # Loopback only; HTTPS reverse proxy is a separate deployment step.
    with make_server('127.0.0.1',8080,MiniApp(ReadViews(db),token,demo_policy=policy),handler_class=QuietHandler) as server:
        server.serve_forever()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from None
