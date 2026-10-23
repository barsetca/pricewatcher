from pricewatch.database import connect, initialize_database
from pricewatch.database.repositories.scheduler_run_repository import SchedulerRunRepository
from pricewatch.domain.enums import SchedulerRunStatus


SLOT = "2026-10-20T03:00:00+00:00"
STARTED = "2026-10-20T03:00:01+00:00"
FINISHED = "2026-10-20T03:05:00+00:00"


def test_slot_is_unique_and_success_is_terminal(tmp_path):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    connection = connect(path)
    try:
        repository = SchedulerRunRepository(connection)
        first = repository.reserve("daily", SLOT, STARTED)
        assert first.status is SchedulerRunStatus.RUNNING
        assert repository.reserve("daily", SLOT, STARTED) is None
        assert repository.finish("daily", SLOT, SchedulerRunStatus.SUCCESS, FINISHED)
        assert repository.reserve("daily", SLOT, STARTED) is None
        connection.commit()
    finally:
        connection.close()


def test_interrupted_slot_gets_exactly_one_recovery_attempt(tmp_path):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    connection = connect(path)
    try:
        repository = SchedulerRunRepository(connection)
        repository.reserve("daily", SLOT, STARTED)
        assert repository.interrupt_running("daily", FINISHED) == 1
        recovered = repository.reserve("daily", SLOT, FINISHED)
        assert recovered.status is SchedulerRunStatus.RUNNING
        assert recovered.attempt_count == 2
        assert repository.interrupt_running("daily", FINISHED) == 1
        assert repository.reserve("daily", SLOT, FINISHED) is None
        assert repository.get("daily", SLOT).status is SchedulerRunStatus.INTERRUPTED
        connection.commit()
    finally:
        connection.close()


def test_failed_slot_is_not_retried_automatically(tmp_path):
    path = tmp_path / "scheduler.db"
    initialize_database(path)
    connection = connect(path)
    try:
        repository = SchedulerRunRepository(connection)
        repository.reserve("daily", SLOT, STARTED)
        assert repository.finish(
            "daily", SLOT, SchedulerRunStatus.FAILED, FINISHED, "RuntimeError: failed"
        )
        assert repository.reserve("daily", SLOT, FINISHED) is None
        stored = repository.latest("daily")
        assert stored.status is SchedulerRunStatus.FAILED
        assert stored.error_message == "RuntimeError: failed"
        connection.commit()
    finally:
        connection.close()
