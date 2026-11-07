"""Надёжная доставка уведомлений в MAX. Сессионная блокировка сериализует рабочие потоки."""
import logging

import psycopg
from psycopg import sql

from app.db.locks import EVENT_LOCK, HOMEWORK_LOCK, NOTIFICATION_WORKER_LOCK
from app.notifications import events as event_queue
from app.notifications import homework as homework_queue
from app.notifications import schedule as schedule_queue
from app.notifications.common import MAX_ATTEMPTS

logger = logging.getLogger(__name__)


def deliver_one(store, send, max_error, *, homework=True, schedule=True, events=False):
    with store.connect() as conn:
        # Demo data must never enter delivery, even with misconfigured flags.
        if conn.execute("SELECT to_regclass('demo_environment')").fetchone()[0]:
            return False
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (NOTIFICATION_WORKER_LOCK,)).fetchone()[0]:
            return False
        if homework:
            # Hold through delivery so deletion, edits and preference updates serialize with sends.
            conn.execute("SELECT pg_advisory_lock(%s)", (HOMEWORK_LOCK,))
            homework_queue.prepare(conn)
        if events:
            conn.execute("SELECT pg_advisory_lock(%s)", (EVENT_LOCK,))
            event_queue.prepare(conn)
        if schedule:
            schedule_queue.expire(conn)
        if not conn.execute("SELECT next_send <= now() FROM schedule_notification_rate").fetchone()[0]:
            return False
        shared = schedule_queue.next_job(conn) if schedule else None
        task = homework_queue.next_job(conn) if homework else None
        event = event_queue.next_job(conn) if events else None
        if event and (not task or event[3] <= task[3]) and (not shared or event[3] <= shared[5]):
            job, user, attempts, _, body, eligible = event
            table = "event_notifications"
            condition = sql.SQL("id = %s")
            key = (job,)
            skip_reason = "ineligible"
        elif task and (not shared or task[3] < shared[5]):
            job, user, attempts, _, subject, deadline, kind, group, eligible = task
            table = "homework_notifications"
            condition = sql.SQL("id = %s")
            key = (job,)
            body = homework_queue.notification_body(subject, deadline, kind, group)
            skip_reason = "ineligible"
        elif shared:
            version, user, body, attempts, eligible, _ = shared
            table = "schedule_notifications"
            condition = sql.SQL("import_id = %s AND user_id = %s")
            key = (version, user)
            skip_reason = "group_changed"
        else:
            return False
        return send_job(conn, send, max_error, table, condition, key, user, body, attempts,
                        eligible, skip_reason)


def send_job(conn, send, max_error, table, condition, key, user, body, attempts, eligible, skip_reason):
    def update(fields):
        return sql.SQL("UPDATE {} SET {} WHERE {}").format(sql.Identifier(table), sql.SQL(fields), condition)

    if not eligible:
        conn.execute(update("status = 'skipped', last_error = %s"), (skip_reason, *key))
        return True
    conn.execute(update(
        """status = 'sending', attempts = attempts + 1, last_attempt = clock_timestamp(),
           next_attempt = clock_timestamp() + interval '2 minutes'"""
    ), key)
    conn.execute("UPDATE schedule_notification_rate SET next_send = clock_timestamp() + interval '1 second'")
    conn.commit()

    status, code, delay = "sent", None, 0
    try:
        send({"user_id": user}, body)
    except max_error as error:
        code = f"http_{error.status}" if error.status else "transport_or_response"
        status = "pending" if (error.retryable or error.status == 401) and attempts + 1 < MAX_ATTEMPTS else "failed"
        delay = max(30 * 2**attempts, error.retry_after or 0)
        if error.status in (401, 429):
            conn.execute(
                """UPDATE schedule_notification_rate
                   SET next_send = greatest(next_send, clock_timestamp() + %s * interval '1 second')""",
                (delay,),
            )
    conn.execute(
        update(
            """status = %s, last_error = %s,
               next_attempt = clock_timestamp() + %s * interval '1 second',
               sent_at = CASE WHEN %s = 'sent' THEN clock_timestamp() END"""
        ),
        (status, code, delay, status, *key),
    )
    return True


def work_notifications(store, send, max_error, stop, *, homework=True, schedule=True, events=False):
    while not stop.is_set():
        try:
            worked = deliver_one(store, send, max_error, homework=homework, schedule=schedule, events=events)
        except psycopg.Error:
            logger.warning("PostgreSQL: очередь уведомлений временно недоступна.")
            worked = False
        stop.wait(1 if worked else 3)
