"""Пользовательский lifecycle Product и Offer."""
from __future__ import annotations
import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.parse import urlsplit
from pricewatch.database.repositories.check_attempt_repository import CheckAttemptRepository
from pricewatch.database.repositories.offer_repository import OfferRepository
from pricewatch.database.repositories.price_history_repository import PriceHistoryRepository
from pricewatch.database.repositories.product_repository import ProductRepository
from pricewatch.database.repositories.user_repository import UserRepository
from pricewatch.domain.enums import ExtractionStatus, FetchStatus, OfferStatus
from pricewatch.domain.models import CheckAttempt, Offer, PriceRecord, Product, User
from pricewatch.scraping.models import PriceCheckResult
from pricewatch.scraping.url_security import UrlSecurity
from pricewatch.services.product_names import normalize_product_name, product_name_comparison_key
from pricewatch.services.url_normalization import UrlNormalizationError, normalize_url

LOG = logging.getLogger(__name__)
@dataclass(frozen=True)
class OperationResult:
    code: str; message: str = ""; product: Product | None = None; offer: Offer | None = None; duplicate_offer: Offer | None = None; check: PriceCheckResult | None = None
@dataclass(frozen=True)
class OfferPreview:
    """Validated, already checked offer input that can be persisted without more HTTP."""
    code: str
    url: str
    normalized_url: str | None = None
    message: str = ""
    check: PriceCheckResult | None = None
    dns_error: str | None = None
    duplicate_offer: Offer | None = None
    previewed_at: str | None = None
    operation_id: str | None = None

    @property
    def source_title(self) -> str | None:
        if not self.check or self.check.fetch.status is not FetchStatus.SUCCESS:
            return None
        title = self.check.extraction.title
        cleaned = title.strip() if isinstance(title, str) else ""
        return cleaned if cleaned and len(cleaned) <= 200 else None
@dataclass(frozen=True)
class ProductDetails:
    product: Product; offers: tuple[Offer, ...]
class ProductService:
    def __init__(self, connection: sqlite3.Connection, price_checker, url_security: UrlSecurity | None = None) -> None:
        self._connection, self._checker, self._security = connection, price_checker, url_security or UrlSecurity()
        self._users, self._products, self._offers = UserRepository(connection), ProductRepository(connection), OfferRepository(connection)
        self._history, self._attempts = PriceHistoryRepository(connection), CheckAttemptRepository(connection)
    def get_or_create_user(self, telegram_user_id: int, username: str | None = None) -> User:
        if existing := self._users.get_by_telegram_user_id(telegram_user_id): return existing
        try:
            with self._connection: return self._users.create(User(telegram_user_id, _now(), username=username))
        except sqlite3.IntegrityError:
            if user := self._users.get_by_telegram_user_id(telegram_user_id): return user
            raise
    def create_product(self, user_id: int, name: str) -> OperationResult:
        if not (name := _name(name)): return OperationResult("INVALID", "product name is empty")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if _duplicate_product_name(self._products.list_for_user(user_id), name):
                self._connection.rollback(); return OperationResult("DUPLICATE", "product name already exists")
            product = self._products.create(Product(user_id, name, _now()))
            self._connection.commit()
            return OperationResult("CREATED", product=product)
        except sqlite3.IntegrityError:
            self._connection.rollback()
            if _duplicate_product_name(self._products.list_for_user(user_id), name): return OperationResult("DUPLICATE", "product name already exists")
            raise
        except Exception:
            self._connection.rollback(); raise
    def create_product_with_offer(self, user_id: int, name: str, url: str) -> OperationResult:
        """Validate/check outside a write transaction, then atomically create both rows."""
        if not (name := _name(name)):
            return OperationResult("INVALID", "product name is empty or too long")
        if _duplicate_product_name(self._products.list_for_user(user_id), name):
            return OperationResult("DUPLICATE", "product name already exists")
        preview = self.preview_offer(user_id, url)
        if preview.code != "READY":
            return _preview_result(preview)
        return self.create_product_with_preview(user_id, name, preview)

    def preview_offer(self, user_id: int, url: str, product_id: int | None = None) -> OfferPreview:
        """Validate and check a URL without changing SQLite.

        The returned object is intentionally short lived. Final persistence repeats
        ownership and duplicate checks in the write transaction, but reuses the
        scrape result so the confirmation flow performs only one HTTP request.
        """
        operation_id = _new_operation_id()
        started = time.monotonic()
        LOG.info(
            "event=initial_offer_check_started outcome=started operation_id=%s "
            "product_id=%s",
            operation_id, product_id,
        )
        if product_id is not None and not self._products.get_for_user(product_id, user_id):
            return _complete_preview(
                OfferPreview(
                    "NOT_FOUND", url, message="product not found",
                    operation_id=operation_id,
                ),
                started, product_id,
            )
        try:
            normalized = normalize_url(url)
        except UrlNormalizationError as error:
            return _complete_preview(
                OfferPreview(
                    "INVALID", url, message=str(error), operation_id=operation_id,
                ),
                started, product_id,
            )
        if duplicate := self._offers.find_duplicate_for_user(normalized, user_id):
            return _complete_preview(
                OfferPreview(
                    "DUPLICATE", url, normalized, duplicate_offer=duplicate,
                    operation_id=operation_id,
                ),
                started, product_id,
            )
        error = self._security.validate(normalized)
        if error and error not in {"hostname cannot be resolved safely", "hostname has no resolved addresses"}:
            return _complete_preview(
                OfferPreview(
                    "BLOCKED", url, normalized, message=error,
                    operation_id=operation_id,
                ),
                started, product_id,
            )
        try:
            check = None if error else self._checker.check(normalized)
        except Exception as exception:
            LOG.error(
                "event=initial_offer_check_failed outcome=error operation_id=%s "
                "product_id=%s duration_ms=%s exception_class=%s",
                operation_id, product_id, _elapsed_ms(started),
                type(exception).__name__,
            )
            raise
        return _complete_preview(
            OfferPreview(
                "READY", url, normalized, check=check, dns_error=error,
                previewed_at=_now(), operation_id=operation_id,
            ),
            started, product_id,
        )

    def create_product_with_preview(self, user_id: int, name: str, preview: OfferPreview) -> OperationResult:
        """Atomically create Product, Offer, CheckAttempt and optional PriceHistory."""
        if preview.code != "READY" or not preview.normalized_url:
            return _preview_result(preview)
        if not _fresh_preview(preview):
            return OperationResult("STALE", "offer preview expired")
        if not (name := _name(name)):
            return OperationResult("INVALID", "product name is empty")
        timestamp = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if not self._users.get_by_id(user_id):
                self._connection.rollback(); return OperationResult("NOT_FOUND")
            if _duplicate_product_name(self._products.list_for_user(user_id), name):
                self._connection.rollback(); return OperationResult("DUPLICATE", "product name already exists")
            if duplicate := self._offers.find_duplicate_for_user(preview.normalized_url, user_id):
                self._connection.rollback(); return OperationResult("DUPLICATE", duplicate_offer=duplicate)
            product = self._products.create(Product(user_id, name, timestamp))
            result = self._persist_in_transaction(
                user_id, product.id, preview.url, preview.normalized_url,
                preview.check, preview.dns_error, timestamp,
            )
            if result.code != "CREATED":
                self._connection.rollback()
                return result
            self._connection.commit()
            completed = OperationResult(
                result.code, result.message, product=product, offer=result.offer,
                duplicate_offer=result.duplicate_offer, check=result.check,
            )
            _log_initial_persisted(completed, preview.operation_id)
            return completed
        except sqlite3.IntegrityError:
            self._connection.rollback()
            if duplicate := self._offers.find_duplicate_for_user(preview.normalized_url, user_id):
                return OperationResult("DUPLICATE", duplicate_offer=duplicate)
            if _duplicate_product_name(self._products.list_for_user(user_id), name):
                return OperationResult("DUPLICATE", "product name already exists")
            _log_initial_persistence_failed(preview.operation_id, None)
            raise
        except Exception as error:
            self._connection.rollback()
            _log_initial_persistence_failed(preview.operation_id, None, error)
            raise
    def rename_product(self, user_id: int, product_id: int, name: str) -> OperationResult:
        if not (name := _name(name)): return OperationResult("INVALID", "product name is empty")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if not self._products.get_for_user(product_id, user_id):
                self._connection.rollback(); return OperationResult("NOT_FOUND")
            if _duplicate_product_name(self._products.list_for_user(user_id), name, product_id):
                self._connection.rollback(); return OperationResult("DUPLICATE", "product name already exists")
            if not self._products.rename(product_id, user_id, name):
                self._connection.rollback(); return OperationResult("NOT_FOUND")
            product = self._products.get_for_user(product_id, user_id)
            self._connection.commit()
            return OperationResult("UPDATED", product=product)
        except sqlite3.IntegrityError:
            self._connection.rollback()
            if _duplicate_product_name(self._products.list_for_user(user_id), name, product_id): return OperationResult("DUPLICATE", "product name already exists")
            raise
        except Exception:
            self._connection.rollback(); raise
    def delete_product(self, user_id: int, product_id: int) -> OperationResult:
        with self._connection: return OperationResult("DELETED" if self._products.delete(product_id, user_id) else "NOT_FOUND")
    def set_user_threshold(self, user_id: int, threshold: float) -> OperationResult:
        if not _threshold(threshold): return OperationResult("INVALID", "threshold must be between 0 and 100")
        with self._connection:
            return OperationResult("UPDATED" if self._users.update_alert_threshold(user_id, threshold) else "NOT_FOUND")
    def get_user(self, user_id: int) -> User | None:
        return self._users.get_by_id(user_id)
    def set_product_threshold(self, user_id: int, product_id: int, threshold: float | None) -> OperationResult:
        if threshold is not None and not _threshold(threshold): return OperationResult("INVALID", "threshold must be between 0 and 100")
        with self._connection:
            return OperationResult("UPDATED" if self._products.update_alert_threshold(product_id, user_id, threshold) else "NOT_FOUND")
    def list_products(self, user_id: int) -> tuple[Product, ...]: return tuple(self._products.list_for_user(user_id))
    def find_products_by_name(self, user_id: int, name: str) -> tuple[Product, ...]:
        key = product_name_comparison_key(name)
        if not key: return ()
        return tuple(
            product for product in self._products.list_for_user(user_id)
            if product_name_comparison_key(product.name) == key
        )
    def get_product_details(self, user_id: int, product_id: int) -> ProductDetails | None:
        product = self._products.get_for_user(product_id, user_id)
        return ProductDetails(product, tuple(self._offers.list_for_product(product_id, user_id))) if product else None
    def get_offer(self, user_id: int, offer_id: int) -> Offer | None:
        return self._offers.get_for_user(offer_id, user_id)
    def list_offer_history(self, user_id: int, offer_id: int, limit: int = 10) -> tuple[PriceRecord, ...]:
        return tuple(self._history.list_for_offer(offer_id, user_id, limit))
    def delete_offer(self, user_id: int, offer_id: int) -> OperationResult:
        with self._connection: return OperationResult("DELETED" if self._offers.delete(offer_id, user_id) else "NOT_FOUND")
    def add_offer(self, user_id: int, product_id: int, url: str) -> OperationResult:
        preview = self.preview_offer(user_id, url, product_id)
        if preview.code != "READY": return _preview_result(preview)
        return self.add_offer_with_preview(user_id, product_id, preview)
    def add_offer_with_preview(self, user_id: int, product_id: int, preview: OfferPreview) -> OperationResult:
        if preview.code != "READY" or not preview.normalized_url: return _preview_result(preview)
        if not _fresh_preview(preview): return OperationResult("STALE", "offer preview expired")
        return self._persist(
            user_id, product_id, preview.url, preview.normalized_url,
            preview.check, preview.dns_error, preview.operation_id,
        )
    def _persist(self, user_id, product_id, url, normalized, check, dns_error, operation_id=None) -> OperationResult:
        timestamp = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            if not self._products.get_for_user(product_id, user_id):
                self._connection.rollback(); return OperationResult("NOT_FOUND")
            if duplicate := self._offers.find_duplicate_for_user(normalized, user_id):
                self._connection.rollback(); return OperationResult("DUPLICATE", duplicate_offer=duplicate)
            result = self._persist_in_transaction(user_id, product_id, url, normalized, check, dns_error, timestamp)
            self._connection.commit()
            _log_initial_persisted(result, operation_id)
            return result
        except sqlite3.IntegrityError as error:
            self._connection.rollback()
            if duplicate := self._offers.find_duplicate_for_user(normalized, user_id):
                return OperationResult("DUPLICATE", duplicate_offer=duplicate)
            _log_initial_persistence_failed(operation_id, product_id, error)
            raise
        except Exception as error:
            self._connection.rollback()
            _log_initial_persistence_failed(operation_id, product_id, error)
            raise
    def _persist_in_transaction(self, user_id, product_id, url, normalized, check, dns_error, timestamp) -> OperationResult:
        """Write an initial Offer graph; caller owns the active transaction."""
        if not self._products.get_for_user(product_id, user_id): return OperationResult("NOT_FOUND")
        ok, minor, currency, message = _price(check)
        offer = self._offers.create(Offer(product_id, url, normalized, timestamp, domain=urlsplit(normalized).hostname, source_title=_source_title(check), current_price_minor=minor if ok else None, currency=currency if ok else None, preferred_fetch_method=check.fetch.method if check else None, status=OfferStatus.ACTIVE if ok else OfferStatus.PENDING, last_checked_at=timestamp, last_success_at=timestamp if ok else None))
        if check is None: attempt = CheckAttempt(offer.id, timestamp, FetchStatus.NETWORK_ERROR, ExtractionStatus.NOT_RUN, error_message=dns_error)
        else: attempt = CheckAttempt(offer.id, timestamp, check.fetch.status, check.extraction.status, fetch_method=check.fetch.method, http_status=check.fetch.http_status, extraction_method=check.extraction.method, confidence_score=check.extraction.confidence_score, detected_price_minor=minor if ok else None, currency=currency if ok else None, elapsed_time_ms=check.fetch.elapsed_time_ms, error_message=message or check.fetch.error_message)
        self._attempts.create(attempt)
        if ok: self._history.create(PriceRecord(offer.id, minor, currency, timestamp))
        return OperationResult("CREATED", offer=offer, check=check)

def _preview_result(preview: OfferPreview) -> OperationResult:
    return OperationResult(preview.code, preview.message, duplicate_offer=preview.duplicate_offer, check=preview.check)
def _source_title(check):
    if not check or not isinstance(check.extraction.title, str): return None
    title = check.extraction.title.strip()
    return title[:200] if title else None
def _fresh_preview(preview):
    try:
        created = datetime.fromisoformat(preview.previewed_at)
    except (TypeError, ValueError):
        return False
    if created.tzinfo is None:
        return False
    now = datetime.now(timezone.utc)
    return now - timedelta(minutes=15) <= created <= now + timedelta(seconds=5)
def _price(check):
    if not check or check.fetch.status is not FetchStatus.SUCCESS or check.extraction.status is not ExtractionStatus.SUCCESS: return False, None, None, "initial price was not reliably extracted"
    currency = (check.extraction.currency or "").strip().upper(); price = check.extraction.price
    if len(currency) != 3 or not currency.isalpha() or not isinstance(price, Decimal) or not price.is_finite() or price <= 0: return False, None, None, "initial price is invalid"
    try: minor = int((price * Decimal("100")).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError): return False, None, None, "initial price is invalid"
    return (True, minor, currency, None) if 0 < minor <= 9_223_372_036_854_775_807 else (False, None, None, "initial price exceeds SQLite INTEGER")
def _name(value):
    cleaned = normalize_product_name(value)
    return cleaned if cleaned and len(cleaned) <= 200 else None
def _threshold(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 100
def _duplicate_product_name(products, name, exclude_id=None):
    key = product_name_comparison_key(name)
    return any(
        product.id != exclude_id and product_name_comparison_key(product.name) == key
        for product in products
    )
def _now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _complete_preview(preview: OfferPreview, started: float, product_id: int | None) -> OfferPreview:
    check = preview.check
    fetch_status = check.fetch.status.value if check is not None else (
        FetchStatus.NETWORK_ERROR.value if preview.dns_error else None
    )
    extraction_status = check.extraction.status.value if check is not None else (
        ExtractionStatus.NOT_RUN.value if preview.dns_error else None
    )
    http_status = check.fetch.http_status if check is not None else None
    if preview.code == "READY" and check is not None and (
        check.fetch.status is FetchStatus.SUCCESS
        and check.extraction.status is ExtractionStatus.SUCCESS
    ):
        outcome, level = "success", logging.INFO
    elif preview.code == "DUPLICATE":
        outcome, level = "duplicate", logging.INFO
    else:
        outcome, level = {
            "READY": "degraded", "NOT_FOUND": "not_found",
            "INVALID": "invalid", "BLOCKED": "blocked",
        }.get(preview.code, "other"), logging.WARNING
    LOG.log(
        level,
        "event=initial_offer_check_completed outcome=%s operation_id=%s "
        "product_id=%s duration_ms=%s http_status=%s fetch_status=%s "
        "extraction_status=%s",
        outcome, preview.operation_id, product_id, _elapsed_ms(started),
        http_status, fetch_status, extraction_status,
    )
    return preview


def _log_initial_persisted(result: OperationResult, operation_id: str | None) -> None:
    if result.code != "CREATED" or result.offer is None:
        return
    check = result.check
    LOG.info(
        "event=initial_offer_persisted outcome=%s operation_id=%s product_id=%s "
        "offer_id=%s http_status=%s fetch_status=%s extraction_status=%s "
        "new_status=%s",
        "active" if result.offer.status is OfferStatus.ACTIVE else "pending",
        operation_id, result.offer.product_id, result.offer.id,
        check.fetch.http_status if check is not None else None,
        check.fetch.status.value if check is not None else FetchStatus.NETWORK_ERROR.value,
        check.extraction.status.value if check is not None else ExtractionStatus.NOT_RUN.value,
        result.offer.status.value,
    )


def _log_initial_persistence_failed(
    operation_id: str | None,
    product_id: int | None,
    error: BaseException | None = None,
) -> None:
    LOG.error(
        "event=initial_offer_persistence_failed outcome=error operation_id=%s "
        "product_id=%s exception_class=%s",
        operation_id, product_id,
        type(error).__name__ if error is not None else "IntegrityError",
    )


def _new_operation_id() -> str:
    return uuid.uuid4().hex[:12]


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
