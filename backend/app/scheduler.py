from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
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
        self._wakeups: dict[str, asyncio.Event] = {}
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def register(self, name: str, task: ScheduledTask) -> None:
        """Register a task or update its callback/interval at runtime.

        Updating a live task wakes its current delay without cancelling a
        callback that may be in the middle of a database or network operation.
        """
        current = self._tasks.get(name)
        if (
            current is not None
            and current.callback == task.callback
            and current.interval == task.interval
        ):
            return
        self._tasks[name] = task
        if self._running and name in self._loops:
            self._wakeups[name].set()
        elif self._running:
            self._wakeups[name] = asyncio.Event()
            self._loops[name] = asyncio.create_task(self._run_loop(name))

    def unregister(self, name: str) -> None:
        self._tasks.pop(name, None)
        self._wakeups.pop(name, None)
        loop = self._loops.pop(name, None)
        if loop is not None:
            loop.cancel()

    def wake(self, name: str) -> bool:
        """Run a registered task after its current callback finishes.

        Queue producers use this to avoid waiting for the full periodic delay.
        Repeated calls coalesce through the task's existing ``Event``.
        """

        wakeup = self._wakeups.get(name)
        if not self._running or wakeup is None or name not in self._tasks:
            return False
        wakeup.set()
        return True

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for name in self._tasks:
            self._wakeups[name] = asyncio.Event()
            self._loops[name] = asyncio.create_task(self._run_loop(name))

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
        self._wakeups.clear()

    async def _run_loop(self, name: str) -> None:
        while self._running and name in self._tasks:
            task = self._tasks[name]
            try:
                await task.callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled task '%s' failed", name)
            wakeup = self._wakeups.get(name)
            if wakeup is None:
                return
            try:
                await asyncio.wait_for(wakeup.wait(), timeout=task.interval)
            except TimeoutError:
                pass
            else:
                wakeup.clear()
