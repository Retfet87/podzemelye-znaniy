"""Read-only Mac readiness; explicit backup and guarded foreground launch.

No MAX request is made by preflight/backup. Production migrations are never run.
"""
import fcntl
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager

import psycopg
from psycopg.conninfo import conninfo_to_dict

from app.config import PROJECT_DIR, admin_id, load_config, max_tls_context
from app.db import Database, local_dsn
from app.db.database import MIGRATIONS


def fail(message):
    raise ValueError(message)


def local_pollers():
    """Conservative legacy detection. Never print command lines or credentials."""
    result = subprocess.run(['ps', '-axo', 'pid=,comm=,args='], capture_output=True, text=True, check=True)
    found = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or int(parts[0]) == os.getpid():
            continue
        if 'python' in Path(parts[1]).name.lower() and re.search(
                r'(?:^|[ /])(?:main|demo_bot)\.py(?:\s|$)|-m\s+(?:main|app\.bootstrap|demo_bot)(?:\s|$)|scripts\.mac_runtime\s+start', parts[2]):
            found.append(int(parts[0]))
    return found


@contextmanager
def machine_lock(token):
    """Same user/token across checkouts and databases; no unlink-on-exit race."""
    folder = Path(tempfile.gettempdir()) / f'podzemelye-launch-{os.getuid()}'
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        fail('Небезопасные права каталога блокировки запуска.')
    name = hashlib.sha256(token.encode()).hexdigest()
    fd = os.open(folder / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('Этот бот уже запущен через start_mac.sh на данном Mac.')
        yield
    finally:
        os.close(fd)


def configuration():
    if os.environ.get('BOT_CONFIG_MODE'):
        fail('Mac launcher не использует Compose-режим: убери BOT_CONFIG_MODE.')
    path = PROJECT_DIR / '.env'
    if not path.is_file() or path.stat().st_mode & 0o077:
        fail('Нужен приватный .env с правами 600 в корне проекта.')
    try:
        config = load_config(path)
    except (OSError, UnicodeError):
        fail('Не удалось прочитать .env: нужны UTF-8 и права текущего пользователя.')
    token = config.get('MAX_BOT_TOKEN', '')
    if not token or not token.isascii() or any(c.isspace() or not c.isprintable() for c in token):
        fail('MAX_BOT_TOKEN отсутствует или имеет неверный формат; значение скрыто.')
    admin_id(config)
    max_tls_context(config)
    local_dsn(config)
    return config


def database_check(config, *, allow_pending=False):
    db = Database.from_config(config)
    try:
        with db.connect() as conn:
            conn.execute('SET TRANSACTION READ ONLY')
            if conn.execute("SELECT to_regclass('demo_environment')").fetchone()[0]:
                fail('Обычный бот не запускается на демонстрационной базе.')
            if not conn.execute("SELECT to_regclass('schema_migrations')").fetchone()[0]:
                fail('Нет schema_migrations. Нужны backup, проверка восстановления и отдельное применение миграций.')
            expected = {p.name for p in MIGRATIONS.glob('*.sql')}
            seen = {row[0] for row in conn.execute('SELECT version FROM schema_migrations')}
            print('Миграции: ' + ', '.join(sorted(seen)))
            if seen != expected and not allow_pending:
                fail('Несовпадение миграций. Отсутствуют: '+', '.join(sorted(expected-seen))+
                     '; лишние: '+', '.join(sorted(seen-expected))+'. Автомиграция запрещена; см. START_HERE.md.')
            if seen != expected:
                print('Копия создаётся до обновления схемы; миграции не применяются.')
            tables = sorted(set(re.findall(r'CREATE TABLE\s+(\w+)', '\n'.join(p.read_text() for p in MIGRATIONS.glob('*.sql') if p.name in seen), re.I)))
            for table in tables:
                exists = conn.execute('SELECT to_regclass(%s)', (table,)).fetchone()[0]
                if not exists:
                    fail('Отсутствует таблица: '+table)
                rights = [conn.execute('SELECT has_table_privilege(%s,%s)', (table,right)).fetchone()[0]
                          for right in ('SELECT','INSERT','UPDATE','DELETE')]
                if not all(rights):
                    fail('Нет таблицы или необходимых прав: '+table)
                # Read-only privilege probe also verifies SELECT for dump.
                conn.execute(psycopg.sql.SQL('SELECT * FROM {} LIMIT 0').format(psycopg.sql.Identifier(table)))
            for (sequence,) in conn.execute("SELECT oid::regclass::text FROM pg_class WHERE relkind='S' AND pg_table_is_visible(oid)"):
                if not conn.execute("SELECT has_sequence_privilege(%s,'USAGE')", (sequence,)).fetchone()[0]:
                    fail('Нет прав на последовательность: '+sequence)
            key = int.from_bytes(hashlib.sha256(config['MAX_BOT_TOKEN'].encode()).digest()[:4], 'big')
            if conn.execute("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND classid=17181017 AND objid=%s AND objsubid=2 AND granted)",(key,)).fetchone()[0]:
                fail('В этой PostgreSQL уже есть активная блокировка MAX poller; второй запуск запрещён.')
            print(f'PostgreSQL доступна; {len(tables)} таблиц и права чтения проверены (без записи).')
    finally:
        db.close()


def preflight(config):
    if sys.version_info[:2] != (3,12):
        fail('Требуется Python 3.12.')
    if sys.prefix == sys.base_prefix:
        fail('Используй виртуальное окружение Python 3.12; см. START_HERE.md.')
    for requirement in (PROJECT_DIR/'requirements.txt').read_text().splitlines():
        package, pinned = requirement.split('==')
        package = package.split('[')[0]
        if version(package) != pinned:
            fail('Версия зависимости не совпадает с requirements.txt: '+package)
    for resource in ('main.py','assets/welcome.png','migrations/007_form_drafts.sql'):
        if not (PROJECT_DIR/resource).is_file():
            fail('Отсутствует ресурс: '+resource)
    if not shutil.which('ffprobe'):
        fail('Для полной версии с видео требуется ffprobe в PATH; см. START_HERE.md.')
    subprocess.run(['ffprobe','-version'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=10)
    if local_pollers():
        fail('Обнаружен другой возможный MAX-бот на Mac. Останови его в исходном терминале; процессы не завершались.')
    database_check(config)
    print('Python 3.12, зависимости, ресурсы и TLS-контекст готовы. MAX-запросы не выполнялись.')
    print('ВНИМАНИЕ: проверь у Сани и на серверах отсутствие другого poller. Локальная проверка этого не гарантирует.')


def backup_identity(config):
    info = conninfo_to_dict(local_dsn(config))
    identity = {key: info.get(key,'') for key in ('host','port','dbname','user')}
    return hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest()


def backup(config):
    database_check(config, allow_pending=True)
    if not shutil.which('pg_dump') or not shutil.which('pg_restore'):
        fail('Не найдены pg_dump/pg_restore; нужен локальный клиент PostgreSQL 16.')
    settings = conninfo_to_dict(local_dsn(config))
    env = {k:v for k,v in os.environ.items() if not k.startswith('PG')}
    for key, value in settings.items():
        if key in ('host','port','user','password','dbname'):
            env[{'dbname':'PGDATABASE'}.get(key,'PG'+key.upper())] = value
    env['PGHOSTADDR'] = settings['host']
    env['PGCONNECT_TIMEOUT'] = '5'
    folder = PROJECT_DIR/'backups/mac'
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    env['BACKUP_DIR'] = str(folder)
    result = subprocess.run(['sh',str(PROJECT_DIR/'scripts/backup.sh')],env=env,capture_output=True,text=True)
    if result.returncode:
        fail('pg_dump/проверка архива завершились ошибкой; готовая копия не опубликована. Пароль/ответ сервера скрыты.')
    path = folder/result.stdout.strip()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'file':path.name,'sha256':digest,'created':time.time(),'database':backup_identity(config)}
    target = folder/(path.name+'.json')
    with target.open('x') as f:
        os.chmod(target,0o600)
        json.dump(manifest,f)
    print('Проверена структура архива (это не восстановление): '+str(path))


def require_backup(config):
    folder = PROJECT_DIR/'backups/mac'
    for manifest in sorted(folder.glob('*.dump.json'),reverse=True):
        try:
            data = json.loads(manifest.read_text())
            age = time.time()-data['created']
            name = data['file']
            if Path(name).name != name or not 0 <= age <= 86400 or data['database'] != backup_identity(config):
                continue
            if hashlib.sha256((folder/name).read_bytes()).hexdigest()==data['sha256']:
                return
        except (OSError,ValueError,KeyError,TypeError):
            continue
    fail('Нет целой резервной копии этой базы за последние 24 часа. Выполни ./scripts/backup_mac.sh; восстановление проверяй отдельно.')


def main():
    try:
        config = configuration()
        mode = sys.argv[1] if len(sys.argv)==2 else ''
        if mode=='backup':
            backup(config)
        elif mode in ('preflight','start'):
            with machine_lock(config['MAX_BOT_TOKEN']):
                preflight(config)
                if mode=='start':
                    require_backup(config)
                    print('Запуск в текущем терминале. Остановка: Ctrl+C.',flush=True)
                    from app.bootstrap import main as run_bot
                    return run_bot()
        else:
            fail('Допустимые действия: preflight, backup, start.')
        return 0
    except ValueError as error:
        print('Проверка остановлена: '+str(error),file=sys.stderr)
    except (OSError,psycopg.Error,subprocess.SubprocessError,PackageNotFoundError):
        print('Проверка остановлена: недоступны PostgreSQL, файл или локальная программа. Подробности подключения скрыты; см. START_HERE.md.',file=sys.stderr)
    return 1


if __name__=='__main__':
    raise SystemExit(main())
