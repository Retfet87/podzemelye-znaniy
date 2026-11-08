"""Проверка пользовательского ввода."""

from urllib.parse import urlsplit


def validate_link(link):
    if not link:
        return
    try:
        url = urlsplit(link)
        if (
            len(link) <= 700
            and url.scheme == "https"
            and url.hostname
            and url.username is None
            and url.port in (None, 443)
            and not any(c.isspace() or ord(c) < 32 or c in "<>\\" for c in link)
        ):
            return
    except ValueError:
        pass
    raise ValueError("Укажи HTTPS-ссылку без пробелов и данных для входа.")


def validate_public_site_url(value):
    """Только явно настроенный публичный HTTPS без токенов, loopback и догадок об адресе."""
    if not value:
        return None
    from ipaddress import ip_address

    try:
        validate_link(value)
        parsed = urlsplit(value)
        host = parsed.hostname.rstrip(".")
        if (
            parsed.password
            or parsed.fragment
            or parsed.query
            or host == "localhost"
            or host.endswith((".localhost", ".local"))
            or "." not in host
        ):
            return None
        try:
            if not ip_address(host).is_global:
                return None
        except ValueError:
            if not any(c.isalpha() for c in host.rsplit(".", 1)[-1]):
                return None
        return value
    except (ValueError, AttributeError):
        return None
