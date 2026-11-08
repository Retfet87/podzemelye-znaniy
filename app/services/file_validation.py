"""Проверка файлов-вложений: расширение, размер, сигнатура."""

from pathlib import PurePath
import unicodedata

FILE_LIMIT = 5 * 1024 * 1024
FILE_TYPES = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".txt": ("text/plain", None),
    ".ics": ("text/calendar", b"BEGIN:VCALENDAR\r\n"),
}


def file_type(name):
    if (
        not isinstance(name, str)
        or not 1 <= len(name) <= 100
        or name != name.strip()
        or any(
            c in '/\\"<>:|?*' or unicodedata.category(c).startswith("C") for c in name
        )
    ):
        raise ValueError("Недопустимое имя файла.")
    suffix = PurePath(name).suffix.lower()
    if suffix not in FILE_TYPES:
        raise ValueError("Можно прикрепить PDF, PNG, JPG, TXT или ICS.")
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
    if PurePath(name).suffix.lower() == ".ics":
        try:
            text = data.decode("utf-8")
            valid = valid and text.endswith("END:VCALENDAR\r\n") and "\x00" not in text
        except UnicodeError:
            valid = False
    if not valid:
        raise ValueError("Содержимое файла не соответствует его расширению.")
