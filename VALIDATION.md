# Проверка стабилизации — 7 ноября 2026

Исходный HEAD: d8dfd4f, ветка review/team, рабочее дерево перед началом чистое. Новый проект и новые функции не создаются. Текущие результаты дополняют исторические CHECKPOINTS.md / DEMO.md / WEB.md; статусы прошлой итерации не являются результатами этой.

## Среда и границы

Docker CLI/Engine, psql, pg_dump и pg_restore не найдены. Системное ПО не устанавливалось. Найдены ранее установленные PostgreSQL 16.9 initdb/postgres/pg_ctl; ими создан новый временный кластер в отдельном task-каталоге, на свободном loopback-порту, с отдельными случайными паролями. Перед provisioning сверено SHOW data_directory. Рабочие .env, БД, бэкапы и токен не используются.

Созданы validation_test с validation_app и отдельная podzemelye_demo с demo_app. Demo SQL из scripts/demo-init.sh применён к новой базе через psycopg; shell/psql-обёртка и Docker entrypoint этим НЕ проверены. Demo-маркер принадлежит bootstrap-роли, demo_app имеет только SELECT. Полный набор выполняется под ограниченной ролью; тесты создают собственные временные схемы.

## C6: аудит и запрет обычного входа в demo-БД

Подтверждён дефект: обычный app.bootstrap не проверял demo-маркер и при ошибочной конфигурации мог обходить DemoRouter/DemoClient. Добавлен Database.reject_demo(), вызываемый до создания MAX-клиента. Real web использует тот же отказ. PostgreSQL-регрессия проверяет отсутствие создания клиента/запуска polling.

Базовый полный прогон с известным публичным XLS: 146 обнаружены, **145 passed / 1 skipped / 0 failed**. После исправления: 147 обнаружены, **146 passed / 1 skipped / 0 failed**. Пропущена реальная MP4-проверка: ffmpeg отсутствует (ffprobe найден). Выполнены существующие DB-тесты чистых/повторных миграций, upgrade fixtures, сохранности пользователей/групп, XLS, заданий/вложений, мероприятий, уведомлений, demo seed/cleanup/concurrency, web scope и дедлайнов.

Добавлен scripts.test_isolated: только явно заданный VALIDATION_DATABASE_URL, loopback *_test, без libpq overrides и с отдельным validation-маркером. Рабочий .env не читается. Имитированный MAX из тестов не приравнивается к реальной интеграции.

Дальнейшие обязательные проверки: отдельная demo-роль/rollback/cleanup, живой HTTP, restart и backup/restore. Docker image, контейнерные сети/перезапуск, настоящий MAX и визуальная проверка пока не выполнены. Push/merge/deploy не выполнялись.
