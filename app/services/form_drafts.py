"""Allowlisted JSON only, never pickle; publication and consumption are atomic."""
from base64 import b64decode, b64encode
from dataclasses import fields
from datetime import datetime
from time import monotonic

from psycopg.types.json import Jsonb

from app.handlers.common import DRAFT_TTL, MAX_DRAFTS


def encode(value):
    if isinstance(value, bytes):
        return {'$bytes': b64encode(value).decode('ascii')}
    if isinstance(value, datetime):
        return {'$date': value.isoformat()}
    if isinstance(value, (list, tuple)):
        return [encode(x) for x in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise ValueError('Unsupported draft value')


def decode(value):
    if isinstance(value, dict):
        if set(value) == {'$bytes'}:
            return b64decode(value['$bytes'], validate=True)
        if set(value) == {'$date'}:
            return datetime.fromisoformat(value['$date'])
        raise ValueError('Invalid draft encoding')
    if isinstance(value, list):
        return [decode(x) for x in value]
    return value


def stores(handlers):
    return {('homework' if type(h).__name__ == 'HomeworkHandler' else 'community'): h.drafts
            for h in handlers if h.drafts is not None}


def load(conn, actor, handlers):
    from app.handlers.community import Draft as CommunityDraft
    from app.handlers.homework import Draft as HomeworkDraft
    mapping = stores(handlers)
    previous = {flow: store.get(actor) for flow, store in mapping.items()}
    for store in mapping.values():
        store.pop(actor, None)
    conn.execute('''DELETE FROM form_drafts WHERE user_id IN
                    (SELECT user_id FROM form_drafts WHERE expires_at <= clock_timestamp()
                     FOR UPDATE SKIP LOCKED)''')
    row = conn.execute('SELECT flow,payload,extract(epoch FROM expires_at-clock_timestamp()) FROM form_drafts WHERE user_id=%s', (actor,)).fetchone()
    if row:
        flow, payload, remaining = row
        cls = HomeworkDraft if flow == 'homework' else CommunityDraft
        allowed = {f.name for f in fields(cls)} - {'touched'}
        if set(payload) - allowed:
            raise ValueError('Unknown saved draft fields')
        draft = cls(**{k: decode(v) for k, v in payload.items()})
        draft.touched = monotonic() - DRAFT_TTL + float(remaining)
        old = previous.get(flow)
        if isinstance(old, cls):
            for f in fields(cls):
                setattr(old, f.name, getattr(draft, f.name))
            draft = old
        mapping[flow][actor] = draft


def save(conn, actor, handlers):
    drafts = [(flow, store[actor]) for flow, store in stores(handlers).items() if actor in store]
    if not drafts:
        conn.execute('DELETE FROM form_drafts WHERE user_id=%s', (actor,))
        return
    flow, draft = drafts[0]
    remaining = DRAFT_TTL - (monotonic() - draft.touched)
    if remaining <= 0:
        conn.execute('DELETE FROM form_drafts WHERE user_id=%s', (actor,))
        return
    # Serialises only the short capacity check, not another user's MAX transfer.
    conn.execute('SELECT pg_advisory_xact_lock(739184224)')
    count = conn.execute('SELECT count(*) FROM form_drafts WHERE user_id<>%s AND expires_at>clock_timestamp()', (actor,)).fetchone()[0]
    if count >= MAX_DRAFTS:
        raise ValueError('Слишком много черновиков. Попробуй позже.')
    payload = {f.name: encode(getattr(draft, f.name)) for f in fields(draft) if f.name != 'touched'}
    conn.execute('''INSERT INTO form_drafts VALUES (%s,%s,%s,clock_timestamp()+%s*interval '1 second')
                    ON CONFLICT (user_id) DO UPDATE SET flow=excluded.flow,payload=excluded.payload,expires_at=excluded.expires_at''',
                 (actor, flow, Jsonb(payload), remaining))
