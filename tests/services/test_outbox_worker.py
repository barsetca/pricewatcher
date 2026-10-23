import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.notification_outbox_repository import NotificationOutboxRepository
from pricewatch.database.repositories.offer_repository import OfferRepository
from pricewatch.database.repositories.product_repository import ProductRepository
from pricewatch.database.repositories.user_repository import UserRepository
from pricewatch.domain.enums import NotificationEventType, NotificationOutboxStatus
from pricewatch.domain.models import NotificationOutbox, Offer, Product, User
from pricewatch.services.notification_formatter import format_notification
from pricewatch.services.outbox_worker import (
    LEASE_SECONDS,
    MAX_UNKNOWN_DELIVERY_ATTEMPTS,
    OutboxRunResult,
    OutboxWorker,
)


START = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value=START):
        self.value = value

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class FakeSender:
    def __init__(self, outcomes=(), on_send=None):
        self.outcomes = list(outcomes)
        self.on_send = on_send
        self.calls = []

    async def send(self, chat_id, text, reply_markup=None):
        self.calls.append((chat_id, text, reply_markup))
        if self.on_send:
            result = self.on_send()
            if hasattr(result, "__await__"):
                await result
        outcome = self.outcomes.pop(0) if self.outcomes else len(self.calls)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def timestamp(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def payload(event_type="PRICE_DROP", *, product="Item", source="Shop"):
    return json.dumps({
        "version": 1,
        "event_type": event_type,
        "detected_at": timestamp(START),
        "product_id": 1,
        "offer_id": 1,
        "product_name": product,
        "source": source,
        "url": "https://shop.test/item",
        "chat_id": 100,
        "previous_price_minor": 10000,
        "new_price_minor": 8000,
        "currency": "EUR",
        "drop_percent": "20",
        "reason": "network failed",
        "pending_check_attempt_id": 7,
    })


def enqueue(path, key, *, chat_id=100, created_at=START, next_attempt_at=START,
            event_type=NotificationEventType.PRICE_DROP, offer_id=None, payload_json=None):
    connection = connect(path)
    try:
        queued = NotificationOutboxRepository(connection).enqueue(NotificationOutbox(
            event_key=key,
            event_type=event_type,
            offer_id=offer_id,
            chat_id=chat_id,
            payload_json=payload_json or payload(event_type.value),
            next_attempt_at=timestamp(next_attempt_at),
            created_at=timestamp(created_at),
        ))
        connection.commit()
        return queued
    finally:
        connection.close()


def get_record(path, outbox_id):
    connection = connect(path)
    try:
        return NotificationOutboxRepository(connection).get_by_id(outbox_id)
    finally:
        connection.close()


@pytest.fixture
def database_path(tmp_path):
    path = tmp_path / "worker.sqlite"
    initialize_database(path)
    return path


@pytest.mark.asyncio
async def test_success_marks_sent_saves_message_id_and_second_run_is_empty(
    database_path, caplog,
):
    queued = enqueue(database_path, "success")
    sender = FakeSender([777])
    worker = OutboxWorker(database_path, sender, clock=Clock())
    caplog.set_level(logging.INFO, logger="pricewatch.services.outbox_worker")

    first = await worker.run_once()
    stored = get_record(database_path, queued.id)

    assert first == OutboxRunResult(sent=1)
    assert len(sender.calls) == 1
    assert (stored.status, stored.attempt_count, stored.telegram_message_id) == (
        NotificationOutboxStatus.SENT, 1, 777,
    )
    messages = [record.getMessage() for record in caplog.records]
    delivered = next(
        message for message in messages
        if "event=outbox_delivery_completed" in message
    )
    cycle = next(
        message for message in messages
        if "event=outbox_cycle_completed" in message
    )
    assert f"outbox_id={queued.id}" in delivered
    assert "event_type=PRICE_DROP attempt=1" in delivered
    assert "outcome=sent" in delivered and "duration_ms=" in delivered
    assert "sent=1 retried=0 failed=0 recovered=0 skipped=0" in cycle
    joined = "\n".join(messages)
    assert "https://" not in joined
    assert "product_name" not in joined and "payload" not in joined
    assert "chat_id" not in joined and "message_id" not in joined

    caplog.clear()
    second = await worker.run_once()
    assert second == OutboxRunResult()
    assert not any(
        "event=outbox_" in record.getMessage() for record in caplog.records
    )


@pytest.mark.asyncio
async def test_retry_backoff_uses_attempt_count_and_unknown_error_is_not_lost(
    database_path, caplog,
):
    queued = enqueue(database_path, "retry-sequence")
    errors = [
        TimedOut("timeout"),
        NetworkError("network"),
        RuntimeError("unknown"),
        NetworkError("fourth"),
        NetworkError("later"),
    ]
    sender = FakeSender(errors)
    clock = Clock()
    worker = OutboxWorker(database_path, sender, clock=clock)
    caplog.set_level(logging.INFO, logger="pricewatch.services.outbox_worker")

    for attempt, delay in enumerate((60, 300, 900, 3600, 3600), start=1):
        result = await worker.run_once()
        stored = get_record(database_path, queued.id)
        assert result == OutboxRunResult(rescheduled=1)
        assert stored.status is NotificationOutboxStatus.PENDING
        assert stored.attempt_count == attempt
        assert stored.next_attempt_at == timestamp(clock.value + timedelta(seconds=delay))
        assert stored.last_error
        clock.advance(delay)
    retry_logs = [
        record.getMessage() for record in caplog.records
        if "event=outbox_delivery_retry" in record.getMessage()
    ]
    assert len(retry_logs) == 5
    for attempt, delay, message in zip(
        range(1, 6), (60, 300, 900, 3600, 3600), retry_logs,
    ):
        assert f"attempt={attempt}" in message
        assert f"retry_delay_ms={delay * 1000}" in message
        assert "outcome=rescheduled" in message
    joined = "\n".join(retry_logs)
    assert "timeout" not in joined and "network" not in joined
    assert "unknown" not in joined and "later" not in joined


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(lambda: RetryAfter(1), id="retry-after"),
        pytest.param(lambda: TimedOut("timeout secret"), id="timed-out"),
        pytest.param(lambda: NetworkError("network secret"), id="network-error"),
    ],
)
@pytest.mark.filterwarnings("ignore:Deprecated since version v22.2")
async def test_known_transient_errors_remain_retryable_after_five_attempts(
    database_path, error_factory,
):
    queued = enqueue(database_path, "unlimited-transient")
    clock = Clock()
    sender = FakeSender([error_factory() for _ in range(6)])
    worker = OutboxWorker(database_path, sender, clock=clock)

    for expected_attempt, delay in enumerate(
        (60, 300, 900, 3600, 3600, 3600), start=1
    ):
        assert await worker.run_once() == OutboxRunResult(rescheduled=1)
        stored = get_record(database_path, queued.id)
        assert stored.status is NotificationOutboxStatus.PENDING
        assert stored.attempt_count == expected_attempt
        clock.advance(delay)


@pytest.mark.asyncio
async def test_invalid_payload_is_terminal_without_send_and_does_not_block_next(
    database_path, caplog,
):
    poison = enqueue(
        database_path,
        "invalid-payload",
        chat_id=101,
        payload_json='{"version":2,"token":"raw-payload-secret"}',
    )
    valid = enqueue(database_path, "valid-after-poison", chat_id=102)
    sender = FakeSender([222])
    caplog.set_level(logging.INFO, logger="pricewatch.services.outbox_worker")

    result = await OutboxWorker(database_path, sender, clock=Clock()).run_once()

    assert result == OutboxRunResult(sent=1, failed=1)
    assert get_record(database_path, poison.id).status is NotificationOutboxStatus.FAILED
    assert get_record(database_path, valid.id).status is NotificationOutboxStatus.SENT
    assert [call[0] for call in sender.calls] == [102]
    failed = next(
        record.getMessage()
        for record in caplog.records
        if "event=outbox_delivery_failed" in record.getMessage()
    )
    assert "outcome=terminal reason=invalid_payload" in failed
    assert "exception_class=InvalidNotificationPayload" in failed
    assert "raw-payload-secret" not in failed
    assert "payload" not in failed.replace("invalid_payload", "")
    assert "chat_id" not in failed


@pytest.mark.asyncio
async def test_unknown_error_retries_four_times_then_fails_and_does_not_block_next(
    database_path, caplog,
):
    unknown = enqueue(database_path, "unknown-limit", chat_id=101)
    later = enqueue(database_path, "after-unknown", chat_id=102)
    errors = [
        RuntimeError(f"unknown secret attempt {attempt}")
        for attempt in range(1, MAX_UNKNOWN_DELIVERY_ATTEMPTS + 1)
    ]
    sender = FakeSender([errors[0], 222, *errors[1:]])
    clock = Clock()
    worker = OutboxWorker(database_path, sender, clock=clock)
    caplog.set_level(logging.INFO, logger="pricewatch.services.outbox_worker")

    first = await worker.run_once()
    assert first == OutboxRunResult(sent=1, rescheduled=1)
    assert get_record(database_path, later.id).status is NotificationOutboxStatus.SENT
    for expected_attempt in range(2, 5):
        delay = 60 if expected_attempt == 2 else (300 if expected_attempt == 3 else 900)
        clock.advance(delay)
        assert await worker.run_once() == OutboxRunResult(rescheduled=1)
        assert get_record(database_path, unknown.id).attempt_count == expected_attempt
    clock.advance(3600)
    terminal = await worker.run_once()
    stored = get_record(database_path, unknown.id)

    assert terminal == OutboxRunResult(failed=1)
    assert (stored.status, stored.attempt_count) == (
        NotificationOutboxStatus.FAILED,
        MAX_UNKNOWN_DELIVERY_ATTEMPTS,
    )
    messages = [record.getMessage() for record in caplog.records]
    retries = [message for message in messages if "outbox_delivery_retry" in message]
    failed = next(
        message for message in messages if "reason=unknown_attempt_limit" in message
    )
    assert len(retries) == 4
    assert "attempt=5" in failed and "exception_class=RuntimeError" in failed
    assert "unknown secret" not in "\n".join(messages)
    assert not any("outbox_delivery_ack_uncertain" in message for message in messages)


@pytest.mark.asyncio
async def test_unexpected_formatter_error_is_unknown_not_invalid_payload(database_path):
    queued = enqueue(database_path, "formatter-infrastructure")
    sender = FakeSender()

    def unavailable_formatter(_payload_json):
        raise RuntimeError("temporary formatter dependency")

    result = await OutboxWorker(
        database_path,
        sender,
        clock=Clock(),
        formatter=unavailable_formatter,
    ).run_once()

    assert result == OutboxRunResult(rescheduled=1)
    stored = get_record(database_path, queued.id)
    assert (stored.status, stored.attempt_count) == (
        NotificationOutboxStatus.PENDING,
        1,
    )
    assert sender.calls == []


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore:Deprecated since version v22.2")
async def test_retry_after_uses_larger_of_telegram_and_regular_delay(database_path):
    longer = enqueue(database_path, "retry-after-long", chat_id=101)
    shorter = enqueue(database_path, "retry-after-short", chat_id=102)
    sender = FakeSender([RetryAfter(600), RetryAfter(30)])
    clock = Clock()

    result = await OutboxWorker(database_path, sender, clock=clock).run_once()

    assert result == OutboxRunResult(rescheduled=2)
    assert get_record(database_path, longer.id).next_attempt_at == timestamp(START + timedelta(seconds=600))
    assert get_record(database_path, shorter.id).next_attempt_at == timestamp(START + timedelta(seconds=60))


@pytest.mark.asyncio
async def test_forbidden_and_bad_request_are_permanent(database_path, caplog):
    forbidden = enqueue(database_path, "forbidden", chat_id=101)
    bad_request = enqueue(database_path, "bad-request", chat_id=102)
    sender = FakeSender([Forbidden("blocked"), BadRequest("bad chat")])

    caplog.set_level(logging.INFO, logger="pricewatch.services.outbox_worker")
    result = await OutboxWorker(database_path, sender, clock=Clock()).run_once()
    repeated = await OutboxWorker(database_path, sender, clock=Clock()).run_once()

    assert result == OutboxRunResult(failed=2)
    assert repeated == OutboxRunResult()
    assert len(sender.calls) == 2
    assert get_record(database_path, forbidden.id).status is NotificationOutboxStatus.FAILED
    assert get_record(database_path, bad_request.id).status is NotificationOutboxStatus.FAILED
    failed_logs = [
        record.getMessage() for record in caplog.records
        if "event=outbox_delivery_failed" in record.getMessage()
    ]
    assert len(failed_logs) == 2
    assert all("outcome=permanent" in message for message in failed_logs)
    assert "exception_class=Forbidden" in failed_logs[0]
    assert "exception_class=BadRequest" in failed_logs[1]
    assert all("reason=telegram_permanent" in message for message in failed_logs)
    assert not any("blocked" in message or "bad chat" in message for message in failed_logs)


@pytest.mark.asyncio
async def test_one_delivery_error_does_not_block_later_ready_event(database_path):
    first = enqueue(database_path, "first", chat_id=101)
    second = enqueue(database_path, "second", chat_id=102)
    sender = FakeSender([TimedOut("timeout"), 222])

    result = await OutboxWorker(database_path, sender, clock=Clock()).run_once()

    assert result == OutboxRunResult(sent=1, rescheduled=1)
    assert get_record(database_path, first.id).status is NotificationOutboxStatus.PENDING
    assert get_record(database_path, second.id).status is NotificationOutboxStatus.SENT
    assert [call[0] for call in sender.calls] == [101, 102]


def test_recover_expired_preserves_attempt_and_ignores_active_lease(
    database_path, caplog,
):
    expired = enqueue(database_path, "expired", chat_id=101)
    active = enqueue(database_path, "active", chat_id=102)
    connection = connect(database_path)
    try:
        repository = NotificationOutboxRepository(connection)
        repository.claim(expired.id, timestamp(START), timestamp(START - timedelta(seconds=1)))
        repository.claim(active.id, timestamp(START), timestamp(START + timedelta(seconds=1)))
        connection.commit()
    finally:
        connection.close()

    caplog.set_level(logging.INFO, logger="pricewatch.services.outbox_worker")
    recovered = OutboxWorker(database_path, FakeSender(), clock=Clock()).recover_expired()

    expired_record = get_record(database_path, expired.id)
    active_record = get_record(database_path, active.id)
    assert recovered == 1
    assert (expired_record.status, expired_record.attempt_count, expired_record.next_attempt_at) == (
        NotificationOutboxStatus.PENDING, 1, timestamp(START),
    )
    assert active_record.status is NotificationOutboxStatus.SENDING
    assert any(
        record.getMessage()
        == "event=outbox_lease_recovered outcome=success recovered=1"
        for record in caplog.records
    )


@pytest.mark.asyncio
async def test_run_once_reports_recovery_and_immediately_delivers_recovered_event(database_path):
    queued = enqueue(database_path, "recover-and-send")
    connection = connect(database_path)
    try:
        assert NotificationOutboxRepository(connection).claim(
            queued.id, timestamp(START), timestamp(START - timedelta(seconds=1))
        ) is not None
        connection.commit()
    finally:
        connection.close()

    result = await OutboxWorker(database_path, FakeSender([88]), clock=Clock()).run_once()
    stored = get_record(database_path, queued.id)

    assert result == OutboxRunResult(recovered=1, sent=1)
    assert (stored.status, stored.attempt_count, stored.telegram_message_id) == (
        NotificationOutboxStatus.SENT, 2, 88,
    )


@pytest.mark.asyncio
async def test_lost_lease_cannot_ack_new_claim(database_path, caplog):
    queued = enqueue(database_path, "stale-lease")
    clock = Clock()

    def replace_lease():
        clock.advance(LEASE_SECONDS + 1)
        connection = connect(database_path)
        try:
            repository = NotificationOutboxRepository(connection)
            now = timestamp(clock.value)
            assert repository.recover_expired(now) == 1
            assert repository.claim(
                queued.id, now, timestamp(clock.value + timedelta(seconds=LEASE_SECONDS))
            ) is not None
            connection.commit()
        finally:
            connection.close()

    sender = FakeSender([500], on_send=replace_lease)
    caplog.set_level(logging.INFO, logger="pricewatch.services.outbox_worker")
    result = await OutboxWorker(database_path, sender, clock=clock).run_once()
    stored = get_record(database_path, queued.id)

    assert result == OutboxRunResult(skipped=1)
    assert (stored.status, stored.attempt_count, stored.telegram_message_id) == (
        NotificationOutboxStatus.SENDING, 2, None,
    )
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "event=outbox_lease_lost outcome=skipped" in message
        and f"outbox_id={queued.id}" in message
        and "event_type=PRICE_DROP attempt=1" in message
        for message in messages
    )
    assert not any("event=outbox_delivery_completed" in message for message in messages)
    assert any("event=outbox_delivery_ack_uncertain" in message for message in messages)


@pytest.mark.asyncio
async def test_lost_lease_during_terminal_failure_does_not_mark_failed(
    database_path, caplog,
):
    queued = enqueue(database_path, "terminal-stale-lease")
    clock = Clock()

    def replace_lease():
        clock.advance(LEASE_SECONDS + 1)
        connection = connect(database_path)
        try:
            repository = NotificationOutboxRepository(connection)
            now = timestamp(clock.value)
            assert repository.recover_expired(now) == 1
            assert repository.claim(
                queued.id,
                now,
                timestamp(clock.value + timedelta(seconds=LEASE_SECONDS)),
            ) is not None
            connection.commit()
        finally:
            connection.close()

    sender = FakeSender([Forbidden("telegram forbidden secret")], on_send=replace_lease)
    caplog.set_level(logging.INFO, logger="pricewatch.services.outbox_worker")

    result = await OutboxWorker(database_path, sender, clock=clock).run_once()
    stored = get_record(database_path, queued.id)

    assert result == OutboxRunResult(skipped=1)
    assert (stored.status, stored.attempt_count) == (
        NotificationOutboxStatus.SENDING,
        2,
    )
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "event=outbox_lease_lost outcome=skipped" in messages
    assert "event=outbox_delivery_failed" not in messages
    assert "telegram forbidden secret" not in messages


@pytest.mark.asyncio
async def test_mark_failed_sqlite_error_escalates(database_path, monkeypatch):
    queued = enqueue(database_path, "mark-failed-error")
    sender = FakeSender([Forbidden("permanent")])

    def fail_mark(*_args, **_kwargs):
        raise sqlite3.OperationalError("mark_failed database secret")

    with monkeypatch.context() as patch:
        patch.setattr(NotificationOutboxRepository, "mark_failed", fail_mark)
        with pytest.raises(sqlite3.OperationalError, match="mark_failed"):
            await OutboxWorker(database_path, sender, clock=Clock()).run_once()

    assert get_record(database_path, queued.id).status is NotificationOutboxStatus.SENDING


@pytest.mark.asyncio
async def test_mark_sent_sqlite_error_escalates_with_safe_uncertainty_event(
    database_path, monkeypatch, caplog,
):
    queued = enqueue(database_path, "mark-sent-error", chat_id=987654321)
    sender = FakeSender([777])

    def fail_mark(*_args, **_kwargs):
        raise sqlite3.OperationalError("mark_sent database secret token=private")

    caplog.set_level(logging.INFO, logger="pricewatch.services.outbox_worker")
    with monkeypatch.context() as patch:
        patch.setattr(NotificationOutboxRepository, "mark_sent", fail_mark)
        with pytest.raises(sqlite3.OperationalError, match="mark_sent"):
            await OutboxWorker(database_path, sender, clock=Clock()).run_once()

    assert get_record(database_path, queued.id).status is NotificationOutboxStatus.SENDING
    messages = [record.getMessage() for record in caplog.records]
    uncertain = next(
        message for message in messages
        if "event=outbox_delivery_ack_uncertain" in message
    )
    assert uncertain == (
        "event=outbox_delivery_ack_uncertain outcome=error "
        f"outbox_id={queued.id} event_type=PRICE_DROP attempt=1 "
        "exception_class=OperationalError"
    )
    joined = "\n".join(messages)
    for forbidden in (
        "mark_sent database secret", "token=private", "987654321",
        "chat_id", "message_id", "https://", "product_name",
    ):
        assert forbidden not in joined


@pytest.mark.asyncio
async def test_cycle_limit_order_and_follow_up_cycle(database_path):
    for index in range(22):
        enqueue(
            database_path, f"event-{index}", chat_id=1000 + index,
            created_at=START + timedelta(seconds=index),
        )
    sender = FakeSender(range(1, 23))
    worker = OutboxWorker(database_path, sender, clock=Clock(START + timedelta(minutes=1)))

    first = await worker.run_once()
    assert first == OutboxRunResult(sent=20)
    assert [call[0] for call in sender.calls] == list(range(1000, 1020))

    second = await worker.run_once()
    assert second == OutboxRunResult(sent=2)
    assert [call[0] for call in sender.calls] == list(range(1000, 1022))


@pytest.mark.asyncio
async def test_delayed_older_event_does_not_block_ready_chronological_order(database_path):
    enqueue(
        database_path, "delayed", chat_id=100,
        created_at=START - timedelta(minutes=2), next_attempt_at=START + timedelta(minutes=5),
    )
    enqueue(database_path, "ready-first", chat_id=101, created_at=START - timedelta(minutes=1))
    enqueue(database_path, "ready-second", chat_id=102, created_at=START)
    sender = FakeSender([1, 2])

    result = await OutboxWorker(database_path, sender, clock=Clock()).run_once()

    assert result == OutboxRunResult(sent=2)
    assert [call[0] for call in sender.calls] == [101, 102]


@pytest.mark.asyncio
async def test_snapshot_is_delivered_after_offer_deletion(database_path):
    connection = connect(database_path)
    try:
        user = UserRepository(connection).create(User(100, timestamp(START)))
        product = ProductRepository(connection).create(Product(user.id, "Deleted item", timestamp(START)))
        offer = OfferRepository(connection).create(Offer(
            product.id, "https://shop.test/deleted", "https://shop.test/deleted", timestamp(START)
        ))
        connection.commit()
    finally:
        connection.close()
    queued = enqueue(
        database_path, "deleted-offer", offer_id=offer.id,
        payload_json=payload(product="Deleted item", source="Deleted shop"),
    )
    connection = connect(database_path)
    try:
        assert OfferRepository(connection).delete(offer.id, user.id)
        connection.commit()
    finally:
        connection.close()
    sender = FakeSender([42])

    result = await OutboxWorker(database_path, sender, clock=Clock()).run_once()

    assert result == OutboxRunResult(sent=1)
    assert get_record(database_path, queued.id).offer_id is None
    assert "Deleted item" in sender.calls[0][1]


@pytest.mark.asyncio
async def test_network_call_holds_no_sqlite_write_transaction(database_path):
    enqueue(database_path, "no-lock")

    def acquire_write_lock():
        connection = sqlite3.connect(database_path, timeout=0)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
        finally:
            connection.close()

    result = await OutboxWorker(
        database_path, FakeSender([10], on_send=acquire_write_lock), clock=Clock()
    ).run_once()

    assert result == OutboxRunResult(sent=1)


@pytest.mark.asyncio
async def test_two_workers_do_not_send_same_actively_leased_event(database_path):
    import asyncio

    enqueue(database_path, "two-workers")
    started = asyncio.Event()
    release = asyncio.Event()

    async def block_first_sender():
        started.set()
        await release.wait()

    first_sender = FakeSender([11], on_send=block_first_sender)
    second_sender = FakeSender([22])
    first_worker = OutboxWorker(database_path, first_sender, clock=Clock())
    second_worker = OutboxWorker(database_path, second_sender, clock=Clock())

    first_task = asyncio.create_task(first_worker.run_once())
    await started.wait()
    second_result = await second_worker.run_once()
    release.set()
    first_result = await first_task

    assert first_result == OutboxRunResult(sent=1)
    assert second_result == OutboxRunResult()
    assert len(first_sender.calls) == 1
    assert second_sender.calls == []


def test_formatter_supports_all_three_event_types():
    price = format_notification(payload("PRICE_DROP")).text
    anomaly = format_notification(payload("ANOMALY_DETECTED"))
    error = format_notification(payload("REPEATED_ERROR")).text

    assert all(value in price for value in ("Снижение цены", "Item", "Shop", "100,00 €", "80,00 €", "20%"))
    assert all(value in anomaly.text for value in ("необычное изменение", "100,00 €", "80,00 €", "Цена требует"))
    assert anomaly.reply_markup.inline_keyboard[0][0].callback_data == "anomaly:confirm:1:7"
    assert all(value in error for value in ("Не удается проверить", "network failed"))
