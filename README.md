# Подземелье знаний — «Четыре мушкетёра»

Чат-бот MAX для учебной группы: расписание и замены, общие/личные домашние задания с файлами, напоминания, мероприятия/RSVP, полезные материалы и экспорт календаря. Даты выбираются кнопками: быстрые дни, календарь, время; ручной ввод необязателен. Формы сохраняются между перезапусками. Административные действия проверяют права и требуют подтверждения.

**MAX:** https://max.ru/ideya_fiks_90_bot — ссылка подтверждена ответом /me действующего бота и публичной страницей MAX. Его нынешнее публичное название — «90_Идея_фикс»; переименование здесь не выполнялось.

**Дополнительный сайт:** https://podzemelye-znaniy-demo.chipperloon.chatgpt.site — самостоятельный пример с вымышленными данными, без рабочей БД и MAX ID. Для нашего направления встроенное mini-app не требуется.

## Версия для сдачи

Актуальный выпускаемый код — **main**; релизная ветка release/hackathon-final-2026-11-08 сохранена. SHA проверяется на странице GitHub Actions. Точное содержимое Docker-образа задаётся digest, а не подвижным тегом latest. Git push сам по себе не обновляет запущенный Mac-процесс.

Короткая карта кода — [ARCHITECTURE.md](ARCHITECTURE.md). Запуск и восстановление — [DEPLOYMENT.md](DEPLOYMENT.md), Mac — [START_HERE.md](START_HERE.md), результаты — [VALIDATION.md](VALIDATION.md). SUBMISSION.md содержит историческую сверку до разрешённого финального выпуска.

## Требования и конфигурация

Python **3.12**, PostgreSQL **16**, ffprobe для проверки видео. Прямые зависимости закреплены в requirements.txt: psycopg/binary, psycopg_pool, xlrd, Pillow. Новых внешних платных API нет. Для контейнеров нужны Docker Engine и Compose v2; Dockerfile устанавливает ffprobe через ffmpeg.

### Запуск без Docker

Из корня существующего проекта:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

Заполни локальный .env: MAX_BOT_TOKEN, DATABASE_URL на 127.0.0.1/::1, MAX_CA_FILE; MAX_SUPER_ADMIN_ID — только ID владельца, если нужны права супер-администратора. Не печатай/не коммить заполненный файл. Создай отдельную пустую базу и роль-владельца PostgreSQL; запусти сервер по инструкции своего пакета. На существующем Mac точный сервер и порт уже настроены, не запускай initdb повторно.

Для новой пустой базы:

```sh
.venv/bin/python manage.py migrate
.venv/bin/python main.py
```

Для существующей базы перед migrate обязательны свежий backup и проверка восстановления отдельно. На уже настроенном Mac:

```sh
./scripts/preflight.sh
./scripts/start_mac.sh
```

Стартер требует свежий целый backup и защищает от второго локального процесса; обычный main.py дополнительно использует PostgreSQL polling lease. **Не запускай второй poller с одним токеном**, в том числе на другом компьютере. Остановка foreground-бота — Ctrl+C. Смена работающей версии — только по START_HERE.md после согласования.

## Docker: сборка и запуск

Из чистого checkout финальной ветки:

```sh
docker build -t podzemelye-znaniy:submission .
cp .env.docker.example .env.docker
chmod 600 .env.docker
```

Заполни POSTGRES_PASSWORD, MAX_BOT_TOKEN и при необходимости MAX_SUPER_ADMIN_ID в .env.docker. Сначала только схема/база, без polling:

```sh
docker compose --env-file .env.docker -p podzemelye-real config --quiet
docker compose --env-file .env.docker -p podzemelye-real up -d --build migrate
```

**Только после остановки другого poller** выбери один способ запуска:

```sh
docker compose --env-file .env.docker -p podzemelye-real --profile live up -d bot
```

Либо напрямую тот же образ и сеть уже созданной базы (не одновременно с Compose bot):

```sh
docker run --init --rm --name podzemelye-bot --stop-signal SIGINT --network podzemelye-real_default --env-file .env.docker -e BOT_CONFIG_MODE=compose -e MAX_CA_FILE=certificates/russian_trusted_root_ca.pem podzemelye-znaniy:submission
```

Проверка: `docker compose --env-file .env.docker -p podzemelye-real logs --tail 30 bot`. Остановка Compose: `docker compose --env-file .env.docker -p podzemelye-real stop bot`. Для прямого run: `docker stop --timeout 50 podzemelye-bot`. Том pgdata сохраняет все данные и bytea-вложения; не используй down --volumes на настоящей базе. Копирование/восстановление — DEPLOYMENT.md.

CI собирает образ в чистом Linux, выполняет PostgreSQL/миграции/smoke/recreate/restore и сохраняет **именно проверенный образ** как артефакт `podzemelye-bot-<SHA>` на странице успешного Actions run. Это готовый linux/amd64 image в архиве, не только Dockerfile; для Apple Silicon нужна эмуляция amd64. Артефакт хранится 14 дней; долговременный образ публикуется в GHCR после проверки main. Команда ниже загружает готовый образ; тег полного main SHA или digest из Actions фиксирует нужную версию:

```sh
docker pull ghcr.io/retfet87/podzemelye-znaniy:latest
```

Запуск загруженного образа: используй в команде docker run выше `ghcr.io/retfet87/podzemelye-znaniy:latest` вместо локального тега. Сохраняются те же env-file, сеть и единственный poller.

После скачивания и распаковки ZIP артефакта:

```sh
shasum -a 256 -c podzemelye-bot.tar.gz.sha256
docker load --input podzemelye-bot.tar.gz
```

Архив не содержит рабочие .env, токен, дампы и runtime. Версии Python-зависимостей фиксированы; базовый image/системные пакеты берутся из Dockerfile при сборке, поэтому обещания побайтово одинаковых независимых сборок нет. Для точного воспроизведения используй сохранённый CI-образ и его SHA-256.

## REAL / CLEAN и DEMO

REAL — обычная база после миграций без вымышленных пользователей/материалов. Все функции работают; группы и официальное расписание добавляет администратор через manage.py и подтверждённый импорт XLS. При отсутствии данных доступны меню и понятные пустые состояния. Никакого автоматического наполнения рабочей базы.

DEMO — **другая база, другая роль и конфигурация**, bootstrap-owned marker, явный allowlist и выключатель доступа. Общий sender очереди на demo запрещён. Нельзя переключателем превратить real-базу в demo. Подготовка compose.demo.yml — DEMO.md. Для уже подготовленной отдельной demo-среды, без запуска MAX:

```sh
docker compose --env-file .env.demo -f compose.demo.yml -p podzemelye-demo-review run --rm demo-seed python seed_demo.py --demo --synthetic-schedule
```

Получаются четыре условные недели с подтверждённым для примера чередованием и разовой заменой, задания со сроками/без срока и TXT, будущие/прошедшие события с медиа/RSVP, 32 полезные карточки из пяти категорий. Всё вымышленное помечено ДЕМО; это не официальный прогноз расписания. Другие импорты не перезаписываются. Реальная следующая неделя доступна только внутри подтверждённого периода XLS; после его окончания бот сообщает об отсутствии публикации.

Для повторения демонстрации без ручного восстановления десятков записей: сначала `seed_demo.py --demo --clean`, затем предыдущая команда с --synthetic-schedule. Это удаляет только отмеченные demo-записи, сохраняет расписание и чужие записи. Для обновления только событий/полезного есть --refresh; дни событий можно явно сдвинуть --date-shift-days. Полный порядок — DEMO.md. Не используй рабочий токен для второго demo-poller.

## Проверки и показ

```sh
.venv/bin/python -m scripts.test_offline
node scripts/test_frontend.mjs
.venv/bin/python -m scripts.build_demo_site
sh scripts/check_docker.sh
```

Полный набор на отдельно подготовленной маркерованной PostgreSQL: `.venv/bin/python -m scripts.test_isolated`. Отдельный smoke: `TEST_DATABASE_URL=... .venv/bin/python -m scripts.smoke_test` (только собственная *_test база). Offline намеренно пропускает PostgreSQL/часть медиа; пропуски не считаются успешными тестами. Источник реального XLS задаётся AKVT_TEST_XLS. Не запускать тесты с рабочей конфигурацией.

Сценарий защиты на 6 минут и автономный резерв — UX_DEMO.md. Телефонная приёмка — MAX_CHECKLIST.md. TLS и проверки прав не отключаются. Long Polling сохранён для прототипа; официальный MAX рекомендует Webhook для production. Массовая рассылка, seed рабочей базы, автоматический merge/deploy и подключение публичного сайта к личным данным не выполняются.
