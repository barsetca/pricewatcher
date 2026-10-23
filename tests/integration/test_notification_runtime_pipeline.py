"""Сквозные M9.6-сценарии на реальной файловой SQLite без внешней сети."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import pytest
from telegram.error import NetworkError

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.notification_outbox_repository import (
    NotificationOutboxRepository,
)
from pricewatch.database.repositories.offer_repository import OfferRepository
from pricewatch.domain.enums import FetchStatus, NotificationOutboxStatus, OfferStatus
from pricewatch.services.delivery_coordinator import DeliveryCoordinator
from pricewatch.services.outbox_worker import OutboxRunResult, OutboxWorker
from pricewatch.telegram.app import build_application
from pricewatch.telegram.sender import TelegramSender
from tests.telegram.test_handler_scenarios import (
    Harness,
    OfflineBot,
    Runner,
    TOKEN,
    _mark_initialized,
    check_result,
)


async def wait_until(predicate, timeout=2):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


def outbox_rows(path):
    connection = connect(path)
    try:
        return connection.execute(
            "SELECT * FROM notification_outbox ORDER BY id"
        ).fetchall()
    finally:
        connection.close()


def stored_offer(path, offer_id, user_id):
    connection = connect(path)
    try:
        return OfferRepository(connection).get_for_user(offer_id, user_id)
    finally:
        connection.close()


def create_offer(runner, name, suffix):
    runner.checker.result = check_result(price="100.00")
    product = runner.direct(1, lambda products, monitoring, user_id: products.create_product(user_id, name).product)
    return runner.direct(
        1,
        lambda products, monitoring, user_id: products.add_offer(
            user_id, product.id, f"https://shop.test/{suffix}"
        ).offer,
    )


async def runtime_harness(path):
    runner = Runner(path)
    bot = OfflineBot()
    worker = OutboxWorker(path, TelegramSender(bot))
    coordinator = DeliveryCoordinator(worker, interval_seconds=60)

    async def wakeup():
        await coordinator.wakeup()

    app = build_application(
        TOKEN,
        frozenset({1, 2}),
        str(path),
        service_runner=runner,
        bot=bot,
        offload_services=False,
        notification_wakeup=wakeup,
    )
    _mark_initialized(app)
    await coordinator.start()
    await asyncio.sleep(0.03)  # startup delivery на пустой очереди
    return runner, bot, Harness(app, bot, runner), coordinator


@pytest.mark.asyncio
async def test_manual_action_to_sqlite_outbox_coordinator_and_telegram_sender(tmp_path):
    path = tmp_path / "pipeline.sqlite"
    initialize_database(path)
    runner, bot, ui, coordinator = await runtime_harness(path)
    try:
        unchanged = create_offer(runner, "Без изменения", "unchanged")
        await ui.callback(f"check:{unchanged.id}")
        assert outbox_rows(path) == []

        below_threshold = create_offer(runner, "Ниже порога", "below-threshold")
        runner.checker.result = check_result(price="95.00")
        await ui.callback(f"check:{below_threshold.id}")
        assert outbox_rows(path) == []

        exact_threshold = create_offer(runner, "Ровно порог", "threshold")
        runner.checker.result = check_result(price="90.00")
        await ui.callback(f"check:{exact_threshold.id}")
        assert outbox_rows(path) == []

        first_drop = create_offer(runner, "Снижение", "drop-one")
        runner.checker.result = check_result(price="89.00")
        await ui.callback(f"check:{first_drop.id}")
        await wait_until(lambda: len(outbox_rows(path)) == 1 and outbox_rows(path)[0]["status"] == "SENT")

        second_drop = create_offer(runner, "Второе снижение", "drop-two")
        runner.checker.result = check_result(price="80.00")
        await ui.callback(f"check:{second_drop.id}")
        await wait_until(lambda: len(outbox_rows(path)) == 2 and all(row["status"] == "SENT" for row in outbox_rows(path)))

        delivered = [message for message in bot.messages if "Снижение цены" in message["text"]]
        assert len(delivered) == 2
        assert all(row["telegram_message_id"] is not None for row in outbox_rows(path))

        # Уже подтверждённые SENT события не отправляются повторно.
        await coordinator.wakeup()
        await asyncio.sleep(0.05)
        assert len([message for message in bot.messages if "Снижение цены" in message["text"]]) == 2
    finally:
        coordinator.request_stop()
        await coordinator.wait_stopped()


@pytest.mark.asyncio
async def test_anomaly_callbacks_confirm_reject_repeat_forged_and_foreign(tmp_path):
    path = tmp_path / "anomaly-pipeline.sqlite"
    initialize_database(path)
    runner, _, ui, coordinator = await runtime_harness(path)
    try:
        confirmed_offer = create_offer(runner, "Подтверждение", "confirm")
        runner.checker.result = check_result(price="50.00")
        await ui.callback(f"check:{confirmed_offer.id}")
        await wait_until(lambda: len(outbox_rows(path)) == 1 and outbox_rows(path)[0]["status"] == "SENT")
        confirmed_row = outbox_rows(path)[0]
        attempt = stored_offer(path, confirmed_offer.id, 1).pending_check_attempt_id

        await ui.callback(
            f"anomaly:confirm:{confirmed_offer.id}:{attempt}",
            message_id=confirmed_row["telegram_message_id"],
            message_text="⚠️ Цена требует подтверждения.",
        )
        resolved = stored_offer(path, confirmed_offer.id, 1)
        assert resolved.current_price_minor == 5000 and resolved.pending_price_minor is None
        await ui.callback(
            f"anomaly:confirm:{confirmed_offer.id}:{attempt}",
            message_id=confirmed_row["telegram_message_id"],
        )
        assert "уже обработана" in ui.last_text

        rejected_offer = create_offer(runner, "Отклонение", "reject")
        runner.checker.result = check_result(price="40.00")
        await ui.callback(f"check:{rejected_offer.id}")
        await wait_until(lambda: len(outbox_rows(path)) == 2 and outbox_rows(path)[1]["status"] == "SENT")
        rejected_row = outbox_rows(path)[1]
        rejected_attempt = stored_offer(path, rejected_offer.id, 1).pending_check_attempt_id

        await ui.callback(
            f"anomaly:reject:{rejected_offer.id}:{rejected_attempt}", message_id=999999
        )
        assert stored_offer(path, rejected_offer.id, 1).pending_check_attempt_id == rejected_attempt
        await ui.callback(
            f"anomaly:reject:{rejected_offer.id}:{rejected_attempt}",
            user_id=2,
            message_id=rejected_row["telegram_message_id"],
        )
        assert stored_offer(path, rejected_offer.id, 1).pending_check_attempt_id == rejected_attempt

        await ui.callback(
            f"anomaly:reject:{rejected_offer.id}:{rejected_attempt}",
            message_id=rejected_row["telegram_message_id"],
        )
        rejected = stored_offer(path, rejected_offer.id, 1)
        assert rejected.current_price_minor == 10000
        assert rejected.pending_price_minor is None
        assert rejected.status is OfferStatus.ACTIVE
    finally:
        coordinator.request_stop()
        await coordinator.wait_stopped()


@pytest.mark.asyncio
async def test_three_errors_recovery_and_delivery_after_offer_deletion(tmp_path):
    path = tmp_path / "error-delete.sqlite"
    initialize_database(path)
    runner, bot, ui, coordinator = await runtime_harness(path)
    try:
        failing = create_offer(runner, "Ошибки", "errors")
        runner.checker.result = check_result(success=False, fetch_status=FetchStatus.TIMEOUT)
        for _ in range(3):
            await ui.callback(f"check:{failing.id}")
        await wait_until(lambda: len(outbox_rows(path)) == 1 and outbox_rows(path)[0]["status"] == "SENT")
        broken = stored_offer(path, failing.id, 1)
        assert broken.status is OfferStatus.ERROR and broken.consecutive_errors == 3

        runner.checker.result = check_result(price="100.00")
        await ui.callback(f"check:{failing.id}")
        recovered = stored_offer(path, failing.id, 1)
        assert recovered.status is OfferStatus.ACTIVE
        assert (recovered.consecutive_errors, recovered.error_notified) == (0, False)
        assert len([message for message in bot.messages if "Не удается проверить" in message["text"]]) == 1
    finally:
        coordinator.request_stop()
        await coordinator.wait_stopped()

    # Snapshot остаётся доставляемым после удаления Offer.
    deleted = create_offer(runner, "Удалённый", "deleted-before-send")
    runner.checker.result = check_result(price="80.00")
    runner.direct(1, lambda products, monitoring, user_id: monitoring.check_for_user(user_id, deleted.id))
    runner.direct(1, lambda products, monitoring, user_id: products.delete_offer(user_id, deleted.id))
    pending = outbox_rows(path)[1]
    assert pending["offer_id"] is None and pending["status"] == "PENDING"

    result = await OutboxWorker(path, TelegramSender(bot)).run_once()
    assert result == OutboxRunResult(sent=1)
    assert outbox_rows(path)[1]["status"] == "SENT"


class MutableClock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class FlakyBot(OfflineBot):
    def __init__(self):
        super().__init__()
        self._failures = 1

    async def send_message(self, *args, **kwargs):
        if self._failures:
            self._failures -= 1
            raise NetworkError("controlled Telegram outage")
        return await super().send_message(*args, **kwargs)


@pytest.mark.asyncio
async def test_telegram_api_error_is_retried_after_one_minute(tmp_path):
    path = tmp_path / "retry.sqlite"
    initialize_database(path)
    runner = Runner(path)
    offer = create_offer(runner, "Retry", "retry")
    runner.checker.result = check_result(price="80.00")
    runner.direct(1, lambda products, monitoring, user_id: monitoring.check_for_user(user_id, offer.id))

    clock = MutableClock(datetime.now(timezone.utc) + timedelta(seconds=1))
    bot = FlakyBot()
    worker = OutboxWorker(path, TelegramSender(bot), clock=clock)
    first = await worker.run_once()
    row = outbox_rows(path)[0]
    assert first == OutboxRunResult(rescheduled=1)
    assert row["status"] == NotificationOutboxStatus.PENDING.value
    assert row["attempt_count"] == 1
    assert datetime.fromisoformat(row["next_attempt_at"]) == clock.value.replace(microsecond=0) + timedelta(seconds=60)

    clock.value += timedelta(seconds=60)
    second = await worker.run_once()
    row = outbox_rows(path)[0]
    assert second == OutboxRunResult(sent=1)
    assert (row["status"], row["attempt_count"]) == ("SENT", 2)


@pytest.mark.asyncio
async def test_crash_window_after_telegram_acceptance_can_redeliver_after_lease(
    tmp_path, caplog,
):
    path = tmp_path / "acceptance-crash.sqlite"
    initialize_database(path)
    runner = Runner(path)
    offer = create_offer(runner, "At least once", "acceptance-crash")
    runner.checker.result = check_result(price="80.00")
    runner.direct(1, lambda products, monitoring, user_id: monitoring.check_for_user(user_id, offer.id))

    clock = MutableClock(datetime.now(timezone.utc) + timedelta(seconds=1))
    bot = OfflineBot()
    interrupted = OutboxWorker(path, TelegramSender(bot), clock=clock)

    def lose_ack(*_args):
        raise RuntimeError("simulated process loss before SQLite mark_sent")

    interrupted._mark_sent = lose_ack
    caplog.set_level(logging.INFO, logger="pricewatch.services.outbox_worker")
    with pytest.raises(RuntimeError, match="simulated process loss"):
        await interrupted.run_once()
    assert outbox_rows(path)[0]["status"] == "SENDING"
    messages = [record.getMessage() for record in caplog.records]
    uncertainty = next(
        message for message in messages
        if "event=outbox_delivery_ack_uncertain" in message
    )
    assert "outcome=error" in uncertainty
    assert "event_type=PRICE_DROP attempt=1" in uncertainty
    assert "exception_class=RuntimeError" in uncertainty
    assert "simulated process loss" not in "\n".join(messages)
    assert "chat_id" not in uncertainty and "message_id" not in uncertainty

    clock.value += timedelta(seconds=301)
    recovered = await OutboxWorker(path, TelegramSender(bot), clock=clock).run_once()
    row = outbox_rows(path)[0]
    assert recovered == OutboxRunResult(recovered=1, sent=1)
    assert (row["status"], row["attempt_count"]) == ("SENT", 2)
    assert len([message for message in bot.messages if "Снижение цены" in message["text"]]) == 2
