"""Межпроцессная защита одного runtime для одной файловой SQLite."""
from __future__ import annotations

import os
from pathlib import Path

try:  # pragma: no cover - Linux является целевой платформой production runtime.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class SingleInstanceLockError(RuntimeError):
    """Безопасный отказ запуска, когда lifecycle lock нельзя захватить."""


class DatabaseInstanceLock:
    """Удерживает advisory ``flock`` до штатного или аварийного завершения процесса.

    Lock-файл намеренно остаётся на persistent filesystem: значение имеет
    открытый file descriptor и OS lock, а не наличие файла. Это исключает race
    между unlink старого PID-файла и новым запуском.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = _canonical_database_path(database_path)
        self.lock_path = self.database_path.with_name(f"{self.database_path.name}.lock")
        self._fd: int | None = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        if fcntl is None:
            raise SingleInstanceLockError(
                "межпроцессная защита недоступна на этой платформе; запуск отменён"
            )
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        except OSError as error:
            raise SingleInstanceLockError(
                "не удалось открыть файл межпроцессной защиты; запуск отменён"
            ) from error
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(fd)
            raise SingleInstanceLockError(
                "другой экземпляр PriceWatch уже использует эту SQLite-базу"
            ) from error
        except OSError as error:
            os.close(fd)
            raise SingleInstanceLockError(
                "не удалось захватить межпроцессную защиту; запуск отменён"
            ) from error
        self._fd = fd

    def release(self) -> None:
        """Освобождает descriptor, но никогда не удаляет lock-файл."""
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "DatabaseInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()


def _canonical_database_path(database_path: str | Path) -> Path:
    raw_path = Path(database_path).expanduser()
    if str(raw_path) == ":memory:":
        raise SingleInstanceLockError(
            "in-memory SQLite не поддерживает межпроцессную защиту; запуск отменён"
        )
    try:
        return raw_path.resolve(strict=False)
    except OSError as error:
        raise SingleInstanceLockError(
            "не удалось канонизировать путь SQLite; запуск отменён"
        ) from error
