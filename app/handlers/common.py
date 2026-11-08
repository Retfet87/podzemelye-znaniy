"""Общие части диалоговых обработчиков: числовые ID из кнопок и хранилище черновиков."""

from time import monotonic

MAX_ID = 2**63 - 1
DRAFT_TTL = 30 * 60
MAX_DRAFTS = 16


def number(value, *, zero=False):
    if not 1 <= len(value) <= 19 or not value.isascii() or not value.isdecimal():
        raise ValueError("Кнопка или ID недействительны.")
    result = int(value)
    if not int(not zero) <= result <= MAX_ID:
        raise ValueError("Кнопка или ID недействительны.")
    return result


class DraftStore(dict):
    """Черновики по MAX ID: ограничены по времени жизни и по количеству."""

    def __init__(self, limit_message="Слишком много черновиков. Попробуй позже."):
        super().__init__()
        self.limit_message = limit_message

    def prune(self):
        now = monotonic()
        for user, draft in list(self.items()):
            if now - draft.touched >= DRAFT_TTL:
                del self[user]

    def put(self, user, draft):
        if user not in self and len(self) >= MAX_DRAFTS:
            raise ValueError(self.limit_message)
        self[user] = draft
