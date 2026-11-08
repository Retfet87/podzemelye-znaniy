"""Команды управления: миграции, группы, импорт расписания."""

import argparse
from collections import Counter
from datetime import date

import psycopg

from app.config import admin_id, load_config
from app.db import Database, local_dsn, migrate
from app.repositories.users import UserRepository
from app.services.schedule_parser import parse_xls, print_summary
from app.services.schedule_publisher import LABELS, describe, preview
from app.services.official_schedule import discover, download


def main():
    parser = argparse.ArgumentParser(description="Настройка локальной БД бота.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="применить миграции")
    groups = commands.add_parser("add-groups", help="добавить учебные группы")
    groups.add_argument("names", nargs="+")
    inspect = commands.add_parser(
        "inspect-schedule", help="разобрать XLS без подключения к БД"
    )
    inspect.add_argument("file")
    inspect.add_argument("--gray-other-building", action="store_true")
    official = commands.add_parser(
        "check-official-schedule",
        help="список официальных XLS или проверка выбранного файла без публикации",
    )
    official.add_argument(
        "--file-url", help="точный адрес XLS со страницы АКВТ; без него только список"
    )
    schedule = commands.add_parser(
        "import-schedule", help="предпросмотр XLS без публикации"
    )
    schedule.add_argument("file")
    for command in (schedule, official):
        command.add_argument("--gray-other-building", action="store_true")
        command.add_argument(
            "--scope", choices=("base", "date"), required=command is schedule
        )
        command.add_argument(
            "--from",
            dest="start",
            type=date.fromisoformat,
            required=command is schedule,
        )
        command.add_argument("--to", dest="end", type=date.fromisoformat)
        command.add_argument("--numerator-monday", type=date.fromisoformat)
        command.add_argument("--group", action="append", default=[])
        command.add_argument("--expect-cancellations", type=int, default=0)
        command.add_argument("--expect-groups", type=int)
        command.add_argument("--expect-lessons", type=int)
        command.add_argument(
            "--stage",
            action="store_true",
            help="создать черновик для подтверждения в MAX",
        )
    commands.add_parser(
        "notification-status", help="сводка очереди без данных получателей"
    )
    args = parser.parse_args()
    try:
        if args.command == "check-official-schedule":
            available = discover()
            if args.file_url is None:
                print(
                    "Официальные XLS (выбери свой учебный год и набор специальностей):"
                )
                print("\n".join(available))
                return 0
            if args.scope is None or args.start is None:
                raise ValueError(
                    "Для проверки выбранного XLS явно укажи --scope и --from."
                )
            digest, names, lessons = download(
                args.file_url, available, gray_other_building=args.gray_other_building
            )
            print("SHA256: " + digest)
        if args.command in ("inspect-schedule", "import-schedule"):
            digest, names, lessons = parse_xls(
                args.file, gray_other_building=args.gray_other_building
            )
            if args.command == "inspect-schedule":
                print_summary(names, lessons)
                return 0
        config = load_config()
        store = Database(local_dsn(config))
        if args.command == "migrate":
            versions = migrate(store)
            print(
                "Применены: " + ", ".join(versions)
                if versions
                else "Миграции уже применены."
            )
            return 0
        store.check_schema()
        if args.command in ("import-schedule", "check-official-schedule"):
            if args.scope == "base" and args.end is None:
                raise ValueError("Для базового расписания явно укажи --to.")
            if (
                args.stage
                and args.scope == "base"
                and (args.expect_groups, args.expect_lessons)
                != (len(names), len(lessons))
            ):
                raise ValueError(
                    "Для базового черновика укажи сверенные --expect-groups и --expect-lessons."
                )
            end = args.end or args.start
            token, changes, new_coverage, cancellations = preview(
                store,
                admin_id(config),
                digest,
                names,
                lessons,
                args.start,
                end,
                args.numerator_monday,
                scope=args.scope,
                selected=args.group,
                expected_cancellations=args.expect_cancellations,
                dry_run=not args.stage,
            )
            counts = Counter(change["kind"] for change in changes)
            print(
                f"Область: {args.scope}, {args.start} — {end}. XLS: {len(names)} групп, {len(lessons)} строк."
            )
            for change in changes:
                hidden = " [скрыто заменой на дату]" if not change["visible"] else ""
                print(f"{change['group']} · {describe(change)}{hidden}")
            for kind, label in LABELS.items():
                print(f"{label}: {counts[kind]}")
            if new_coverage:
                print(
                    "Версия добавляет даты или группы, для которых раньше не было расписания."
                )
            if token:
                print(f"Подтверди в личном диалоге MAX: /schedule_confirm {token}")
                print(f"Отмени в личном диалоге MAX: /schedule_cancel {token}")
            elif args.stage and cancellations != args.expect_cancellations:
                print(
                    "Черновик заблокирован. Сверь отмены и передай их точное число через --expect-cancellations."
                )
                return 1
            else:
                print("Черновик не создан. Расписание и очередь не изменены.")
        elif args.command == "notification-status":
            with store.connect() as conn:
                for status, count in conn.execute(
                    "SELECT status, count(*) FROM schedule_notifications GROUP BY status ORDER BY status"
                ):
                    print(f"{status}: {count}")
        else:
            names = [name.strip() for name in args.names]
            if any(not name or len(name) > 80 for name in names):
                raise ValueError(
                    "Название группы должно содержать от 1 до 80 символов."
                )
            UserRepository(store).add_groups(names)
            print("Список групп обновлён.")
    except OSError:
        parser.exit(
            1, "Не удалось открыть локальный файл. Проверь путь и права доступа.\n"
        )
    except (UnicodeError, psycopg.Error):
        parser.exit(
            1, "Операция не выполнена. Проверь .env, подключение и схему PostgreSQL.\n"
        )
    except ValueError as error:
        parser.exit(1, f"{error}\n")
    return 0
