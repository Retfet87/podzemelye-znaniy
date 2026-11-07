"""Versioned useful fixtures, kept separate from all production data."""
import json
from pathlib import Path

from app.demo import verify_demo
from app.services.validation import validate_link

FIXTURES = Path(__file__).resolve().parents[1] / 'fixtures/demo/useful.json'


def seed_useful(conn):
    verify_demo(conn)
    conn.execute('SELECT pg_advisory_xact_lock(7210196)')
    conn.execute('''CREATE TABLE IF NOT EXISTS demo_fixture_keys (
        namespace text NOT NULL, fixture_key text NOT NULL,
        record_id bigint UNIQUE NOT NULL REFERENCES useful_materials ON DELETE CASCADE,
        PRIMARY KEY (namespace, fixture_key))''')
    added = 0
    for card in json.loads(FIXTURES.read_text()):
        if conn.execute("SELECT 1 FROM demo_fixture_keys WHERE namespace = 'useful-v1' AND fixture_key = %s", (card['key'],)).fetchone():
            continue
        validate_link(card['link'])
        if not (len(card['title']) <= 120 and len(card['body']) <= 1800 and card['title'].startswith('ДЕМО')):
            raise ValueError('Некорректная demo fixture.')
        item = conn.execute('''INSERT INTO useful_materials (group_id,category,title,body,link)
            VALUES (NULL,%s,%s,%s,%s) RETURNING id''',
            tuple(card[k] for k in ('category','title','body','link'))).fetchone()[0]
        conn.execute("INSERT INTO demo_records VALUES ('material',%s)", (item,))
        if card['key'] == 'kruto-19':
            conn.execute("INSERT INTO useful_files VALUES (%s,1,'DEMO_notes.txt',%s)",
                         (item, card['body'].encode('utf-8')))
        conn.execute("INSERT INTO demo_fixture_keys VALUES ('useful-v1',%s,%s)", (card['key'],item))
        added += 1
    return added


def clean_useful(conn):
    verify_demo(conn)
    conn.execute('SELECT pg_advisory_xact_lock(7210196)')
    if not conn.execute("SELECT to_regclass('demo_fixture_keys')").fetchone()[0]:
        return
    conn.execute('''DELETE FROM useful_materials WHERE id IN
        (SELECT record_id FROM demo_fixture_keys WHERE namespace = 'useful-v1')
        AND id IN (SELECT record_id FROM demo_records WHERE kind = 'material')''')
    conn.execute("DELETE FROM demo_records WHERE kind = 'material' AND NOT EXISTS (SELECT 1 FROM useful_materials WHERE id = record_id)")
