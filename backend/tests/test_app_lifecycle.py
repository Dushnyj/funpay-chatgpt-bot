import pytest
from app.app_lifecycle import AppLifecycle


def test_lifecycle_creates_components():
    lc = AppLifecycle(golden_key="", category_id=0)
    assert lc.scheduler is not None
    assert lc.runner is None  # без golden_key


async def test_lifecycle_start_stop_without_golden_key():
    """Без golden_key Runner не стартует, но Scheduler должен работать."""
    lc = AppLifecycle(golden_key="", category_id=0)
    await lc.start()
    assert lc.scheduler.running is True
    await lc.stop()
    assert lc.scheduler.running is False


async def test_register_periodic_tasks():
    lc = AppLifecycle(golden_key="", category_id=0)
    await lc.start()
    assert "expire_overdue" in lc.scheduler._tasks
    assert "limits_check" in lc.scheduler._tasks
    assert "lot_auto_manager" in lc.scheduler._tasks
    assert "bump" in lc.scheduler._tasks
    assert "refresh_recover" in lc.scheduler._tasks
    await lc.stop()


async def test_stop_is_idempotent():
    lc = AppLifecycle(golden_key="", category_id=0)
    await lc.stop()  # без start — не падает
    await lc.stop()
