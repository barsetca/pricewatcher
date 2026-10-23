"""Минимальные asyncio-границы для неотменяемого синхронного кода."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any


async def run_in_daemon_thread(function: Callable[..., Any], /, *args, **kwargs) -> Any:
    """Выполняет sync callable вне event loop без default ThreadPoolExecutor.

    Отмена await не пытается небезопасно остановить поток. Daemon thread не
    удерживает завершение процесса после исчерпания общего shutdown budget.
    """
    completed = threading.Event()
    outcome: dict[str, Any] = {}

    def target() -> None:
        try:
            outcome["result"] = function(*args, **kwargs)
        except BaseException as error:
            outcome["error"] = error
        finally:
            completed.set()

    threading.Thread(target=target, name="pricewatch-blocking", daemon=True).start()
    while not completed.is_set():
        await asyncio.sleep(0.01)
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")
