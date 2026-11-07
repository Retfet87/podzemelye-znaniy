import json
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import polling
from app.bootstrap import main as run_main
from app.config import PROJECT_DIR, load_config, max_tls_context
from app.handlers import Deps, build_router
from app.handlers.menus import HOME
from app.handlers.registration import WELCOME
from app.max_api.client import MaxClient, MaxError
from app.max_api.updates import start_recipient
from helpers import handle_update


def message_update(text="/start", *, chat_id=20, is_bot=False):
    return {
        "update_type": "message_created",
        "message": {
            "sender": {"user_id": 10, "is_bot": is_bot},
            "recipient": {"chat_id": chat_id},
            "body": {"text": text},
        },
    }


class BotTests(unittest.TestCase):
    def test_config_reads_token_from_file(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_DIR) as folder:
            env = Path(folder) / ".env"
            env.write_text("# Настройки\nMAX_BOT_TOKEN=test=value\n", encoding="utf-8")
            self.assertEqual(load_config(env), {"MAX_BOT_TOKEN": "test=value"})

    @patch("app.bootstrap.MaxClient")
    def test_empty_token_stops_before_creating_client(self, client_class):
        with patch("app.bootstrap.load_config", return_value={"MAX_BOT_TOKEN": ""}):
            with self.assertLogs("app", level="ERROR") as logs:
                self.assertEqual(run_main(), 1)
        self.assertIn("MAX_BOT_TOKEN отсутствует", logs.output[0])
        client_class.assert_not_called()

    def test_start_routes_reply_to_chat_or_sender(self):
        self.assertEqual(start_recipient(message_update(" /start ")), {"chat_id": 20})
        self.assertEqual(start_recipient(message_update(chat_id=None)), {"user_id": 10})

    def test_other_messages_are_ignored(self):
        self.assertIsNone(start_recipient(message_update("/starter")))
        self.assertIsNone(start_recipient(message_update(None)))
        self.assertIsNone(start_recipient(message_update(is_bot=True)))

    @patch("app.max_api.client.http.client.HTTPSConnection")
    def test_http_contract_for_polling_and_welcome(self, connection_class):
        connection = connection_class.return_value
        response = connection.getresponse.return_value
        response.status = 200
        update = message_update()
        response.read.side_effect = [
            json.dumps({"marker": 8, "updates": [update]}).encode(),
            b'{"message": {}}',
        ]
        client = MaxClient("test-token", Mock())
        self.assertEqual(client.get_updates(7), ([update], 8))
        client.send_message({"chat_id": 20}, WELCOME)

        poll, send = connection.request.call_args_list
        self.assertEqual(poll.args, ("GET", "/updates?timeout=30&types=message_created%2Cmessage_callback&marker=7"))
        self.assertEqual(poll.kwargs["headers"]["Authorization"], "test-token")
        self.assertEqual(send.args, ("POST", "/messages?chat_id=20"))
        self.assertEqual(json.loads(send.kwargs["body"]), {"text": WELCOME})

    @patch("app.max_api.client.http.client.HTTPSConnection")
    def test_auth_error_does_not_expose_response_body_or_token(self, connection_class):
        response = connection_class.return_value.getresponse.return_value
        response.status = 401
        client = MaxClient("test-token", Mock())
        with self.assertRaises(MaxError) as failure:
            client.get_updates(None)
        self.assertEqual(failure.exception.status, 401)
        self.assertNotIn("test-token", str(failure.exception))
        response.read.assert_not_called()

    @patch("app.polling.time.sleep")
    def test_polling_retries_and_passes_marker_after_reply(self, sleep):
        client = Mock()
        client.get_updates.side_effect = [
            MaxError("Ошибка соединения", retryable=True),
            ([message_update()], 8),
            KeyboardInterrupt,
        ]
        with self.assertLogs("app", level="INFO"):
            with self.assertRaises(KeyboardInterrupt):
                store = Mock()
                store.start.return_value = ("А-01",)
                polling.run_polling(client, build_router(Deps(client, store, store, store)))
        sleep.assert_called_once_with(2)
        client.send_message.assert_called_once_with(
            {"chat_id": 20}, f"{WELCOME}\nТвоя группа: А-01.", attachments=HOME
        )
        self.assertEqual([call.args for call in client.get_updates.call_args_list], [(None,), (None,), (8,)])


def callback_update(payload="group:5", *, user_id=10):
    return {
        "update_type": "message_callback",
        "callback": {
            "callback_id": "click",
            "payload": payload,
            "user": {"user_id": user_id, "is_bot": False},
        },
        "message": {"sender": {"user_id": 99, "is_bot": True}},
    }


class RegistrationTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.store = Mock()

    def test_first_start_uses_max_id_and_shows_group_buttons(self):
        self.store.start.return_value = None
        self.store.active_groups.return_value = [(5, "А-01")]
        handle_update(self.client, self.store, message_update())
        self.store.start.assert_called_once_with(10)
        self.client.send_message.assert_called_once_with(
            {"chat_id": 20},
            f"{WELCOME}\nВыбери свою группу:",
            attachments=[{"type": "inline_keyboard", "payload": {
                "buttons": [[{"type": "callback", "text": "А-01", "payload": "group:5"}]],
            }}],
        )

    def test_repeat_start_shows_schedule_without_group_lookup(self):
        self.store.start.return_value = ("А-01",)
        handle_update(self.client, self.store, message_update())
        self.store.active_groups.assert_not_called()
        self.client.send_message.assert_called_once_with(
            {"chat_id": 20}, f"{WELCOME}\nТвоя группа: А-01.", attachments=HOME,
        )

    def test_callback_uses_clicker_not_message_sender(self):
        self.store.choose_group.return_value = ("saved", "А-01")
        handle_update(self.client, self.store, callback_update())
        self.store.choose_group.assert_called_once_with(10, 5)
        self.client.answer.assert_called_once_with("click", text="Регистрация завершена. Твоя группа: А-01.", attachments=HOME)

    def test_invalid_and_unavailable_groups(self):
        for value in ("-1", "0", "９", "9223372036854775808", "1" * 5000):
            handle_update(self.client, self.store, callback_update(f"group:{value}"))
        self.store.choose_group.assert_not_called()
        self.store.choose_group.return_value = ("unavailable", None)
        handle_update(self.client, self.store, callback_update())
        self.assertIn("Группа недоступна", self.client.answer.call_args.kwargs["notification"])

    def test_empty_group_list_still_greets(self):
        self.store.start.return_value = None
        self.store.active_groups.return_value = []
        handle_update(self.client, self.store, message_update())
        self.client.send_message.assert_called_once_with(
            {"chat_id": 20}, f"{WELCOME}\nГруппы пока не добавлены. Попробуй /start позже.",
        )

    def test_group_chat_keeps_greeting_without_registering(self):
        update = message_update()
        update["message"]["recipient"]["chat_type"] = "chat"
        handle_update(self.client, self.store, update)
        self.store.start.assert_not_called()
        self.assertIn("в личном диалоге", self.client.send_message.call_args.args[1])

    @patch("app.max_api.client.http.client.HTTPSConnection")
    def test_callback_answer_removes_keyboard(self, connection_class):
        connection = connection_class.return_value
        response = connection.getresponse.return_value
        response.status = 200
        response.read.return_value = b'{"success": true}'
        MaxClient("test-token", Mock()).answer("click&", text="Сохранено")
        call = connection.request.call_args
        self.assertEqual(call.args, ("POST", "/answers?callback_id=click%26"))
        self.assertEqual(json.loads(call.kwargs["body"]), {
            "message": {"text": "Сохранено", "attachments": []},
        })

    def test_tls_with_project_ca_still_verifies_hostname_and_certificates(self):
        ca = "certificates/russian_trusted_root_ca.pem"
        context = max_tls_context({"MAX_CA_FILE": ca})
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        der = ssl.PEM_cert_to_DER_cert((PROJECT_DIR / ca).read_text())
        self.assertIn(der, context.get_ca_certs(binary_form=True))
        with self.assertRaises(OSError):
            max_tls_context({"MAX_CA_FILE": "certificates/missing.pem"})
