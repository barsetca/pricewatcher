from types import SimpleNamespace

import pytest

from pricewatch.config import load_telegram_settings
from pricewatch.telegram.auth import access_error


def test_telegram_settings_require_token_and_valid_whitelist(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1")
    with pytest.raises(ValueError, match="TOKEN"):
        load_telegram_settings()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:" + "offline_test_token_" * 2)
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,bad")
    with pytest.raises(ValueError, match="numeric"):
        load_telegram_settings()
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1, 2")
    assert load_telegram_settings().allowed_user_ids == frozenset({1, 2})


def test_telegram_settings_reject_invalid_token_without_echoing_secret(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "not-a-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "123")
    with pytest.raises(ValueError, match="invalid Bot API token format") as error:
        load_telegram_settings()
    assert "not-a-token" not in str(error.value)


def test_whitelist_requires_private_effective_user():
    private = SimpleNamespace(effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(type="private"))
    denied = SimpleNamespace(effective_user=SimpleNamespace(id=8), effective_chat=SimpleNamespace(type="private"))
    group = SimpleNamespace(effective_user=SimpleNamespace(id=7), effective_chat=SimpleNamespace(type="group"))
    assert access_error(private, frozenset({7})) is None
    assert "8" in access_error(denied, frozenset({7}))
    assert access_error(group, frozenset({7})) == "Доступ к PriceWatch не разрешен."
