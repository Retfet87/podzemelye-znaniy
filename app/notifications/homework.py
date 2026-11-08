"""Очередь уведомлений о домашних заданиях."""

from app.timeutil import format_deadline


def prepare(conn):
    # Build reminder jobs only when due, using current membership. Missing the
    # 24-hour window leaves the 3-hour reminder, and expired tasks generate none.
    conn.execute("""
        INSERT INTO homework_notifications
            (homework_id, user_id, kind, version, next_attempt, valid_until)
        SELECT h.id, u.max_user_id, reminder.kind, h.reminder_version,
               greatest(h.deadline - reminder.advance, now()), h.deadline - reminder.cutoff
        FROM homework h JOIN groups g ON g.id = h.group_id
        JOIN users u ON (h.owner_id IS NULL AND g.active AND u.group_id = h.group_id OR u.max_user_id = h.owner_id)
        CROSS JOIN (VALUES ('24h', interval '24 hours', interval '3 hours'),
                           ('3h', interval '3 hours', interval '0 hours')) reminder(kind, advance, cutoff)
        WHERE h.deadline > now() + reminder.cutoff AND h.deadline <= now() + reminder.advance
          AND NOT EXISTS (SELECT 1 FROM homework_notifications n WHERE n.homework_id = h.id
                          AND n.user_id = u.max_user_id AND n.kind = reminder.kind
                          AND n.version = h.reminder_version)
        ON CONFLICT DO NOTHING
    """)
    conn.execute("""
        UPDATE homework_notifications
        SET status = CASE WHEN status = 'sending' THEN 'failed' ELSE 'skipped' END,
            last_error = CASE WHEN status = 'sending' THEN 'unconfirmed' ELSE 'expired' END
        WHERE status IN ('pending', 'sending') AND (valid_until <= now() OR attempts = 5)
    """)


def next_job(conn):
    return conn.execute("""
        SELECT n.id, n.user_id, n.attempts, n.next_attempt, h.subject, h.deadline, n.kind,
               g.name, coalesce(p.enabled, true) AND
                   CASE WHEN h.owner_id IS NULL THEN g.active AND coalesce(u.group_id = h.group_id, false)
                        ELSE h.owner_id = n.user_id END AND
                   (n.kind = 'new' OR n.version = h.reminder_version) AS eligible
        FROM homework_notifications n JOIN homework h ON h.id = n.homework_id
        JOIN groups g ON g.id = h.group_id JOIN users u ON u.max_user_id = n.user_id
        LEFT JOIN homework_preferences p ON p.user_id = n.user_id AND p.category =
            CASE WHEN n.kind = 'new' THEN 'new' WHEN h.owner_id IS NULL THEN 'group_reminder'
                 ELSE 'personal_reminder' END
        WHERE n.status IN ('pending', 'sending') AND n.next_attempt <= now()
        ORDER BY n.next_attempt, n.id LIMIT 1
    """).fetchone()


def notification_body(subject, deadline, kind, group):
    if kind == "new":
        return f"Новое общее задание · {group}\n{subject}\n{format_deadline(deadline)}\nОткрой /homework."
    return f"Приближается срок задания по предмету «{subject}».\nСрок: {format_deadline(deadline)}\nОткрой /homework."
