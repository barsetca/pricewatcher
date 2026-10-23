"""Repository постоянного состояния ежедневного scheduler."""

from __future__ import annotations

import sqlite3

from pricewatch.domain.enums import SchedulerRunStatus
from pricewatch.domain.models import SchedulerRun


class SchedulerRunRepository:
    """Резервирует UTC-слоты, не управляя transaction boundary."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, job_name: str, scheduled_for: str) -> SchedulerRun | None:
        row = self._connection.execute(
            "SELECT * FROM scheduler_runs WHERE job_name = ? AND scheduled_for = ?",
            (job_name, scheduled_for),
        ).fetchone()
        return SchedulerRun.from_row(row) if row else None

    def latest(self, job_name: str) -> SchedulerRun | None:
        row = self._connection.execute(
            """
            SELECT * FROM scheduler_runs
            WHERE job_name = ?
            ORDER BY scheduled_for DESC
            LIMIT 1
            """,
            (job_name,),
        ).fetchone()
        return SchedulerRun.from_row(row) if row else None

    def reserve(self, job_name: str, scheduled_for: str, started_at: str) -> SchedulerRun | None:
        """Создаёт слот или один раз восстанавливает INTERRUPTED attempt."""
        inserted = self._connection.execute(
            """
            INSERT INTO scheduler_runs (
                job_name, scheduled_for, started_at, status, attempt_count
            ) VALUES (?, ?, ?, 'RUNNING', 1)
            ON CONFLICT(job_name, scheduled_for) DO NOTHING
            """,
            (job_name, scheduled_for, started_at),
        ).rowcount == 1
        if inserted:
            return self.get(job_name, scheduled_for)
        retried = self._connection.execute(
            """
            UPDATE scheduler_runs
            SET status = 'RUNNING', started_at = ?, finished_at = NULL,
                error_message = NULL, attempt_count = attempt_count + 1
            WHERE job_name = ? AND scheduled_for = ?
              AND status = 'INTERRUPTED' AND attempt_count = 1
            """,
            (started_at, job_name, scheduled_for),
        ).rowcount == 1
        return self.get(job_name, scheduled_for) if retried else None

    def interrupt_running(self, job_name: str, interrupted_at: str) -> int:
        return self._connection.execute(
            """
            UPDATE scheduler_runs
            SET status = 'INTERRUPTED', finished_at = ?,
                error_message = 'process stopped before completion'
            WHERE job_name = ? AND status = 'RUNNING'
            """,
            (interrupted_at, job_name),
        ).rowcount

    def finish(
        self,
        job_name: str,
        scheduled_for: str,
        status: SchedulerRunStatus,
        finished_at: str,
        error_message: str | None = None,
    ) -> bool:
        if status not in {SchedulerRunStatus.SUCCESS, SchedulerRunStatus.FAILED}:
            raise ValueError("scheduler run can finish only as SUCCESS or FAILED")
        return self._connection.execute(
            """
            UPDATE scheduler_runs
            SET status = ?, finished_at = ?, error_message = ?
            WHERE job_name = ? AND scheduled_for = ? AND status = 'RUNNING'
            """,
            (status.value, finished_at, error_message, job_name, scheduled_for),
        ).rowcount == 1
