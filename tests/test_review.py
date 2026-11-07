"""Regression checks for security and deployment findings from the team review."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app.config import load_config
from app.db import Database, local_dsn
from app.handlers import Deps, build_router
from app.handlers.community import CommunityHandler, Draft as CommunityDraft
from app.handlers.homework import Draft as HomeworkDraft, HomeworkHandler
from app.handlers.registration import department_groups_keyboard, prefixes_keyboard
from app.max_api.client import MaxError
from app.max_api.files import transfer
from app.polling import run_polling
from psycopg.conninfo import conninfo_to_dict
from test_main import callback_update, message_update


class ReviewInputTests(unittest.TestCase):
    def test_compose_configuration_cannot_inherit_an_arbitrary_database(self):
        password = "spaces, 'quotes', backslash \\ and $signs"
        with patch.dict(os.environ, {"BOT_CONFIG_MODE": "compose", "POSTGRES_PASSWORD": password,
                                     "DATABASE_URL": "postgresql://remote/untrusted"}, clear=True):
            config = load_config()
        self.assertNotIn("DATABASE_URL", config)
        dsn = local_dsn(config)
        self.assertEqual(conninfo_to_dict(dsn), {
            "host": "postgres", "user": "postgres", "dbname": "podzemelye", "password": password,
        })
        with patch("app.db.database.psycopg.connect") as connect:
            Database(dsn).connect()
            connect.assert_called_once_with(dsn, connect_timeout=5)
        with self.assertRaisesRegex(ValueError, "POSTGRES_PASSWORD"):
            local_dsn({"BOT_CONFIG_MODE": "compose"})

    def test_one_blocked_recipient_does_not_stop_other_updates(self):
        client, router = Mock(), Mock()
        client.get_updates.side_effect = [([{"n": 1}, {"n": 2}], 9), KeyboardInterrupt]
        router.dispatch.side_effect = [MaxError("blocked", status=403), None]
        with self.assertLogs("app", level="WARNING"), self.assertRaises(KeyboardInterrupt):
            run_polling(client, router)
        self.assertEqual(router.dispatch.call_count, 2)
        self.assertEqual(client.get_updates.call_args.args, (9,))

    def test_registration_keyboard_pages_stay_within_max_limits(self):
        groups = [(i, f"ГРУППА-{i}") for i in range(101)]
        first = department_groups_keyboard("ГРУППА", groups)
        second = department_groups_keyboard("ГРУППА", groups, 40)
        for keyboard in (first, second, prefixes_keyboard([(i, f"GROUP{chr(65+i)}-1") for i in range(26)])):
            self.assertLessEqual(len(keyboard[0]["payload"]["buttons"]), 30)
        def ids(keyboard):
            return {
            button["payload"] for row in keyboard[0]["payload"]["buttons"]
            for button in row if button["payload"].startswith("group:")
        }
        self.assertEqual(len(ids(first) | ids(second)), 80)
        self.assertFalse(ids(first) & ids(second))

    def test_group_callback_cannot_register_a_student_in_a_group_chat(self):
        client, store = Mock(), Mock()
        update = callback_update()
        update["message"]["recipient"]["chat_type"] = "chat"
        build_router(Deps(client, store, store, store, 90)).dispatch(update)
        store.choose_group.assert_not_called()
        self.assertIn("личном", client.answer.call_args.kwargs["notification"])

    def test_homework_text_does_not_activate_a_different_section(self):
        client, store = Mock(), Mock()
        homework, community = HomeworkHandler(), CommunityHandler()
        draft = HomeworkDraft(1, False, step="body", subject="Математика")
        homework.drafts.put(10, draft)
        router = build_router(Deps(client, store, store, store, 90),
                              homework=homework, community=community)
        update = message_update("материалы")
        update["message"]["recipient"]["chat_type"] = "dialog"
        router.dispatch(update)
        self.assertEqual(draft.body, "материалы")
        self.assertEqual(draft.step, "deadline")

    def test_event_title_can_equal_a_menu_word(self):
        client, store = Mock(), Mock()
        community = CommunityHandler()
        draft = CommunityDraft("event")
        community.drafts.put(10, draft)
        router = build_router(Deps(client, store, store, store, 90), community=community)
        update = message_update("мероприятия")
        update["message"]["recipient"]["chat_type"] = "dialog"
        router.dispatch(update)
        self.assertEqual(draft.title, "мероприятия")
        self.assertEqual(draft.step, 1)

    def test_invalid_content_length_is_sanitized(self):
        with patch("app.max_api.files.http.client.HTTPSConnection") as connect:
            response = connect.return_value.getresponse.return_value
            response.status = 200
            response.getheader.return_value = "private-invalid-value"
            with self.assertRaises(ValueError) as failure:
                transfer(None, "https://fd.oneme.ru/file", "GET", limit=100)
            self.assertNotIn("private-invalid-value", str(failure.exception))
            response.read.assert_not_called()

    def test_teacher_page_respects_max_message_limit(self):
        client = Mock()
        db = Mock()
        db.schedule_teachers.return_value = ("А-01", [
            (f"Преподаватель {i}", "Очень длинный предмет " * 80, "ауд. 1")
            for i in range(30)
        ])
        CommunityHandler.show_teachers(db, ["group", "0"], client)
        text, buttons = client.call_args.args
        self.assertLessEqual(len(text), 4000)
        self.assertTrue(any(payload.startswith("teachers_auto:group:") for _, payload in buttons))

    def test_backup_failure_cannot_publish_an_archive(self):
        script = Path(__file__).resolve().parents[1] / "scripts/backup.sh"
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            binary = root / "bin"
            binary.mkdir()
            dump = binary / "pg_dump"
            dump.write_text("#!/bin/sh\nprintf corrupt\nexit 1\n")
            dump.chmod(0o700)
            output = root / "backups"
            result = subprocess.run(["/bin/sh", str(script)], env={
                "PATH": str(binary) + os.pathsep + os.environ["PATH"],
                "BACKUP_DIR": str(output),
            }, capture_output=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(list(output.iterdir()), [])
