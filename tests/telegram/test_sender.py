from types import SimpleNamespace

import pytest
from telegram.error import TimedOut

from pricewatch.telegram.sender import TELEGRAM_NETWORK_TIMEOUT_SECONDS, TelegramSender


class Bot:
    def __init__(self, result=None, error=None):
        self.result = result or SimpleNamespace(message_id=321)
        self.error = error
        self.calls = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.result


@pytest.mark.asyncio
async def test_sender_returns_message_id_and_applies_bounded_timeouts():
    bot = Bot()
    markup = object()

    message_id = await TelegramSender(bot).send(123, "text", markup)

    assert message_id == 321
    assert bot.calls == [{
        "chat_id": 123,
        "text": "text",
        "reply_markup": markup,
        "disable_web_page_preview": True,
        "connect_timeout": TELEGRAM_NETWORK_TIMEOUT_SECONDS,
        "read_timeout": TELEGRAM_NETWORK_TIMEOUT_SECONDS,
        "write_timeout": TELEGRAM_NETWORK_TIMEOUT_SECONDS,
        "pool_timeout": TELEGRAM_NETWORK_TIMEOUT_SECONDS,
    }]


@pytest.mark.asyncio
async def test_sender_preserves_telegram_exception_for_worker_classification():
    error = TimedOut("offline timeout")
    with pytest.raises(TimedOut) as raised:
        await TelegramSender(Bot(error=error)).send(123, "text")
    assert raised.value is error


def test_sender_rejects_timeout_above_contract():
    with pytest.raises(ValueError, match="between 1 and 30"):
        TelegramSender(Bot(), timeout_seconds=31)
