import http.client
import json
from pathlib import PurePath
import re
import unicodedata
from urllib.parse import urlsplit
from uuid import uuid4

FILE_LIMIT = 5 * 1024 * 1024

CDN_SUFFIXES = (".max.ru", ".oneme.ru", ".okcdn.ru", ".cdn-max.ru")
FILE_TYPES = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".txt": ("text/plain", None),
}


def cdn_target(url):
    try:
        if not isinstance(url, str) or len(url) > 8192 or any(c.isspace() for c in url):
            raise ValueError
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or not host.endswith(CDN_SUFFIXES)
                or not re.fullmatch(r"[a-z0-9.-]+", host) or parsed.port not in (None, 443)
                or parsed.username is not None or parsed.fragment):
            raise ValueError
        return host, (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    except ValueError:
        raise ValueError("MAX вернул недопустимый адрес файла.") from None


def file_type(name):
    if (not isinstance(name, str) or not 1 <= len(name) <= 100 or name != name.strip()
            or any(c in '/\\"<>:|?*' or unicodedata.category(c).startswith("C") for c in name)):
        raise ValueError("Недопустимое имя файла.")
    suffix = PurePath(name).suffix.lower()
    if suffix not in FILE_TYPES:
        raise ValueError("Можно прикрепить PDF, PNG, JPG или TXT.")
    return FILE_TYPES[suffix]


def validate_data(name, data):
    _, signature = file_type(name)
    if not 0 < len(data) <= FILE_LIMIT:
        raise ValueError("Размер файла должен быть от 1 байта до 5 МБ.")
    if signature:
        valid = data.startswith(signature)
    else:
        try:
            text = data.decode("utf-8-sig")
            valid = bool(text) and all(c.isprintable() or c in "\t\r\n" for c in text)
        except UnicodeDecodeError:
            valid = False
    if not valid:
        raise ValueError("Содержимое файла не соответствует его расширению.")


def receive_file(context, attachment):
    payload = attachment.get("payload") if isinstance(attachment, dict) else None
    if not isinstance(payload, dict) or attachment.get("type") != "file":
        raise ValueError("Отправь вложение как файл.")
    # MAX currently returns metadata beside payload; older shapes document it inside.
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
    raw = transfer(client.context, result.get("url"), "POST", limit=65536, body=body,
                   headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
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
    # The URL comes from an authenticated MAX update/API response. Do not follow
    # redirects or send credentials to the CDN.
    connection = http.client.HTTPSConnection(host, timeout=10, context=context)
    try:
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        length = response.getheader("Content-Length")
        if not 200 <= response.status < 300 or (length is not None and not 0 <= int(length) <= limit):
            raise ValueError("MAX не передал файл допустимого размера.")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError("MAX не передал файл допустимого размера.")
        return data
    except (OSError, http.client.HTTPException, UnicodeError):
        raise ValueError("Файл временно недоступен. Попробуй ещё раз.") from None
    finally:
        connection.close()
