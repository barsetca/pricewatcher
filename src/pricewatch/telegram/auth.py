"""Централизованная whitelist-проверка Telegram updates."""
from __future__ import annotations


def access_error(update, allowed_user_ids: frozenset[int]) -> str | None:
    user = getattr(update, "effective_user", None)
    chat = getattr(update, "effective_chat", None)
    user_id = getattr(user, "id", None)
    if getattr(chat, "type", "private") != "private" or not isinstance(user_id, int) or user_id not in allowed_user_ids:
        return f"Доступ к PriceWatch не разрешен. Ваш Telegram ID: {user_id}" if isinstance(user_id, int) and getattr(chat, "type", "private") == "private" else "Доступ к PriceWatch не разрешен."
    return None
