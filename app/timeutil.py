"""Часовой пояс колледжа и разбор/вывод сроков."""
from datetime import datetime
from zoneinfo import ZoneInfo

SAMARA = ZoneInfo("Europe/Samara")


def parse_deadline(text, now=None):
    try:
        deadline = datetime.strptime(text, "%d.%m.%Y %H:%M").replace(tzinfo=SAMARA)
    except ValueError:
        raise ValueError("Введи срок в формате ДД.ММ.ГГГГ ЧЧ:ММ (Астрахань).") from None
    if deadline <= (now or datetime.now(SAMARA)):
        raise ValueError("Срок должен быть в будущем.")
    return deadline


def format_deadline(deadline):
    return deadline.astimezone(SAMARA).strftime("%d.%m.%Y %H:%M (Астрахань)") if deadline else "без срока"
