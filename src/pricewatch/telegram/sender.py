"""Тонкий асинхронный адаптер отправки сообщений через Telegram Bot API."""

from __future__ import annotations

from typing import Any


TELEGRAM_NETWORK_TIMEOUT_SECONDS = 30


class TelegramSender:
    """Отправляет готовый текст и не знает о SQLite или Outbox."""

    def __init__(self, bot: Any, timeout_seconds: int = TELEGRAM_NETWORK_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0 or timeout_seconds > TELEGRAM_NETWORK_TIMEOUT_SECONDS:
            raise ValueError("Telegram timeout must be between 1 and 30 seconds")
        self._bot = bot
        self._timeout_seconds = timeout_seconds

    async def send(self, chat_id: int, text: str, reply_markup: Any = None) -> int:
        message = await self._bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
            connect_timeout=self._timeout_seconds,
            read_timeout=self._timeout_seconds,
            write_timeout=self._timeout_seconds,
            pool_timeout=self._timeout_seconds,
        )
        return message.message_id
