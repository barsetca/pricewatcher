import logging
import sqlite3
from decimal import Decimal

import pytest

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.offer_repository import OfferRepository
from pricewatch.database.repositories.product_repository import ProductRepository
from pricewatch.database.repositories.user_repository import UserRepository
from pricewatch.domain.enums import ExtractionMethod, ExtractionStatus, FetchMethod, FetchStatus, OfferStatus
from pricewatch.domain.models import Offer, Product, User
from pricewatch.scraping.models import ExtractionResult, FetchResult, PriceCheckResult
from pricewatch.services.monitoring_service import EventType, MonitoringService

TIME = "2026-10-14T00:00:00Z"


class Checker:
    def __init__(self, item): self.item, self.calls = item, []
    def check(self, url): self.calls.append(url); return self.item


def checked(price=None, status=FetchStatus.SUCCESS):
    fetch = FetchResult("https://shop.test/x", FetchMethod.REQUESTS, status, http_status=200, elapsed_time_ms=3, error_message="failed" if status is not FetchStatus.SUCCESS else None)
    extraction = ExtractionResult(ExtractionStatus.SUCCESS if price is not None else ExtractionStatus.PARTIAL, price=Decimal(str(price)) if price is not None else None, currency="EUR" if price is not None else None, method=ExtractionMethod.JSON_LD)
    return PriceCheckResult(fetch, extraction, (fetch,))


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "db.sqlite"; initialize_database(path); db = connect(path)
    user = UserRepository(db).create(User(1, TIME)); product = ProductRepository(db).create(Product(user.id, "Item", TIME))
    offer = OfferRepository(db).create(Offer(product.id, "https://shop.test/x", "https://shop.test/x", TIME, status=OfferStatus.PENDING))
    db.commit()
    checker = Checker(checked(100))
    yield db, user, product, offer, checker
    db.close()


def test_baseline_drop_history_and_threshold(setup):
    db, user, product, offer, checker = setup; service = MonitoringService(db, checker)
    assert service.check_for_user(user.id, offer.id).events == ()
    checker.item = checked(90); assert service.check_for_user(user.id, offer.id).events == ()
    checker.item = checked(80); result = service.check_for_user(user.id, offer.id)
    assert result.events[0].type is EventType.PRICE_DROP
    assert len(db.execute("SELECT * FROM price_history").fetchall()) == 3


def test_anomaly_confirm_and_no_drop(setup):
    db, user, _, offer, checker = setup; service = MonitoringService(db, checker); service.check_for_user(user.id, offer.id)
    checker.item = checked(50); anomaly = service.check_for_user(user.id, offer.id)
    assert anomaly.events[0].type is EventType.ANOMALY_DETECTED
    assert len(db.execute("SELECT * FROM price_history").fetchall()) == 1
    pending_id = OfferRepository(db).get_for_user(offer.id, user.id).pending_check_attempt_id
    assert service.confirm_anomaly(user.id, offer.id, pending_id).code == "CONFIRMED"
    assert len(db.execute("SELECT * FROM price_history").fetchall()) == 2
    assert service.confirm_anomaly(user.id, offer.id, pending_id).code == "NOT_FOUND"


def test_errors_pending_and_repeated_notification(setup):
    db, user, _, offer, checker = setup; service = MonitoringService(db, checker); service.check_for_user(user.id, offer.id)
    checker.item = checked(None, FetchStatus.TIMEOUT)
    assert not service.check_for_user(user.id, offer.id).events
    assert not service.check_for_user(user.id, offer.id).events
    assert service.check_for_user(user.id, offer.id).code == "ERROR"
    assert service.check_for_user(user.id, offer.id).events == ()


def test_active_offer_stays_active_on_first_and_second_errors_then_becomes_error(setup):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    service.check_for_user(user.id, offer.id)
    history_before = db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    checker.item = checked(None, FetchStatus.TIMEOUT)

    first = service.check_for_user(user.id, offer.id)
    after_first = OfferRepository(db).get_for_user(offer.id, user.id)
    second = service.check_for_user(user.id, offer.id)
    after_second = OfferRepository(db).get_for_user(offer.id, user.id)
    third = service.check_for_user(user.id, offer.id)
    after_third = OfferRepository(db).get_for_user(offer.id, user.id)

    assert (first.code, after_first.status, after_first.consecutive_errors) == (
        "PENDING", OfferStatus.ACTIVE, 1,
    )
    assert (second.code, after_second.status, after_second.consecutive_errors) == (
        "PENDING", OfferStatus.ACTIVE, 2,
    )
    assert (third.code, after_third.status, after_third.consecutive_errors) == (
        "ERROR", OfferStatus.ERROR, 3,
    )
    assert len(third.events) == 1 and third.events[0].type is EventType.REPEATED_ERROR
    assert after_third.current_price_minor == 10000
    assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == history_before
    assert db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 4
    assert db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1


@pytest.mark.parametrize("error_count", [1, 2])
def test_success_after_active_offer_errors_resets_error_episode(setup, error_count):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    service.check_for_user(user.id, offer.id)
    checker.item = checked(None, FetchStatus.TIMEOUT)
    for _ in range(error_count):
        service.check_for_user(user.id, offer.id)

    checker.item = checked(100)
    recovered = service.check_for_user(user.id, offer.id)
    state = OfferRepository(db).get_for_user(offer.id, user.id)

    assert recovered.code == "SUCCESS"
    assert (state.status, state.consecutive_errors, state.error_notified) == (
        OfferStatus.ACTIVE, 0, False,
    )
    assert db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0


def test_initial_pending_offer_remains_pending_through_error_threshold(setup):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    checker.item = checked(None, FetchStatus.BLOCKED)

    results = [service.check_for_user(user.id, offer.id) for _ in range(3)]
    state = OfferRepository(db).get_for_user(offer.id, user.id)

    assert [result.code for result in results] == ["PENDING", "PENDING", "PENDING"]
    assert (state.status, state.consecutive_errors, state.error_notified) == (
        OfferStatus.PENDING, 3, True,
    )
    assert len(results[-1].events) == 1
    assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 1


def test_error_does_not_close_suspicious_offer_or_create_repeated_error(setup):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    service.check_for_user(user.id, offer.id)
    checker.item = checked(50)
    service.check_for_user(user.id, offer.id)
    suspicious = OfferRepository(db).get_for_user(offer.id, user.id)
    outbox_before = db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0]
    checker.item = checked(None, FetchStatus.TIMEOUT)

    result = service.check_for_user(user.id, offer.id)
    state = OfferRepository(db).get_for_user(offer.id, user.id)

    assert result.code == "PENDING" and result.events == ()
    assert (state.status, state.consecutive_errors, state.pending_check_attempt_id) == (
        OfferStatus.SUSPICIOUS, suspicious.consecutive_errors,
        suspicious.pending_check_attempt_id,
    )
    assert db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == outbox_before


def test_disabled_offer_is_not_checked(setup):
    db, user, _, offer, checker = setup
    OfferRepository(db).update_check_state(offer.id, status=OfferStatus.DISABLED)
    db.commit()
    service = MonitoringService(db, checker)

    result = service.check_for_user(user.id, offer.id)

    assert result.code == "SKIPPED"
    assert checker.calls == []
    assert db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 0


def test_ownership_and_batch_skips_inactive(setup):
    db, user, product, offer, checker = setup; service = MonitoringService(db, checker)
    assert service.check_for_user(999, offer.id).code == "NOT_FOUND"
    ProductRepository(db).set_active(product.id, user.id, False)
    assert service.check_all().results == ()


def test_error_offer_retry_uses_normalized_url_and_preserves_source_url(setup):
    db, user, _, offer, checker = setup
    source_url = "https://shop.test/x?utm_source=manual"
    normalized_url = "https://shop.test/x"
    db.execute(
        """
        UPDATE offers
        SET url = ?, normalized_url = ?, status = 'ERROR', consecutive_errors = 3
        WHERE id = ?
        """,
        (source_url, normalized_url, offer.id),
    )
    db.commit()

    result = MonitoringService(db, checker).check_for_user(user.id, offer.id)
    state = OfferRepository(db).get_for_user(offer.id, user.id)

    assert result.code == "SUCCESS"
    assert checker.calls == [normalized_url]
    assert state.url == source_url
    assert state.normalized_url == normalized_url
    assert (state.status, state.consecutive_errors) == (OfferStatus.ACTIVE, 0)


def test_scheduled_batch_uses_normalized_url(setup):
    db, _, _, offer, checker = setup
    source_url = "https://shop.test/x?utm_campaign=daily"
    normalized_url = "https://shop.test/x"
    db.execute(
        "UPDATE offers SET url = ?, normalized_url = ? WHERE id = ?",
        (source_url, normalized_url, offer.id),
    )
    db.commit()

    result = MonitoringService(db, checker).check_all()
    state = OfferRepository(db).get_internal(offer.id)

    assert [item.code for item in result.results] == ["SUCCESS"]
    assert checker.calls == [normalized_url]
    assert (state.url, state.normalized_url) == (source_url, normalized_url)


def test_same_pending_anomaly_preserves_original_decision_target(setup):
    db, user, _, offer, checker = setup; service = MonitoringService(db, checker)
    service.check_for_user(user.id, offer.id)
    checker.item = checked(50); service.check_for_user(user.id, offer.id)
    first = OfferRepository(db).get_for_user(offer.id, user.id)
    checker.item = checked(50); repeated = service.check_for_user(user.id, offer.id)
    second = OfferRepository(db).get_for_user(offer.id, user.id)
    assert repeated.events == ()
    assert (second.pending_check_attempt_id, second.pending_price_detected_at) == (first.pending_check_attempt_id, first.pending_price_detected_at)


def test_stale_snapshot_from_second_connection_creates_no_history(tmp_path):
    path = tmp_path / "concurrent.sqlite"; initialize_database(path)
    first, second = connect(path), connect(path)
    user = UserRepository(first).create(User(9, TIME)); product = ProductRepository(first).create(Product(user.id, "Item", TIME))
    offer = OfferRepository(first).create(Offer(product.id, "https://shop.test/x", "https://shop.test/x", TIME)); first.commit()
    snapshot = OfferRepository(first).get_for_user(offer.id, user.id)
    OfferRepository(second).update_check_state(offer.id, last_checked_at="newer"); second.commit()
    stale = MonitoringService(first, Checker(checked(100)))._persist(offer.id, user.id, checked(100), snapshot.state_version)
    assert stale.code == "STALE"
    assert first.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
    assert first.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 0
    assert first.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
    first.close(); second.close()


def test_ambiguous_variant_price_never_enters_history_or_notifications(setup):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    service.check_for_user(user.id, offer.id)
    history_before = db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0]
    ambiguous = PriceCheckResult(
        FetchResult(
            "https://shop.test/x",
            FetchMethod.REQUESTS,
            FetchStatus.SUCCESS,
            final_url="https://shop.test/x",
            http_status=200,
            html="<html>variant conflict</html>",
        ),
        ExtractionResult(
            ExtractionStatus.AMBIGUOUS,
            method=ExtractionMethod.JSON_LD,
            diagnostics={"reason": "product variant identity mismatch"},
        ),
    )
    checker.item = ambiguous
    result = service.check_for_user(user.id, offer.id)
    assert result.events == ()
    assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == history_before
    assert db.execute("SELECT COUNT(*) FROM notification_outbox").fetchone()[0] == 0
    attempt = db.execute("SELECT extraction_status, detected_price_minor FROM check_attempts ORDER BY id DESC LIMIT 1").fetchone()
    assert tuple(attempt) == (ExtractionStatus.AMBIGUOUS.value, None)


def test_offer_success_logging_uses_only_safe_operational_fields(setup, caplog):
    db, user, product, offer, checker = setup
    caplog.set_level(logging.INFO, logger="pricewatch.services.monitoring_service")

    result = MonitoringService(db, checker).check_for_user(user.id, offer.id)

    assert result.code == "SUCCESS"
    messages = [record.getMessage() for record in caplog.records]
    started = next(message for message in messages if "event=offer_check_started" in message)
    completed = next(message for message in messages if "event=offer_check_completed" in message)
    run_id = started.split("run_id=", 1)[1].split()[0]
    assert f"run_id={run_id}" in completed
    assert f"offer_id={offer.id}" in completed
    assert f"product_id={product.id}" in completed
    assert "outcome=success" in completed
    assert "http_status=200" in completed
    assert "fetch_status=SUCCESS" in completed
    assert "extraction_status=SUCCESS" in completed
    assert "previous_status=PENDING new_status=ACTIVE consecutive_errors=0" in completed
    assert "duration_ms=" in completed
    joined = "\n".join(messages)
    assert "https://" not in joined
    assert "Item" not in joined
    assert "failed" not in joined


@pytest.mark.parametrize(
    ("fetch_status", "extraction_status", "http_status"),
    [
        (FetchStatus.HTTP_ERROR, ExtractionStatus.NOT_RUN, 503),
        (FetchStatus.BLOCKED, ExtractionStatus.NOT_RUN, 403),
        (FetchStatus.ROBOTS_DENIED, ExtractionStatus.NOT_RUN, None),
        (FetchStatus.SUCCESS, ExtractionStatus.PARTIAL, 200),
        (FetchStatus.SUCCESS, ExtractionStatus.AMBIGUOUS, 200),
    ],
)
def test_expected_degradation_is_warning_with_enum_outcome(
    setup, caplog, fetch_status, extraction_status, http_status,
):
    db, user, _, offer, checker = setup
    fetch = FetchResult(
        "https://sensitive.test/private?token=synthetic",
        FetchMethod.REQUESTS,
        fetch_status,
        http_status=http_status,
        error_message="synthetic private failure",
    )
    checker.item = PriceCheckResult(
        fetch,
        ExtractionResult(extraction_status, method=ExtractionMethod.NONE),
        (fetch,),
    )
    caplog.set_level(logging.INFO, logger="pricewatch.services.monitoring_service")

    result = MonitoringService(db, checker).check_for_user(user.id, offer.id)

    assert result.code == "PENDING"
    record = next(
        record for record in caplog.records
        if "event=offer_check_completed" in record.getMessage()
    )
    message = record.getMessage()
    assert record.levelno == logging.WARNING
    assert "outcome=pending" in message
    assert f"http_status={http_status}" in message
    assert f"fetch_status={fetch_status.value}" in message
    assert f"extraction_status={extraction_status.value}" in message
    assert "previous_status=PENDING new_status=PENDING consecutive_errors=1" in message
    assert "https://" not in message
    assert "synthetic private failure" not in message


def test_offer_error_transitions_and_recovery_are_logged(setup, caplog):
    db, user, _, offer, checker = setup
    service = MonitoringService(db, checker)
    service.check_for_user(user.id, offer.id)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="pricewatch.services.monitoring_service")
    checker.item = checked(None, FetchStatus.TIMEOUT)

    service.check_for_user(user.id, offer.id)
    service.check_for_user(user.id, offer.id)
    service.check_for_user(user.id, offer.id)
    checker.item = checked(100)
    service.check_for_user(user.id, offer.id)

    completed = [
        record.getMessage() for record in caplog.records
        if "event=offer_check_completed" in record.getMessage()
    ]
    assert any(
        "outcome=pending" in message
        and "previous_status=ACTIVE new_status=ACTIVE consecutive_errors=1" in message
        for message in completed
    )
    assert any(
        "outcome=error" in message
        and "previous_status=ACTIVE new_status=ERROR consecutive_errors=3" in message
        for message in completed
    )
    assert any(
        "outcome=success" in message
        and "previous_status=ERROR new_status=ACTIVE consecutive_errors=0" in message
        for message in completed
    )


def test_checker_runtime_error_is_isolated_and_later_offers_are_checked(
    setup, caplog,
):
    db, user, _, first, _ = setup
    products = ProductRepository(db)
    offers = OfferRepository(db)
    middle_product = products.create(Product(user.id, "Middle", TIME))
    middle = offers.create(Offer(
        middle_product.id, "https://shop.test/private?token=secret",
        "https://shop.test/private?token=secret", TIME,
        status=OfferStatus.PENDING,
    ))
    last_product = products.create(Product(user.id, "Last", TIME))
    last = offers.create(Offer(
        last_product.id, "https://shop.test/last", "https://shop.test/last",
        TIME, status=OfferStatus.PENDING,
    ))
    db.commit()

    class PerOfferChecker:
        def __init__(self):
            self.calls = []

        def check(self, url):
            self.calls.append(url)
            if url == middle.normalized_url:
                raise RuntimeError(
                    "private checker failure https://shop.test/leak?token=secret"
                )
            return checked(100)

    checker = PerOfferChecker()
    caplog.set_level(logging.INFO, logger="pricewatch.services.monitoring_service")

    result = MonitoringService(db, checker).check_all()

    assert [item.code for item in result.results] == [
        "SUCCESS", "INTERNAL_ERROR", "SUCCESS",
    ]
    assert checker.calls == [
        first.normalized_url, middle.normalized_url, last.normalized_url,
    ]
    assert db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 2
    middle_state = offers.get_for_user(middle.id, user.id)
    assert (
        middle_state.status,
        middle_state.current_price_minor,
        middle_state.consecutive_errors,
    ) == (OfferStatus.PENDING, None, 0)
    failed = [
        record.getMessage() for record in caplog.records
        if "event=offer_check_failed" in record.getMessage()
    ]
    assert len(failed) == 1
    assert "outcome=internal_error" in failed[0]
    assert f"offer_id={middle.id}" in failed[0]
    assert "exception_class=RuntimeError" in failed[0]
    assert "run_id=" in failed[0]
    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "private checker failure" not in joined
    assert "https://" not in joined
    assert "token=secret" not in joined
    assert "chat_id" not in joined and "telegram" not in joined.lower()
    assert not any(
        "event=offer_check_completed" in message and f"offer_id={middle.id}" in message
        for message in joined.splitlines()
    )
    assert "event=event_created" not in joined


def test_persistence_invariant_failure_escalates_and_rolls_back(
    setup, caplog, monkeypatch,
):
    db, _, _, offer, checker = setup
    service = MonitoringService(db, checker)

    def fail(_attempt):
        raise ValueError(
            "synthetic private failure https://shop.test/private?token=secret"
        )

    monkeypatch.setattr(service._attempts, "create", fail)
    caplog.set_level(logging.INFO, logger="pricewatch.services.monitoring_service")

    with pytest.raises(ValueError, match="synthetic private failure"):
        service.check_all()

    assert db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
    state = OfferRepository(db).get_internal(offer.id)
    assert (state.status, state.current_price_minor, state.consecutive_errors) == (
        OfferStatus.PENDING, None, 0,
    )
    failed = next(
        record.getMessage() for record in caplog.records
        if "event=offer_check_failed" in record.getMessage()
    )
    assert f"offer_id={offer.id}" in failed
    assert "exception_class=ValueError" in failed
    assert "synthetic private failure" not in failed
    assert "https://" not in failed


def test_sqlite_operational_error_is_not_localized_and_rolls_back(
    setup, monkeypatch,
):
    db, _, _, offer, checker = setup
    service = MonitoringService(db, checker)

    def fail(_attempt):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(service._attempts, "create", fail)

    with pytest.raises(sqlite3.OperationalError, match="database unavailable"):
        service.check_all()

    assert db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
    state = OfferRepository(db).get_internal(offer.id)
    assert (state.status, state.current_price_minor) == (OfferStatus.PENDING, None)


def test_commit_error_is_not_success_and_transaction_is_rolled_back(setup):
    db, _, _, offer, checker = setup
    service = MonitoringService(db, checker)

    class FailingCommitConnection:
        def __init__(self, connection):
            self.connection = connection
            self.rollback_called = False

        def execute(self, *args, **kwargs):
            return self.connection.execute(*args, **kwargs)

        def commit(self):
            raise sqlite3.OperationalError("commit unavailable")

        def rollback(self):
            self.rollback_called = True
            return self.connection.rollback()

    failing = FailingCommitConnection(db)
    service._connection = failing

    with pytest.raises(sqlite3.OperationalError, match="commit unavailable"):
        service.check_all()

    assert failing.rollback_called
    assert db.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
    state = OfferRepository(db).get_internal(offer.id)
    assert (state.status, state.current_price_minor, state.consecutive_errors) == (
        OfferStatus.PENDING, None, 0,
    )
