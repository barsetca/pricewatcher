import asyncio
import sqlite3
import threading

import pytest

from pricewatch.async_utils import run_in_daemon_thread


@pytest.mark.asyncio
async def test_cancelled_wait_does_not_close_sqlite_connection_owned_by_daemon_thread(tmp_path):
    path = tmp_path / "thread-owner.sqlite"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE audit (value TEXT NOT NULL)")
    connection.commit()
    connection.close()

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    worker_thread = []

    def transaction():
        own_connection = sqlite3.connect(path)
        try:
            worker_thread.append(threading.get_ident())
            own_connection.execute("BEGIN IMMEDIATE")
            own_connection.execute("INSERT INTO audit(value) VALUES ('committed by owner')")
            started.set()
            release.wait(timeout=2)
            own_connection.commit()
        finally:
            own_connection.close()
            finished.set()

    task = asyncio.create_task(run_in_daemon_thread(transaction))
    while not started.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Отмена asyncio-ожидания не остановила sync transaction и не попыталась
    # закрыть connection из event-loop thread.
    assert not finished.is_set()
    assert worker_thread != [threading.get_ident()]
    release.set()
    assert finished.wait(timeout=1)

    verification = sqlite3.connect(path)
    try:
        assert verification.execute("SELECT value FROM audit").fetchall() == [
            ("committed by owner",)
        ]
    finally:
        verification.close()
