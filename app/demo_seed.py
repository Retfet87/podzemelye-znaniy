"""Versioned demo fixtures from site.json, kept separate from all production data."""
from pathlib import Path

from app.demo import verify_demo
from app.demo_site_data import events as site_events, useful as site_useful
from app.services.validation import validate_link

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures/demo/useful.json'
NAMESPACE = 'site-v1'
LEGACY_EVENT_TITLES = (
    'ДЕМО — студенческий хакатон',
    'ДЕМО — творческая мастерская',
    'ДЕМО — спортивная встреча',
    'ДЕМО — как прошёл учебный квест',
)


def _ensure_tables(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS demo_fixture_keys (
        namespace text NOT NULL,
        fixture_key text NOT NULL,
        record_id bigint UNIQUE NOT NULL REFERENCES useful_materials ON DELETE CASCADE,
        PRIMARY KEY (namespace, fixture_key))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS demo_fixture_events (
        namespace text NOT NULL,
        fixture_key text NOT NULL,
        record_id bigint UNIQUE NOT NULL REFERENCES events ON DELETE CASCADE,
        PRIMARY KEY (namespace, fixture_key))''')


def seed_useful(conn):
    verify_demo(conn)
    conn.execute('SELECT pg_advisory_xact_lock(7210196)')
    _ensure_tables(conn)
    added = 0
    for card in site_useful():
        if conn.execute(
            "SELECT 1 FROM demo_fixture_keys WHERE namespace IN ('site-v1', 'useful-v1') AND fixture_key = %s",
            (card['key'],),
        ).fetchone():
            continue
        validate_link(card['link'])
        if not (len(card['title']) <= 120 and len(card['body']) <= 1800 and card['title'].startswith('ДЕМО')):
            raise ValueError('Некорректная demo fixture.')
        item = conn.execute(
            '''INSERT INTO useful_materials (group_id, category, title, body, link)
               VALUES (NULL, %s, %s, %s, %s) RETURNING id''',
            (card['category'], card['title'], card['body'], card['link']),
        ).fetchone()[0]
        conn.execute("INSERT INTO demo_records VALUES ('material', %s) ON CONFLICT DO NOTHING", (item,))
        for slot, (fname, raw_bytes) in enumerate(card.get('files', ()), 1):
            conn.execute(
                'INSERT INTO useful_files VALUES (%s, %s, %s, %s)',
                (item, slot, fname, raw_bytes),
            )
        conn.execute("INSERT INTO demo_fixture_keys VALUES (%s, %s, %s)", (NAMESPACE, card['key'], item))
        added += 1
    return added


def clean_useful(conn):
    verify_demo(conn)
    conn.execute('SELECT pg_advisory_xact_lock(7210196)')
    if not conn.execute("SELECT to_regclass('demo_fixture_keys')").fetchone()[0]:
        return
    conn.execute('''DELETE FROM useful_materials WHERE id IN
        (SELECT record_id FROM demo_fixture_keys WHERE namespace IN ('site-v1', 'useful-v1'))
        AND id IN (SELECT record_id FROM demo_records WHERE kind = 'material')''')
    conn.execute(
        "DELETE FROM demo_records WHERE kind = 'material' "
        "AND NOT EXISTS (SELECT 1 FROM useful_materials WHERE id = record_id)"
    )


def seed_events(conn, *, date_shift_days: int = 0):
    verify_demo(conn)
    conn.execute('SELECT pg_advisory_xact_lock(7210196)')
    _ensure_tables(conn)
    added = 0
    for item in site_events(date_shift_days=date_shift_days):
        if conn.execute(
            "SELECT 1 FROM demo_fixture_events WHERE namespace = %s AND fixture_key = %s",
            (NAMESPACE, item['key']),
        ).fetchone():
            continue
        event_id = conn.execute(
            '''INSERT INTO events
               (title, description, starts_at, place, state, tags, report_body, report_link, report_published)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id''',
            (
                item['title'],
                item['description'],
                item['starts_at'],
                item['place'],
                item['state'],
                item['tags'],
                item['report_body'],
                item['report_link'],
                item['report_published'],
            ),
        ).fetchone()[0]
        conn.execute("INSERT INTO demo_records VALUES ('event', %s) ON CONFLICT DO NOTHING", (event_id,))
        for slot, (kind, fname, raw_bytes) in enumerate(item['media'], 1):
            conn.execute(
                'INSERT INTO event_media VALUES (%s, %s, %s, %s, %s, %s)',
                (event_id, item['media_section'], slot, kind, fname, raw_bytes),
            )
        conn.execute(
            "INSERT INTO demo_fixture_events VALUES (%s, %s, %s)",
            (NAMESPACE, item['key'], event_id),
        )
        added += 1
    return added


def clean_events(conn):
    verify_demo(conn)
    conn.execute('SELECT pg_advisory_xact_lock(7210196)')
    if conn.execute("SELECT to_regclass('demo_fixture_events')").fetchone()[0]:
        conn.execute('''DELETE FROM events WHERE id IN
            (SELECT record_id FROM demo_fixture_events WHERE namespace = 'site-v1')
            AND id IN (SELECT record_id FROM demo_records WHERE kind = 'event')''')
    # Удаление старых демо-событий (до введения demo_fixture_events) строго по пересечению с demo_records
    conn.execute(
        '''DELETE FROM events WHERE title = ANY(%s)
           AND id IN (SELECT record_id FROM demo_records WHERE kind = 'event')''',
        (list(LEGACY_EVENT_TITLES),),
    )
    conn.execute(
        "DELETE FROM demo_records WHERE kind = 'event' "
        "AND NOT EXISTS (SELECT 1 FROM events WHERE id = record_id)"
    )


def refresh_site_demo(conn, *, date_shift_days: int = 0) -> tuple[int, int]:
    """Удаляет только помеченные демо-карточки Полезного и Мероприятий и пересоздаёт их из site.json."""
    verify_demo(conn)
    conn.execute('SELECT pg_advisory_xact_lock(7210196)')
    clean_useful(conn)
    clean_events(conn)
    u_count = seed_useful(conn)
    e_count = seed_events(conn, date_shift_days=date_shift_days)
    return u_count, e_count