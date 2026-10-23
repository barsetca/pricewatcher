"""Координация startup, periodic и event-driven запусков OutboxWorker."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from pricewatch.services.outbox_worker import MAX_MESSAGES_PER_CYCLE, OutboxRunResult


LOG = logging.getLogger(__name__)
DELIVERY_INTERVAL_SECONDS = 60


class DeliveryCoordinator:
    """Владеет одним worker и объединяет wakeup-сигналы без потери импульса."""

    def __init__(
        self,
        worker,
        *,
        interval_seconds: float = DELIVERY_INTERVAL_SECONDS,
        on_fatal: Callable[[BaseException], None] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("delivery interval must be positive")
        self._worker = worker
        self._interval_seconds = interval_seconds
        self._on_fatal = on_fatal
        self._signal = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        self._accepting = False
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._accepting = True
        self._signal.set()  # startup delivery
        self._task = asyncio.create_task(self._run(), name="outbox-delivery-coordinator")

    async def wakeup(self) -> None:
        """Быстро подаёт coalescing-сигнал и никогда не ждёт delivery cycle."""
        if self._accepting:
            self._signal.set()

    def request_stop(self) -> None:
        self._accepting = False
        self._signal.set()

    async def wait_stopped(self) -> None:
        if self._task is not None:
            await self._task

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        try:
            while self._accepting:
                try:
                    await asyncio.wait_for(
                        self._signal.wait(), timeout=self._interval_seconds
                    )
                except asyncio.TimeoutError:
                    pass
                self._signal.clear()
                if not self._accepting:
                    break
                result = await self._run_cycle()
                processed = result.sent + result.rescheduled + result.failed + result.skipped
                if processed >= MAX_MESSAGES_PER_CYCLE:
                    self._signal.set()
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            LOG.error(
                "event=outbox_coordinator_failed outcome=error exception_class=%s",
                type(error).__name__,
            )
            self._accepting = False
            if self._on_fatal is not None:
                self._on_fatal(error)

    async def _run_cycle(self) -> OutboxRunResult:
        async with self._cycle_lock:
            return await self._worker.run_once()
