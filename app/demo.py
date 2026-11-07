"""Dedicated demo environment. Never reads production configuration or credentials."""
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
import os

import psycopg
from psycopg.conninfo import conninfo_to_dict

from app.db import Database
from app.handlers.context import Context
from app.max_api.client import MaxClient

LABEL = 'ДЕМО · вымышленные данные\n'
REQUEST = ContextVar('demo_request', default=None)


def verify_demo(conn):
    """The marker is provisioned by the isolated container, never by a seed."""
    if not conn.execute("SELECT to_regclass('demo_environment')").fetchone()[0]:
        raise ValueError('Нет маркера изолированной demo-БД. Запись запрещена.')
    if conn.execute('SELECT environment FROM demo_environment').fetchall() != [('podzemelye-demo-v1',)]:
        raise ValueError('Неверный маркер demo-БД. Запись запрещена.')


class DemoDatabase(Database):
    def __init__(self, dsn, *, pool_size=0, **kwargs):
        if pool_size:
            raise ValueError('DemoDatabase использует только прямые проверяемые соединения.')
        super().__init__(dsn, pool_size=0, **kwargs)

    def connect(self):
        if self._scope.get() is not None:
            return super().connect()  # Outer unit of work already verified the demo marker.
        conn = super().connect()
        try:
            verify_demo(conn)
        except BaseException:
            conn.close()
            raise
        return conn


def demo_database():
    # No load_config(), .env, TEST_DATABASE_URL or DATABASE_URL fallback.
    dsn = os.environ.get('DEMO_DATABASE_URL', '')
    try:
        values = conninfo_to_dict(dsn) if dsn else {}
    except psycopg.Error:
        raise ValueError('Некорректный DEMO_DATABASE_URL.') from None
    if (values.get('host') not in ('127.0.0.1', '::1', 'demo-postgres')
            or values.get('dbname') != 'podzemelye_demo'
            or values.get('user') != 'demo_app'
            or not values.get('password')
            or set(values) - {'host', 'port', 'dbname', 'user', 'password', 'connect_timeout'}):
        raise ValueError('Нужен явный DEMO_DATABASE_URL для demo_app в изолированной podzemelye_demo.')
    return DemoDatabase(dsn)


@dataclass(frozen=True)
class DemoPolicy:
    users: frozenset
    switch: Path
    enabled: bool

    @classmethod
    def from_env(cls):
        values = os.environ.get('DEMO_ALLOWED_USER_IDS', '').split(',')
        if not values or any(not v.isascii() or not v.isdecimal() or not 0 < int(v) < 2**63 for v in values):
            raise ValueError('Задай явный список DEMO_ALLOWED_USER_IDS без пробелов.')
        return cls(frozenset(map(int, values)), Path('/run/demo/enabled'), os.environ.get('DEMO_ENABLED') == '1')

    def permits(self, actor):
        if not self.enabled or actor not in self.users:
            return False
        try:
            return self.switch.read_bytes() == b'1\n'
        except OSError:
            return False


class DemoClient(MaxClient):
    """Final transport guard; no worker or arbitrary recipient can send demo content."""
    def __init__(self, token, context, policy):
        super().__init__(token, context)
        self.policy = policy

    def send_message(self, recipient, text, *, attachments=None):
        # /start historically addresses a private chat_id. In demo every message
        # is addressed to the authenticated actor, never an incoming chat_id.
        active = REQUEST.get()
        if active is not None and set(recipient) == {'chat_id'}:
            recipient = {'user_id': active[0]}
        return super().send_message(recipient, text, attachments=attachments)

    def upload_file(self, name, data):
        return super().upload_file('DEMO_' + name, data)

    def request(self, method, path, params, *, payload=None, timeout=15):
        if (method, path) == ('GET', '/updates'):
            return super().request(method, path, params, timeout=timeout)
        active = REQUEST.get()
        if active is None or not self.policy.permits(active[0]):
            raise ValueError('ДЕМО выключено или доступ запрещён.')
        actor, callback = active
        if (method, path) == ('POST', '/messages') and params == {'user_id': actor}:
            payload = dict(payload)
            payload['text'] = LABEL + (payload.get('text') or '')[:4000-len(LABEL)]
        elif (method, path) == ('POST', '/answers') and callback and params == {'callback_id': callback}:
            payload = dict(payload)
            if 'message' in payload:
                message = dict(payload['message'])
                message['text'] = LABEL + (message.get('text') or '')[:4000-len(LABEL)]
                payload['message'] = message
            else:
                payload['notification'] = LABEL + (payload.get('notification') or '')[:200]
        elif not ((method, path) == ('POST', '/uploads') or (method == 'GET' and path.startswith('/videos/'))):
            raise ValueError('ДЕМО: отправка этому адресату или операция запрещена.')
        return super().request(method, path, params, payload=payload, timeout=timeout)


class DemoRouter:
    def __init__(self, router, policy):
        self.router, self.policy = router, policy

    def dispatch(self, update):
        ctx = Context.parse(self.router.deps, update)
        if not ctx.private or not self.policy.permits(ctx.actor):
            return
        token = REQUEST.set((ctx.actor, ctx.callback_id))
        try:
            return self.router.dispatch(update)
        finally:
            REQUEST.reset(token)
