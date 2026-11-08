"""Передача файлов через CDN MAX: скачивание вложений и загрузка своих."""

import http.client
import json
import re
from urllib.parse import urlsplit
from uuid import uuid4

from app.services.file_validation import FILE_LIMIT, file_type, validate_data

CDN_SUFFIXES = (".max.ru", ".oneme.ru", ".okcdn.ru", ".cdn-max.ru")


def cdn_target(url):
    try:
        if not isinstance(url, str) or len(url) > 8192 or any(c.isspace() for c in url):
            raise ValueError
        parsed = urlsplit(url)
        host = parsed.hostname
        if (
            parsed.scheme != "https"
            or not host
            or not host.endswith(CDN_SUFFIXES)
            or not re.fullmatch(r"[a-z0-9.-]+", host)
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.fragment
        ):
            raise ValueError
        return host, (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    except ValueError:
        raise ValueError("MAX вернул недопустимый адрес файла.") from None


def receive_file(context, attachment):
    payload = attachment.get("payload") if isinstance(attachment, dict) else None
    if not isinstance(payload, dict) or attachment.get("type") != "file":
        raise ValueError("Отправь вложение как файл.")
    # Поддерживаем метаданные рядом с payload и прежний вложенный формат MAX.
    name = attachment.get("filename", payload.get("filename"))
    size = attachment.get("size", payload.get("size"))
    file_type(name)
    if type(size) is not int or not 0 < size <= FILE_LIMIT:
        raise ValueError("Размер файла должен быть от 1 байта до 5 МБ.")
    data = transfer(context, payload.get("url"), "GET", limit=FILE_LIMIT)
    if len(data) != size:
        raise ValueError("Файл загружен не полностью.")
    validate_data(name, data)
    return name, data


def upload_file(client, name, data):
    validate_data(name, data)
    mime, _ = file_type(name)
    result = client.request("POST", "/uploads", {"type": "file"})
    boundary = uuid4().hex
    prefix = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="data"; filename="{name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode()
    body = prefix + data + f"\r\n--{boundary}--\r\n".encode()
    raw = transfer(
        client.context,
        result.get("url"),
        "POST",
        limit=65536,
        body=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        reply = json.loads(raw)
        token = reply.get("token") if isinstance(reply, dict) else None
    except (ValueError, UnicodeError):
        token = None
    if not isinstance(token, str) or not token:
        raise ValueError("MAX не подтвердил загрузку файла.")
    return {"type": "file", "payload": {"token": token}}


def transfer(context, url, method, *, limit, body=None, headers=None):
    host, target = cdn_target(url)
    # Даже URL из ответа MAX не разрешает редиректы и передачу токена на CDN.
    connection = http.client.HTTPSConnection(host, timeout=10, context=context)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        length = response.getheader("Content-Length")
        if length is not None and (
            not isinstance(length, str)
            or not length.isascii()
            or not length.isdecimal()
            or len(length) > 12
        ):
            raise ValueError("MAX не передал файл допустимого размера.")
        if not 200 <= response.status < 300 or (
            length is not None and not 0 <= int(length) <= limit
        ):
            raise ValueError("MAX не передал файл допустимого размера.")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("MAX не передал файл допустимого размера.")
        return data
    except (OSError, http.client.HTTPException, UnicodeError):
        raise ValueError("Файл временно недоступен. Попробуй ещё раз.") from None
    finally:
        connection.close()
