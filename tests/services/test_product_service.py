from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
import logging
from threading import Barrier

import pytest

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.check_attempt_repository import CheckAttemptRepository
from pricewatch.database.repositories.price_history_repository import PriceHistoryRepository
from pricewatch.domain.enums import ExtractionMethod, ExtractionStatus, FetchMethod, FetchStatus, OfferStatus
from pricewatch.scraping.models import ExtractionResult, FetchResult, PriceCheckResult
from pricewatch.scraping.url_security import UrlSecurity
from pricewatch.services.product_service import ProductService
from pricewatch.services.url_normalization import UrlNormalizationError, normalize_url


class Checker:
    def __init__(self, result):
        self.result, self.calls = result, []
    def check(self, url):
        self.calls.append(url)
        return self.result


def security(addresses):
    return UrlSecurity(lambda host, port: [(None, None, None, None, (address, 0)) for address in addresses.get(host, [])])


def result(success=True, price=Decimal("12.345"), currency="EUR"):
    fetch = FetchResult("https://shop.test/item", FetchMethod.REQUESTS, FetchStatus.SUCCESS, "https://shop.test/item", 200, "<html>", 7)
    extraction = ExtractionResult(
        ExtractionStatus.SUCCESS if success else ExtractionStatus.PARTIAL,
        title="Store title", price=price if success else None, currency=currency if success else None,
        method=ExtractionMethod.JSON_LD,
    )
    return PriceCheckResult(fetch, extraction, (fetch,))


@pytest.fixture
def service(tmp_path):
    path = tmp_path / "pricewatch.db"
    initialize_database(path)
    connection = connect(path)
    checker = Checker(result())
    yield ProductService(connection, checker, security({"shop.test": ["93.184.216.34"]})), checker, connection
    connection.close()


def test_url_normalization_preserves_semantics_and_removes_tracking():
    assert normalize_url("HTTPS://SHOP.Test:443/a%2Fb?sku=1&tag=x&utm_source=x&sku=2#part") == "https://shop.test/a%2Fb?sku=1&tag=x&sku=2"
    assert normalize_url("http://[2001:db8::1]:80/a?gclid=x&v=1") == "http://[2001:db8::1]/a?v=1"
    assert normalize_url("https://münich.example/ü?q=%2F") == "https://xn--mnich-kva.example/ü?q=%2F"
    assert normalize_url("https://shop.test:8443/a?variant=red") == "https://shop.test:8443/a?variant=red"
    with pytest.raises(UrlNormalizationError):
        normalize_url("ftp://shop.test/a")
    for invalid in ("https://user:pass@shop.test/a", "https://shop.test/a\x00b", "https://[invalid/a"):
        with pytest.raises(UrlNormalizationError):
            normalize_url(invalid)


def test_user_product_lifecycle_and_ownership(service):
    api, _, _ = service
    user = api.get_or_create_user(101, "alice")
    assert api.get_or_create_user(101).id == user.id
    created = api.create_product(user.id, "  Phone  ")
    assert created.code == "CREATED" and created.product.name == "Phone"
    assert api.create_product(user.id, "phone").code == "DUPLICATE"
    other = api.get_or_create_user(102)
    assert api.rename_product(other.id, created.product.id, "Other").code == "NOT_FOUND"
    assert api.rename_product(user.id, created.product.id, "  New phone ").product.name == "New phone"
    assert api.create_product(other.id, "new PHONE").code == "CREATED"
    assert api.delete_product(other.id, created.product.id).code == "NOT_FOUND"
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        api.create_product(999999, "orphan")


def test_active_offer_baseline_duplicate_and_cascade(service, caplog):
    api, checker, connection = service
    user = api.get_or_create_user(201)
    first = api.create_product(user.id, "First").product
    second = api.create_product(user.id, "Second").product
    caplog.set_level(logging.INFO, logger="pricewatch.services.product_service")
    source_url = "HTTPS://SHOP.test:443/item?sku=7&utm_source=x#top"
    normalized_url = "https://shop.test/item?sku=7"
    added = api.add_offer(user.id, first.id, source_url)
    assert added.code == "CREATED" and added.offer.status is OfferStatus.ACTIVE
    assert checker.calls == [normalized_url]
    assert added.offer.url == source_url
    assert added.offer.normalized_url == normalized_url
    assert added.offer.current_price_minor == 1235 and added.offer.currency == "EUR"
    assert len(PriceHistoryRepository(connection).list_for_offer(added.offer.id, user.id)) == 1
    assert len(CheckAttemptRepository(connection).list_for_offer(added.offer.id, user.id)) == 1
    duplicate = api.add_offer(user.id, second.id, normalized_url)
    assert duplicate.code == "DUPLICATE" and len(checker.calls) == 1
    messages = [record.getMessage() for record in caplog.records]
    started = next(
        message for message in messages
        if "event=initial_offer_check_started" in message
    )
    operation_id = started.split("operation_id=", 1)[1].split()[0]
    completed = next(
        message for message in messages
        if "event=initial_offer_check_completed outcome=success" in message
    )
    persisted = next(
        message for message in messages
        if "event=initial_offer_persisted" in message
    )
    assert f"operation_id={operation_id}" in completed
    assert f"operation_id={operation_id}" in persisted
    assert "http_status=200 fetch_status=SUCCESS extraction_status=SUCCESS" in completed
    assert f"product_id={first.id} offer_id={added.offer.id}" in persisted
    assert "outcome=active" in persisted and "new_status=ACTIVE" in persisted
    safe_logs = "\n".join((started, completed, persisted))
    assert "https://" not in safe_logs
    assert "First" not in safe_logs
    assert "user_id=" not in safe_logs
    assert "chat_id=" not in safe_logs
    assert "telegram" not in safe_logs.lower()
    assert api.delete_product(user.id, first.id).code == "DELETED"
    assert CheckAttemptRepository(connection).get_internal(1) is None


def test_pending_failure_security_and_foreign_offer(service, caplog):
    api, checker, connection = service
    user = api.get_or_create_user(301)
    product = api.create_product(user.id, "Item").product
    checker.result = result(False)
    caplog.set_level(logging.INFO, logger="pricewatch.services.product_service")
    pending = api.add_offer(user.id, product.id, "https://shop.test/item")
    assert pending.code == "CREATED" and pending.offer.status is OfferStatus.PENDING
    assert PriceHistoryRepository(connection).list_for_offer(pending.offer.id, user.id) == []
    other = api.get_or_create_user(302)
    assert api.delete_offer(other.id, pending.offer.id).code == "NOT_FOUND"
    blocked = api.add_offer(user.id, product.id, "http://127.0.0.1/private")
    assert blocked.code == "BLOCKED" and len(checker.calls) == 1
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "event=initial_offer_check_completed outcome=degraded" in message
        and "fetch_status=SUCCESS extraction_status=PARTIAL" in message
        for message in messages
    )
    assert any(
        "event=initial_offer_persisted outcome=pending" in message
        and "new_status=PENDING" in message
        for message in messages
    )
    assert any(
        "event=initial_offer_check_completed outcome=blocked" in message
        for message in messages
    )


def test_temporary_dns_failure_is_saved_pending_without_checker(tmp_path):
    path = tmp_path / "pricewatch.db"
    initialize_database(path)
    connection = connect(path)
    checker = Checker(result())
    api = ProductService(connection, checker, UrlSecurity(lambda host, port: []))
    user = api.get_or_create_user(401)
    product = api.create_product(user.id, "Item").product
    added = api.add_offer(user.id, product.id, "https://missing.test/item")
    assert added.code == "CREATED" and added.offer.status is OfferStatus.PENDING
    assert checker.calls == []
    connection.close()


@pytest.mark.parametrize("price,currency", [
    (Decimal("0"), "EUR"), (Decimal("-1"), "EUR"), (Decimal("NaN"), "EUR"),
    (Decimal("Infinity"), "EUR"), (Decimal("0.001"), "EUR"), (Decimal("12"), None),
    (Decimal("12"), "EURO"), (Decimal("99999999999999999"), "EUR"),
])
def test_invalid_initial_money_is_pending(service, price, currency):
    api, checker, connection = service
    user = api.get_or_create_user(500 + len(str(price)))
    product = api.create_product(user.id, str(price)).product
    checker.result = result(True, price, currency)
    added = api.add_offer(user.id, product.id, "https://shop.test/item?variant=" + str(product.id))
    assert added.offer.status is OfferStatus.PENDING
    assert PriceHistoryRepository(connection).list_for_offer(added.offer.id, user.id) == []


def test_success_extraction_cannot_override_failed_fetch(service):
    api, checker, connection = service
    user = api.get_or_create_user(601)
    product = api.create_product(user.id, "Item").product
    bad_fetch = FetchResult("https://shop.test/item", FetchMethod.REQUESTS, FetchStatus.TIMEOUT, error_message="timeout")
    checker.result = PriceCheckResult(bad_fetch, ExtractionResult(ExtractionStatus.SUCCESS, price=Decimal("10"), currency="EUR"), (bad_fetch,))
    added = api.add_offer(user.id, product.id, "https://shop.test/item")
    assert added.offer.status is OfferStatus.PENDING
    assert PriceHistoryRepository(connection).list_for_offer(added.offer.id, user.id) == []


def test_transaction_rolls_back_when_attempt_or_history_fails(
    service, monkeypatch, caplog,
):
    api, _, connection = service
    user = api.get_or_create_user(701)
    product = api.create_product(user.id, "Item").product
    caplog.set_level(logging.INFO, logger="pricewatch.services.product_service")
    monkeypatch.setattr(api._attempts, "create", lambda attempt: (_ for _ in ()).throw(RuntimeError("attempt failure")))
    with pytest.raises(RuntimeError, match="attempt failure"):
        api.add_offer(user.id, product.id, "https://shop.test/item?a=1")
    assert api.get_product_details(user.id, product.id).offers == ()
    monkeypatch.undo()
    monkeypatch.setattr(api._history, "create", lambda record: (_ for _ in ()).throw(RuntimeError("history failure")))
    with pytest.raises(RuntimeError, match="history failure"):
        api.add_offer(user.id, product.id, "https://shop.test/item?a=2")
    assert api.get_product_details(user.id, product.id).offers == ()
    assert connection.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 0
    assert not any(
        "event=initial_offer_persisted" in record.getMessage()
        for record in caplog.records
    )
    failed = [
        record.getMessage() for record in caplog.records
        if "event=initial_offer_persistence_failed" in record.getMessage()
    ]
    assert len(failed) == 2
    assert all("exception_class=RuntimeError" in message for message in failed)
    assert not any("attempt failure" in message or "history failure" in message for message in failed)


def test_duplicate_is_rechecked_after_scraping_without_second_offer(service):
    api, checker, _ = service
    user = api.get_or_create_user(801)
    product = api.create_product(user.id, "Item").product
    original = checker.check
    def insert_during_check(url):
        checker.check = original
        first = api.add_offer(user.id, product.id, url)
        assert first.code == "CREATED"
        return original(url)
    checker.check = insert_during_check
    duplicate = api.add_offer(user.id, product.id, "https://shop.test/item?x=1")
    assert duplicate.code == "DUPLICATE"
    assert len(api.get_product_details(user.id, product.id).offers) == 1


def test_create_product_with_offer_compensates_duplicate_or_unsafe_url(service):
    api, checker, connection = service
    user = api.get_or_create_user(901)
    existing = api.create_product(user.id, "Existing").product
    assert api.add_offer(user.id, existing.id, "https://shop.test/item?sku=1").code == "CREATED"
    duplicate = api.create_product_with_offer(user.id, "New duplicate", "https://shop.test/item?sku=1")
    assert duplicate.code == "DUPLICATE"
    assert [product.name for product in api.list_products(user.id)] == ["Existing"]
    unsafe = api.create_product_with_offer(user.id, "Unsafe", "http://127.0.0.1/private")
    assert unsafe.code == "BLOCKED"
    assert [product.name for product in api.list_products(user.id)] == ["Existing"]
    assert len(checker.calls) == 1


def test_create_product_with_offer_compensates_unexpected_persistence_error(service, monkeypatch):
    api, _, connection = service
    user = api.get_or_create_user(902)
    monkeypatch.setattr(api._attempts, "create", lambda *args: (_ for _ in ()).throw(RuntimeError("write failed")))
    with pytest.raises(RuntimeError, match="write failed"):
        api.create_product_with_offer(user.id, "Will not remain", "https://shop.test/item")
    assert api.list_products(user.id) == ()
    assert connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0


def test_preview_does_not_write_and_finalize_reuses_single_scrape(service):
    api, checker, connection = service
    user = api.get_or_create_user(903)
    preview = api.preview_offer(user.id, "https://shop.test/item?preview=1")
    assert preview.code == "READY" and preview.source_title == "Store title"
    assert api.list_products(user.id) == ()
    assert connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
    created = api.create_product_with_preview(user.id, "Previewed", preview)
    assert created.code == "CREATED" and created.product and created.offer
    assert len(checker.calls) == 1


def test_expired_preview_is_not_persisted_or_refetched(service):
    api, checker, _ = service
    user = api.get_or_create_user(904)
    preview = api.preview_offer(user.id, "https://shop.test/expired")
    expired = replace(preview, previewed_at="2000-01-01T00:00:00+00:00")
    result_value = api.create_product_with_preview(user.id, "Expired", expired)
    assert result_value.code == "STALE"
    assert api.list_products(user.id) == ()
    assert len(checker.calls) == 1


@pytest.mark.parametrize("repository", ["_offers", "_attempts", "_history"])
def test_atomic_product_graph_rolls_back_at_each_write_stage(service, monkeypatch, repository):
    api, _, connection = service
    user = api.get_or_create_user({"_offers": 909, "_attempts": 910, "_history": 911}[repository])
    target = getattr(api, repository)
    monkeypatch.setattr(target, "create", lambda *args: (_ for _ in ()).throw(RuntimeError(repository)))
    with pytest.raises(RuntimeError, match=repository):
        api.create_product_with_offer(user.id, repository, f"https://shop.test/item?stage={repository}")
    assert api.list_products(user.id) == ()
    assert connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM check_attempts").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0


def test_atomic_create_never_deletes_preexisting_same_name(service):
    api, _, _ = service
    user = api.get_or_create_user(920)
    existing = api.create_product(user.id, "Existing").product
    result = api.create_product_with_offer(user.id, "existing", "https://shop.test/new")
    assert result.code == "DUPLICATE"
    assert [item.id for item in api.list_products(user.id)] == [existing.id]


def test_concurrent_atomic_create_keeps_one_product_for_duplicate_url(tmp_path):
    path = tmp_path / "concurrent.db"
    initialize_database(path)
    bootstrap = connect(path)
    checker = Checker(result())
    safe = security({"shop.test": ["93.184.216.34"]})
    user = ProductService(bootstrap, checker, safe).get_or_create_user(930)
    bootstrap.close()
    ready = Barrier(2)

    def prepare_and_create(name):
        connection = connect(path)
        try:
            api = ProductService(connection, checker, safe)
            preview = api.preview_offer(user.id, "https://shop.test/concurrent")
            assert preview.code == "READY"
            ready.wait(timeout=5)
            return api.create_product_with_preview(user.id, name, preview).code
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(prepare_and_create, ("First", "Second")))
    connection = connect(path)
    try:
        api = ProductService(connection, checker, safe)
        assert sorted(codes) == ["CREATED", "DUPLICATE"]
        assert len(api.list_products(user.id)) == 1
        assert connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 1
    finally:
        connection.close()
