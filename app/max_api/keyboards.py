"""Построители inline-клавиатур MAX."""


def callback_button(text, payload):
    return {"type": "callback", "text": "⬅️ Назад" if text in ("Назад", "К списку", "К категориям", "К мероприятию") else text, "payload": payload}


def inline_keyboard(rows, *, home=True):
    """rows — список рядов, в каждом ряду список кнопок (словарей)."""
    rows = [list(row) for row in rows]
    if home and not any(b.get("payload") == "nav:home" for row in rows for b in row):
        rows.append([callback_button("🏠 Домой", "nav:home")])
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def vertical_keyboard(buttons, *, prefix=""):
    """По одной кнопке в ряд. buttons — пары (подпись, payload без префикса)."""
    if not buttons:
        return []
    return inline_keyboard([
        [callback_button(label[:70], prefix + payload)] for label, payload in buttons
    ])


def with_home(attachments):
    if any(a.get("type") == "inline_keyboard" for a in attachments or []):
        return attachments
    return list(attachments or []) + inline_keyboard([])
