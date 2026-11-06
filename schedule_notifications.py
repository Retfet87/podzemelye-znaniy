"""Persistent MAX delivery. The session lock serializes notification workers."""
import logging

import psycopg
from psycopg import sql

from homework_notifications import next_job, notification_body, prepare

logger = logging.getLogger(__name__)
WORKER_LOCK = 281952766
MAX_ATTEMPTS = 5


def deliver_one(store, send, max_error, *, homework=True, schedule=True):
    with store.connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK,)).fetchone()[0]:
            return False
        if homework:
            # Hold through delivery so deletion, edits and preference updates serialize with sends.
            conn.execute("SELECT pg_advisory_lock(281952767)")
            prepare(conn)
        if schedule:
            expire_schedule(conn)
        if not conn.execute("SELECT next_send <= now() FROM schedule_notification_rate").fetchone()[0]:
            return False
        shared = next_schedule(conn) if schedule else None
        task = next_job(conn) if homework else None
        if task and (not shared or task[3] < shared[5]):
            job, user, attempts, _, subject, deadline, kind, group, eligible = task
            table = "homework_notifications"
            condition = sql.SQL("id = %s")
            key = (job,)
            body = notification_body(subject, deadline, kind, group)
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


def expire_schedule(conn):
    conn.execute(
        """UPDATE schedule_notifications SET status = 'skipped', last_error = 'expired'
           WHERE status = 'pending'
             AND valid_through < (clock_timestamp() AT TIME ZONE 'Europe/Samara')::date"""
    )
    conn.execute(
        """UPDATE schedule_notifications
           SET status = 'failed', last_error = 'unconfirmed'
           WHERE status = 'sending' AND next_attempt <= now()
             AND (attempts = %s OR
                  valid_through < (clock_timestamp() AT TIME ZONE 'Europe/Samara')::date)""",
        (MAX_ATTEMPTS,),
    )


def next_schedule(conn):
    return conn.execute(
        """SELECT n.import_id, n.user_id, n.body, n.attempts,
                  coalesce(u.group_id = n.group_id AND g.active, false), n.next_attempt
           FROM schedule_notifications n JOIN users u ON u.max_user_id = n.user_id
           JOIN groups g ON g.id = n.group_id
           WHERE n.status IN ('pending', 'sending') AND n.next_attempt <= now()
           AND NOT EXISTS (
               SELECT 1 FROM schedule_notifications earlier
               WHERE earlier.user_id = n.user_id AND earlier.import_id < n.import_id
                 AND earlier.status IN ('pending', 'sending')
           )
           ORDER BY n.next_attempt, n.import_id, n.user_id LIMIT 1"""
    ).fetchone()


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


def work_notifications(store, send, max_error, stop, *, homework=True, schedule=True):
    while not stop.is_set():
        try:
            worked = deliver_one(store, send, max_error, homework=homework, schedule=schedule)
        except psycopg.Error:
            logger.warning("PostgreSQL: очередь уведомлений временно недоступна.")
            worked = False
        stop.wait(1 if worked else 3)
