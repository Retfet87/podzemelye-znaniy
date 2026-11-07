"""Explicit, repeatable demo content. Never reads .env or a MAX token."""
import argparse
from datetime import datetime, timedelta
from pathlib import Path
import os

from psycopg.conninfo import conninfo_to_dict

from app.db import Database, local_dsn
from app.services.schedule_parser import parse_xls
from app.services.schedule_publisher import preview, decide
from app.timeutil import SAMARA

DEMO_USER = 900000011
ADMIN = 900000090


def demo_database():
    dsn = local_dsn({'TEST_DATABASE_URL': os.environ.get('TEST_DATABASE_URL')}, 'TEST_DATABASE_URL')
    if not conninfo_to_dict(dsn)['dbname'].endswith(('_test', '_demo')):
        raise ValueError('Нужна отдельная база *_test или *_demo.')
    return Database(dsn)


def clean(conn):
    # Table names are fixed constants; ids come exclusively from the marker table.
    for kind, table, key in [('homework', 'homework', 'id'), ('event', 'events', 'id'),
                             ('material', 'useful_materials', 'id'), ('user', 'users', 'max_user_id')]:
        conn.execute(f'DELETE FROM {table} WHERE {key} IN (SELECT record_id FROM demo_records WHERE kind = %s)', (kind,))
    conn.execute('DELETE FROM demo_records')


def seed(db):
    db.check_schema()
    with db.connect() as conn:
        conn.execute('SELECT pg_advisory_xact_lock(7210196)')
        if conn.execute('SELECT 1 FROM demo_records LIMIT 1').fetchone():
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
        conn.execute('INSERT INTO users (max_user_id, group_id, institution_code, welcome_seen) VALUES (%s,%s,%s,true)', (DEMO_USER, group, institution))
        conn.execute("INSERT INTO demo_records VALUES ('user', %s)", (DEMO_USER,))
        def mark(kind, identifier):
            conn.execute('INSERT INTO demo_records VALUES (%s,%s)', (kind, identifier))
        for owner, body in [(None, 'Демо — подготовить практическую работу №3'),
                            (None, 'Демо — прочитать рекомендации перед занятием'),
                            (DEMO_USER, 'Демо — личный план подготовки')]:
            task = conn.execute('INSERT INTO homework (group_id,owner_id,subject,body) VALUES (%s,%s,%s,%s) RETURNING id',
                                (group, owner, subject, body)).fetchone()[0]
            mark('homework', task)
            conn.execute('INSERT INTO homework_files VALUES (%s,1,%s,%s)', (task, 'demo.txt', 'Демо — образец, не настоящее задание.'.encode()))
        now = datetime.now(SAMARA)
        picture = (Path(__file__).parent / 'assets/welcome.png').read_bytes()
        for title, hours, tags, report in [('Демо — студенческий хакатон', 48, ['hackathon','study'], False),
                                         ('Демо — спортивная встреча', 72, ['sport'], False),
                                         ('Демо — как прошёл учебный квест', -48, ['culture','other'], True)]:
            event = conn.execute('''INSERT INTO events (title,description,starts_at,place,state,tags,report_body,report_published)
                VALUES (%s,%s,%s,%s,'published',%s,%s,%s) RETURNING id''',
                (title, 'Пример для демонстрации. Не реальное мероприятие АКВТ.', now + timedelta(hours=hours),
                 'Демо — место не назначено', tags, 'Демо — пример итогов, событие вымышлено.' if report else '', report)).fetchone()[0]
            mark('event', event)
            conn.execute('INSERT INTO event_media VALUES (%s,%s,1,\'image\',\'demo.png\',%s)',
                         (event, 'report' if report else 'event', picture))
        records = [('curators','Демо — куратор','Пример: Иванов Иван Иванович. Контакт не настоящий: +7 (000) 000-00-00.',''),
                   ('teachers','Демо — преподаватель','Пример: Петров Пётр Петрович — преподаватель информатики.',''),
                   ('rooms','Демо — кабинет №101','Пример учебной аудитории. Не справочная информация АКВТ.',''),
                   ('study','Демо — методические рекомендации','Безопасный текстовый образец для демонстрации.',''),
                   ('links','Демо — пример полезной ссылки','Ссылка на официальный сайт АКВТ; карточка демонстрационная.','https://akvt.ru/')]
        for category, title, body, link in records:
            item = conn.execute('INSERT INTO useful_materials (group_id,category,title,body,link) VALUES (%s,%s,%s,%s,%s) RETURNING id',
                                (group,category,title,body,link)).fetchone()[0]
            mark('material', item)
            if category == 'study':
                conn.execute('INSERT INTO useful_files VALUES (%s,1,\'demo.txt\',%s)', (item, b'Demo study notes. Not official.'))
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--demo', action='store_true', required=True)
    parser.add_argument('--clean', action='store_true')
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
                print('Демо-данные уже существуют; дублей нет.')
                return
            if conn.execute('SELECT 1 FROM users LIMIT 1').fetchone() or conn.execute('SELECT 1 FROM schedule_imports LIMIT 1').fetchone():
                raise ValueError('Автоимпорт XLS разрешён только в пустую тестовую базу. Для готового расписания убери --xls.')
        digest, groups, lessons = parse_xls(args.xls)
        today = datetime.now(SAMARA).date()
        monday = today - timedelta(days=today.weekday())
        token, _, _, cancellations = preview(db, ADMIN, digest, groups, lessons, monday, monday + timedelta(days=13),
                                  args.numerator_monday, scope='base', dry_run=False)
        if cancellations:
            raise ValueError('Импорт меняет существующее расписание: используй обычный предпросмотр вручную.')
        if token and decide(db, ADMIN, ADMIN, token, confirm=True) != 'applied':
            raise ValueError('Расписание не применено.')
    print('Демо-данные созданы.' if seed(db) else 'Демо-данные уже существуют; дублей нет.')


if __name__ == '__main__':
    try:
        main()
    except ValueError as error:
        raise SystemExit(str(error)) from None
