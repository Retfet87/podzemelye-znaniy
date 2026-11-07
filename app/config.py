"""Конфигурация: чтение .env, TLS-контекст для MAX и настройки администратора."""
import re
import ssl
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def load_config(path=PROJECT_DIR / ".env"):
    """Читает простые строки КЛЮЧ=значение из локального файла."""
    if not path.exists():
        return {}

    config = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            config[key.strip()] = value.strip()
    return config


def max_tls_context(config):
    context = ssl.create_default_context()
    ca_file = config.get("MAX_CA_FILE")
    if ca_file:
        context.load_verify_locations(cafile=PROJECT_DIR / Path(ca_file).expanduser())
    return context


def admin_id(config):
    value = config.get("MAX_SUPER_ADMIN_ID", "")
    if not value:
        return None
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) >= 2**63:
        raise ValueError("Некорректная настройка MAX_SUPER_ADMIN_ID.")
    return int(value)
