"""Validated XLS previews and transactional publication."""
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import date, datetime, timedelta
import re
import unicodedata
from uuid import uuid4

from psycopg.types.json import Jsonb

from app.db.locks import SCHEDULE_UPDATE_LOCK
from app.repositories.schedule import ScheduleRepository
from app.timeutil import SAMARA

LABELS = {"added": "Добавлено", "cancelled": "Отменено", "changed": "Изменено"}
WEEKS = {"numerator": "числитель", "denominator": "знаменатель", "all": ""}


def current_week_monday() -> date:
    today = datetime.now(SAMARA).date()
    return today - timedelta(days=today.weekday())


def normalized(lesson):
    def text(value):
        return " ".join(unicodedata.normalize("NFKC", value).casefold().split())

    def parts(value):
        return sorted({re.sub(r"[.\s]+", "", text(part)) for part in value.split(" / ")})

    return (text(lesson["subject"]), parts(lesson["teacher"]),
            parts(lesson["room"]), sorted(text(part) for part in lesson["note"].split(" / ")))


def day_slots(lessons, day, anchor):
    week = None if anchor is None else (
        "denominator" if ((day - anchor).days // 7) % 2 else "numerator")
    return {
        (x["pair"], x["parity"] if week is None else "all"): x
        for x in lessons if x["weekday"] == day.weekday()
        and (week is None or x["parity"] in ("all", week))
    }


def compare(group, day, old, new, visible):
    # Expand all-week rows when either side shows separate, unconfirmed variants.
    for pair in sorted({pair for pair, _ in old.keys() | new.keys()}):
        split = any((pair, week) in old or (pair, week) in new
                    for week in ("numerator", "denominator"))
        for week in ("numerator", "denominator") if split else ("all",):
            before = old.get((pair, week), old.get((pair, "all")))
            after = new.get((pair, week), new.get((pair, "all")))
            if before and after and normalized(before) == normalized(after):
                continue
            if before or after:
                yield dict(group=group, day=day.isoformat(), pair=pair, week=week,
                           kind=("changed" if before and after else
                                 "cancelled" if before else "added"),
                           before=before, after=after, visible=visible)


def describe(change):
    def lesson(x):
        return " · ".join(x[field] for field in ("subject", "teacher", "room", "note")
                          if x[field]) if x else "—"

    week = WEEKS[change["week"]]
    label = f" ({week})" if week else ""
    return (f'{change["day"]}, пара {change["pair"]}{label}. {LABELS[change["kind"]]}: '
            f'{lesson(change["before"])} → {lesson(change["after"])}')


def head(conn):
    return conn.execute("SELECT coalesce(max(id), 0) FROM schedule_imports").fetchone()[0]


def preview(store, owner, digest, names, lessons, start, end, anchor, *, scope,
            selected=(), expected_cancellations=0, dry_run=True):
    if (scope not in ("base", "date") or start > end or (end - start).days > 369
            or (scope == "date" and (start != end or not selected))
            or (scope == "base" and selected) or (anchor and anchor.weekday())):
        raise ValueError("Проверь режим, группы, даты и понедельник числителя.")
    minimum_start = datetime.now(SAMARA).date() if scope == "date" else current_week_monday()
    if not dry_run and (owner is None or start < minimum_start):
        raise ValueError("Для публикации нужны настроенный администратор и дата не раньше текущей недели.")
    by_group = defaultdict(list)
    names = set(names)
    groups = sorted(set(selected) if scope == "date" else names)
    if not groups or not set(groups) <= names:
        raise ValueError("В XLS отсутствует выбранная группа.")
    for item in lessons:
        if item.group in groups and (scope == "base" or item.weekday == start.weekday()):
            if item.subject == "Предмет не указан":
                raise ValueError("В выбранной области есть строка без предмета.")
            by_group[item.group].append(asdict(item))
    if scope == "base" and any(not by_group[name] for name in groups):
        raise ValueError("В базовом XLS есть пустая группа.")
    with store.connect() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEDULE_UPDATE_LOCK,))
        revision = head(conn)
        previous = conn.execute(
            """SELECT i.id, i.valid_from, i.valid_to, i.numerator_monday, i.scope, g.name
               FROM schedule_imports i JOIN schedule_groups sg ON sg.import_id = i.id
               JOIN groups g ON g.id = sg.group_id
               WHERE i.valid_from <= %s AND i.valid_to >= %s AND g.name = ANY(%s)
               ORDER BY (i.scope = 'date') DESC, i.id DESC""",
            (end, start, groups),
        ).fetchall()
        if scope == "base":
            if anchor is None and any(row[3] and row[4] == "base" for row in previous):
                raise ValueError("В действующем расписании известен тип недели. Укажи понедельник числителя.")
            latest = conn.execute(
                """SELECT g.name, count(l.pair)
                   FROM schedule_groups sg JOIN groups g ON g.id = sg.group_id
                   LEFT JOIN schedule_lessons l USING (import_id, group_id)
                   WHERE sg.import_id = (SELECT max(id) FROM schedule_imports WHERE scope = 'base')
                   GROUP BY g.name"""
            ).fetchall()
            if latest and {name for name, _ in latest} != names:
                raise ValueError("Состав групп базового XLS отличается от действующей версии.")
            if any(len(by_group[name]) * 5 < count * 4 for name, count in latest):
                raise ValueError("В одной из групп потеряно более 20% строк. Проверь полноту XLS.")
        old_lessons = defaultdict(list)
        for version, group, weekday, pair, parity, subject, teacher, room, note in conn.execute(
            """SELECT l.import_id, g.name, l.weekday, l.pair, l.parity, l.subject,
                      l.teacher, l.room, l.note
               FROM schedule_lessons l JOIN groups g ON g.id = l.group_id
               WHERE l.import_id = ANY(%s) AND g.name = ANY(%s)""",
            (sorted({row[0] for row in previous}), groups),
        ):
            old_lessons[version, group].append(dict(weekday=weekday, pair=pair, parity=parity,
                                                    subject=subject, teacher=teacher, room=room, note=note))
        versions = defaultdict(list)
        for version, first, last, monday, kind, group in previous:
            versions[group].append((version, first, last, monday, kind))
        changes = []
        new_coverage = False
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            for name in groups:
                covering = [row for row in versions[name] if row[1] <= day <= row[2]]
                current = next((row for row in covering if scope == "date" or row[4] == "base"), None)
                if scope == "date" and current is None:
                    raise ValueError("Для выбранной группы на эту дату отсутствует базовое расписание.")
                new_coverage |= current is None
                old_rows = old_lessons[current[0], name] if current else []
                old_anchor = current[3] if current else None
                new_anchor = anchor
                if scope == "date":
                    base = next((row for row in covering if row[4] == "base"), None)
                    new_anchor = anchor or old_anchor or (base[3] if base else None)
                    if new_anchor:
                        # Store only the applicable lessons as unconditional for this date.
                        applicable = day_slots(by_group[name], day, new_anchor).values()
                        by_group[name] = [dict(row, parity="all") for row in applicable]
                    if ((not new_anchor and any(x["parity"] != "all" for x in by_group[name]))
                            or (not old_anchor and any(x["parity"] != "all" and
                                                       x["weekday"] == day.weekday() for x in old_rows))):
                        raise ValueError("Для замены на дату сначала подтверди тип недели в базовом расписании.")
                old = day_slots(old_rows, day, old_anchor)
                new = day_slots(by_group[name], day, new_anchor)
                visible = scope == "date" or not covering or covering[0][4] == "base"
                changes.extend(compare(name, day, old, new, visible))
        cancellations = sum(c["kind"] == "cancelled" for c in changes)
        # Always show the diff. A mismatch can never yield a publishable token.
        token = None
        if (changes or new_coverage) and not dry_run and cancellations == expected_cancellations:
            token = uuid4()
            candidate = dict(digest=digest, groups=groups, scope=scope,
                             start=start.isoformat(), end=end.isoformat(),
                             anchor=anchor.isoformat() if anchor else None,
                             lessons=[x for name in groups for x in by_group[name]])
            conn.execute(
                """INSERT INTO schedule_previews (token, owner_id, base_head, candidate, changes)
                   VALUES (%s, %s, %s, %s, %s)""",
                (token, owner, revision, Jsonb(candidate), Jsonb(changes)),
            )
        return token, changes, new_coverage, cancellations


def decide(store, configured_admin, actor, token, *, confirm):
    if configured_admin is None or actor != configured_admin:
        return "forbidden"
    with store.connect() as conn, conn.cursor() as cur:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (SCHEDULE_UPDATE_LOCK,))
        draft = conn.execute(
            """SELECT base_head, candidate, changes, status, expires_at > clock_timestamp()
               FROM schedule_previews WHERE token = %s AND owner_id = %s""",
            (token, actor),
        ).fetchone()
        if not draft:
            return "missing"
        revision, candidate, changes, status, fresh = draft
        if status != "pending":
            return status
        if not confirm:
            conn.execute("UPDATE schedule_previews SET status = 'cancelled' WHERE token = %s", (token,))
            return "cancelled"
        minimum_start = (datetime.now(SAMARA).date() if candidate["scope"] == "date"
                         else current_week_monday())
        if not fresh or date.fromisoformat(candidate["start"]) < minimum_start:
            return "expired"
        if revision != head(conn):
            return "stale"
        version = conn.execute(
            """INSERT INTO schedule_imports
               (source_sha256, valid_from, valid_to, numerator_monday, scope, published_at, preview_token)
               VALUES (%s, %s, %s, %s, %s, clock_timestamp(), %s) RETURNING id""",
            (candidate["digest"], candidate["start"], candidate["end"], candidate["anchor"],
             candidate["scope"], token),
        ).fetchone()[0]
        groups = ScheduleRepository.save_lessons(cur, version, candidate["groups"], candidate["lessons"])
        cur.executemany(
            """INSERT INTO schedule_changes
               (import_id, group_id, day, pair, week, kind, before_lesson, after_lesson, visible)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [(version, groups[c["group"]], c["day"], c["pair"], c["week"], c["kind"],
              Jsonb(c["before"]) if c["before"] else None,
              Jsonb(c["after"]) if c["after"] else None, c["visible"]) for c in changes],
        )
        affected = defaultdict(list)
        for change in changes:
            if change["visible"] and date.fromisoformat(change["day"]) >= datetime.now(SAMARA).date():
                affected[change["group"]].append(change)
        for name, group_changes in affected.items():
            counts = Counter(c["kind"] for c in group_changes)
            summary = ", ".join(f"{label.lower()} {counts[k]}" for k, label in LABELS.items() if counts[k])
            lines = [f"Расписание · {name} · обновление №{version}",
                     f'{group_changes[0]["day"]} — {group_changes[-1]["day"]}, {summary}.',
                     "Актуальные занятия доступны в меню «Расписание»."]
            for change in group_changes:
                line = describe(change)
                if sum(map(len, lines)) + len(lines) + len(line) > 3700:
                    lines.append("Остальные изменения смотри в расписании.")
                    break
                lines.append(line)
            conn.execute(
                """INSERT INTO schedule_notifications (import_id, user_id, group_id, body, valid_through)
                   SELECT %s, u.max_user_id, g.id, %s, %s
                   FROM users u JOIN groups g ON g.id = u.group_id
                   WHERE g.id = %s AND g.active""",
                (version, "\n".join(lines), group_changes[-1]["day"], groups[name]),
            )
        conn.execute(
            "UPDATE schedule_previews SET status = 'applied', import_id = %s WHERE token = %s",
            (version, token),
        )
        return "applied"