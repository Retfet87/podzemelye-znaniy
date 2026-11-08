"""Тексты о мероприятиях."""
from app.timeutil import format_deadline


def event_text(title, description, starts_at, place):
    return (f"{title}\n{format_deadline(starts_at)}\nМесто: {place or 'не указано'}"
            + (f"\n\n{description}" if description else ""))
