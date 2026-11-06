from community import event_text


def prepare(conn):
    conn.execute("""
        INSERT INTO event_notifications (event_id, user_id, kind, version, valid_until)
        SELECT e.id, p.user_id, 'reminder', e.reminder_version, e.starts_at
        FROM events e JOIN event_participants p ON p.event_id = e.id
        WHERE e.state = 'published' AND e.starts_at > now()
          AND e.starts_at <= now() + interval '24 hours'
          AND NOT EXISTS (
              SELECT 1 FROM event_notifications n WHERE n.event_id = e.id AND n.user_id = p.user_id
                AND n.kind = 'reminder' AND n.version = e.reminder_version)
        ON CONFLICT DO NOTHING
    """)
    conn.execute("""
        UPDATE event_notifications
        SET status = CASE WHEN status = 'sending' THEN 'failed' ELSE 'skipped' END,
            last_error = CASE WHEN status = 'sending' THEN 'unconfirmed' ELSE 'expired' END
        WHERE (status = 'pending' AND valid_until <= now())
           OR (status = 'sending' AND next_attempt <= now()
               AND (valid_until <= now() OR attempts = 5))
    """)


def next_job(conn):
    row = conn.execute("""
        SELECT n.id, n.user_id, n.attempts, n.next_attempt, n.kind, n.body,
               e.title, e.description, e.starts_at, e.place,
               p.user_id IS NOT NULL AND CASE n.kind
                  WHEN 'reminder' THEN e.state = 'published' AND e.starts_at > now()
                                       AND n.version = e.reminder_version
                  WHEN 'changed' THEN e.state = 'published' AND e.starts_at > now()
                                      AND n.version = e.revision
                  ELSE e.state = 'cancelled'
               END
        FROM event_notifications n JOIN events e ON e.id = n.event_id
        LEFT JOIN event_participants p ON p.event_id = n.event_id AND p.user_id = n.user_id
        WHERE n.status IN ('pending', 'sending') AND n.next_attempt <= now()
        ORDER BY n.next_attempt, n.id LIMIT 1
    """).fetchone()
    if row is None:
        return None
    job, user, attempts, due, kind, body, title, description, starts, place, eligible = row
    if kind == "reminder":
        body = "Напоминание о мероприятии\n" + event_text(title, description, starts, place)
    return job, user, attempts, due, body, eligible
