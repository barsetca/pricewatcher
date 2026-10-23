import asyncio
import logging
import signal

import pytest

from pricewatch.runtime import PriceWatchRuntime, RuntimeState


class Updater:
    def __init__(self, calls):
        self.calls = calls
        self.running = False

    async def start_polling(self):
        self.calls.append("polling_start")
        self.running = True

    async def stop(self):
        self.calls.append("polling_stop")
        self.running = False


class Application:
    def __init__(self, calls):
        self.calls = calls
        self.updater = Updater(calls)
        self.running = False
        self.post_init = self.post_stop = self.post_shutdown = None

    async def initialize(self):
        self.calls.append("initialize")

    async def start(self):
        self.calls.append("application_start")
        self.running = True

    async def stop(self):
        self.calls.append("application_stop")
        self.running = False

    async def shutdown(self):
        self.calls.append("shutdown")


class Component:
    def __init__(self, name, calls, *, hang=False):
        self.name = name
        self.calls = calls
        self.hang = hang
        self.stopped = asyncio.Event()
        self.cancelled = False

    async def start(self):
        self.calls.append(f"{self.name}_start")

    def request_stop(self):
        self.calls.append(f"{self.name}_request_stop")
        if not self.hang:
            self.stopped.set()

    async def wait_stopped(self):
        await self.stopped.wait()

    def cancel(self):
        self.cancelled = True
        self.stopped.set()


async def wait_state(runtime, state):
    while runtime.state is not state:
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_runtime_uses_explicit_ptb_lifecycle_and_orderly_shutdown(caplog):
    caplog.set_level(logging.INFO, logger="pricewatch.runtime")
    calls = []
    application = Application(calls)
    coordinator = Component("coordinator", calls)
    scheduler = Component("scheduler", calls)
    runtime = PriceWatchRuntime(application, coordinator, scheduler)
    task = asyncio.create_task(runtime.run(install_signals=False))
    await wait_state(runtime, RuntimeState.RUNNING)
    runtime.request_stop()

    assert await task == 0
    assert runtime.state is RuntimeState.STOPPED
    assert calls[:5] == [
        "initialize", "polling_start", "application_start",
        "coordinator_start", "scheduler_start",
    ]
    assert calls.index("polling_stop") < calls.index("application_stop") < calls.index("shutdown")
    events = [record.getMessage() for record in caplog.records if "event=runtime_" in record.getMessage()]
    assert [message.split()[0] for message in events] == [
        "event=runtime_starting", "event=runtime_started",
        "event=runtime_stopping", "event=runtime_stopped",
    ]
    assert "outcome=success" in events[-1]
    assert "duration_ms=" in events[1] and "duration_ms=" in events[-1]


@pytest.mark.asyncio
async def test_runtime_shutdown_has_one_shared_budget_and_cancels_hung_components():
    calls = []
    application = Application(calls)
    coordinator = Component("coordinator", calls, hang=True)
    scheduler = Component("scheduler", calls, hang=True)
    runtime = PriceWatchRuntime(
        application, coordinator, scheduler, shutdown_timeout=0.08
    )
    task = asyncio.create_task(runtime.run(install_signals=False))
    await wait_state(runtime, RuntimeState.RUNNING)
    started = asyncio.get_running_loop().time()
    runtime.request_stop()

    assert await task == 1
    assert asyncio.get_running_loop().time() - started < 0.3
    assert coordinator.cancelled and scheduler.cancelled
    assert runtime.state is RuntimeState.STOPPED


@pytest.mark.asyncio
async def test_partial_startup_failure_stops_resources_that_became_active(caplog):
    caplog.set_level(logging.INFO, logger="pricewatch.runtime")
    calls = []
    application = Application(calls)

    async def fail_after_starting():
        calls.append("application_start")
        application.running = True
        raise RuntimeError("startup failed")

    application.start = fail_after_starting
    coordinator = Component("coordinator", calls)
    scheduler = Component("scheduler", calls)
    runtime = PriceWatchRuntime(application, coordinator, scheduler)

    assert await runtime.run(install_signals=False) == 1
    assert calls == [
        "initialize", "polling_start", "application_start",
        "scheduler_request_stop", "coordinator_request_stop",
        "polling_stop", "application_stop", "shutdown",
    ]
    assert runtime.state is RuntimeState.STOPPED
    messages = [record.getMessage() for record in caplog.records]
    assert not any(message.startswith("event=runtime_started ") for message in messages)
    assert any(
        "event=runtime_failed outcome=error" in message
        and "exception_class=RuntimeError" in message
        for message in messages
    )
    assert not any("startup failed" in message for message in messages)
    assert any("event=runtime_stopped outcome=error" in message for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT])
async def test_installed_process_signal_requests_orderly_stop(monkeypatch, stop_signal):
    calls = []
    callbacks = {}
    removed = []
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(
        loop, "add_signal_handler", lambda item, callback: callbacks.__setitem__(item, callback)
    )
    monkeypatch.setattr(
        loop, "remove_signal_handler", lambda item: removed.append(item) or True
    )
    runtime = PriceWatchRuntime(
        Application(calls), Component("coordinator", calls), Component("scheduler", calls)
    )
    task = asyncio.create_task(runtime.run())
    await wait_state(runtime, RuntimeState.RUNNING)

    callbacks[stop_signal]()

    assert await task == 0
    assert set(callbacks) == {signal.SIGTERM, signal.SIGINT}
    assert set(removed) == {signal.SIGTERM, signal.SIGINT}
    assert runtime.state is RuntimeState.STOPPED
