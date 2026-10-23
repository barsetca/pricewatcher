import sqlite3

import pytest

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.check_attempt_repository import CheckAttemptRepository
from pricewatch.database.repositories.offer_repository import OfferRepository
from pricewatch.database.repositories.price_history_repository import PriceHistoryRepository
from pricewatch.database.repositories.product_repository import ProductRepository
from pricewatch.database.repositories.user_repository import UserRepository
from pricewatch.domain.enums import AnomalyDecision, ExtractionStatus, FetchStatus
from pricewatch.domain.models import CheckAttempt, Offer, PriceRecord, Product, User


TIME = "2026-10-08T00:00:00Z"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "pricewatch.db"
    initialize_database(path)
    connection = connect(path)
    yield connection
    connection.close()


def repository_tree(connection):
    users = UserRepository(connection)
    products = ProductRepository(connection)
    offers = OfferRepository(connection)
    user_a = users.create(User(1, TIME))
    user_b = users.create(User(2, TIME))
    product_a = products.create(Product(user_a.id, "A", TIME))
    product_b = products.create(Product(user_b.id, "B", TIME))
    offer_a = offers.create(Offer(product_a.id, "https://a", "https://a", TIME))
    offer_b = offers.create(Offer(product_b.id, "https://b", "https://b", TIME))
    return users, products, offers, user_a, user_b, product_a, product_b, offer_a, offer_b


def test_product_user_operations_do_not_expose_or_change_another_users_product(db):
    users, products, _, user_a, user_b, product_a, product_b, _, _ = repository_tree(db)
    assert users.get_by_telegram_user_id(1).id == user_a.id
    assert users.update_username(user_a.id, "name")
    assert users.update_alert_threshold(user_a.id, 15)
    assert products.get_for_user(product_b.id, user_a.id) is None
    assert not products.rename(product_b.id, user_a.id, "Changed")
    assert not products.update_alert_threshold(product_b.id, user_a.id, 99)
    assert not products.set_active(product_b.id, user_a.id, False)
    assert not products.delete(product_b.id, user_a.id)
    unchanged = products.get_for_user(product_b.id, user_b.id)
    assert (unchanged.name, unchanged.alert_threshold, unchanged.is_active) == ("B", None, True)
    assert products.rename(product_a.id, user_a.id, "New")
    with pytest.raises(sqlite3.IntegrityError):
        products.create(Product(user_a.id, "new", TIME))
    assert products.create(Product(user_b.id, "new", TIME))


def test_offer_user_operations_do_not_expose_or_change_another_users_offer(db):
    _, products, offers, user_a, user_b, product_a, product_b, offer_a, offer_b = repository_tree(db)
    assert offers.get_for_user(offer_b.id, user_a.id) is None
    assert offers.list_for_product(product_b.id, user_a.id) == []
    assert not offers.update_details(offer_b.id, user_a.id, store_name="Changed")
    assert not offers.set_active(offer_b.id, user_a.id, False)
    assert not offers.delete(offer_b.id, user_a.id)
    unchanged = offers.get_for_user(offer_b.id, user_b.id)
    assert (unchanged.store_name, unchanged.is_active) == (None, True)
    assert offers.update_details(offer_a.id, user_a.id, store_name="A store")
    assert offers.set_active(offer_a.id, user_a.id, False)
    assert offers.list_for_product(product_a.id, user_a.id)[0].id == offer_a.id
    assert products.get_for_user(product_b.id, user_b.id) is not None


def test_duplicate_detection_is_per_user_and_spans_products(db):
    _, products, offers, user_a, user_b, product_a, _, offer_a, _ = repository_tree(db)
    second_product = products.create(Product(user_a.id, "second", TIME))
    offers.create(Offer(second_product.id, "https://a-duplicate", "https://a", TIME))
    assert offers.find_duplicate_for_user("https://a", user_a.id).id == offer_a.id
    assert offers.find_duplicate_for_user("https://a", user_b.id) is None
    assert offers.find_duplicate_for_user("unknown", user_a.id) is None
    assert offers.list_for_product(product_a.id, user_a.id)[0].id == offer_a.id


def test_pending_attempt_must_belong_to_offer_and_failed_update_preserves_state(db):
    _, _, offers, user_a, _, _, _, offer_a, offer_b = repository_tree(db)
    attempts = CheckAttemptRepository(db)
    own_attempt = attempts.create(CheckAttempt(offer_a.id, TIME, FetchStatus.SUCCESS, ExtractionStatus.SUCCESS))
    other_attempt = attempts.create(CheckAttempt(offer_b.id, TIME, FetchStatus.SUCCESS, ExtractionStatus.SUCCESS))
    assert offers.set_pending_anomaly(offer_a.id, user_a.id, 900, TIME, own_attempt.id)
    before = offers.get_for_user(offer_a.id, user_a.id)
    assert not offers.set_pending_anomaly(offer_a.id, user_a.id, 800, "later", other_attempt.id)
    after = offers.get_for_user(offer_a.id, user_a.id)
    assert (after.pending_price_minor, after.pending_price_detected_at, after.pending_check_attempt_id) == (
        before.pending_price_minor, before.pending_price_detected_at, before.pending_check_attempt_id
    )


def test_history_user_methods_hide_foreign_records_and_honor_limit(db):
    _, _, _, user_a, user_b, _, _, offer_a, offer_b = repository_tree(db)
    history = PriceHistoryRepository(db)
    history.create(PriceRecord(offer_a.id, 100, "EUR", TIME))
    latest = history.create(PriceRecord(offer_a.id, 90, "EUR", "2026-10-09T00:00:00Z"))
    history.create(PriceRecord(offer_b.id, 50, "EUR", TIME))
    assert history.latest_for_offer(offer_a.id).id == latest.id
    assert history.list_for_offer(offer_b.id, user_a.id) == []
    assert history.list_for_offer(offer_a.id, user_b.id, limit=1) == []
    assert [record.id for record in history.list_for_offer(offer_a.id, user_a.id, limit=1)] == [latest.id]


def test_attempt_user_operations_hide_foreign_records_and_preserve_decision(db):
    _, _, _, user_a, user_b, _, _, offer_a, offer_b = repository_tree(db)
    attempts = CheckAttemptRepository(db)
    own_attempt = attempts.create(CheckAttempt(offer_a.id, TIME, FetchStatus.SUCCESS, ExtractionStatus.SUCCESS))
    foreign_attempt = attempts.create(CheckAttempt(offer_b.id, TIME, FetchStatus.SUCCESS, ExtractionStatus.SUCCESS))
    assert attempts.get_for_user(foreign_attempt.id, user_a.id) is None
    assert attempts.list_for_offer(offer_b.id, user_a.id) == []
    assert attempts.list_for_offer(offer_b.id, user_a.id, limit=1) == []
    assert not attempts.set_anomaly_decision(foreign_attempt.id, user_a.id, AnomalyDecision.REJECTED, TIME)
    assert attempts.get_for_user(foreign_attempt.id, user_b.id).anomaly_decision is None
    assert attempts.set_anomaly_decision(own_attempt.id, user_a.id, AnomalyDecision.CONFIRMED, TIME)


def test_repositories_leave_transaction_boundary_to_caller(db):
    users = UserRepository(db)
    products = ProductRepository(db)
    offers = OfferRepository(db)
    db.commit()
    db.execute("BEGIN")
    transient_user = users.create(User(99, TIME))
    transient_product = products.create(Product(transient_user.id, "transient", TIME))
    offers.create(Offer(transient_product.id, "https://transient", "https://transient", TIME))
    db.rollback()
    assert users.get_by_telegram_user_id(99) is None
    assert db.execute("SELECT COUNT(*) FROM products WHERE name = 'transient'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM offers WHERE url = 'https://transient'").fetchone()[0] == 0
    db.execute("BEGIN")
    users.create(User(98, TIME))
    db.commit()
    assert users.get_by_telegram_user_id(98) is not None
