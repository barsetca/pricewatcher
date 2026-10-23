"""Ежедневный monitoring scheduler с persistent UTC-слотами."""

from __future__ import annotations

import asyncio
import logging
import time as time_module
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import schedule

from pricewatch.async_utils import run_in_daemon_thread
from pricewatch.database.connection import connect
from pricewatch.database.repositories.scheduler_run_repository import SchedulerRunRepository
from pricewatch.domain.enums import SchedulerRunStatus
from pricewatch.services.monitoring_service import _monitoring_run


LOG = logging.getLogger(__name__)
DAILY_JOB_NAME = "daily_monitoring"
DAILY_RUN_HOUR_UTC = 3
SCHEDULER_POLL_SECONDS = 0.25
MAX_SCHEDULER_ERROR_LENGTH = 2000


class DailyMonitoringScheduler:
    """Запускает один `check_all` для каждого актуального UTC-слота."""

    def __init__(
        self,
        database_path: str | Path,
        batch_runner: Callable[[], Any],
        notification_wakeup: Callable[[], Awaitable[None]],
        *,
        clock: Callable[[], datetime] | None = None,
        schedule_engine: schedule.Scheduler | None = None,
        on_fatal: Callable[[BaseException], None] | None = None,
    ) -> None:
        self._database_path = database_path
        self._batch_runner = batch_runner
        self._notification_wakeup = notification_wakeup
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._schedule = schedule_engine or schedule.Scheduler()
        self._on_fatal = on_fatal
        self._accepting = False
        self._heartbeat_due = False
        self._last_seen_slot: datetime | None = None
        self._batch_lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._accepting = True
        self._schedule.every().day.at("03:00", "UTC").do(self._heartbeat)
        self._task = asyncio.create_task(self._run(), name="daily-monitoring-scheduler")

    def request_stop(self) -> None:
        self._accepting = False
        self._schedule.clear()

    async def wait_stopped(self) -> None:
        if self._task is not None:
            await self._task

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def run_startup_recovery(self) -> bool:
        """Восстанавливает stale RUNNING и при необходимости выполняет catch-up."""
        now = self._now()
        slot = latest_scheduled_slot(now)
        reserved = await run_in_daemon_thread(self._reserve_startup_slot, now, slot)
        self._last_seen_slot = slot
        if not reserved:
            return False
        await self._execute_reserved(slot)
        return True

    async def run_current_slot(self) -> bool:
        """Резервирует текущий наступивший слот и выполняет его не более раза."""
        now = self._now()
        slot = latest_scheduled_slot(now)
        reserved = await run_in_daemon_thread(self._reserve_slot, slot, now)
        self._last_seen_slot = slot
        if not reserved:
            return False
        await self._execute_reserved(slot)
        return True

    async def _run(self) -> None:
        try:
            await self.run_startup_recovery()
            while self._accepting:
                self._schedule.run_pending()
                if self._heartbeat_due:
                    self._heartbeat_due = False
                    current_slot = latest_scheduled_slot(self._now())
                    if current_slot != self._last_seen_slot:
                        await self.run_current_slot()
                await asyncio.sleep(SCHEDULER_POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            LOG.exception("Daily scheduler stopped after a critical error")
            self._accepting = False
            if self._on_fatal is not None:
                self._on_fatal(error)

    def _heartbeat(self) -> None:
        self._heartbeat_due = True

    async def _execute_reserved(self, slot: datetime) -> None:
        async with self._batch_lock:
            run_id = _new_run_id()
            started = time_module.monotonic()
            LOG.info(
                "event=batch_started outcome=started run_id=%s slot=%s",
                run_id, _timestamp(slot),
            )
            try:
                result = await run_in_daemon_thread(self._run_batch, run_id)
            except asyncio.CancelledError:
                # Нельзя отменить уже выполняющийся sync HTTP/SQLite код. Строка
                # остаётся RUNNING и будет восстановлена следующим процессом.
                raise
            except Exception as error:
                await run_in_daemon_thread(self._finish_slot, slot, False, error)
                LOG.error(
                    "event=batch_failed outcome=error run_id=%s slot=%s "
                    "duration_ms=%s exception_class=%s",
                    run_id, _timestamp(slot), _elapsed_ms(started),
                    type(error).__name__,
                )
                return
            await run_in_daemon_thread(self._finish_slot, slot, True, None)
            total, success, pending, errors, internal_errors, suspicious, stale, skipped, not_found, other = (
                _batch_counts(result)
            )
            LOG.info(
                "event=batch_completed outcome=%s run_id=%s slot=%s "
                "duration_ms=%s offers_total=%s success=%s pending=%s errors=%s "
                "internal_errors=%s suspicious=%s stale=%s skipped=%s "
                "not_found=%s other=%s events=%s",
                "partial" if internal_errors else "success",
                run_id, _timestamp(slot), _elapsed_ms(started), total, success,
                pending, errors, internal_errors, suspicious, stale, skipped, not_found, other,
                len(getattr(result, "events", ()) or ()),
            )
            if getattr(result, "events", ()):
                try:
                    await self._notification_wakeup()
                except Exception:
                    # Monitoring и Outbox уже committed. Периодический delivery
                    # подберёт события, даже если быстрый сигнал временно сломан.
                    LOG.exception("Failed to wake Outbox delivery after daily monitoring")

    def _run_batch(self, run_id: str):
        with _monitoring_run(run_id):
            return self._batch_runner()

    def _reserve_startup_slot(self, now: datetime, slot: datetime) -> bool:
        connection = connect(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            repository = SchedulerRunRepository(connection)
            previous = repository.latest(DAILY_JOB_NAME)
            interrupted = repository.interrupt_running(DAILY_JOB_NAME, _timestamp(now))
            if interrupted:
                LOG.warning("Marked %s stale scheduler run(s) as INTERRUPTED", interrupted)
            # Первый в истории запуск до 03:00 UTC ждёт ближайший слот.
            if previous is None and now < _today_slot(now):
                connection.commit()
                return False
            reserved = repository.reserve(
                DAILY_JOB_NAME, _timestamp(slot), _timestamp(now)
            )
            connection.commit()
            return reserved is not None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _reserve_slot(self, slot: datetime, now: datetime) -> bool:
        connection = connect(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            reserved = SchedulerRunRepository(connection).reserve(
                DAILY_JOB_NAME, _timestamp(slot), _timestamp(now)
            )
            connection.commit()
            return reserved is not None
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _finish_slot(
        self, slot: datetime, success: bool, error: BaseException | None
    ) -> None:
        connection = connect(self._database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            status = SchedulerRunStatus.SUCCESS if success else SchedulerRunStatus.FAILED
            message = None if error is None else _error_text(error)
            updated = SchedulerRunRepository(connection).finish(
                DAILY_JOB_NAME, _timestamp(slot), status,
                _timestamp(self._now()), message,
            )
            if not updated:
                raise RuntimeError("scheduler slot lost its RUNNING state")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduler clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)


def latest_scheduled_slot(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    utc = now.astimezone(timezone.utc)
    today = _today_slot(utc)
    return today if utc >= today else today - timedelta(days=1)


def _today_slot(now: datetime) -> datetime:
    return datetime.combine(
        now.astimezone(timezone.utc).date(),
        time(DAILY_RUN_HOUR_UTC, tzinfo=timezone.utc),
    )


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _error_text(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:MAX_SCHEDULER_ERROR_LENGTH]


def _batch_counts(result) -> tuple[int, int, int, int, int, int, int, int, int, int]:
    counts = {
        "SUCCESS": 0, "PENDING": 0, "ERROR": 0, "INTERNAL_ERROR": 0,
        "SUSPICIOUS": 0, "STALE": 0, "SKIPPED": 0, "NOT_FOUND": 0,
        "OTHER": 0,
    }
    results = tuple(getattr(result, "results", ()) or ())
    for item in results:
        code = getattr(item, "code", None)
        counts[code if code in counts else "OTHER"] += 1
    return (
        len(results), counts["SUCCESS"], counts["PENDING"], counts["ERROR"],
        counts["INTERNAL_ERROR"], counts["SUSPICIOUS"], counts["STALE"],
        counts["SKIPPED"], counts["NOT_FOUND"], counts["OTHER"],
    )


def _new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def _elapsed_ms(started: float) -> int:
    return max(0, round((time_module.monotonic() - started) * 1000))
