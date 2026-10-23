"""Форматирование сохранённых Outbox snapshot для Telegram."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from pricewatch.domain.enums import NotificationEventType


@dataclass(frozen=True)
class PreparedNotification:
    text: str
    reply_markup: Any = None


DISPLAY_TIMEZONE = ZoneInfo("Europe/Madrid")


class InvalidNotificationPayload(ValueError):
    """Stored payload cannot become valid without changing the Outbox row."""


def format_notification(payload_json: str) -> PreparedNotification:
    """Строит plain-text уведомление только из payload v1.

    Денежная модель проекта сохраняет все принятые цены как 1/100 валютной
    единицы. Отдельного справочника ISO 4217 exponent в проекте пока нет.
    """
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as error:
        raise InvalidNotificationPayload("notification payload is not valid JSON") from error
    if not isinstance(payload, dict):
        raise InvalidNotificationPayload("notification payload must be a JSON object")
    if payload.get("version") != 1:
        raise InvalidNotificationPayload("unsupported notification payload version")

    try:
        event_type = NotificationEventType(payload.get("event_type"))
    except (TypeError, ValueError) as error:
        raise InvalidNotificationPayload("unsupported notification event type") from error
    product = _text(payload.get("product_name"), "Товар без названия")
    source = _text(payload.get("source"), "Источник не указан")
    url = _text(payload.get("url"), "URL не указан")
    detected_at = _datetime(payload.get("detected_at"))
    offer_id = _positive_int(payload.get("offer_id"), "offer_id")

    if event_type is NotificationEventType.PRICE_DROP:
        return PreparedNotification("\n".join([
            "🔻 Снижение цены", "", product, "", f"Магазин: {source}", "",
            f"Было: {_money(payload.get('previous_price_minor'), payload.get('currency'))}",
            f"Стало: {_money(payload.get('new_price_minor'), payload.get('currency'))}",
            f"Снижение: {_percent(payload.get('drop_percent'))}", "",
            f"Обнаружено: {detected_at}", f"Ссылка: {url}",
        ]), _notification_markup(offer_id))

    if event_type is NotificationEventType.ANOMALY_DETECTED:
        attempt_id = _positive_int(
            payload.get("pending_check_attempt_id"), "pending_check_attempt_id"
        )
        confirm = f"anomaly:confirm:{offer_id}:{attempt_id}"
        reject = f"anomaly:reject:{offer_id}:{attempt_id}"
        markup = _notification_markup(
            offer_id,
            (("✅ Подтвердить", confirm), ("❌ Отклонить", reject)),
        )
        return PreparedNotification("\n".join([
            "⚠️ Обнаружено необычное изменение цены", "", product, "",
            f"Магазин: {source}", "",
            f"Предыдущая цена: {_money(payload.get('previous_price_minor'), payload.get('currency'))}",
            f"Обнаруженная цена: {_money(payload.get('new_price_minor'), payload.get('currency'))}",
            "", "Цена требует подтверждения.", "",
            f"Обнаружено: {detected_at}", f"Ссылка: {url}",
        ]), markup)

    return PreparedNotification("\n".join([
        "⚠️ Не удается проверить цену", "", product, "", f"Магазин: {source}", "",
        "Три последовательные проверки завершились ошибкой.", "",
        f"Причина: {_reason(payload.get('reason'))}", "",
        f"Последняя попытка: {detected_at}", f"Ссылка: {url}",
    ]), _notification_markup(offer_id))


def _notification_markup(
    offer_id: int,
    actions: tuple[tuple[str, str], ...] = (),
) -> InlineKeyboardMarkup:
    rows = []
    if actions:
        rows.append([
            _callback_button(title, callback_data)
            for title, callback_data in actions
        ])
    rows.extend((
        [_callback_button("Открыть предложение", f"offer:{offer_id}")],
        [_callback_button("Главное меню", "menu:home")],
    ))
    return InlineKeyboardMarkup(rows)


def _callback_button(title: str, callback_data: str) -> InlineKeyboardButton:
    if len(callback_data.encode("utf-8")) > 64:
        raise InvalidNotificationPayload(
            "notification callback_data exceeds Telegram 64-byte limit"
        )
    return InlineKeyboardButton(title, callback_data=callback_data)


def _text(value: object, fallback: str) -> str:
    return str(value).strip() if value is not None and str(value).strip() else fallback


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise InvalidNotificationPayload(f"{field} must be a positive integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as error:
        raise InvalidNotificationPayload(
            f"{field} must be a positive integer"
        ) from error
    if parsed <= 0 or str(parsed) != str(value):
        raise InvalidNotificationPayload(f"{field} must be a positive integer")
    return parsed


def _money(minor: object, currency: object) -> str:
    if isinstance(minor, bool):
        return "не указана"
    try:
        amount = Decimal(int(minor)) / Decimal("100")  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return "не указана"
    rendered = format(amount, ".2f").replace(".", ",")
    code = _text(currency, "")
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(code.upper())
    return f"{rendered} {symbol or code}".rstrip()


def _percent(value: object) -> str:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return "не указано"
    if not decimal.is_finite():
        return "не указано"
    return f"{format(decimal, 'f').replace('.', ',')}%"


def _datetime(value: object) -> str:
    if value is None or not str(value).strip():
        return "время не указано"
    raw = str(value).strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return "время не указано"
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(DISPLAY_TIMEZONE).strftime("%d.%m.%Y, %H:%M")


def _reason(value: object) -> str:
    reason = _text(value, "не удалось получить цену со страницы")
    return {
        "price was not reliably extracted": "не удалось получить цену со страницы",
        "currency changed": "валюта цены неожиданно изменилась",
        "invalid price or currency": "получена некорректная цена или валюта",
        "price outside SQLite INTEGER": "полученная цена находится вне допустимого диапазона",
    }.get(reason, reason)
