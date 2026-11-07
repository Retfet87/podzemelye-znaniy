"""Проверка пользовательского ввода."""
from urllib.parse import urlsplit


def validate_link(link):
    if not link:
        return
    try:
        url = urlsplit(link)
        if (len(link) <= 700 and url.scheme == "https" and url.hostname
                and url.username is None and url.port in (None, 443)
                and not any(c.isspace() or ord(c) < 32 or c in '<>\\' for c in link)):
            return
    except ValueError:
        pass
    raise ValueError("Укажи HTTPS-ссылку без пробелов и данных для входа.")
