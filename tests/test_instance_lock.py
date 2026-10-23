"""Process-level acceptance для lifecycle lock без Telegram polling."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from pricewatch.instance_lock import DatabaseInstanceLock, SingleInstanceLockError


HOLDER_CODE = """
import sys
import time
from pricewatch.instance_lock import DatabaseInstanceLock, SingleInstanceLockError

lock = DatabaseInstanceLock(sys.argv[1])
try:
    lock.acquire()
except SingleInstanceLockError:
    print('BUSY', flush=True)
    raise SystemExit(3)
print('READY', flush=True)
while True:
    time.sleep(1)
"""


def _holder(path: Path) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [sys.executable, "-c", HOLDER_CODE, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    assert process.stdout is not None
    assert process.stdout.readline().strip() == "READY"
    return process


def _stop(process: subprocess.Popen[str], signal_number: int = signal.SIGTERM) -> None:
    if process.poll() is None:
        process.send_signal(signal_number)
    process.wait(timeout=5)


def test_first_process_holds_lock_and_second_main_refuses_before_polling(tmp_path):
    database_path = tmp_path / "pricewatch.db"
    holder = _holder(database_path)
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "pricewatch.main"],
            capture_output=True,
            text=True,
            timeout=5,
            env={
                **os.environ,
                "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
                "PRICEWATCH_DATABASE_PATH": str(database_path),
                "TELEGRAM_BOT_TOKEN": "123456:" + "offline_test_token_" * 2,
                "TELEGRAM_ALLOWED_USER_IDS": "1",
                "TELEGRAM_MODE": "polling",
            },
        )
        assert completed.returncode == 3
        assert "другой экземпляр PriceWatch" in completed.stderr
        assert not database_path.exists()
    finally:
        _stop(holder)


def test_different_databases_allow_independent_processes(tmp_path):
    first = _holder(tmp_path / "one.db")
    second = _holder(tmp_path / "two.db")
    try:
        assert first.poll() is None
        assert second.poll() is None
    finally:
        _stop(first)
        _stop(second)


def test_normal_release_keeps_file_and_allows_next_process(tmp_path):
    database_path = tmp_path / "pricewatch.db"
    first = _holder(database_path)
    _stop(first)
    assert (tmp_path / "pricewatch.db.lock").is_file()
    second = _holder(database_path)
    _stop(second)


@pytest.mark.parametrize("stop_signal", [signal.SIGTERM, signal.SIGINT, signal.SIGKILL])
def test_signal_or_crash_releases_process_lock(tmp_path, stop_signal):
    database_path = tmp_path / "pricewatch.db"
    first = _holder(database_path)
    _stop(first, stop_signal)
    second = _holder(database_path)
    _stop(second)


def test_equivalent_path_cannot_bypass_lock(tmp_path):
    database_path = tmp_path / "pricewatch.db"
    first = _holder(database_path)
    try:
        equivalent = tmp_path / "." / database_path.name
        completed = subprocess.run(
            [sys.executable, "-c", HOLDER_CODE, str(equivalent)],
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        )
        assert completed.returncode == 3
        assert completed.stdout == "BUSY\n"
    finally:
        _stop(first)


def test_lock_open_error_refuses_start_without_acquiring(monkeypatch, tmp_path):
    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr("pricewatch.instance_lock.os.open", denied)
    lock = DatabaseInstanceLock(tmp_path / "pricewatch.db")
    with pytest.raises(SingleInstanceLockError, match="не удалось открыть"):
        lock.acquire()
    assert not lock.acquired
