"""Построители inline-клавиатур MAX."""


def callback_button(text, payload):
    return {"type": "callback", "text": text, "payload": payload}


def inline_keyboard(rows):
    """rows — список рядов, в каждом ряду список кнопок (словарей)."""
    return [{"type": "inline_keyboard", "payload": {"buttons": rows}}]


def vertical_keyboard(buttons, *, prefix=""):
    """По одной кнопке в ряд. buttons — пары (подпись, payload без префикса)."""
    if not buttons:
        return []
    return inline_keyboard([
        [callback_button(label[:70], prefix + payload)] for label, payload in buttons
    ])
