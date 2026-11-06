"""Persistent MAX delivery. The session lock serializes notification workers."""
import logging

import psycopg

logger = logging.getLogger(__name__)
WORKER_LOCK = 281952766
MAX_ATTEMPTS = 5


def deliver_one(store, send, max_error):
    with store.connect() as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(%s)", (WORKER_LOCK,)).fetchone()[0]:
            return False
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
        if not conn.execute("SELECT next_send <= now() FROM schedule_notification_rate").fetchone()[0]:
            return False
        row = conn.execute(
            """SELECT n.import_id, n.user_id, n.body, n.attempts,
                      coalesce(u.group_id = n.group_id AND g.active, false)
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
        if not row:
            return False
        version, user, body, attempts, eligible = row
        key = (version, user)
        if not eligible:
            conn.execute(
                """UPDATE schedule_notifications SET status = 'skipped', last_error = 'group_changed'
                   WHERE import_id = %s AND user_id = %s""", key,
            )
            return True
        conn.execute(
            """UPDATE schedule_notifications
               SET status = 'sending', attempts = attempts + 1, last_attempt = clock_timestamp(),
                   next_attempt = clock_timestamp() + interval '2 minutes'
               WHERE import_id = %s AND user_id = %s""", key,
        )
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
            """UPDATE schedule_notifications SET status = %s, last_error = %s,
                   next_attempt = clock_timestamp() + %s * interval '1 second',
                   sent_at = CASE WHEN %s = 'sent' THEN clock_timestamp() END
               WHERE import_id = %s AND user_id = %s""",
            (status, code, delay, status, *key),
        )
        return True


def work_notifications(store, send, max_error, stop):
    while not stop.is_set():
        try:
            worked = deliver_one(store, send, max_error)
        except psycopg.Error:
            logger.warning("PostgreSQL: очередь уведомлений временно недоступна.")
            worked = False
        stop.wait(1 if worked else 3)
