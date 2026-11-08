"""Explicit, repeatable demo content. Never reads .env or a MAX token."""
import argparse
from datetime import datetime, timedelta
from pathlib import Path

from app.demo import demo_database, verify_demo
from app.demo_seed import clean_events, clean_useful, refresh_site_demo, seed_events, seed_useful
from app.services.schedule_parser import parse_xls
from app.services.schedule_publisher import decide, preview
from app.timeutil import SAMARA

DEMO_USER = 900000011
ADMIN = 900000090


def clean(conn):
    verify_demo(conn)
    conn.execute('SELECT pg_advisory_xact_lock(7210196)')
    clean_events(conn)
    clean_useful(conn)
    for kind, table, key in [
        ('homework', 'homework', 'id'),
        ('event', 'events', 'id'),
        ('material', 'useful_materials', 'id'),
        ('user', 'users', 'max_user_id'),
    ]:
        conn.execute(
            f'DELETE FROM {table} WHERE {key} IN (SELECT record_id FROM demo_records WHERE kind = %s)',
            (kind,),
        )
    conn.execute('DELETE FROM demo_records')


def seed(db, *, refresh: bool = False, date_shift_days: int = 0):
    db.check_schema()
    with db.connect() as conn:
        verify_demo(conn)
        conn.execute('SELECT pg_advisory_xact_lock(7210196)')
        if refresh:
            refresh_site_demo(conn, date_shift_days=date_shift_days)
        if conn.execute("SELECT 1 FROM demo_records WHERE kind = 'user'").fetchone():
            seed_events(conn, date_shift_days=date_shift_days)
            seed_useful(conn)
            return False
        row = conn.execute('''SELECT g.id, g.institution_code, l.subject FROM groups g
            JOIN schedule_groups sg ON sg.group_id = g.id JOIN schedule_imports i ON i.id = sg.import_id
            JOIN schedule_lessons l ON l.import_id = i.id AND l.group_id = g.id
            WHERE g.active AND i.scope = 'base' AND i.valid_from <= current_date AND i.valid_to >= current_date
            ORDER BY i.id DESC, g.name, l.pair LIMIT 1''').fetchone()
        if not row:
            raise ValueError('Сначала импортируй реальное XLS в тестовую базу; вымышленное расписание не создаётся.')
        group, institution, subject = row
        if conn.execute('SELECT 1 FROM users WHERE max_user_id = %s', (DEMO_USER,)).fetchone():
            raise ValueError('Зарезервированный демо-пользователь уже существует без метки. Данные не изменены.')
        conn.execute(
            'INSERT INTO users (max_user_id, group_id, institution_code, welcome_seen) VALUES (%s,%s,%s,true)',
            (DEMO_USER, group, institution),
        )
        conn.execute("INSERT INTO demo_records VALUES ('user', %s)", (DEMO_USER,))

        def mark(kind, identifier):
            conn.execute('INSERT INTO demo_records VALUES (%s,%s)', (kind, identifier))

        for owner, body in [
            (None, 'ДЕМО — подготовить практическую работу №3'),
            (None, 'ДЕМО — прочитать рекомендации перед занятием'),
            (DEMO_USER, 'ДЕМО — личный план подготовки'),
        ]:
            task = conn.execute(
                'INSERT INTO homework (group_id,owner_id,subject,body) VALUES (%s,%s,%s,%s) RETURNING id',
                (group, owner, subject, body),
            ).fetchone()[0]
            mark('homework', task)
            conn.execute(
                'INSERT INTO homework_files VALUES (%s,1,%s,%s)',
                (task, 'demo.txt', 'ДЕМО — образец, не настоящее задание.'.encode()),
            )
        seed_events(conn, date_shift_days=date_shift_days)
        seed_useful(conn)
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', required=True)
    parser.add_argument('--clean', action='store_true')
    parser.add_argument('--refresh', action='store_true', help='обновить карточки сайта (Полезное и Мероприятия)')
    parser.add_argument('--date-shift-days', type=int, default=0, help='сдвиг дат мероприятий в днях')
    parser.add_argument('--xls', type=Path)
    parser.add_argument('--numerator-monday', type=lambda s: datetime.strptime(s, '%Y-%m-%d').date())
    args = parser.parse_args()
    db = demo_database()
    db.check_schema()
    if args.clean:
        with db.connect() as conn:
            conn.execute('SELECT pg_advisory_xact_lock(7210196)')
            clean(conn)
        print('Удалены только помеченные демо-записи. Настоящее расписание сохранено.')
        return
    if args.xls:
        with db.connect() as conn:
            if conn.execute('SELECT 1 FROM demo_records LIMIT 1').fetchone():
                if args.refresh:
                    refresh_site_demo(conn, date_shift_days=args.date_shift_days)
                    print('Демо-данные сайта обновлены.')
                else:
                    print('Демо-данные уже существуют; дублей нет.')
                return
            if conn.execute('SELECT 1 FROM users LIMIT 1').fetchone() or conn.execute('SELECT 1 FROM schedule_imports LIMIT 1').fetchone():
                raise ValueError('Автоимпорт XLS разрешён только в пустую тестовую базу. Для готового расписания убери --xls.')
        digest, groups, lessons = parse_xls(args.xls)
        today = datetime.now(SAMARA).date()
        monday = today - timedelta(days=today.weekday())
        token, _, _, cancellations = preview(
            db, ADMIN, digest, groups, lessons, monday, monday + timedelta(days=13),
            args.numerator_monday, scope='base', dry_run=False,
        )
        if cancellations:
            raise ValueError('Импорт меняет существующее расписание: используй обычный предпросмотр вручную.')
        if token and decide(db, ADMIN, ADMIN, token, confirm=True) != 'applied':
            raise ValueError('Расписание не применено.')
    created = seed(db, refresh=args.refresh, date_shift_days=args.date_shift_days)
    print('Демо-данные созданы.' if created else 'Демо-данные синхронизированы; дублей нет.')


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from None