"""Bounded synthetic MAX-router benchmark. Never reads .env or contacts MAX.

PERFORMANCE_DATABASE_URL must be a separately provisioned *_test DB with the
bootstrap-owned validation_environment marker. Each run owns one temporary schema.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import math
import os
from pathlib import Path
import platform
from threading import Lock, local
from time import perf_counter, monotonic, sleep
from unittest.mock import patch
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.db import Database, local_dsn, migrate
from app.handlers import Deps, build_router
from app.handlers.community import Draft
from app.repositories.community import CommunityStore
from app.repositories.users import UserRepository
from app.services.schedule_parser import Lesson
from app.services.schedule_publisher import preview, decide
from app.timeutil import SAMARA


class Metrics:
    def __init__(self):
        self.lock = Lock()
        self.pids = set()
        self.opened = self.live = self.peak = self.queries = self.checkouts = 0


class CountingConnection(psycopg.Connection):
    metrics = None

    @classmethod
    def connect(cls, *args, **kwargs):
        conn = super().connect(*args, **kwargs)
        conn.measurements = cls.metrics
        with conn.measurements.lock:
            conn.measurements.pids.add(conn.info.backend_pid)
            conn.measurements.opened += 1
            conn.measurements.live += 1
            conn.measurements.peak = max(conn.measurements.peak, conn.measurements.live)
        return conn

    def execute(self, *args, **kwargs):
        with self.measurements.lock:
            self.measurements.queries += 1
        return super().execute(*args, **kwargs)

    def __del__(self):
        # psycopg's PGconn destructor closes a discarded maintenance connection
        # without calling Connection.close(). Account for the equivalent close
        # here, otherwise our instrumentation falsely reports a leak.
        if hasattr(self, 'measurements'):
            self.close()
        super().__del__()

    def close(self):
        was_open = not self.closed
        super().close()
        if was_open:
            with self.measurements.lock:
                self.measurements.live -= 1


class MeasuredDatabase(Database):
    @contextmanager
    def connect(self):
        with self.metrics.lock:
            self.metrics.checkouts += 1
        with super().connect() as conn:
            yield conn


class OfflineMAX:
    def __init__(self):
        self.current = local()

    def send_message(self, recipient, text, *, attachments=None):
        self.capture(text)

    def answer(self, callback_id, *, text=None, notification=None, attachments=None):
        self.capture(text or notification or '')

    def capture(self, text):
        self.current.responses += 1
        if any(value in text for value in ('База временно недоступна', 'Что-то пошло не так', 'Черновик закрыт')):
            self.current.failed = True


def checked_dsn(value):
    dsn = local_dsn({'PERFORMANCE_DATABASE_URL': value}, 'PERFORMANCE_DATABASE_URL')
    values = conninfo_to_dict(dsn)
    if not values['dbname'].endswith('_test') or set(values) - {'host', 'port', 'dbname', 'user', 'password'}:
        raise ValueError('Нужна отдельная *_test БД без libpq overrides.')
    with Database(dsn).connect() as conn:
        if not conn.execute("SELECT to_regclass('public.validation_environment')").fetchone()[0]:
            raise ValueError('Нет bootstrap-owned validation-маркера.')
        if conn.execute('SELECT environment FROM public.validation_environment').fetchall() != [('podzemelye-validation-v1',)]:
            raise ValueError('Неверный validation-маркер.')
        if not conn.execute("SELECT pg_get_userbyid(relowner) <> current_user FROM pg_class WHERE oid='public.validation_environment'::regclass").fetchone()[0]:
            raise ValueError('Validation-маркер должен принадлежать отдельной bootstrap-роли.')
        if conn.execute("SELECT has_table_privilege(current_user, 'public.validation_environment', 'UPDATE')").fetchone()[0]:
            raise ValueError('Используй ограниченную validation-роль, не владельца маркера.')
    return dsn


def fixture(db):
    migrate(db)
    users = UserRepository(db)
    users.add_groups(['PERF-01'])
    gid = users.active_groups()[0][0]
    now = datetime.now(SAMARA)
    monday = now.date() - timedelta(days=now.weekday())
    rows = [Lesson('PERF-01', day, pair, 'all', 'Тестовый предмет', '', '', '', ())
            for day in range(6) for pair in range(1, 4)]
    token, _, _, _ = preview(db, 899999, 'synthetic-performance', ['PERF-01'], rows,
                             monday, monday + timedelta(days=13), monday, scope='base', dry_run=False)
    assert decide(db, 899999, 899999, token, confirm=True) == 'applied'
    for actor in range(810000, 810100):
        users.start(actor)
        users.choose_group(actor, gid)
    with db.connect() as conn:
        conn.execute("INSERT INTO homework (group_id,subject,body,deadline) VALUES (%s,'Тестовый предмет','Синтетический пример',%s)",
                     (gid, now + timedelta(hours=12)))
    store = CommunityStore(db, 899999, 899999)
    event = store.save_event(Draft('event', title='Синтетическое событие', body='Без реального MAX',
                                   starts_at=now + timedelta(days=3)))
    assert store.transition(event, 1, True)
    return event


def measure(dsn, workload, count, workers, pool_size, event):
    import app.db.database as module
    metrics = Metrics()
    CountingConnection.metrics = metrics
    with patch.object(module.psycopg, 'connect', CountingConnection.connect):
        if pool_size:
            original = module.ConnectionPool
            def factory(*args, **kwargs):
                return original(*args, **kwargs, connection_class=CountingConnection)
            pool_patch = patch.object(module, 'ConnectionPool', factory)
        else:
            pool_patch = patch.object(module.psycopg, 'connect', CountingConnection.connect)
        with pool_patch:
            db = MeasuredDatabase(dsn, **({'pool_size': pool_size} if pool_size else {}))
            db.metrics = metrics
            client = OfflineMAX()
            router = build_router(Deps.create(client, db))
            def request(index):
                client.current.responses = 0
                client.current.failed = False
                actor = 810000 + index % 100
                start = perf_counter()
                error = False
                try:
                    if workload == 'start':
                        update = {'update_type':'message_created','message':{'sender':{'user_id':actor},
                                  'recipient':{'chat_type':'dialog'},'body':{'text':'/start'}}}
                    else:
                        payload = 'schedule:week' if workload == 'week' else f'community:event:{event}'
                        update = {'update_type':'message_callback','callback':{'callback_id':str(index),
                                  'user':{'user_id':actor},'payload':payload},'message':{'recipient':{'chat_type':'dialog'}}}
                    router.dispatch(update)
                    error = client.current.failed or client.current.responses != 1
                except Exception:
                    error = True
                return (perf_counter() - start) * 1000, int(error)
            started = perf_counter()
            try:
                if workers == 1:
                    samples = [request(i) for i in range(count)]
                else:
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        samples = list(executor.map(request, range(count)))
                elapsed = perf_counter() - started
            finally:
                if hasattr(db, 'close'):
                    db.close()
    latencies = sorted(sample[0] for sample in samples)
    # Independent server check AFTER timing and instrumentation; not a monitor
    # connection during the workload. No backend identifiers enter the report.
    with psycopg.connect(dsn, autocommit=True) as observer:
        deadline = monotonic() + 2
        while True:
            remaining = observer.execute('SELECT count(*) FROM pg_stat_activity WHERE pid=ANY(%s)',
                                         (list(metrics.pids),)).fetchone()[0]
            if not remaining or monotonic() >= deadline:
                break
            sleep(.01)
    assert remaining == 0, 'Server connections remain after database.close()'
    assert metrics.live == 0, 'Instrumented connection count did not return to zero'
    return {'workload':workload,'requests':count,'workers':workers,'pool_size':pool_size,
            'elapsed_seconds':round(elapsed,6),'mean_ms':round(sum(latencies)/count,3),
            'p95_ms':round(latencies[math.ceil(count*.95)-1],3),'errors':sum(x[1] for x in samples),
            'physical_connections_created':metrics.opened,'peak_established_connections':metrics.peak,
            'connections_after_close':metrics.live,'server_connections_after_close':remaining,'checkouts':metrics.checkouts,'sql_execute_calls':metrics.queries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pool-size', type=int, choices=range(0, 9), default=0)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    dsn = checked_dsn(os.environ.get('PERFORMANCE_DATABASE_URL'))
    schema = 'performance_' + uuid4().hex
    base = Database(dsn)
    with base.connect() as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
    try:
        scoped = make_conninfo(dsn, options=f'-c search_path={schema},public')
        event = fixture(Database(scoped))
        results = [measure(scoped, workload, n, w, args.pool_size, event)
                   for workload in ('start', 'week', 'event') for n, w in ((10,1),(50,1),(100,8))]
        with base.connect() as conn:
            pg = conn.execute('SHOW server_version').fetchone()[0]
        report = {'python':platform.python_version(),'machine':platform.machine(),'cpus':os.cpu_count(),
                  'postgres':pg,'dataset':{'students':100,'groups':1,'lessons':18,'shared_homework':1,'events':1},
                  'transport':'mock MAX, no network API; real PostgreSQL loopback SCRAM',
                  'p95':'nearest rank; latency includes queue wait and handler reply; elapsed excludes pool shutdown',
                  'peak':'instrumented established connections, including idle pool connections; no monitor connection',
                  'sql':'execute calls, including pool checks/reset; seed/provisioning excluded','results':results}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(results, indent=2))
        return 1 if any(r['errors'] for r in results) else 0
    finally:
        with base.connect() as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


if __name__ == '__main__':
    raise SystemExit(main())
