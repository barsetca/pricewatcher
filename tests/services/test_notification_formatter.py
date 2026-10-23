import json

import pytest

from pricewatch.services.notification_formatter import format_notification


def payload(event_type, **changes):
    value = {
        "version": 1,
        "event_type": event_type,
        "detected_at": "2026-10-20T12:35:00+00:00",
        "product_id": 9,
        "offer_id": 123,
        "product_name": "Ноутбук <Lenovo> & IdeaPad",
        "source": "Amazon <ES>",
        "url": "https://example.com/product?a=1&b=2",
        "chat_id": 42,
        "previous_price_minor": 89900,
        "new_price_minor": 74900,
        "currency": "EUR",
        "drop_percent": "16.68520578420467185761957731",
        "reason": "price was not reliably extracted",
        "pending_check_attempt_id": 456,
    }
    value.update(changes)
    return json.dumps(value, ensure_ascii=False)


def notification_buttons(prepared):
    return [
        button
        for row in prepared.reply_markup.inline_keyboard
        for button in row
    ]


def test_price_drop_uses_decimal_snapshot_and_madrid_summer_time():
    prepared = format_notification(payload("PRICE_DROP"))

    buttons = notification_buttons(prepared)
    assert [button.text for button in buttons] == [
        "Открыть предложение", "Главное меню",
    ]
    assert [button.callback_data for button in buttons] == ["offer:123", "menu:home"]
    assert "Было: 899,00 €" in prepared.text
    assert "Стало: 749,00 €" in prepared.text
    assert "Снижение: 16,68520578420467185761957731%" in prepared.text
    assert "Обнаружено: 20.10.2026, 14:35" in prepared.text
    assert "Ноутбук <Lenovo> & IdeaPad" in prepared.text


def test_anomaly_has_two_short_callbacks_and_does_not_mutate_payload():
    source = payload("ANOMALY_DETECTED")
    prepared = format_notification(source)
    buttons = notification_buttons(prepared)

    assert [button.text for button in buttons] == [
        "✅ Подтвердить", "❌ Отклонить", "Открыть предложение", "Главное меню",
    ]
    assert [button.callback_data for button in buttons] == [
        "anomaly:confirm:123:456", "anomaly:reject:123:456",
        "offer:123", "menu:home",
    ]
    assert all(len(button.callback_data.encode("utf-8")) <= 64 for button in buttons)
    assert "Предыдущая цена: 899,00 €" in prepared.text
    assert json.loads(source)["pending_check_attempt_id"] == 456


def test_repeated_error_translates_standard_reason_and_madrid_winter_time():
    prepared = format_notification(payload(
        "REPEATED_ERROR", detected_at="2026-12-20T12:35:00Z"
    ))

    buttons = notification_buttons(prepared)
    assert [button.text for button in buttons] == [
        "Открыть предложение", "Главное меню",
    ]
    assert [button.callback_data for button in buttons] == ["offer:123", "menu:home"]
    assert "Три последовательные проверки" in prepared.text
    assert "Причина: не удалось получить цену со страницы" in prepared.text
    assert "Последняя попытка: 20.12.2026, 13:35" in prepared.text


def test_optional_values_have_safe_plain_text_fallbacks_and_iso_currency():
    prepared = format_notification(payload(
        "PRICE_DROP", product_name=None, source=None, url=None,
        detected_at=None, previous_price_minor=None, new_price_minor=123,
        currency="CHF", drop_percent=None,
    ))

    assert "Товар без названия" in prepared.text
    assert "Источник не указан" in prepared.text
    assert "Было: не указана" in prepared.text
    assert "Стало: 1,23 CHF" in prepared.text
    assert "Снижение: не указано" in prepared.text
    assert "время не указано" in prepared.text


@pytest.mark.parametrize("field", ["offer_id", "pending_check_attempt_id"])
def test_anomaly_requires_positive_callback_identifiers(field):
    with pytest.raises(ValueError, match=field):
        format_notification(payload("ANOMALY_DETECTED", **{field: None}))


@pytest.mark.parametrize("event_type", ["PRICE_DROP", "REPEATED_ERROR"])
def test_navigation_notifications_require_positive_offer_id(event_type):
    with pytest.raises(ValueError, match="offer_id"):
        format_notification(payload(event_type, offer_id=None))


@pytest.mark.parametrize(
    "event_type", ["PRICE_DROP", "ANOMALY_DETECTED", "REPEATED_ERROR"]
)
def test_all_notification_callback_data_fit_telegram_limit(event_type):
    prepared = format_notification(payload(
        event_type,
        offer_id=9_223_372_036_854_775_807,
        pending_check_attempt_id=9_223_372_036_854_775_807,
    ))

    assert all(
        len(button.callback_data.encode("utf-8")) <= 64
        for button in notification_buttons(prepared)
    )


def test_rejects_unsupported_payload_version():
    with pytest.raises(ValueError, match="version"):
        format_notification(payload("PRICE_DROP", version=2))
