#!/bin/sh
# Isolated smoke test. The bot service is never started and no MAX token is read.
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    echo "Docker Engine недоступен." >&2
    exit 2
fi
project="podzemelye-check-$$"
export POSTGRES_DB=podzemelye POSTGRES_USER=podzemelye
export POSTGRES_PASSWORD=isolated-integration-password
export MAX_BOT_TOKEN= MAX_SUPER_ADMIN_ID= COMPOSE_PROFILES=
export SCHEDULE_NOTIFICATIONS_ENABLED=0 HOMEWORK_NOTIFICATIONS_ENABLED=0 EVENT_NOTIFICATIONS_ENABLED=0
compose() {
    docker compose --env-file "$root/.env.docker.example" -f "$root/docker-compose.yml" -p "$project" "$@"
}
trap 'compose down --volumes --remove-orphans' EXIT HUP INT TERM
compose config --quiet
compose up -d --build migrate
compose run --rm -T migrate python manage.py migrate
compose exec -T postgres createdb -U podzemelye podzemelye_test
database=$(compose ps -q postgres)
compose exec -T postgres psql -U podzemelye -d podzemelye_test -v ON_ERROR_STOP=1 -c "REVOKE CONNECT ON DATABASE podzemelye_test FROM PUBLIC; CREATE TABLE public.validation_environment (environment text PRIMARY KEY); INSERT INTO public.validation_environment VALUES ('podzemelye-validation-v1');"
compose exec -T postgres createdb -U podzemelye podzemelye_demo
docker exec -i -e POSTGRES_USER=podzemelye -e DEMO_DB_PASSWORD=isolated-demo-password "$database" sh < scripts/demo-init.sh
compose exec -T postgres psql -U podzemelye -d podzemelye_demo -v ON_ERROR_STOP=1 -c "CREATE TABLE public.validation_environment (environment text PRIMARY KEY); INSERT INTO public.validation_environment VALUES ('podzemelye-validation-v1'); GRANT SELECT ON public.validation_environment TO demo_app;"
image=$(compose images -q migrate | head -n 1)
test_url="postgresql://podzemelye:$POSTGRES_PASSWORD@127.0.0.1:5432/podzemelye_test"
docker run --rm --network "container:$database" --volume "$root/tests:/app/tests:ro" \
    --env "VALIDATION_DATABASE_URL=$test_url" \
    --env "VALIDATION_DEMO_DATABASE_URL=postgresql://demo_app:isolated-demo-password@127.0.0.1:5432/podzemelye_demo" \
    "$image" python -m scripts.test_isolated
docker run --rm --network "container:$database" \
    --env "TEST_DATABASE_URL=$test_url" "$image" python -m scripts.smoke_test
compose run --rm -T migrate python - <<'PY'
from app.config import load_config
from app.db import Database, local_dsn
db = Database(local_dsn(load_config()))
with db.connect() as conn:
    group = conn.execute("INSERT INTO groups (name) VALUES ('ДЕМО-01') RETURNING id").fetchone()[0]
    conn.execute("INSERT INTO users (max_user_id, group_id, institution_code) VALUES (900000001, %s, 'akvt')", (group,))
    task = conn.execute(
        "INSERT INTO homework (group_id, owner_id, subject, body) VALUES (%s,900000001,'ДЕМО','Проверка') RETURNING id",
        (group,),
    ).fetchone()[0]
    conn.execute("INSERT INTO homework_files VALUES (%s, 1, 'demo.txt', %s)", (task, b"homework attachment"))
    material = conn.execute(
        "INSERT INTO useful_materials (category,title,body,link) VALUES ('study','ДЕМО','Проверка','') RETURNING id"
    ).fetchone()[0]
    conn.execute("INSERT INTO useful_files VALUES (%s,1,'useful.txt',%s)", (material, b"useful attachment"))
    conn.execute("INSERT INTO form_drafts VALUES (900000001,'homework','{}',now()+interval '30 minutes')")
PY
compose down
compose up -d migrate
compose run --rm -T migrate python - <<'PY'
from app.config import load_config
from app.db import Database, local_dsn
db = Database(local_dsn(load_config()))
db.check_schema()
with db.connect() as conn:
    assert conn.execute("SELECT data FROM homework_files").fetchone() == (b"homework attachment",)
    assert conn.execute("SELECT data FROM useful_files").fetchone() == (b"useful attachment",)
    assert conn.execute("SELECT count(*) FROM users").fetchone() == (1,)
    assert conn.execute("SELECT count(*) FROM event_notifications").fetchone() == (0,)
    assert conn.execute("SELECT count(*) FROM form_drafts").fetchone() == (1,)
print("Данные и оба вида вложений пережили пересоздание контейнеров.")
PY
mkdir -p backups
export BACKUP_UID=$(id -u) BACKUP_GID=$(id -g)
backup_output=$(compose run --rm -T backup)
archive=$(printf '%s\n' "$backup_output" | tail -n 1)
case "$archive" in backup_*.dump) ;; *) echo "Не найден архив резервной копии." >&2; exit 1;; esac
compose exec -T postgres createdb -U podzemelye restore_check
compose exec -T postgres pg_restore -U podzemelye -d restore_check --exit-on-error < "$root/backups/$archive"
restored=$(compose exec -T postgres psql -U podzemelye -d restore_check -Atc \
    "SELECT encode(data,'hex') FROM homework_files; SELECT encode(data,'hex') FROM useful_files;")
expected=$(printf '%s\n%s' 686f6d65776f726b206174746163686d656e74 75736566756c206174746163686d656e74)
test "$restored" = "$expected"
scheduled_output=$(compose run --rm -T -e BACKUP_RUN_ONCE=1 backup-daily)
scheduled_archive=$(printf '%s\n' "$scheduled_output" | tail -n 1)
case "$scheduled_archive" in backup_*.dump) ;; *) echo "Не найден scheduled-архив." >&2; exit 1;; esac
compose exec -T postgres createdb -U podzemelye restore_scheduled_check
compose exec -T postgres pg_restore -U podzemelye -d restore_scheduled_check --exit-on-error < "$root/backups/scheduled-v1/$scheduled_archive"
scheduled_restored=$(compose exec -T postgres psql -U podzemelye -d restore_scheduled_check -Atc \
    "SELECT encode(data,'hex') FROM homework_files; SELECT encode(data,'hex') FROM useful_files; SELECT count(*) FROM form_drafts; SELECT count(*) FROM schema_migrations;")
expected_scheduled=$(printf '%s\n1\n7' "$expected")
test "$scheduled_restored" = "$expected_scheduled"
echo "Docker: тесты, сохранение вложений/черновиков, one-shot и scheduled restore прошли."
