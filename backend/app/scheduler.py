from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScheduledTask:
    """Периодическая задача: callback вызывается каждые interval секунд."""

    callback: Callable[[], Awaitable[None]]
    interval: float


class Scheduler:
    """Периодический планировщик задач на asyncio.

    Регистрирует задачи, запускает их в background корутинах.
    Ошибки в одной задаче не останавливают другие (изоляция).
    """

    def __init__(self) -> None:
        self._tasks: dict[str, ScheduledTask] = {}
        self._loops: dict[str, asyncio.Task] = {}
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def register(self, name: str, task: ScheduledTask) -> None:
        self._tasks[name] = task

    def unregister(self, name: str) -> None:
        self._tasks.pop(name, None)
        loop = self._loops.pop(name, None)
        if loop is not None:
            loop.cancel()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for name, task in self._tasks.items():
            self._loops[name] = asyncio.create_task(self._run_loop(name, task))

    async def stop(self) -> None:
        self._running = False
        for loop in list(self._loops.values()):
            loop.cancel()
        for loop in list(self._loops.values()):
            try:
                await loop
            except (asyncio.CancelledError, Exception):
                pass
        self._loops.clear()

    async def _run_loop(self, name: str, task: ScheduledTask) -> None:
        while self._running:
            try:
                await task.callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled task '%s' failed", name)
            await asyncio.sleep(task.interval)
