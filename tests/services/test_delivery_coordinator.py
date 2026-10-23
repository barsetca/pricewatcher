import asyncio
import logging

import pytest

from pricewatch.services.delivery_coordinator import DeliveryCoordinator
from pricewatch.services.outbox_worker import OutboxRunResult


class Worker:
    def __init__(self, results=()):
        self.results = list(results)
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def run_once(self):
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.block:
                await self.release.wait()
            return self.results.pop(0) if self.results else OutboxRunResult()
        finally:
            self.active -= 1


async def wait_until(predicate, timeout=1):
    async def poll():
        while not predicate():
            await asyncio.sleep(0.001)
    await asyncio.wait_for(poll(), timeout)


@pytest.mark.asyncio
async def test_startup_periodic_and_manual_signals_use_one_worker_cycle_at_a_time():
    worker = Worker()
    worker.block = True
    coordinator = DeliveryCoordinator(worker, interval_seconds=0.03)
    await coordinator.start()
    await asyncio.wait_for(worker.started.wait(), 1)
    for _ in range(20):
        await coordinator.wakeup()
    assert worker.calls == 1
    worker.release.set()
    await wait_until(lambda: worker.calls >= 2)
    assert worker.max_active == 1
    coordinator.request_stop()
    await coordinator.wait_stopped()


@pytest.mark.asyncio
async def test_full_batch_requests_one_fair_sequential_follow_up():
    worker = Worker([OutboxRunResult(sent=20), OutboxRunResult(sent=1)])
    coordinator = DeliveryCoordinator(worker, interval_seconds=10)
    await coordinator.start()
    await wait_until(lambda: worker.calls == 2)
    assert worker.max_active == 1
    coordinator.request_stop()
    await coordinator.wait_stopped()


@pytest.mark.asyncio
async def test_wakeup_after_stop_is_ignored():
    worker = Worker()
    coordinator = DeliveryCoordinator(worker, interval_seconds=10)
    await coordinator.start()
    await wait_until(lambda: worker.calls == 1)
    coordinator.request_stop()
    await coordinator.wait_stopped()
    await coordinator.wakeup()
    await asyncio.sleep(0)
    assert worker.calls == 1


@pytest.mark.asyncio
async def test_unhandled_worker_error_is_reported_as_fatal(caplog):
    class BrokenWorker:
        async def run_once(self):
            raise RuntimeError("SQLite infrastructure failed")

    fatal = []
    coordinator = DeliveryCoordinator(
        BrokenWorker(), interval_seconds=10, on_fatal=fatal.append
    )
    caplog.set_level(logging.INFO, logger="pricewatch.services.delivery_coordinator")
    await coordinator.start()
    await coordinator.wait_stopped()

    assert len(fatal) == 1
    assert isinstance(fatal[0], RuntimeError)
    assert not coordinator.running
    messages = [record.getMessage() for record in caplog.records]
    assert messages == [
        "event=outbox_coordinator_failed outcome=error "
        "exception_class=RuntimeError"
    ]
    assert "SQLite infrastructure failed" not in messages[0]
