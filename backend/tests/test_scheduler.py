import asyncio
from unittest.mock import AsyncMock

import pytest

from app.scheduler import Scheduler, ScheduledTask


async def test_scheduler_starts_and_stops():
    sched = Scheduler()
    task = AsyncMock()
    sched.register("test", ScheduledTask(callback=task, interval=0.05))
    await sched.start()
    await asyncio.sleep(0.15)
    await sched.stop()
    assert task.call_count >= 1


async def test_scheduler_stop_without_start():
    sched = Scheduler()
    await sched.stop()  # не падает


async def test_scheduler_runs_registered_task():
    sched = Scheduler()
    call_count = 0

    async def counter():
        nonlocal call_count
        call_count += 1

    sched.register("counter", ScheduledTask(callback=counter, interval=0.02))
    await sched.start()
    await asyncio.sleep(0.07)
    await sched.stop()
    assert call_count >= 2


async def test_scheduler_isolates_task_errors():
    sched = Scheduler()
    good_count = 0

    async def failing():
        raise RuntimeError("boom")

    async def good():
        nonlocal good_count
        good_count += 1

    sched.register("failing", ScheduledTask(callback=failing, interval=0.02))
    sched.register("good", ScheduledTask(callback=good, interval=0.02))
    await sched.start()
    await asyncio.sleep(0.07)
    await sched.stop()
    assert good_count >= 1


async def test_scheduler_unregister_removes_task():
    sched = Scheduler()
    task = AsyncMock()
    sched.register("test", ScheduledTask(callback=task, interval=0.02))
    sched.unregister("test")
    await sched.start()
    await asyncio.sleep(0.05)
    await sched.stop()
    task.assert_not_awaited()


async def test_register_updates_live_interval_without_restart():
    sched = Scheduler()
    task = AsyncMock()
    sched.register("test", ScheduledTask(callback=task, interval=60))
    await sched.start()
    await asyncio.sleep(0.02)
    assert task.await_count == 1

    sched.register("test", ScheduledTask(callback=task, interval=0.01))
    await asyncio.sleep(0.04)
    await sched.stop()

    assert task.await_count >= 2


async def test_wake_runs_registered_task_without_waiting_for_interval():
    sched = Scheduler()
    task = AsyncMock()
    sched.register("test", ScheduledTask(callback=task, interval=60))
    await sched.start()
    await asyncio.sleep(0.02)
    assert task.await_count == 1

    assert sched.wake("test") is True
    await asyncio.sleep(0.02)
    await sched.stop()

    assert task.await_count >= 2


async def test_wake_is_noop_for_missing_or_stopped_task():
    sched = Scheduler()
    assert sched.wake("missing") is False
