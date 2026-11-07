"""HTTP-клиент платформы MAX (Long Polling, отправка сообщений, ответы на кнопки)."""
import http.client
import json
import ssl
import time
from urllib.parse import urlencode

from app.max_api import files

API_HOST = "platform-api2.max.ru"
POLL_TIMEOUT = 30


class MaxError(Exception):
    def __init__(self, message, *, retryable=False, status=None, retry_after=None, attachment_pending=False):
        super().__init__(message)
        self.retryable = retryable
        self.status = status
        self.retry_after = retry_after
        self.attachment_pending = attachment_pending


class MaxClient:
    def __init__(self, token, context):
        self.token = token
        self.context = context

    def request(self, method, path, params, *, payload=None, timeout=15):
        target = f"{path}?{urlencode(params)}"
        headers = {"Authorization": self.token, "Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        connection = http.client.HTTPSConnection(
            API_HOST, timeout=timeout, context=self.context
        )
        try:
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            status = response.status
            if not 200 <= status < 300:
                if status in (401, 403):
                    raise MaxError(
                        f"MAX отклонил доступ (HTTP {status}). Проверь MAX_BOT_TOKEN и права бота.",
                        status=status,
                    )
                retry_after = response.getheader("Retry-After") if status == 429 else None
                retry_after = (int(retry_after) if isinstance(retry_after, str)
                               and retry_after.isascii() and retry_after.isdecimal()
                               and len(retry_after) <= 9 else None)
                attachment_pending = False
                if status == 400 and method == "POST" and path == "/messages":
                    try:
                        error_body = json.loads(response.read(8192))
                        attachment_pending = (isinstance(error_body, dict)
                                              and error_body.get("code") == "attachment.not.ready")
                    except (ValueError, UnicodeError):
                        pass
                raise MaxError(
                    f"MAX вернул HTTP {status} для {method} {path}.",
                    retryable=status in (408, 429) or 500 <= status < 600,
                    status=status, retry_after=retry_after, attachment_pending=attachment_pending,
                )
            result = json.loads(response.read())
        except ssl.SSLCertVerificationError:
            raise MaxError(
                "Не удалось проверить TLS-сертификат MAX. Проверь MAX_CA_FILE и доверенные сертификаты."
            ) from None
        except (OSError, http.client.HTTPException):
            raise MaxError(
                f"Ошибка соединения с MAX при {method} {path}.", retryable=True
            ) from None
        except (ValueError, UnicodeError):
            raise MaxError(f"Некорректные данные для {method} {path}.") from None
        finally:
            connection.close()

        if not isinstance(result, dict):
            raise MaxError(f"Неожиданный ответ MAX для {method} {path}.")
        return result

    def receive_file(self, attachment):
        """Скачивает вложение-файл из сообщения пользователя."""
        return files.receive_file(self.context, attachment)

    def upload_file(self, name, data):
        """Загружает файл в MAX и возвращает вложение для send_message."""
        return files.upload_file(self, name, data)

    def get_updates(self, marker):
        params = {"timeout": POLL_TIMEOUT, "types": "message_created,message_callback"}
        if marker is not None:
            params["marker"] = marker
        result = self.request("GET", "/updates", params, timeout=POLL_TIMEOUT + 10)
        updates = result.get("updates")
        next_marker = result.get("marker")
        if not isinstance(updates, list) or (
            next_marker is not None and type(next_marker) is not int
        ):
            raise MaxError("MAX вернул обновления в неожиданном формате.")
        return updates, next_marker

    def send_message(self, recipient, text, *, attachments=None):
        payload = {"text": text}
        if attachments is not None:
            payload["attachments"] = attachments
        for attempt in range(3):
            try:
                result = self.request("POST", "/messages", recipient, payload=payload)
                break
            except MaxError as error:
                if not error.attachment_pending or attempt == 2:
                    raise
                time.sleep(attempt + 1)
        if not isinstance(result.get("message"), dict):
            raise MaxError("MAX не подтвердил отправку сообщения.", retryable=True)

    def answer(self, callback_id, *, text=None, notification=None, attachments=None):
        payload = (
            {"message": {"text": text, "attachments": attachments or []}}
            if text is not None else {"notification": notification}
        )
        self.request("POST", "/answers", {"callback_id": callback_id}, payload=payload)
