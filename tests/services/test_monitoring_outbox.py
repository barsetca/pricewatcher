import json
import logging
import sqlite3
from decimal import Decimal

import pytest

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.offer_repository import OfferRepository
from pricewatch.database.repositories.product_repository import ProductRepository
from pricewatch.database.repositories.user_repository import UserRepository
from pricewatch.domain.enums import ExtractionMethod, ExtractionStatus, FetchMethod, FetchStatus, NotificationOutboxStatus, OfferStatus
from pricewatch.domain.models import Offer, Product, User
from pricewatch.scraping.models import ExtractionResult, FetchResult, PriceCheckResult
from pricewatch.services.monitoring_service import EventType, MonitoringService


TIME = "2026-10-20T00:00:00+00:00"
TELEGRAM_USER_ID = 900_001


class Checker:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def check(self, url):
        self.calls.append(url)
        return self.result


def checked(price=None, status=FetchStatus.SUCCESS):
    fetch = FetchResult(
        "https://shop.test/item", FetchMethod.REQUESTS, status,
        http_status=200, elapsed_time_ms=3,
        error_message="failed" if status is not FetchStatus.SUCCESS else None,
    )
    extraction = ExtractionResult(
        ExtractionStatus.SUCCESS if price is not None else ExtractionStatus.PARTIAL,
        price=Decimal(str(price)) if price is not None else None,
        currency="EUR" if price is not None else None,
        method=ExtractionMethod.JSON_LD,
    )
    return PriceCheckResult(fetch, extraction, (fetch,))


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "monitoring-outbox.sqlite"
    initialize_database(path)
    db = connect(path)
    user = UserRepository(db).create(User(TELEGRAM_USER_ID, TIME))
    product = ProductRepository(db).create(Product(user.id, "Тестовый товар", TIME))
    offer = OfferRepository(db).create(Offer(
        product.id, "https://shop.test/item", "https://shop.test/item", TIME,
        store_name="Test Shop", status=OfferStatus.PENDING,
    ))
    db.commit()
    checker = Checker(checked(100))
    yield db, user, product, offer, checker
    db.close()


def outbox_rows(db):
    return db.execute("SELECT * FROM notification_outbox ORDER BY id").fetchall()


def fail_enqueue(_event):
    raise sqlite3.IntegrityError("forced outbox failure")


def test_price_drop_outbox_threshold_baseline_payload_recipient_and_offer_deletion(
    setup, caplog,
):
    db, user, product, offer, checker = setup
    service = MonitoringService(db, checker)
    caplog.set_level(logging.INFO, logger="pricewatch.services.monitoring_service")

    assert service.check_for_user(user.id, offer.id).events == ()
    assert outbox_rows(db) == []

    checker.result = checked(90)
    assert service.check_for_user(user.id, offer.id).events == ()
    assert outbox_rows(db) == []

    checker.result = checked(80)
    result = service.check_for_user(user.id, offer.id)
    rows = outbox_rows(db)
    latest_history_id = db.execute(
        "SELECT id FROM price_history WHERE offer_id = ? ORDER BY id DESC LIMIT 1", (offer.id,)
    ).fetchone()[0]

    assert len(rows) == 1
    row = rows[0]
    assert result.events[0].type is EventType.PRICE_DROP
    assert row["event_key"] == f"PRICE_DROP:price_history:{latest_history_id}"
    assert row["chat_id"] == TELEGRAM_USER_ID != user.id
    assert row["status"] == NotificationOutboxStatus.PENDING.value
    payload = json.loads(row["payload_json"])
    assert payload == {
        "version": 1,
        "event_type": "PRICE_DROP",
        "detected_at": result.events[0].timestamp,
        "product_id": product.id,
        "offer_id": offer.id,
        "product_name": "Тестовый товар",
        "source": "Test Shop",
        "url": "https://shop.test/item",
        "chat_id": TELEGRAM_USER_ID,
        "previous_price_minor": 9000,
        "new_price_minor": 8000,
        "currency": "EUR",
        "drop_percent": str(result.events[0].drop_percent),
        "reason": None,
        "pending_check_attempt_id": None,
    }
    assert isinstance(payload["drop_percent"], str)
    assert Decimal(payload["drop_percent"]) == result.events[0].drop_percent
    created = [
        record.getMessage() for record in caplog.records
        if "event=event_created" in record.getMessage()
    ]
    assert len(created) == 1
    assert "outcome=committed" in created[0]
    assert "event_type=PRICE_DROP" in created[0]
    assert f"product_id={product.id} offer_id={offer.id}" in created[0]
    assert "https://" not in created[0]
    assert "Тестовый товар" not in created[0]
    assert str(TELEGRAM_USER_ID) not in created[0]

    assert OfferRepository(db).delete(offer.id, user.id)
    db.commit()
    retained = db.execute("SELECT offer_id, payload_json FROM notification_outbox").fetchone()
    assert retained["offer_id"] is None
    assert json.loads(retained["payload_json"]) == payload


def test_new_anomaly_enqueues_once_with_pending_attempt_and_confirmation_creates_no_drop(
    setup, caplog,
):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    caplog.set_level(logging.INFO, logger="pricewatch.services.monitoring_service")
    service.check_for_user(user.id, offer.id)

    checker.result = checked(50)
    first = service.check_for_user(user.id, offer.id)
    pending = OfferRepository(db).get_for_user(offer.id, user.id).pending_check_attempt_id
    rows = outbox_rows(db)
    payload = json.loads(rows[0]["payload_json"])

    assert first.events[0].type is EventType.ANOMALY_DETECTED
    assert len(rows) == 1
    assert rows[0]["event_key"] == f"ANOMALY_DETECTED:check_attempt:{pending}"
    assert payload["pending_check_attempt_id"] == pending
    assert payload["previous_price_minor"] == 10000
    assert payload["new_price_minor"] == 5000
    created = [
        record.getMessage() for record in caplog.records
        if "event=event_created" in record.getMessage()
    ]
    assert len(created) == 1
    assert "outcome=committed" in created[0]
    assert "event_type=ANOMALY_DETECTED" in created[0]

    repeated = service.check_for_user(user.id, offer.id)
    assert repeated.events == ()
    assert len(outbox_rows(db)) == 1

    assert service.confirm_anomaly(user.id, offer.id, pending).code == "CONFIRMED"
    assert len(outbox_rows(db)) == 1


def test_repeated_error_episode_enqueues_once_and_can_repeat_after_recovery(
    setup, caplog,
):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    caplog.set_level(logging.INFO, logger="pricewatch.services.monitoring_service")
    service.check_for_user(user.id, offer.id)
    checker.result = checked(None, FetchStatus.TIMEOUT)

    assert service.check_for_user(user.id, offer.id).events == ()
    assert service.check_for_user(user.id, offer.id).events == ()
    third = service.check_for_user(user.id, offer.id)
    fourth = service.check_for_user(user.id, offer.id)

    assert third.events[0].type is EventType.REPEATED_ERROR
    assert fourth.events == ()
    assert len(outbox_rows(db)) == 1
    first_payload = json.loads(outbox_rows(db)[0]["payload_json"])
    assert first_payload["reason"] == "price was not reliably extracted"

    checker.result = checked(100)
    assert service.check_for_user(user.id, offer.id).events == ()
    recovered = OfferRepository(db).get_for_user(offer.id, user.id)
    assert (recovered.consecutive_errors, recovered.error_notified) == (0, False)

    checker.result = checked(None, FetchStatus.TIMEOUT)
    service.check_for_user(user.id, offer.id)
    service.check_for_user(user.id, offer.id)
    new_episode = service.check_for_user(user.id, offer.id)
    rows = outbox_rows(db)
    assert new_episode.events[0].type is EventType.REPEATED_ERROR
    assert len(rows) == 2
    assert rows[0]["event_key"] != rows[1]["event_key"]
    created = [
        record.getMessage() for record in caplog.records
        if "event=event_created" in record.getMessage()
    ]
    assert len(created) == 2
    assert all(
        "outcome=committed" in message and "event_type=REPEATED_ERROR" in message
        for message in created
    )


def test_price_drop_enqueue_failure_rolls_back_history_attempt_and_offer_state(
    setup, caplog,
):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    service.check_for_user(user.id, offer.id)
    before_offer = OfferRepository(db).get_for_user(offer.id, user.id)
    before_history = db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    before_attempts = db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0]
    service._outbox.enqueue = fail_enqueue
    checker.result = checked(80)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="pricewatch.services.monitoring_service")

    with pytest.raises(sqlite3.IntegrityError, match="forced outbox failure"):
        service.check_for_user(user.id, offer.id)

    after_offer = OfferRepository(db).get_for_user(offer.id, user.id)
    assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == before_history
    assert db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == before_attempts
    assert (after_offer.current_price_minor, after_offer.state_version) == (
        before_offer.current_price_minor, before_offer.state_version,
    )
    assert outbox_rows(db) == []
    messages = [record.getMessage() for record in caplog.records]
    assert not any("event=event_created" in message for message in messages)
    assert any(
        "event=offer_check_failed" in message
        and "exception_class=IntegrityError" in message
        for message in messages
    )
    assert not any("forced outbox failure" in message for message in messages)


def test_anomaly_enqueue_failure_rolls_back_attempt_and_pending_state(setup, caplog):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    service.check_for_user(user.id, offer.id)
    before_offer = OfferRepository(db).get_for_user(offer.id, user.id)
    before_attempts = db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0]
    service._outbox.enqueue = fail_enqueue
    checker.result = checked(50)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="pricewatch.services.monitoring_service")

    with pytest.raises(sqlite3.IntegrityError, match="forced outbox failure"):
        service.check_for_user(user.id, offer.id)

    after_offer = OfferRepository(db).get_for_user(offer.id, user.id)
    assert db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == before_attempts
    assert (
        after_offer.status, after_offer.pending_price_minor,
        after_offer.pending_check_attempt_id, after_offer.state_version,
    ) == (
        before_offer.status, None, None, before_offer.state_version,
    )
    assert outbox_rows(db) == []
    assert not any(
        "event=event_created" in record.getMessage() for record in caplog.records
    )


def test_repeated_error_enqueue_failure_rolls_back_attempt_counter_and_flag(
    setup, caplog,
):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    service.check_for_user(user.id, offer.id)
    checker.result = checked(None, FetchStatus.TIMEOUT)
    service.check_for_user(user.id, offer.id)
    service.check_for_user(user.id, offer.id)
    before_offer = OfferRepository(db).get_for_user(offer.id, user.id)
    before_attempts = db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0]
    service._outbox.enqueue = fail_enqueue
    caplog.clear()
    caplog.set_level(logging.INFO, logger="pricewatch.services.monitoring_service")

    with pytest.raises(sqlite3.IntegrityError, match="forced outbox failure"):
        service.check_for_user(user.id, offer.id)

    after_offer = OfferRepository(db).get_for_user(offer.id, user.id)
    assert db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == before_attempts
    assert (
        after_offer.status, after_offer.consecutive_errors,
        after_offer.error_notified, after_offer.state_version,
    ) == (
        before_offer.status, 2, False, before_offer.state_version,
    )
    assert outbox_rows(db) == []
    assert not any(
        "event=event_created" in record.getMessage() for record in caplog.records
    )


def test_manual_and_batch_checks_use_the_same_outbox_rules(setup):
    db, user, _, first_offer, checker = setup
    second_product = ProductRepository(db).create(Product(user.id, "Второй товар", TIME))
    second_offer = OfferRepository(db).create(Offer(
        second_product.id, "https://shop.test/second", "https://shop.test/second", TIME,
        store_name="Second Shop", status=OfferStatus.PENDING,
    ))
    db.commit()
    service = MonitoringService(db, checker)
    service.check_for_user(user.id, first_offer.id)
    service.check_for_user(user.id, second_offer.id)

    checker.result = checked(80)
    manual = service.check_for_user(user.id, first_offer.id)
    batch = service.check_all()

    assert [event.offer_id for event in manual.events] == [first_offer.id]
    assert [event.offer_id for event in batch.events] == [second_offer.id]
    assert {row["offer_id"] for row in outbox_rows(db)} == {first_offer.id, second_offer.id}


def test_second_check_with_same_snapshot_is_stale_without_extra_persistence(setup):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    service.check_for_user(user.id, offer.id)
    snapshot = OfferRepository(db).get_for_user(offer.id, user.id)

    first = service._persist(offer.id, user.id, checked(80), snapshot.state_version)
    counts_after_first = (
        db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0],
        db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0],
        db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0],
    )
    second = service._persist(offer.id, user.id, checked(70), snapshot.state_version)

    assert first.events[0].type is EventType.PRICE_DROP
    assert second.code == "STALE"
    assert (
        db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0],
        db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0],
        db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0],
    ) == counts_after_first == (2, 2, 1)
