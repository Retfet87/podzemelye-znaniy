"""Данные демо-сайта: единый источник для бота и сайта."""

from datetime import datetime, timedelta
import json
from pathlib import Path
import re

from app.services.validation import validate_link
from app.timeutil import SAMARA

ROOT = Path(__file__).resolve().parents[1]
SITE_JSON = ROOT / "fixtures/demo/site.json"
ASSETS = ROOT / "demo-site/assets"

FORBIDDEN = {
    "max_user_id",
    "user_id",
    "initData",
    "password",
    "token",
    "DATABASE_URL",
    "owner_id",
}

ALLOWED_MEDIA_SRCS = {"./assets/dungeon.png", "./assets/demo-flower.mp4"}
ALLOWED_FILE_NAMES = {"demo-task.txt"}

TAG_MAP = {
    "Учёба": "study",
    "Конкурсы": "contest",
    "Хакатон": "hackathon",
    "Хакатоны": "hackathon",
    "Культура": "culture",
    "Спорт": "sport",
    "Важное": "important",
    "Другое": "other",
}

MONTHS_RU = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


def read_asset_bytes(rel_path: str) -> tuple[str, bytes]:
    """Безопасно читает только разрешённые файлы из demo-site/assets."""
    if (
        not isinstance(rel_path, str)
        or not rel_path
        or ".." in rel_path
        or "\\" in rel_path
    ):
        raise ValueError("Недопустимый путь к медиафайлу демо-сайта.")
    clean = (
        rel_path.removeprefix("./assets/")
        if rel_path.startswith("./assets/")
        else rel_path
    )
    if "/" in clean or clean not in {"dungeon.png", "demo-flower.mp4", "demo-task.txt"}:
        raise ValueError(f"Файл не входит в белый список демо-ресурсов: {rel_path}")
    if (ASSETS / clean).is_symlink():
        raise ValueError("Символические ссылки в demo-ресурсах запрещены.")
    target = (ASSETS / clean).resolve()
    if target.parent != ASSETS.resolve() or not target.is_file() or target.is_symlink():
        raise ValueError(f"Ресурс демо-сайта недоступен: {clean}")
    return clean, target.read_bytes()


def parse_site_subtitle(
    subtitle: str,
    *,
    is_history: bool = False,
    year: int = 2026,
    date_shift_days: int = 0,
) -> tuple[datetime, str]:
    """
    Разбирает строку вида:
    - '12 ноября · 14:00 · ДЕМО-площадка' -> (2026-11-12 14:00+04:00, 'ДЕМО-площадка')
    - '6 ноября · ДЕМО-лаборатория' (для истории без времени принимает 12:00).
    """
    parts = [p.strip() for p in (subtitle or "").split(" · ") if p.strip()]
    if len(parts) == 3:
        date_part, time_part, place = parts
    elif len(parts) == 2 and is_history:
        date_part, place = parts
        time_part = "12:00"
    else:
        raise ValueError(f"Некорректный формат subtitle мероприятия: {subtitle!r}")

    m_date = re.fullmatch(r"(\d{1,2})\s+([а-яё]+)", date_part.casefold())
    if not m_date or m_date.group(2) not in MONTHS_RU:
        raise ValueError(f"Не удалось разобрать дату из subtitle: {date_part!r}")
    day = int(m_date.group(1))
    month = MONTHS_RU[m_date.group(2)]

    m_time = re.fullmatch(r"(\d{2}):(\d{2})", time_part)
    if not m_time:
        raise ValueError(f"Не удалось разобрать время из subtitle: {time_part!r}")
    hour, minute = int(m_time.group(1)), int(m_time.group(2))

    dt = datetime(year, month, day, hour, minute, tzinfo=SAMARA)
    if date_shift_days:
        dt = dt + timedelta(days=int(date_shift_days))
    return dt, place


def map_tags(labels: list[str]) -> list[str]:
    result = []
    for label in labels or []:
        if label not in TAG_MAP:
            raise ValueError(f"Неизвестный тег демо-сайта: {label!r}")
        code = TAG_MAP[label]
        if code not in result:
            result.append(code)
    return sorted(result)


def load_site() -> dict:
    """Читает и проверяет fixtures/demo/site.json по правилам публичной сборки."""
    data = json.loads(SITE_JSON.read_text(encoding="utf-8"))

    def require(condition, message="Invalid public demo fixture"):
        if not condition:
            raise ValueError(message)

    require(
        isinstance(data, dict)
        and data.get("demo") is True
        and set(data.get("sections", ()))
        == {"schedule", "homework", "events", "useful"}
    )

    def walk(value):
        if isinstance(value, dict):
            require(not (set(value) & FORBIDDEN), "Private fields in public fixture")
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            require("postgresql://" not in value and "X-Max-Init-Data" not in value)
            require(
                not re.search(
                    r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D]",
                    value,
                ),
                "Decorative emoji in public fixture",
            )

    walk(data)
    for section in data["sections"].values():
        require(section.get("demo") is True and bool(section.get("cards")))
        for card in section["cards"] + section.get("history", []):
            require(
                isinstance(card.get("title"), str) and card["title"].startswith("ДЕМО")
            )
            for media in card.get("media", []):
                require(media.get("src") in ALLOWED_MEDIA_SRCS)
    require(len(data["sections"]["useful"]["cards"]) == 32)
    return data


def events(*, date_shift_days: int = 0) -> list[dict]:
    """Возвращает 3 будущих мероприятия и 1 запись истории из site.json, готовые для БД."""
    section = load_site()["sections"]["events"]
    result = []

    for item in section["cards"]:
        starts_at, place = parse_site_subtitle(
            item["subtitle"], is_history=False, date_shift_days=date_shift_days
        )
        title = item["title"]
        body = item["body"]
        if not (1 <= len(title) <= 120 and len(body) <= 1800 and len(place) <= 300):
            raise ValueError("Превышены лимиты полей мероприятия.")
        media_items = []
        for m in item.get("media", []):
            kind = m.get("type")
            if kind not in ("image", "video"):
                raise ValueError("Неизвестный тип медиа.")
            fname, raw = read_asset_bytes(m.get("src", ""))
            media_items.append((kind, fname, raw))
        result.append(
            {
                "key": item["id"],
                "title": title,
                "description": body,
                "starts_at": starts_at,
                "place": place,
                "state": "published",
                "tags": map_tags(item.get("tags", [])),
                "report_published": False,
                "report_body": "",
                "report_link": "",
                "media_section": "event",
                "media": media_items,
            }
        )

    for item in section.get("history", []):
        starts_at, place = parse_site_subtitle(
            item["subtitle"], is_history=True, date_shift_days=date_shift_days
        )
        title = item["title"]
        body = item["body"]
        report_body = item.get("report_body", "")
        report_link = item.get("report_link", "")
        validate_link(report_link)
        if not (
            1 <= len(title) <= 120
            and len(body) <= 1800
            and len(report_body) <= 1800
            and len(place) <= 300
        ):
            raise ValueError("Превышены лимиты полей истории мероприятия.")
        media_items = []
        for m in item.get("media", []):
            kind = m.get("type")
            if kind not in ("image", "video"):
                raise ValueError("Неизвестный тип медиа.")
            fname, raw = read_asset_bytes(m.get("src", ""))
            media_items.append((kind, fname, raw))
        result.append(
            {
                "key": item["id"],
                "title": title,
                "description": body,
                "starts_at": starts_at,
                "place": place,
                "state": "published",
                "tags": map_tags(item.get("tags", [])),
                "report_published": bool(item.get("report_published", True)),
                "report_body": report_body,
                "report_link": report_link,
                "media_section": "report",
                "media": media_items,
            }
        )

    return result


def useful() -> list[dict]:
    """Возвращает 32 карточки раздела «Полезное» из site.json с их файлами."""
    cards = load_site()["sections"]["useful"]["cards"]
    result = []
    for card in cards:
        title = card["title"]
        body = (
            "ДЕМО · вымышленные данные. Не справочная информация АКВТ.\n" + card["body"]
        )
        link = card.get("link", "")
        category = card["category"]
        validate_link(link)
        if not (
            1 <= len(title) <= 120 and len(body) <= 1800 and title.startswith("ДЕМО")
        ):
            raise ValueError("Некорректная карточка Полезного в site.json.")
        attachments = []
        for fname in card.get("files", []):
            if fname not in ALLOWED_FILE_NAMES:
                raise ValueError(f"Неразрешённое вложение в Полезном: {fname}")
            clean_name, raw = read_asset_bytes(fname)
            attachments.append((clean_name, raw))
        result.append(
            {
                "key": card["id"],
                "category": category,
                "title": title,
                "body": body,
                "link": link,
                "files": attachments,
            }
        )
    return result
