"""Один цикл надёжной доставки событий SQLite Outbox."""

from __future__ import annotations

import logging
import math
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Protocol

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from pricewatch.async_utils import run_in_daemon_thread
from pricewatch.database.connection import connect
from pricewatch.database.repositories.notification_outbox_repository import NotificationOutboxRepository
from pricewatch.services.notification_formatter import (
    InvalidNotificationPayload,
    PreparedNotification,
    format_notification,
)


LOG = logging.getLogger(__name__)
LEASE_SECONDS = 300
MAX_MESSAGES_PER_CYCLE = 20
MAX_UNKNOWN_DELIVERY_ATTEMPTS = 5


class Sender(Protocol):
    async def send(self, chat_id: int, text: str, reply_markup: object = None) -> int: ...


@dataclass(frozen=True)
class OutboxRunResult:
    recovered: int = 0
    sent: int = 0
    rescheduled: int = 0
    failed: int = 0
    skipped: int = 0


class OutboxWorker:
    def __init__(
        self,
        database_path: str | Path,
        sender: Sender,
        *,
        clock: Callable[[], datetime] | None = None,
        formatter: Callable[[str], PreparedNotification] = format_notification,
    ) -> None:
        self._database_path = database_path
        self._sender = sender
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._formatter = formatter

    def recover_expired(self) -> int:
        now = self._now()
        connection = connect(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            recovered = NotificationOutboxRepository(connection).recover_expired(_timestamp(now))
            connection.commit()
            if recovered:
                LOG.info(
                    "event=outbox_lease_recovered outcome=success recovered=%s",
                    recovered,
                )
            return recovered
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def process_ready(self) -> OutboxRunResult:
        ready = await run_in_daemon_thread(self._list_ready)
        sent = rescheduled = failed = skipped = 0
        for candidate in ready:
            claimed = await run_in_daemon_thread(self._claim, candidate.id)
            if claimed is None:
                skipped += 1
                continue
            started = time.monotonic()
            try:
                prepared = self._formatter(claimed.payload_json)
                message_id = await self._sender.send(
                    claimed.chat_id, prepared.text, prepared.reply_markup
                )
            except sqlite3.Error:
                raise
            except Exception as error:
                terminal_reason = _terminal_failure_reason(
                    error, claimed.attempt_count
                )
                updated, retry_delay = await run_in_daemon_thread(
                    self._finish_failure,
                    claimed,
                    error,
                    terminal_reason is not None,
                )
                if not updated:
                    LOG.warning(
                        "event=outbox_lease_lost outcome=skipped outbox_id=%s "
                        "event_type=%s attempt=%s duration_ms=%s",
                        claimed.id, claimed.event_type.value, claimed.attempt_count,
                        _elapsed_ms(started),
                    )
                    skipped += 1
                elif terminal_reason is not None:
                    outcome = (
                        "permanent"
                        if terminal_reason == "telegram_permanent"
                        else "terminal"
                    )
                    LOG.warning(
                        "event=outbox_delivery_failed outcome=%s reason=%s "
                        "outbox_id=%s event_type=%s attempt=%s duration_ms=%s "
                        "exception_class=%s",
                        outcome, terminal_reason, claimed.id,
                        claimed.event_type.value, claimed.attempt_count,
                        _elapsed_ms(started), type(error).__name__,
                    )
                    failed += 1
                else:
                    LOG.warning(
                        "event=outbox_delivery_retry outcome=rescheduled "
                        "outbox_id=%s event_type=%s attempt=%s retry_delay_ms=%s "
                        "duration_ms=%s exception_class=%s",
                        claimed.id, claimed.event_type.value, claimed.attempt_count,
                        retry_delay * 1000, _elapsed_ms(started),
                        type(error).__name__,
                    )
                    rescheduled += 1
                continue
            try:
                acknowledged = await run_in_daemon_thread(
                    self._mark_sent, claimed.id, claimed.lease_token, message_id
                )
            except Exception as error:
                LOG.error(
                    "event=outbox_delivery_ack_uncertain outcome=error "
                    "outbox_id=%s event_type=%s attempt=%s exception_class=%s",
                    claimed.id, claimed.event_type.value, claimed.attempt_count,
                    type(error).__name__,
                )
                raise
            if acknowledged:
                LOG.info(
                    "event=outbox_delivery_completed outcome=sent outbox_id=%s "
                    "event_type=%s attempt=%s duration_ms=%s",
                    claimed.id, claimed.event_type.value, claimed.attempt_count,
                    _elapsed_ms(started),
                )
                sent += 1
            else:
                LOG.warning(
                    "event=outbox_delivery_ack_uncertain outcome=lease_lost "
                    "outbox_id=%s event_type=%s attempt=%s",
                    claimed.id, claimed.event_type.value, claimed.attempt_count,
                )
                LOG.warning(
                    "event=outbox_lease_lost outcome=skipped outbox_id=%s "
                    "event_type=%s attempt=%s duration_ms=%s",
                    claimed.id, claimed.event_type.value, claimed.attempt_count,
                    _elapsed_ms(started),
                )
                skipped += 1
        return OutboxRunResult(sent=sent, rescheduled=rescheduled, failed=failed, skipped=skipped)

    async def run_once(self) -> OutboxRunResult:
        started = time.monotonic()
        try:
            recovered = await run_in_daemon_thread(self.recover_expired)
            processed = await self.process_ready()
            result = OutboxRunResult(
                recovered=recovered,
                sent=processed.sent,
                rescheduled=processed.rescheduled,
                failed=processed.failed,
                skipped=processed.skipped,
            )
        except Exception as error:
            LOG.error(
                "event=outbox_cycle_failed outcome=error duration_ms=%s "
                "exception_class=%s",
                _elapsed_ms(started), type(error).__name__,
            )
            raise
        if any((result.recovered, result.sent, result.rescheduled, result.failed, result.skipped)):
            LOG.info(
                "event=outbox_cycle_completed outcome=processed duration_ms=%s "
                "sent=%s retried=%s failed=%s recovered=%s skipped=%s",
                _elapsed_ms(started), result.sent, result.rescheduled,
                result.failed, result.recovered, result.skipped,
            )
        return result

    def _list_ready(self):
        now = _timestamp(self._now())
        connection = connect(self._database_path)
        try:
            return NotificationOutboxRepository(connection).list_ready(
                now, limit=MAX_MESSAGES_PER_CYCLE
            )
        finally:
            connection.close()

    def _claim(self, outbox_id: int):
        now = self._now()
        connection = connect(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            claimed = NotificationOutboxRepository(connection).claim(
                outbox_id,
                _timestamp(now),
                _timestamp(now + timedelta(seconds=LEASE_SECONDS)),
            )
            connection.commit()
            return claimed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _mark_sent(self, outbox_id: int, lease_token: str, message_id: int) -> bool:
        connection = connect(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            updated = NotificationOutboxRepository(connection).mark_sent(
                outbox_id, lease_token, _timestamp(self._now()), message_id
            )
            connection.commit()
            return updated
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _finish_failure(
        self, claimed, error: Exception, terminal: bool
    ) -> tuple[bool, int]:
        connection = connect(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            repository = NotificationOutboxRepository(connection)
            if terminal:
                updated = repository.mark_failed(
                    claimed.id, claimed.lease_token, _error_text(error)
                )
                delay = 0
            else:
                completed_at = self._now()
                delay = _retry_delay_seconds(claimed.attempt_count)
                retry_after = _retry_after_seconds(error)
                if retry_after is not None:
                    delay = max(delay, retry_after)
                updated = repository.reschedule(
                    claimed.id,
                    claimed.lease_token,
                    _timestamp(completed_at + timedelta(seconds=delay)),
                    _error_text(error),
                )
            connection.commit()
            return updated, delay
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Outbox worker clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)


def _retry_delay_seconds(attempt_count: int) -> int:
    if attempt_count <= 1:
        return 60
    if attempt_count == 2:
        return 300
    if attempt_count == 3:
        return 900
    return 3600


def _retry_after_seconds(error: Exception) -> int | None:
    if not isinstance(error, RetryAfter):
        return None
    value = error.retry_after
    seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
    return max(0, math.ceil(seconds))


def _terminal_failure_reason(error: Exception, attempt_count: int) -> str | None:
    if isinstance(error, InvalidNotificationPayload):
        return "invalid_payload"
    if isinstance(error, (Forbidden, BadRequest)):
        return "telegram_permanent"
    if isinstance(error, (RetryAfter, TimedOut, NetworkError)):
        return None
    if attempt_count >= MAX_UNKNOWN_DELIVERY_ATTEMPTS:
        return "unknown_attempt_limit"
    return None


def _error_text(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"[:2000]


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))
