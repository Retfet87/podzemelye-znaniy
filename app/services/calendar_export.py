"""RFC 5545 exports. No attendee lists, MAX IDs or personal homework."""
import unicodedata
from datetime import UTC, datetime, time, timezone
from hashlib import sha256

from app.services.schedule_formatter import BELLS
from app.timeutil import SAMARA


def escape(value):
    value = str(value).replace('\r\n', '\n').replace('\r', '\n')
    value = ''.join(c for c in value if c == '\n' or not unicodedata.category(c).startswith('C'))
    return value.replace('\\', '\\\\').replace('\n', '\\n').replace(';', '\\;').replace(',', '\\,')


def fold(line):
    # Fold on UTF-8 character boundaries; continuation space counts toward 75 octets.
    parts, chunk = [], ''
    for char in line:
        if len((chunk + char).encode()) > 75:
            parts.append(chunk)
            chunk = ' '
        chunk += char
    return '\r\n'.join(parts + [chunk])


def calendar(items, *, stamp=None):
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//Podzemelye//MAX calendar//RU',
             'CALSCALE:GREGORIAN', 'BEGIN:VTIMEZONE', 'TZID:Europe/Samara',
             'BEGIN:STANDARD', 'DTSTART:20141026T020000', 'TZOFFSETFROM:+0400',
             'TZOFFSETTO:+0400', 'TZNAME:+04', 'END:STANDARD', 'END:VTIMEZONE']
    stamp = (stamp or datetime.now(UTC)).astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')
    for key, title, start, end, location, description in items:
        uid = sha256(key.encode()).hexdigest() + '@podzemelye'
        lines += ['BEGIN:VEVENT', 'UID:' + uid, 'DTSTAMP:' + stamp,
                  'DTSTART;TZID=Europe/Samara:' + start.astimezone(SAMARA).strftime('%Y%m%dT%H%M%S')]
        if end is not None:
            lines += ['DTEND;TZID=Europe/Samara:' + end.astimezone(SAMARA).strftime('%Y%m%dT%H%M%S')]
        lines += ['SUMMARY:' + escape(title), 'LOCATION:' + escape(location),
                  'DESCRIPTION:' + escape(description), 'END:VEVENT']
    lines += ['END:VCALENDAR']
    return ('\r\n'.join(fold(line) for line in lines) + '\r\n').encode('utf-8')


def schedule_calendar(rows, *, stamp=None):
    items = []
    for day, group, version, anchor, pair, parity, subject, teacher, room, note in rows:
        if version is None:
            raise ValueError('На часть недели расписание ещё не загружено. Экспорт отменён.')
        if pair is None:
            continue
        if parity != 'all':
            if anchor is None:
                raise ValueError('Тип недели не подтверждён. Сначала уточни числитель у администратора.')
            current = 'denominator' if ((day - anchor).days // 7) % 2 else 'numerator'
            if parity != current:
                continue
        first, last = BELLS[pair - 1].split('–')
        start = datetime.combine(day, time.fromisoformat(first), SAMARA)
        end = datetime.combine(day, time.fromisoformat(last), SAMARA)
        # Slot identity stays stable after a subject/room substitution.
        key = f'akvt/schedule/{group}/{day}/{pair}'
        items.append((key, f'{group} · {subject}', start, end, room or '',
                      ' · '.join(x for x in (teacher, note) if x)))
    if not items:
        raise ValueError('На выбранную неделю занятий нет. Файл не создан.')
    return calendar(items, stamp=stamp)


def event_calendar(event_id, row):
    title, body, start, place, state, *_ = row
    if state != 'published':
        raise ValueError('Экспорт доступен только для опубликованного мероприятия.')
    # No invented duration: DTSTART alone is valid for events without a known end.
    return calendar([(f'akvt/event/{event_id}', title, start, None, place, body)])
