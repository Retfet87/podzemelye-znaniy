# Архитектура

Бот разложен по слоям. Зависимости идут только сверху вниз: верхний слой знает о нижних, нижний о верхних — нет.

```
main.py / manage.py          тонкие точки входа
app/
├── bootstrap.py             сборка: настройки → БД → клиент → роутер → воркер → polling
├── polling.py               цикл Long Polling, повторы при сбоях сети
├── cli.py                   команды manage.py (миграции, группы, импорт расписания)
├── config.py                .env, TLS-контекст, MAX_SUPER_ADMIN_ID
├── timeutil.py              часовой пояс Астрахани, разбор и вывод сроков
├── seed_akvt.py             автоматическое заполнение "Полезное"
│
├── handlers/                ← что делать с событием
│   ├── router.py            разбор события и передача первому подходящему обработчику
│   ├── context.py           Deps (клиент, БД, репозитории) и Context (разобранное событие)
│   ├── base.py              Handler: on_message / on_callback
│   ├── common.py            number(), DraftStore (черновики с TTL и лимитом)
│   ├── menus.py             клавиатуры «Главная» и «Расписание»
│   ├── admin.py             /schedule_confirm, /schedule_cancel
│   ├── registration.py      /start, выбор группы
│   ├── schedule.py          сегодня / завтра / неделя
│   ├── homework.py          диалог «Домашние задания»
│   └── community.py         диалоги «Мероприятия» и «Полезное»
│
├── services/                ← правила предметной области, без Telegram/MAX и без SQL
│   ├── schedule_parser.py   чтение XLS сетки расписания
│   ├── schedule_publisher.py  предпросмотр изменений и транзакционная публикация
│   ├── schedule_formatter.py  текст расписания, звонки, периоды
│   ├── file_validation.py   расширение, размер, сигнатура вложений
│   ├── validation.py        проверка HTTPS-ссылок
│   └── events.py            текст карточки мероприятия
│
├── repositories/            ← весь SQL
│   ├── users.py             пользователи и группы
│   ├── schedule.py          выборка занятий, сохранение версий
│   ├── homework.py          задания, права, настройки уведомлений
│   └── community.py         мероприятия, материалы
│
├── notifications/           ← фоновая очередь исходящих уведомлений
│   ├── worker.py            выбор следующего задания, отправка, повторы
│   ├── schedule.py / homework.py / events.py   очереди по типам
│   └── common.py
│
├── max_api/                 ← всё, что знает про платформу MAX
│   ├── client.py            MaxClient, MaxError
│   ├── files.py             загрузка и скачивание файлов через CDN
│   ├── updates.py           безопасный разбор JSON событий
│   └── keyboards.py         построители inline-клавиатур
│
└── db/
    ├── database.py          Database.connect(), check_schema(), local_dsn()
    ├── migrations.py        migrate()
    └── locks.py             номера advisory-блокировок в одном месте
```

## Как проходит событие

1. `polling.run_polling` получает пачку событий от MAX и вызывает `router.dispatch(update)`.
2. `Context.parse` один раз разбирает событие: кто действует (`actor`), тип чата, текст, `payload` кнопки. Некорректные события получают вид `ignored`.
3. Роутер по очереди спрашивает обработчики. Для сообщений порядок важен: `AdminHandler` → `CommunityHandler` → `HomeworkHandler` → `RegistrationHandler`. Кнопки различаются по префиксу `payload` (`hw:`, `community:`, `schedule:`, `group:`).
4. Первый обработчик, вернувший `True`, забирает событие. Если у человека открылся черновик в одном диалоге, роутер закрывает черновик в другом.

## Как добавить новый раздел

1. Таблицы — новый файл в `migrations/`.
2. SQL — класс в `repositories/`.
3. Правила и тексты без ввода-вывода — в `services/`.
4. Обработчик — класс `Handler` в `handlers/` с `on_message` и/или `on_callback`.
5. Добавь его в список в `handlers/__init__.py::build_router` и кнопку в `handlers/menus.py`.

## Что было → что стало

| Было | Стало |
|---|---|
| `main.py` (клиент, парсинг, роутинг, polling, запуск) | `max_api/client.py`, `max_api/updates.py`, `handlers/router.py`, `handlers/registration.py`, `handlers/schedule.py`, `handlers/admin.py`, `polling.py`, `bootstrap.py`, `config.py` |
| `db.py` (`RegistrationStore`, `migrate`) | `db/database.py`, `db/migrations.py`, `repositories/users.py`, `repositories/schedule.py` |
| `homework.py` | `repositories/homework.py`, `timeutil.py` |
| `homework_dialog.py` | `handlers/homework.py`, `handlers/common.py` |
| `homework_files.py` | `max_api/files.py`, `services/file_validation.py` |
| `homework_notifications.py`, `event_notifications.py`, `schedule_notifications.py` | `notifications/homework.py`, `notifications/events.py`, `notifications/schedule.py`, `notifications/worker.py` |
| `community.py` | `repositories/community.py`, `services/events.py`, `services/validation.py` |
| `community_dialog.py` | `handlers/community.py` |
| `schedule_view.py` | `services/schedule_formatter.py`, `handlers/menus.py` |
| `schedule_import.py` | `services/schedule_parser.py` |
| `schedule_updates.py` | `services/schedule_publisher.py`, `config.py` (`admin_id`) |
| `manage.py` | `cli.py` (`manage.py` остался как точка входа) |

## Что намеренно не менялось

SQL-запросы, тексты сообщений, `payload` кнопок, номера advisory-блокировок, миграции и формат `.env`. Это перенос и разделение по слоям, а не переписывание логики.

Незначительные изменения: `RegistrationStore` разделён на `Database` + `UserRepository` + `ScheduleRepository`; `ScheduleRepository.schedule()` переименован в `for_user()`; методы клиента `receive_homework_file` / `upload_homework_file` стали `receive_file` / `upload_file`, потому что ими пользуются и мероприятия; блокировка заданий `281952767` теперь передаётся параметром, а не литералом в SQL.

## Что можно улучшить дальше

- В `handlers/homework.py` и `handlers/community.py` по-прежнему большие классы диалогов. Следующий шаг — вынести тексты и клавиатуры в отдельные `views`, а шаги черновика в таблицу переходов.
- Разбить `repositories/homework.py` на «чтение» и «запись», а общие SQL-фрагменты прав (`VISIBLE`, `EDITABLE`) вынести в отдельный модуль.
- Заменить `threading` на `asyncio`, если потребуется больше одновременных пользователей.
