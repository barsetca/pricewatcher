import asyncio
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import schedule

from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.scheduler_run_repository import SchedulerRunRepository
from pricewatch.domain.enums import SchedulerRunStatus
from pricewatch.services.daily_scheduler import (
    DAILY_JOB_NAME, DailyMonitoringScheduler, latest_scheduled_slot,
)
from pricewatch.services.monitoring_service import _current_run_id


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def stored(path, slot):
    connection = connect(path)
    try:
        return SchedulerRunRepository(connection).get(DAILY_JOB_NAME, slot)
    finally:
        connection.close()


def scheduler(path, clock, batches, wakeups, outcome=None):
    def run_batch():
        batches.append(clock.value)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome or SimpleNamespace(events=())

    async def wakeup():
        wakeups.append(True)

    return DailyMonitoringScheduler(path, run_batch, wakeup, clock=clock)


def test_latest_slot_is_always_0300_utc():
    before = datetime(2026, 10, 20, 2, 59, tzinfo=timezone.utc)
    after = datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc)
    assert latest_scheduled_slot(before).isoformat() == "2026-10-19T03:00:00+00:00"
    assert latest_scheduled_slot(after).isoformat() == "2026-10-20T03:00:00+00:00"


@pytest.mark.asyncio
async def test_schedule_library_registers_one_daily_0300_utc_job(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="pricewatch.services.daily_scheduler")
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    engine = schedule.Scheduler()
    clock = Clock(datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc))

    async def wakeup():
        return None

    component = DailyMonitoringScheduler(
        path, lambda: SimpleNamespace(events=()), wakeup,
        clock=clock, schedule_engine=engine,
    )
    await component.start()
    assert len(engine.jobs) == 1
    job = engine.jobs[0]
    assert job.unit == "days" and str(job.at_time) == "03:00:00"
    assert str(job.at_time_zone) == "UTC"
    component.request_stop()
    await component.wait_stopped()
    assert not any("event=batch_" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_first_start_before_0300_waits_and_after_0300_runs_once(tmp_path):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    batches, wakeups = [], []
    early = Clock(datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc))
    assert not await scheduler(path, early, batches, wakeups).run_startup_recovery()
    assert batches == []

    late = Clock(datetime(2026, 10, 20, 4, 0, tzinfo=timezone.utc))
    assert await scheduler(path, late, batches, wakeups).run_startup_recovery()
    assert not await scheduler(path, late, batches, wakeups).run_startup_recovery()
    assert len(batches) == 1
    assert stored(path, "2026-10-20T03:00:00+00:00").status is SchedulerRunStatus.SUCCESS


@pytest.mark.asyncio
async def test_multiple_missed_days_create_only_latest_compensating_slot(tmp_path):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    connection = connect(path)
    try:
        repository = SchedulerRunRepository(connection)
        repository.reserve(DAILY_JOB_NAME, "2026-10-17T03:00:00+00:00", "2026-10-17T03:00:01+00:00")
        repository.finish(
            DAILY_JOB_NAME, "2026-10-17T03:00:00+00:00",
            SchedulerRunStatus.SUCCESS, "2026-10-17T03:05:00+00:00",
        )
        connection.commit()
    finally:
        connection.close()
    batches, wakeups = [], []
    clock = Clock(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc))

    assert await scheduler(path, clock, batches, wakeups).run_startup_recovery()

    connection = connect(path)
    try:
        slots = [row[0] for row in connection.execute(
            "SELECT scheduled_for FROM scheduler_runs ORDER BY scheduled_for"
        )]
    finally:
        connection.close()
    assert slots == ["2026-10-17T03:00:00+00:00", "2026-10-20T03:00:00+00:00"]


@pytest.mark.asyncio
async def test_failed_slot_is_recorded_and_not_retried_on_restart(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="pricewatch.services.daily_scheduler")
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    batches, wakeups = [], []
    clock = Clock(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc))
    failed = scheduler(path, clock, batches, wakeups, RuntimeError("network failed"))
    assert await failed.run_startup_recovery()
    assert not await failed.run_startup_recovery()
    row = stored(path, "2026-10-20T03:00:00+00:00")
    assert row.status is SchedulerRunStatus.FAILED
    assert "network failed" in row.error_message
    assert len(batches) == 1
    messages = [record.getMessage() for record in caplog.records]
    failed_logs = [message for message in messages if "event=batch_failed" in message]
    assert len(failed_logs) == 1
    assert "exception_class=RuntimeError" in failed_logs[0]
    assert "network failed" not in failed_logs[0]
    assert not any("event=batch_completed" in message for message in messages)


@pytest.mark.asyncio
async def test_batch_logs_share_run_id_context_and_safe_aggregates(tmp_path, caplog):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    clock = Clock(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc))
    observed_run_ids = []

    def run_batch():
        observed_run_ids.append(_current_run_id())
        return SimpleNamespace(
            results=(
                SimpleNamespace(code="SUCCESS"),
                SimpleNamespace(code="PENDING"),
                SimpleNamespace(code="ERROR"),
                SimpleNamespace(code="INTERNAL_ERROR"),
            ),
            events=(object(),),
        )

    async def wakeup():
        return None

    caplog.set_level(logging.INFO, logger="pricewatch.services.daily_scheduler")
    component = DailyMonitoringScheduler(path, run_batch, wakeup, clock=clock)

    assert await component.run_startup_recovery()

    messages = [record.getMessage() for record in caplog.records]
    started = next(message for message in messages if "event=batch_started" in message)
    completed = next(message for message in messages if "event=batch_completed" in message)
    started_id = re.search(r"run_id=([a-f0-9]{12})", started).group(1)
    completed_id = re.search(r"run_id=([a-f0-9]{12})", completed).group(1)
    assert observed_run_ids == [started_id] == [completed_id]
    assert "outcome=partial" in completed
    assert "offers_total=4 success=1 pending=1 errors=1 internal_errors=1" in completed
    assert "events=1" in completed and "duration_ms=" in completed
    assert stored(path, "2026-10-20T03:00:00+00:00").status is SchedulerRunStatus.SUCCESS


@pytest.mark.asyncio
async def test_stale_running_gets_one_recovery_and_events_wake_delivery(tmp_path):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    connection = connect(path)
    try:
        SchedulerRunRepository(connection).reserve(
            DAILY_JOB_NAME, "2026-10-20T03:00:00+00:00", "2026-10-20T03:00:01+00:00"
        )
        connection.commit()
    finally:
        connection.close()
    batches, wakeups = [], []
    clock = Clock(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc))
    current = scheduler(path, clock, batches, wakeups, SimpleNamespace(events=(object(),)))

    assert await current.run_startup_recovery()

    row = stored(path, "2026-10-20T03:00:00+00:00")
    assert row.status is SchedulerRunStatus.SUCCESS and row.attempt_count == 2
    assert wakeups == [True]


@pytest.mark.asyncio
async def test_wakeup_failure_does_not_change_successful_slot_or_escape(tmp_path):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    clock = Clock(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc))

    async def broken_wakeup():
        raise RuntimeError("coordinator unavailable")

    component = DailyMonitoringScheduler(
        path,
        lambda: SimpleNamespace(events=(object(),)),
        broken_wakeup,
        clock=clock,
    )

    assert await component.run_startup_recovery()
    row = stored(path, "2026-10-20T03:00:00+00:00")
    assert row.status is SchedulerRunStatus.SUCCESS


@pytest.mark.asyncio
async def test_concurrent_due_requests_execute_only_one_batch(tmp_path):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    clock = Clock(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc))
    started = threading.Event()
    release = threading.Event()
    calls = []

    def run_batch():
        calls.append(True)
        started.set()
        release.wait(timeout=2)
        return SimpleNamespace(events=())

    async def wakeup():
        return None

    component = DailyMonitoringScheduler(path, run_batch, wakeup, clock=clock)
    first = asyncio.create_task(component.run_current_slot())
    while not started.is_set():
        await asyncio.sleep(0.001)
    second = asyncio.create_task(component.run_current_slot())
    await asyncio.sleep(0.03)
    release.set()

    assert await asyncio.gather(first, second) == [True, False]
    assert calls == [True]


@pytest.mark.asyncio
async def test_two_scheduler_instances_atomically_compete_for_one_slot(tmp_path):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    clock = Clock(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc))
    calls = []

    def run_batch():
        calls.append(threading.get_ident())
        return SimpleNamespace(events=())

    async def wakeup():
        return None

    first = DailyMonitoringScheduler(path, run_batch, wakeup, clock=clock)
    second = DailyMonitoringScheduler(path, run_batch, wakeup, clock=clock)
    results = await asyncio.gather(first.run_current_slot(), second.run_current_slot())

    assert sorted(results) == [False, True]
    assert len(calls) == 1


def test_server_timezone_offset_does_not_change_utc_slot():
    local_time = datetime(2026, 10, 20, 4, 30, tzinfo=timezone(timedelta(hours=2)))
    assert latest_scheduled_slot(local_time).isoformat() == "2026-10-19T03:00:00+00:00"


@pytest.mark.asyncio
async def test_cancel_during_batch_leaves_running_slot_for_one_bounded_recovery(tmp_path):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    clock = Clock(datetime(2026, 10, 20, 10, 0, tzinfo=timezone.utc))
    started = threading.Event()
    release = threading.Event()
    calls = []

    def interrupted_batch():
        calls.append("interrupted")
        started.set()
        release.wait(timeout=2)
        return SimpleNamespace(events=())

    async def wakeup():
        return None

    interrupted = DailyMonitoringScheduler(path, interrupted_batch, wakeup, clock=clock)
    task = asyncio.create_task(interrupted.run_current_slot())
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.03)

    slot = "2026-10-20T03:00:00+00:00"
    assert stored(path, slot).status is SchedulerRunStatus.RUNNING

    recovery = DailyMonitoringScheduler(
        path,
        lambda: calls.append("recovered") or SimpleNamespace(events=()),
        wakeup,
        clock=clock,
    )
    assert await recovery.run_startup_recovery()
    row = stored(path, slot)
    assert (row.status, row.attempt_count) == (SchedulerRunStatus.SUCCESS, 2)
    assert calls == ["interrupted", "recovered"]


@pytest.mark.asyncio
async def test_scheduler_infrastructure_error_is_reported_as_fatal(tmp_path):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    fatal = []

    async def wakeup():
        return None

    component = DailyMonitoringScheduler(
        path,
        lambda: SimpleNamespace(events=()),
        wakeup,
        clock=Clock(datetime(2026, 10, 20, 2, 0, tzinfo=timezone.utc)),
        on_fatal=fatal.append,
    )

    async def broken_startup():
        raise RuntimeError("scheduler SQLite unavailable")

    component.run_startup_recovery = broken_startup
    await component.start()
    await component.wait_stopped()

    assert len(fatal) == 1
    assert isinstance(fatal[0], RuntimeError)
    assert not component.running
