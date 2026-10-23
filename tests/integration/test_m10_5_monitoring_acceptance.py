"""M10.5: сквозная приёмка monitoring/outbox на детерминированных ответах."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from telegram.error import NetworkError

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.offer_repository import OfferRepository
from pricewatch.domain.enums import (
    ExtractionMethod,
    ExtractionStatus,
    FetchMethod,
    FetchStatus,
    OfferStatus,
)
from pricewatch.scraping.models import ExtractionResult, FetchResult, PriceCheckResult
from pricewatch.scraping.url_security import UrlSecurity
from pricewatch.services.monitoring_service import EventType, MonitoringService
from pricewatch.services.outbox_worker import OutboxRunResult, OutboxWorker
from pricewatch.services.product_service import ProductService
from pricewatch.telegram.sender import TelegramSender


URL = "https://shop.test/item"
TELEGRAM_USER_ID = 420_001


class MutableChecker:
    def __init__(self, result: PriceCheckResult) -> None:
        self.result = result
        self.calls: list[str] = []

    def check(self, url: str) -> PriceCheckResult:
        self.calls.append(url)
        return self.result


class MockTelegramBot:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_once:
            self.fail_once = False
            raise NetworkError("controlled Telegram outage")
        return SimpleNamespace(message_id=700 + len(self.calls))


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def safe_security() -> UrlSecurity:
    return UrlSecurity(
        lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
    )


def checked(
    price: str | None,
    *,
    fetch_status: FetchStatus = FetchStatus.SUCCESS,
    extraction_status: ExtractionStatus | None = None,
    currency: str = "EUR",
) -> PriceCheckResult:
    status = extraction_status or (
        ExtractionStatus.SUCCESS if price is not None else ExtractionStatus.PARTIAL
    )
    fetch = FetchResult(
        URL,
        FetchMethod.REQUESTS,
        fetch_status,
        final_url=URL,
        http_status=200 if fetch_status is FetchStatus.SUCCESS else None,
        elapsed_time_ms=5,
        error_message=None if fetch_status is FetchStatus.SUCCESS else "controlled failure",
    )
    extraction = ExtractionResult(
        status,
        title="Controlled product",
        price=Decimal(price) if price is not None else None,
        currency=currency if price is not None else None,
        method=ExtractionMethod.JSON_LD,
        confidence_score=0.95 if price is not None else None,
    )
    return PriceCheckResult(fetch, extraction, (fetch,))


def bootstrap(path):
    initialize_database(path)
    connection = connect(path)
    checker = MutableChecker(checked("100.00"))
    products = ProductService(connection, checker, safe_security())
    user = products.get_or_create_user(TELEGRAM_USER_ID, "m10_5")
    created = products.create_product_with_offer(user.id, "Controlled product", URL)
    assert created.code == "CREATED"
    assert created.offer is not None
    return connection, checker, user, created.offer


def rows(connection, table: str):
    return connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


def test_controlled_price_rules_persistence_outbox_and_idempotency(tmp_path):
    path = tmp_path / "controlled-monitoring.sqlite"
    connection, checker, user, offer = bootstrap(path)
    monitoring = MonitoringService(connection, checker)
    try:
        # Baseline ProductService: одна цена/attempt, уведомления нет.
        assert len(rows(connection, "price_history")) == 1
        assert len(rows(connection, "check_attempts")) == 1
        assert rows(connection, "notification_outbox") == []

        # Повторная идентичная цена.
        unchanged = monitoring.check_for_user(user.id, offer.id)
        assert unchanged.code == "SUCCESS" and unchanged.events == ()

        # Ровно 10%: строгое правило > 10%, события нет.
        checker.result = checked("90.00")
        exact_threshold = monitoring.check_for_user(user.id, offer.id)
        assert exact_threshold.code == "SUCCESS" and exact_threshold.events == ()

        # С 90 до 80 — больше 10%, создаётся ровно один PRICE_DROP.
        checker.result = checked("80.00")
        drop = monitoring.check_for_user(user.id, offer.id)
        assert [event.type for event in drop.events] == [EventType.PRICE_DROP]

        # Повышение подтверждается в истории, но не создаёт drop.
        checker.result = checked("95.00")
        increase = monitoring.check_for_user(user.id, offer.id)
        assert increase.code == "SUCCESS" and increase.events == ()

        # Ровно 50% считается anomaly. Повтор той же pending anomaly не дублирует Outbox.
        checker.result = checked("47.50")
        anomaly = monitoring.check_for_user(user.id, offer.id)
        assert [event.type for event in anomaly.events] == [EventType.ANOMALY_DETECTED]
        pending = OfferRepository(connection).get_for_user(offer.id, user.id)
        assert pending.status is OfferStatus.SUSPICIOUS
        assert pending.pending_check_attempt_id is not None
        repeated = monitoring.check_for_user(user.id, offer.id)
        assert repeated.code == "SUSPICIOUS" and repeated.events == ()
        assert len(rows(connection, "notification_outbox")) == 2

        confirmed = monitoring.confirm_anomaly(
            user.id, offer.id, pending.pending_check_attempt_id
        )
        assert confirmed.code == "CONFIRMED"
        history_after_confirmation = len(rows(connection, "price_history"))

        # Fetch error и AMBIGUOUS создают CheckAttempt, но не цену/уведомление.
        checker.result = checked(None, fetch_status=FetchStatus.TIMEOUT)
        error = monitoring.check_for_user(user.id, offer.id)
        assert error.events == ()
        checker.result = checked(
            None,
            extraction_status=ExtractionStatus.AMBIGUOUS,
        )
        ambiguous = monitoring.check_for_user(user.id, offer.id)
        assert ambiguous.events == ()
        assert len(rows(connection, "price_history")) == history_after_confirmation
        assert len(rows(connection, "notification_outbox")) == 2

        outbox = rows(connection, "notification_outbox")
        drop_payload = json.loads(outbox[0]["payload_json"])
        anomaly_payload = json.loads(outbox[1]["payload_json"])
        assert (
            drop_payload["event_type"],
            drop_payload["previous_price_minor"],
            drop_payload["new_price_minor"],
            drop_payload["currency"],
            drop_payload["offer_id"],
            drop_payload["chat_id"],
        ) == ("PRICE_DROP", 9000, 8000, "EUR", offer.id, TELEGRAM_USER_ID)
        assert anomaly_payload["event_type"] == "ANOMALY_DETECTED"
        assert anomaly_payload["pending_check_attempt_id"] == pending.pending_check_attempt_id

        attempts = rows(connection, "check_attempts")
        assert attempts[-2]["fetch_status"] == FetchStatus.TIMEOUT.value
        assert attempts[-2]["detected_price_minor"] is None
        assert attempts[-1]["extraction_status"] == ExtractionStatus.AMBIGUOUS.value
        assert attempts[-1]["detected_price_minor"] is None
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_outbox_mock_telegram_retry_content_once_and_no_loss(tmp_path):
    path = tmp_path / "delivery.sqlite"
    connection, checker, user, offer = bootstrap(path)
    try:
        checker.result = checked("80.00")
        result = MonitoringService(connection, checker).check_for_user(user.id, offer.id)
        assert [event.type for event in result.events] == [EventType.PRICE_DROP]
        detected_at = datetime.fromisoformat(result.events[0].timestamp)
    finally:
        connection.close()

    clock = Clock(detected_at + timedelta(seconds=1))
    bot = MockTelegramBot(fail_once=True)
    worker = OutboxWorker(path, TelegramSender(bot), clock=clock)

    first = await worker.run_once()
    assert first == OutboxRunResult(rescheduled=1)
    connection = connect(path)
    try:
        pending = rows(connection, "notification_outbox")[0]
        assert (pending["status"], pending["attempt_count"]) == ("PENDING", 1)
        assert datetime.fromisoformat(pending["next_attempt_at"]) == clock.value.replace(
            microsecond=0
        ) + timedelta(seconds=60)
    finally:
        connection.close()

    clock.value += timedelta(seconds=60)
    second = await worker.run_once()
    third = await worker.run_once()
    assert second == OutboxRunResult(sent=1)
    assert third == OutboxRunResult()
    assert len(bot.calls) == 2  # одна временная ошибка + одна успешная доставка
    delivered = bot.calls[-1]
    assert delivered["chat_id"] == TELEGRAM_USER_ID
    assert all(
        fragment in delivered["text"]
        for fragment in (
            "Снижение цены",
            "Controlled product",
            "Было: 100,00 €",
            "Стало: 80,00 €",
            "Снижение: 20%",
            URL,
        )
    )

    connection = connect(path)
    try:
        sent = rows(connection, "notification_outbox")[0]
        assert (sent["status"], sent["attempt_count"], sent["telegram_message_id"]) == (
            "SENT", 2, 702,
        )
    finally:
        connection.close()
