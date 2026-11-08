"""Серверная проверка initData MAX без сетевых запросов и обхода учётной записи."""

import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


class InvalidLaunch(ValueError):
    pass


def verify_init_data(raw, bot_token, *, now=None, ttl=300):
    try:
        if not isinstance(bot_token, str) or not bot_token:
            raise ValueError
        if not isinstance(raw, str) or not raw or len(raw.encode("utf-8")) > 8192:
            raise ValueError
        if re.search(r"%(?![0-9a-fA-F]{2})", raw):
            raise ValueError
        pairs = parse_qsl(
            raw,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=32,
        )
        data = dict(pairs)
        if len(data) != len(pairs) or not all(key for key, _ in pairs):
            raise ValueError
        signature = data.pop("hash")
        if not re.fullmatch("[0-9a-f]{64}", signature):
            raise ValueError
        launch = "\n".join(f"{k}={data[k]}" for k in sorted(data))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, launch.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError
        stamp = data["auth_date"]
        if not re.fullmatch("[0-9]{1,12}", stamp):
            raise ValueError
        age = (time.time() if now is None else now) - int(stamp)
        if age < -30 or age > ttl:
            raise ValueError
        user = json.loads(data["user"])
        actor = user.get("id") if isinstance(user, dict) else None
        if type(actor) is not int or not 0 < actor < 2**63:
            raise ValueError
        return actor
    except (ValueError, KeyError, UnicodeError, AttributeError, TypeError):
        raise InvalidLaunch("Открой приложение заново из MAX.") from None
