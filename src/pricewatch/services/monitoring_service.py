"""Регулярная проверка Offer и решения о будущих уведомлениях."""
from __future__ import annotations
import json
import logging
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from enum import Enum
from pricewatch.database.repositories.check_attempt_repository import CheckAttemptRepository
from pricewatch.database.repositories.notification_outbox_repository import NotificationOutboxRepository
from pricewatch.database.repositories.offer_repository import OfferRepository
from pricewatch.database.repositories.price_history_repository import PriceHistoryRepository
from pricewatch.database.repositories.product_repository import ProductRepository
from pricewatch.database.repositories.user_repository import UserRepository
from pricewatch.domain.enums import AnomalyDecision, ExtractionStatus, FetchStatus, NotificationEventType, OfferStatus
from pricewatch.domain.models import CheckAttempt, NotificationOutbox, PriceRecord

LOG = logging.getLogger(__name__)
_MONITORING_RUN_ID: ContextVar[str | None] = ContextVar(
    "pricewatch_monitoring_run_id", default=None
)

class EventType(str, Enum):
    PRICE_DROP = "PRICE_DROP"; ANOMALY_DETECTED = "ANOMALY_DETECTED"; REPEATED_ERROR = "REPEATED_ERROR"
@dataclass(frozen=True)
class MonitoringEvent:
    type: EventType; user_id: int; product_id: int; offer_id: int; url: str; timestamp: str
    previous_price_minor: int | None = None; new_price_minor: int | None = None; currency: str | None = None
    drop_percent: Decimal | None = None; reason: str | None = None
@dataclass(frozen=True)
class MonitoringResult:
    code: str; offer_id: int; events: tuple[MonitoringEvent, ...] = (); message: str = ""
@dataclass(frozen=True)
class AnomalyNotificationValidation:
    code: str
@dataclass(frozen=True)
class BatchResult:
    results: tuple[MonitoringResult, ...]
    @property
    def events(self): return tuple(event for result in self.results for event in result.events)

class MonitoringService:
    def __init__(self, connection: sqlite3.Connection, price_checker) -> None:
        self._connection, self._checker = connection, price_checker
        self._offers, self._products, self._users = OfferRepository(connection), ProductRepository(connection), UserRepository(connection)
        self._history, self._attempts = PriceHistoryRepository(connection), CheckAttemptRepository(connection)
        self._outbox = NotificationOutboxRepository(connection)
    def check_for_user(self, user_id: int, offer_id: int) -> MonitoringResult:
        offer = self._offers.get_for_user(offer_id, user_id)
        return self._check(offer, user_id) if offer else MonitoringResult("NOT_FOUND", offer_id)
    def check_all(self) -> BatchResult:
        results = []
        for offer in self._offers.list_for_monitoring():
            product = self._products.get_internal(offer.product_id)
            if not product: continue
            results.append(self._check(offer, product.user_id))
        return BatchResult(tuple(results))
    def confirm_anomaly(self, user_id: int, offer_id: int, expected_pending_check_attempt_id: int) -> MonitoringResult:
        return self._resolve(user_id, offer_id, expected_pending_check_attempt_id, AnomalyDecision.CONFIRMED)
    def reject_anomaly(self, user_id: int, offer_id: int, expected_pending_check_attempt_id: int) -> MonitoringResult:
        return self._resolve(user_id, offer_id, expected_pending_check_attempt_id, AnomalyDecision.REJECTED)
    def validate_anomaly_notification(self, user_id: int, chat_id: int, telegram_message_id: int, offer_id: int, expected_pending_check_attempt_id: int) -> AnomalyNotificationValidation:
        """Проверяет происхождение callback без изменения monitoring state."""
        user = self._users.get_by_id(user_id)
        if not user or user.telegram_user_id != chat_id:
            return AnomalyNotificationValidation("INVALID")
        notification = self._outbox.get_sent_by_telegram_message(chat_id, telegram_message_id)
        if not notification or notification.event_type is not NotificationEventType.ANOMALY_DETECTED:
            return AnomalyNotificationValidation("INVALID")
        try:
            payload = json.loads(notification.payload_json)
        except (TypeError, json.JSONDecodeError):
            return AnomalyNotificationValidation("INVALID")
        if not isinstance(payload, dict) or any((
            payload.get("version") != 1,
            payload.get("event_type") != NotificationEventType.ANOMALY_DETECTED.value,
            payload.get("chat_id") != chat_id,
            payload.get("offer_id") != offer_id,
            payload.get("pending_check_attempt_id") != expected_pending_check_attempt_id,
            notification.offer_id not in (None, offer_id),
        )):
            return AnomalyNotificationValidation("INVALID")
        if notification.offer_id is None:
            return AnomalyNotificationValidation("STALE")
        offer = self._offers.get_for_user(offer_id, user_id)
        if not offer or offer.pending_price_minor is None or offer.pending_check_attempt_id != expected_pending_check_attempt_id:
            return AnomalyNotificationValidation("STALE")
        return AnomalyNotificationValidation("VALID")
    def _check(self, offer, user_id: int) -> MonitoringResult:
        run_id = _current_run_id() or _new_run_id()
        started = time.monotonic()
        LOG.info(
            "event=offer_check_started outcome=started run_id=%s offer_id=%s "
            "product_id=%s previous_status=%s",
            run_id, offer.id, offer.product_id, offer.status.value,
        )
        check = None
        try:
            product = self._products.get_internal(offer.product_id)
            if (
                not product or not product.is_active or not offer.is_active
                or offer.status is OfferStatus.DISABLED
            ):
                result = MonitoringResult("SKIPPED", offer.id)
            else:
                try:
                    check = self._checker.check(offer.normalized_url)  # сеть строго до write transaction
                except Exception as error:
                    # The checker owns no SQLite transaction. An unexpected
                    # fetch/extraction failure is local to this Offer and cannot
                    # safely be represented as one of the persisted fetch enums.
                    LOG.error(
                        "event=offer_check_failed outcome=internal_error run_id=%s "
                        "offer_id=%s product_id=%s duration_ms=%s "
                        "previous_status=%s exception_class=%s",
                        run_id, offer.id, offer.product_id, _elapsed_ms(started),
                        offer.status.value, type(error).__name__,
                    )
                    return MonitoringResult("INTERNAL_ERROR", offer.id)
                result = self._persist(offer.id, user_id, check, offer.state_version)
        except Exception as error:
            LOG.error(
                "event=offer_check_failed outcome=error run_id=%s offer_id=%s "
                "product_id=%s duration_ms=%s previous_status=%s exception_class=%s",
                run_id, offer.id, offer.product_id, _elapsed_ms(started),
                offer.status.value, type(error).__name__,
            )
            raise
        for event in result.events:
            LOG.info(
                "event=event_created outcome=committed run_id=%s event_type=%s "
                "product_id=%s offer_id=%s",
                run_id, event.type.value, event.product_id, event.offer_id,
            )
        _log_offer_completed(run_id, started, offer, result, check)
        return result
    def _persist(self, offer_id, user_id, check, expected_version) -> MonitoringResult:
        now = _now(); valid, price, currency, reason = _price(check)
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            offer = self._offers.get_internal(offer_id); product = self._products.get_internal(offer.product_id) if offer else None
            if not offer or not product or product.user_id != user_id or not offer.is_active or not product.is_active:
                self._connection.rollback(); return MonitoringResult("SKIPPED", offer_id)
            if offer.state_version != expected_version:
                self._connection.rollback(); return MonitoringResult("STALE", offer_id)
            user = self._users.get_by_id(user_id)
            if not user:
                self._connection.rollback(); return MonitoringResult("SKIPPED", offer_id)
            previous = self._history.latest_for_offer(offer.id)
            if valid and offer.currency and offer.currency != currency:
                valid, reason = False, "currency changed"
            attempt = CheckAttempt(offer.id, now, check.fetch.status, check.extraction.status, fetch_method=check.fetch.method, http_status=check.fetch.http_status, extraction_method=check.extraction.method, confidence_score=check.extraction.confidence_score, detected_price_minor=price if valid else None, currency=currency if valid else None, elapsed_time_ms=check.fetch.elapsed_time_ms, error_message=reason or check.fetch.error_message)
            if valid and previous and _anomaly(previous.price_minor, price):
                attempt = CheckAttempt(**{**attempt.__dict__, "is_suspicious": True})
                attempt = self._attempts.create(attempt)
                changed = offer.pending_price_minor != price
                if changed:
                    self._offers.update_check_state(offer.id, status=OfferStatus.SUSPICIOUS, pending_price_minor=price, pending_price_detected_at=now, pending_check_attempt_id=attempt.id, last_checked_at=now)
                else:
                    self._offers.update_check_state(offer.id, last_checked_at=now)
                event = (MonitoringEvent(EventType.ANOMALY_DETECTED, user_id, product.id, offer.id, offer.url, now, previous.price_minor, price, currency),) if changed else ()
                if event:
                    self._enqueue_event(
                        event[0], f"ANOMALY_DETECTED:check_attempt:{attempt.id}",
                        product, offer, user, pending_check_attempt_id=attempt.id,
                    )
                self._connection.commit()
                return MonitoringResult("SUSPICIOUS", offer.id, event)
            attempt = self._attempts.create(attempt)
            if valid:
                price_record = self._history.create(PriceRecord(offer.id, price, currency, now))
                self._offers.update_check_state(offer.id, status=OfferStatus.ACTIVE, current_price_minor=price, currency=currency, consecutive_errors=0, error_notified=False, last_checked_at=now, last_success_at=now, pending_price_minor=None, pending_price_detected_at=None, pending_check_attempt_id=None)
                if previous and _drop(previous.price_minor, price, _threshold(product.alert_threshold, user.alert_threshold)):
                    percent = Decimal(previous.price_minor - price) * 100 / Decimal(previous.price_minor)
                    event = MonitoringEvent(EventType.PRICE_DROP, user_id, product.id, offer.id, offer.url, now, previous.price_minor, price, currency, percent)
                    self._enqueue_event(event, f"PRICE_DROP:price_history:{price_record.id}", product, offer, user)
                    self._connection.commit()
                    return MonitoringResult("SUCCESS", offer.id, (event,))
                self._connection.commit()
                return MonitoringResult("SUCCESS", offer.id)
            if offer.status is OfferStatus.SUSPICIOUS:
                self._connection.commit(); return MonitoringResult("PENDING", offer.id, message=reason or "check failed while anomaly is pending")
            errors = offer.consecutive_errors + 1
            has_baseline = previous is not None
            if not has_baseline:
                status = OfferStatus.PENDING
            elif errors >= 3:
                status = OfferStatus.ERROR
            else:
                status = OfferStatus.ACTIVE
            notify = errors >= 3 and not offer.error_notified
            self._offers.update_check_state(offer.id, status=status, consecutive_errors=errors, error_notified=notify or offer.error_notified, last_checked_at=now)
            event = (MonitoringEvent(EventType.REPEATED_ERROR, user_id, product.id, offer.id, offer.url, now, reason=reason or check.fetch.error_message or "price unavailable"),) if notify else ()
            if event:
                self._enqueue_event(
                    event[0], f"REPEATED_ERROR:check_attempt:{attempt.id}",
                    product, offer, user,
                )
            self._connection.commit()
            return MonitoringResult("ERROR" if status is OfferStatus.ERROR else "PENDING", offer.id, event)
        except Exception:
            self._connection.rollback(); raise
    def _enqueue_event(self, event, event_key, product, offer, user, pending_check_attempt_id=None) -> None:
        payload = {
            "version": 1,
            "event_type": event.type.value,
            "detected_at": event.timestamp,
            "product_id": product.id,
            "offer_id": offer.id,
            "product_name": product.name,
            "source": offer.store_name or offer.domain or offer.source_title or offer.url,
            "url": offer.url,
            "chat_id": user.telegram_user_id,
            "previous_price_minor": event.previous_price_minor,
            "new_price_minor": event.new_price_minor,
            "currency": event.currency,
            "drop_percent": str(event.drop_percent) if event.drop_percent is not None else None,
            "reason": event.reason,
            "pending_check_attempt_id": pending_check_attempt_id,
        }
        self._outbox.enqueue(NotificationOutbox(
            event_key=event_key,
            event_type=NotificationEventType(event.type.value),
            offer_id=offer.id,
            chat_id=user.telegram_user_id,
            payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            next_attempt_at=event.timestamp,
            created_at=event.timestamp,
        ))
    def _resolve(self, user_id, offer_id, expected_attempt_id, decision) -> MonitoringResult:
        now = _now()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            offer = self._offers.get_for_user(offer_id, user_id)
            if not offer or offer.pending_price_minor is None or offer.pending_check_attempt_id is None:
                self._connection.rollback(); return MonitoringResult("NOT_FOUND", offer_id)
            if offer.pending_check_attempt_id != expected_attempt_id:
                self._connection.rollback(); return MonitoringResult("STALE", offer_id)
            attempt = self._attempts.get_for_user(offer.pending_check_attempt_id, user_id)
            if not attempt or attempt.offer_id != offer.id:
                self._connection.rollback(); return MonitoringResult("NOT_FOUND", offer_id)
            self._attempts.set_anomaly_decision(attempt.id, user_id, decision, now)
            if decision is AnomalyDecision.CONFIRMED:
                self._history.create(PriceRecord(offer.id, offer.pending_price_minor, offer.currency, offer.pending_price_detected_at))
                self._offers.update_check_state(offer.id, status=OfferStatus.ACTIVE, current_price_minor=offer.pending_price_minor, consecutive_errors=0, error_notified=False, last_success_at=offer.pending_price_detected_at, pending_price_minor=None, pending_price_detected_at=None, pending_check_attempt_id=None)
            else:
                status = OfferStatus.ACTIVE if offer.current_price_minor is not None else OfferStatus.PENDING
                self._offers.update_check_state(offer.id, status=status, pending_price_minor=None, pending_price_detected_at=None, pending_check_attempt_id=None)
            self._connection.commit(); return MonitoringResult("CONFIRMED" if decision is AnomalyDecision.CONFIRMED else "REJECTED", offer_id)
        except Exception:
            self._connection.rollback(); raise
def _price(check):
    if check.fetch.status is not FetchStatus.SUCCESS or check.extraction.status is not ExtractionStatus.SUCCESS: return False, None, None, "price was not reliably extracted"
    price, currency = check.extraction.price, (check.extraction.currency or "").upper()
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0 or len(currency) != 3 or not currency.isalpha(): return False, None, None, "invalid price or currency"
    try: minor = int((price * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, ValueError, OverflowError): return False, None, None, "invalid price"
    return (True, minor, currency, None) if 0 < minor <= 9_223_372_036_854_775_807 else (False, None, None, "price outside SQLite INTEGER")
def _anomaly(old, new): return abs(new - old) * 2 >= old
def _drop(old, new, threshold): return new < old and Decimal(old - new) * 100 > Decimal(old) * threshold
def _threshold(product, user): return Decimal(str(product if product is not None else user))
def _now(): return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log_offer_completed(run_id, started, offer, result, check) -> None:
    fetch_status = check.fetch.status.value if check is not None else None
    extraction_status = check.extraction.status.value if check is not None else None
    http_status = check.fetch.http_status if check is not None else None
    outcome = {
        "SUCCESS": "success", "PENDING": "pending", "ERROR": "error",
        "SUSPICIOUS": "suspicious", "STALE": "stale", "SKIPPED": "skipped",
        "NOT_FOUND": "not_found",
    }.get(result.code, "other")
    if result.code == "SUCCESS":
        new_status, consecutive_errors = OfferStatus.ACTIVE.value, 0
    elif result.code == "SUSPICIOUS":
        new_status, consecutive_errors = OfferStatus.SUSPICIOUS.value, offer.consecutive_errors
    elif result.code == "ERROR":
        new_status, consecutive_errors = OfferStatus.ERROR.value, offer.consecutive_errors + 1
    elif result.code == "PENDING" and offer.status is OfferStatus.SUSPICIOUS:
        new_status, consecutive_errors = OfferStatus.SUSPICIOUS.value, offer.consecutive_errors
    elif result.code == "PENDING" and offer.current_price_minor is None:
        new_status, consecutive_errors = OfferStatus.PENDING.value, offer.consecutive_errors + 1
    elif result.code == "PENDING":
        new_status, consecutive_errors = OfferStatus.ACTIVE.value, offer.consecutive_errors + 1
    else:
        new_status, consecutive_errors = offer.status.value, offer.consecutive_errors
    level = (
        logging.WARNING
        if result.code in {"PENDING", "ERROR", "STALE"}
        or (check is not None and (
            check.fetch.status is not FetchStatus.SUCCESS
            or check.extraction.status is not ExtractionStatus.SUCCESS
        ))
        else logging.INFO
    )
    LOG.log(
        level,
        "event=offer_check_completed outcome=%s run_id=%s offer_id=%s "
        "product_id=%s duration_ms=%s http_status=%s fetch_status=%s "
        "extraction_status=%s previous_status=%s new_status=%s "
        "consecutive_errors=%s",
        outcome, run_id, offer.id, offer.product_id, _elapsed_ms(started),
        http_status, fetch_status, extraction_status, offer.status.value,
        new_status, consecutive_errors,
    )


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _current_run_id() -> str | None:
    return _MONITORING_RUN_ID.get()


@contextmanager
def _monitoring_run(run_id: str) -> Iterator[None]:
    token = _MONITORING_RUN_ID.set(run_id)
    try:
        yield
    finally:
        _MONITORING_RUN_ID.reset(token)


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
