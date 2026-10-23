"""Пользовательский lifecycle Product и Offer."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlsplit

from pricewatch.database.repositories.check_attempt_repository import CheckAttemptRepository
from pricewatch.database.repositories.offer_repository import OfferRepository
from pricewatch.database.repositories.price_history_repository import PriceHistoryRepository
from pricewatch.database.repositories.product_repository import ProductRepository
from pricewatch.database.repositories.user_repository import UserRepository
from pricewatch.domain.enums import ExtractionStatus, OfferStatus
from pricewatch.domain.models import CheckAttempt, Offer, PriceRecord, Product, User
from pricewatch.scraping.models import PriceCheckResult
from pricewatch.scraping.url_security import UrlSecurity
from pricewatch.services.url_normalization import UrlNormalizationError, normalize_url


@dataclass(frozen=True)
class OperationResult:
    code: str
    message: str = ""
    product: Product | None = None
    offer: Offer | None = None
    duplicate_offer: Offer | None = None
    check: PriceCheckResult | None = None


@dataclass(frozen=True)
class ProductDetails:
    product: Product
    offers: tuple[Offer, ...]


class ProductService:
    def __init__(self, connection: sqlite3.Connection, price_checker, url_security: UrlSecurity | None = None) -> None:
        self._connection = connection
        self._checker = price_checker
        self._security = url_security or UrlSecurity()
        self._users = UserRepository(connection)
        self._products = ProductRepository(connection)
        self._offers = OfferRepository(connection)
        self._history = PriceHistoryRepository(connection)
        self._attempts = CheckAttemptRepository(connection)

    def get_or_create_user(self, telegram_user_id: int, username: str | None = None) -> User:
        existing = self._users.get_by_telegram_user_id(telegram_user_id)
        if existing:
            return existing
        try:
            with self._connection:
                return self._users.create(User(telegram_user_id, _now(), username=username))
        except sqlite3.IntegrityError:
            user = self._users.get_by_telegram_user_id(telegram_user_id)
            if user:
                return user
            raise

    def create_product(self, user_id: int, name: str) -> OperationResult:
        normalized = _name(name)
        if not normalized:
            return OperationResult("INVALID", "product name is empty")
        try:
            with self._connection:
                product = self._products.create(Product(user_id, normalized, _now()))
            return OperationResult("CREATED", product=product)
        except sqlite3.IntegrityError:
            return OperationResult("DUPLICATE", "product name already exists")

    def rename_product(self, user_id: int, product_id: int, name: str) -> OperationResult:
        normalized = _name(name)
        if not normalized:
            return OperationResult("INVALID", "product name is empty")
        try:
            with self._connection:
                if not self._products.get_for_user(product_id, user_id):
                    return OperationResult("NOT_FOUND")
                if not self._products.rename(product_id, user_id, normalized):
                    return OperationResult("NOT_FOUND")
                return OperationResult("UPDATED", product=self._products.get_for_user(product_id, user_id))
        except sqlite3.IntegrityError:
            return OperationResult("DUPLICATE", "product name already exists")

    def delete_product(self, user_id: int, product_id: int) -> OperationResult:
        with self._connection:
            return OperationResult("DELETED" if self._products.delete(product_id, user_id) else "NOT_FOUND")

    def list_products(self, user_id: int) -> tuple[Product, ...]:
        return tuple(self._products.list_for_user(user_id))

    def get_product_details(self, user_id: int, product_id: int) -> ProductDetails | None:
        product = self._products.get_for_user(product_id, user_id)
        return ProductDetails(product, tuple(self._offers.list_for_product(product_id, user_id))) if product else None

    def delete_offer(self, user_id: int, offer_id: int) -> OperationResult:
        with self._connection:
            return OperationResult("DELETED" if self._offers.delete(offer_id, user_id) else "NOT_FOUND")

    def add_offer(self, user_id: int, product_id: int, url: str) -> OperationResult:
        # Ownership/duplicate checks не удерживают write transaction во время сети.
        if not self._products.get_for_user(product_id, user_id):
            return OperationResult("NOT_FOUND")
        try:
            normalized_url = normalize_url(url)
        except UrlNormalizationError as error:
            return OperationResult("INVALID", str(error))
        duplicate = self._offers.find_duplicate_for_user(normalized_url, user_id)
        if duplicate:
            return OperationResult("DUPLICATE", duplicate_offer=duplicate)

        security_error = self._security.validate(normalized_url)
        if security_error and not _temporary_dns_error(security_error):
            return OperationResult("BLOCKED", security_error)
        # DNS failure is explicitly persistable PENDING: no HTTP check is made.
        check = None if security_error else self._checker.check(normalized_url)
        return self._persist_initial(user_id, product_id, url, normalized_url, check, security_error)

    def _persist_initial(self, user_id: int, product_id: int, url: str, normalized_url: str, check: PriceCheckResult | None, dns_error: str | None) -> OperationResult:
        timestamp = _now()
        successful, price_minor, currency, message = _initial_price(check)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if not self._products.get_for_user(product_id, user_id):
                self._connection.rollback()
                return OperationResult("NOT_FOUND")
            duplicate = self._offers.find_duplicate_for_user(normalized_url, user_id)
            if duplicate:
                self._connection.rollback()
                return OperationResult("DUPLICATE", duplicate_offer=duplicate)
            parts = urlsplit(normalized_url)
            offer = self._offers.create(Offer(
                product_id, url, normalized_url, timestamp, domain=parts.hostname,
                source_title=check.extraction.title if check else None,
                current_price_minor=price_minor if successful else None,
                currency=currency if successful else None,
                preferred_fetch_method=check.fetch.method if check else None,
                status=OfferStatus.ACTIVE if successful else OfferStatus.PENDING,
                last_checked_at=timestamp, last_success_at=timestamp if successful else None,
            ))
            attempt = _attempt(offer.id, timestamp, check, price_minor if successful else None, currency if successful else None, dns_error or message)
            self._attempts.create(attempt)
            if successful:
                self._history.create(PriceRecord(offer.id, price_minor, currency, timestamp))
            self._connection.commit()
            return OperationResult("CREATED", offer=offer, check=check)
        except Exception:
            self._connection.rollback()
            raise


def _attempt(offer_id: int, timestamp: str, check: PriceCheckResult | None, price: int | None, currency: str | None, error: str | None) -> CheckAttempt:
    if check is None:
        from pricewatch.domain.enums import FetchStatus
        return CheckAttempt(offer_id, timestamp, FetchStatus.NETWORK_ERROR, ExtractionStatus.NOT_RUN, error_message=error)
    return CheckAttempt(
        offer_id, timestamp, check.fetch.status, check.extraction.status,
        fetch_method=check.fetch.method, http_status=check.fetch.http_status,
        extraction_method=check.extraction.method, confidence_score=check.extraction.confidence_score,
        detected_price_minor=price, currency=currency, elapsed_time_ms=check.fetch.elapsed_time_ms,
        error_message=error or check.fetch.error_message,
    )


def _initial_price(check: PriceCheckResult | None) -> tuple[bool, int | None, str | None, str | None]:
    if not check or check.extraction.status is not ExtractionStatus.SUCCESS:
        return False, None, None, "initial price was not reliably extracted"
    currency = (check.extraction.currency or "").strip().upper()
    if len(currency) != 3 or not currency.isalpha():
        return False, None, None, "initial price has invalid currency"
    try:
        price = check.extraction.price
        if price is None or not isinstance(price, Decimal) or price <= 0:
            return False, None, None, "initial price is not positive"
        minor = int((price * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError):
        return False, None, None, "initial price is invalid"
    if minor <= 0 or minor > 9_223_372_036_854_775_807:
        return False, None, None, "initial price exceeds SQLite INTEGER"
    return True, minor, currency, None


def _temporary_dns_error(error: str) -> bool:
    return error in {"hostname cannot be resolved safely", "hostname has no resolved addresses"}


def _name(value: str) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
