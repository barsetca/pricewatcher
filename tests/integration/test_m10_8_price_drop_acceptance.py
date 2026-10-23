"""M10.8: контролируемая offline-приёмка полного PRICE_DROP pipeline."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from telegram.error import NetworkError

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.offer_repository import OfferRepository
from pricewatch.domain.enums import ExtractionMethod, ExtractionStatus, FetchMethod, FetchStatus
from pricewatch.scraping.models import ExtractionResult, FetchResult, PriceCheckResult
from pricewatch.scraping.url_security import UrlSecurity
from pricewatch.services.monitoring_service import EventType, MonitoringService
from pricewatch.services.notification_formatter import format_notification
from pricewatch.services.outbox_worker import OutboxRunResult, OutboxWorker
from pricewatch.services.product_service import ProductService
from pricewatch.telegram.sender import TelegramSender


URL = "https://shop.test/m10-8-controlled-price"
TELEGRAM_USER_ID = 880_008


class ControlledChecker:
    def __init__(self, result: PriceCheckResult) -> None:
        self.result = result

    def check(self, url: str) -> PriceCheckResult:
        assert url == URL
        return self.result


class ControlledBot:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.calls: list[dict] = []

    async def send_message(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail_once:
            self.fail_once = False
            raise NetworkError("controlled Telegram outage")
        return SimpleNamespace(message_id=8000 + len(self.calls))


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def checked(price: str) -> PriceCheckResult:
    fetch = FetchResult(
        URL,
        FetchMethod.REQUESTS,
        FetchStatus.SUCCESS,
        final_url=URL,
        http_status=200,
        elapsed_time_ms=1,
    )
    extraction = ExtractionResult(
        ExtractionStatus.SUCCESS,
        title="ТЕСТ M10.8 — контролируемая цена",
        price=Decimal(price),
        currency="EUR",
        method=ExtractionMethod.JSON_LD,
        confidence_score=0.95,
    )
    return PriceCheckResult(fetch, extraction, (fetch,))


def failed(kind: str) -> PriceCheckResult:
    if kind == "http_403":
        fetch = FetchResult(
            URL,
            FetchMethod.REQUESTS,
            FetchStatus.BLOCKED,
            final_url=URL,
            http_status=403,
            elapsed_time_ms=1,
            error_message="controlled HTTP 403",
        )
        extraction = ExtractionResult(ExtractionStatus.NOT_RUN)
    else:
        fetch = FetchResult(
            URL,
            FetchMethod.REQUESTS,
            FetchStatus.SUCCESS,
            final_url=URL,
            http_status=200,
            elapsed_time_ms=1,
        )
        extraction = ExtractionResult(
            ExtractionStatus.PARTIAL,
            title="ТЕСТ M10.8 — контролируемая цена",
            method=ExtractionMethod.JSON_LD,
            diagnostics={"reason": "controlled extraction failure"},
        )
    return PriceCheckResult(fetch, extraction, (fetch,))


def bootstrap(path, initial: PriceCheckResult | None = None):
    initialize_database(path)
    connection = connect(path)
    checker = ControlledChecker(initial or checked("100.00"))
    security = UrlSecurity(
        lambda host, port: [(None, None, None, None, ("93.184.216.34", 0))]
    )
    products = ProductService(connection, checker, security)
    user = products.get_or_create_user(TELEGRAM_USER_ID, "m10_8")
    created = products.create_product_with_offer(
        user.id,
        "ТЕСТ M10.8 — контролируемое снижение цены",
        URL,
    )
    assert created.code == "CREATED" and created.offer is not None
    return connection, checker, user, created.offer


def table_rows(connection, table: str):
    return connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()


@pytest.mark.parametrize(
    ("new_price", "expected_drop"),
    [("100.00", False), ("95.00", False), ("90.00", False), ("89.00", True)],
    ids=["unchanged", "below-threshold", "exact-threshold", "above-threshold"],
)
def test_price_drop_thresholds_persistence_payload_and_content(
    tmp_path, new_price, expected_drop
):
    path = tmp_path / f"threshold-{new_price}.sqlite"
    connection, checker, user, offer = bootstrap(path)
    try:
        assert len(table_rows(connection, "price_history")) == 1
        assert table_rows(connection, "notification_outbox") == []

        checker.result = checked(new_price)
        result = MonitoringService(connection, checker).check_for_user(user.id, offer.id)
        state = OfferRepository(connection).get_for_user(offer.id, user.id)
        outbox = table_rows(connection, "notification_outbox")

        assert len(table_rows(connection, "price_history")) == 2
        assert len(table_rows(connection, "check_attempts")) == 2
        assert state.current_price_minor == int(Decimal(new_price) * 100)
        assert bool(result.events) is expected_drop
        assert len(outbox) == int(expected_drop)

        if expected_drop:
            assert result.events[0].type is EventType.PRICE_DROP
            payload = json.loads(outbox[0]["payload_json"])
            assert (
                payload["previous_price_minor"],
                payload["new_price_minor"],
                payload["drop_percent"],
                payload["chat_id"],
                payload["offer_id"],
            ) == (10000, 8900, "11", TELEGRAM_USER_ID, offer.id)
            text = format_notification(outbox[0]["payload_json"]).text
            assert all(fragment in text for fragment in (
                "Снижение цены",
                "ТЕСТ M10.8",
                "Было: 100,00 €",
                "Стало: 89,00 €",
                "Снижение: 11%",
                URL,
            ))
    finally:
        connection.close()


def test_first_confirmed_price_is_baseline_without_notification(tmp_path):
    path = tmp_path / "first-baseline.sqlite"
    connection, checker, user, offer = bootstrap(path, failed("extraction"))
    try:
        assert table_rows(connection, "price_history") == []
        checker.result = checked("89.00")

        result = MonitoringService(connection, checker).check_for_user(user.id, offer.id)

        assert result.events == ()
        assert len(table_rows(connection, "price_history")) == 1
        assert table_rows(connection, "notification_outbox") == []
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ["http_403", "extraction"])
def test_failures_create_attempt_but_no_false_price_or_notification(tmp_path, kind):
    path = tmp_path / f"failure-{kind}.sqlite"
    connection, checker, user, offer = bootstrap(path)
    try:
        checker.result = failed(kind)
        result = MonitoringService(connection, checker).check_for_user(user.id, offer.id)
        state = OfferRepository(connection).get_for_user(offer.id, user.id)

        assert result.events == ()
        assert state.current_price_minor == 10000
        assert len(table_rows(connection, "price_history")) == 1
        assert len(table_rows(connection, "check_attempts")) == 2
        assert table_rows(connection, "notification_outbox") == []
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_pending_event_survives_reopen_retries_and_is_delivered_once(tmp_path):
    path = tmp_path / "retry-and-restart.sqlite"
    connection, checker, user, offer = bootstrap(path)
    checker.result = checked("89.00")
    result = MonitoringService(connection, checker).check_for_user(user.id, offer.id)
    detected_at = datetime.fromisoformat(result.events[0].timestamp)
    connection.close()

    reopened = connect(path)
    try:
        pending = table_rows(reopened, "notification_outbox")[0]
        assert (pending["status"], pending["attempt_count"]) == ("PENDING", 0)
    finally:
        reopened.close()

    clock = Clock(detected_at + timedelta(seconds=1))
    bot = ControlledBot(fail_once=True)
    worker = OutboxWorker(path, TelegramSender(bot), clock=clock)
    assert await worker.run_once() == OutboxRunResult(rescheduled=1)

    reopened = connect(path)
    try:
        pending = table_rows(reopened, "notification_outbox")[0]
        assert (pending["status"], pending["attempt_count"]) == ("PENDING", 1)
        assert datetime.fromisoformat(pending["next_attempt_at"]) == (
            clock.value.replace(microsecond=0) + timedelta(seconds=60)
        )
    finally:
        reopened.close()

    clock.value += timedelta(seconds=60)
    assert await worker.run_once() == OutboxRunResult(sent=1)
    assert await worker.run_once() == OutboxRunResult()
    assert len(bot.calls) == 2

    reopened = connect(path)
    try:
        sent = table_rows(reopened, "notification_outbox")[0]
        assert (sent["status"], sent["attempt_count"], sent["telegram_message_id"]) == (
            "SENT", 2, 8002,
        )
        checker.result = checked("89.00")
        repeated = MonitoringService(reopened, checker).check_for_user(user.id, offer.id)
        assert repeated.events == ()
        assert len(table_rows(reopened, "notification_outbox")) == 1
    finally:
        reopened.close()
