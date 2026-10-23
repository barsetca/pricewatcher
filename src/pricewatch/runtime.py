"""Production lifecycle Telegram polling, delivery coordinator и scheduler."""

from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Any

from pricewatch.config import Settings, TelegramSettings
from pricewatch.database.connection import connect
from pricewatch.scraping.price_checker import PriceChecker
from pricewatch.scraping.requests_fetcher import RequestsFetcher
from pricewatch.scraping.robots_policy import RobotsPolicy
from pricewatch.scraping.universal_extractor import UniversalExtractor
from pricewatch.services.daily_scheduler import DailyMonitoringScheduler
from pricewatch.services.delivery_coordinator import DeliveryCoordinator
from pricewatch.services.monitoring_service import MonitoringService
from pricewatch.services.outbox_worker import OutboxWorker
from pricewatch.telegram.app import build_application
from pricewatch.telegram.sender import TelegramSender


LOG = logging.getLogger(__name__)
SHUTDOWN_TIMEOUT_SECONDS = 30


def _elapsed_ms(started: float) -> int:
    return max(0, round((time.monotonic() - started) * 1000))


class RuntimeState(str, Enum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class PriceWatchRuntime:
    def __init__(
        self,
        application: Any,
        coordinator: DeliveryCoordinator,
        scheduler: DailyMonitoringScheduler,
        *,
        shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
    ) -> None:
        self.application = application
        self.coordinator = coordinator
        self.scheduler = scheduler
        self.shutdown_timeout = shutdown_timeout
        self.state = RuntimeState.CREATED
        self._stop_event = asyncio.Event()
        self._fatal_error: BaseException | None = None
        self._signals_installed: list[signal.Signals] = []
        self._initialized = False
        self._started = False
        self._polling = False

    def request_stop(self, error: BaseException | None = None) -> None:
        if error is not None and self._fatal_error is None:
            self._fatal_error = error
        self._stop_event.set()

    async def run(self, *, install_signals: bool = True) -> int:
        started = time.monotonic()
        self.state = RuntimeState.STARTING
        LOG.info("event=runtime_starting outcome=started")
        if install_signals:
            self._install_signal_handlers()
        try:
            await self.application.initialize()
            self._initialized = True
            if self.application.post_init is not None:
                await self.application.post_init(self.application)
            await self.application.updater.start_polling()
            self._polling = True
            await self.application.start()
            self._started = True
            await self.coordinator.start()
            await self.scheduler.start()
            self.state = RuntimeState.RUNNING
            LOG.info(
                "event=runtime_started outcome=success duration_ms=%s",
                _elapsed_ms(started),
            )
            await self._stop_event.wait()
        except asyncio.CancelledError:
            self.request_stop()
            raise
        except BaseException as error:
            LOG.error(
                "event=runtime_failed outcome=error duration_ms=%s exception_class=%s",
                _elapsed_ms(started), type(error).__name__,
            )
            self.request_stop(error)
        finally:
            await self._shutdown()
            self._remove_signal_handlers()
        return 1 if self._fatal_error is not None else 0

    async def _shutdown(self) -> None:
        started = time.monotonic()
        self.state = RuntimeState.STOPPING
        LOG.info("event=runtime_stopping outcome=started")
        self.scheduler.request_stop()
        self.coordinator.request_stop()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.shutdown_timeout

        if self._polling or self.application.updater.running:
            await self._within_budget(self.application.updater.stop(), deadline, "Telegram polling stop")
        self._polling = False
        if self._started or self.application.running:
            await self._within_budget(self.application.stop(), deadline, "Telegram application stop")
            if self.application.post_stop is not None:
                await self._within_budget(
                    self.application.post_stop(self.application), deadline, "Telegram post_stop"
                )
        self._started = False

        components_stopped = await self._within_budget(
            asyncio.gather(
                self.scheduler.wait_stopped(), self.coordinator.wait_stopped(),
                return_exceptions=True,
            ),
            deadline,
            "background components",
            reserve_seconds=1.0,
        )
        if not components_stopped:
            self.scheduler.cancel()
            self.coordinator.cancel()
            await asyncio.sleep(0)

        if self._initialized:
            await self._within_budget(self.application.shutdown(), deadline, "Telegram shutdown")
            if self.application.post_shutdown is not None:
                await self._within_budget(
                    self.application.post_shutdown(self.application), deadline,
                    "Telegram post_shutdown",
                )
        self._initialized = False
        self.state = RuntimeState.STOPPED
        LOG.info(
            "event=runtime_stopped outcome=%s duration_ms=%s",
            "error" if self._fatal_error is not None else "success",
            _elapsed_ms(started),
        )

    async def _within_budget(
        self,
        awaitable: Awaitable[Any],
        deadline: float,
        label: str,
        *,
        reserve_seconds: float = 0,
    ) -> bool:
        remaining = max(0.0, deadline - asyncio.get_running_loop().time() - reserve_seconds)
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()  # type: ignore[union-attr]
            LOG.error("Shutdown budget exhausted before %s", label)
            self._record_shutdown_failure(label, "budget exhausted")
            return False
        try:
            await asyncio.wait_for(awaitable, timeout=remaining)
            return True
        except asyncio.TimeoutError:
            LOG.error("Timed out while waiting for %s", label)
            self._record_shutdown_failure(label, "timed out")
            return False
        except Exception as error:
            LOG.exception("Failed during %s", label)
            self._record_shutdown_failure(label, f"{type(error).__name__}: {error}")
            return False

    def _record_shutdown_failure(self, label: str, reason: str) -> None:
        if self._fatal_error is None:
            self._fatal_error = RuntimeError(f"shutdown failed during {label}: {reason}")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for item in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(item, self.request_stop)
            except (NotImplementedError, RuntimeError):
                LOG.warning("Signal handler %s is unavailable", item.name)
            else:
                self._signals_installed.append(item)

    def _remove_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for item in self._signals_installed:
            loop.remove_signal_handler(item)
        self._signals_installed.clear()


def create_runtime(
    settings: Settings,
    telegram: TelegramSettings,
    *,
    application_builder: Callable[..., Any] = build_application,
    sender_factory: Callable[..., Any] = TelegramSender,
    worker_factory: Callable[..., Any] = OutboxWorker,
    coordinator_factory: Callable[..., DeliveryCoordinator] = DeliveryCoordinator,
    scheduler_factory: Callable[..., DailyMonitoringScheduler] = DailyMonitoringScheduler,
    batch_runner: Callable[[], Any] | None = None,
) -> PriceWatchRuntime:
    holder: dict[str, Any] = {}

    async def notification_wakeup() -> None:
        coordinator = holder.get("coordinator")
        if coordinator is not None:
            await coordinator.wakeup()

    def fatal(error: BaseException) -> None:
        runtime = holder.get("runtime")
        if runtime is not None:
            runtime.request_stop(error)

    application = application_builder(
        telegram.token,
        telegram.allowed_user_ids,
        str(settings.database_path),
        notification_wakeup=notification_wakeup,
    )
    worker = worker_factory(settings.database_path, sender_factory(application.bot))
    coordinator = coordinator_factory(worker, on_fatal=fatal)
    holder["coordinator"] = coordinator
    scheduler = scheduler_factory(
        settings.database_path,
        batch_runner or partial(_run_daily_monitoring, settings.database_path),
        notification_wakeup,
        on_fatal=fatal,
    )
    runtime = PriceWatchRuntime(application, coordinator, scheduler)
    holder["runtime"] = runtime
    return runtime


def _run_daily_monitoring(database_path: str | Path):
    """Создаёт весь sync service graph внутри batch thread."""
    connection = connect(database_path)
    try:
        checker = PriceChecker(RobotsPolicy(), RequestsFetcher(), UniversalExtractor())
        return MonitoringService(connection, checker).check_all()
    finally:
        connection.close()
