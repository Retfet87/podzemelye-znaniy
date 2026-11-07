"""Очередь уведомлений об обновлении расписания."""
from app.notifications.common import MAX_ATTEMPTS


def expire(conn):
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


def next_job(conn):
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
