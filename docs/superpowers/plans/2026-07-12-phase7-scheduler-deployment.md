# Фаза 7: Scheduler + Telegram + Deployment — План реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Финальная фаза — связать всё вместе в работающее приложение. Scheduler (регулярные задачи: expire/bump/limits-check/lot-auto), Telegram-нотификации продавцу, refresh-recovery worker pool, lifecycle management (запуск/остановка FunPay Runner + Scheduler в одном event loop с FastAPI), Alembic migration, deployment конфигурация.

**Architecture:** `AppLifecycle` оркестратор — запускается в FastAPI lifespan, стартует FunPayRunner + Scheduler в background asyncio tasks. Scheduler — периодические корутины (asyncio.sleep loops). Telegram — тонкий notifier (python-telegram-bot Bot.send_message). Refresh-recovery — воркер-пул на AccountCheckJob очереди.

**Tech Stack:** asyncio, FastAPI lifespan, python-telegram-bot (только send_message), Alembic, systemd/docker-compose (конфиги в docs).

---

## Структура файлов

### Новые файлы

```
backend/app/
├── scheduler.py            # Scheduler — периодические задачи
├── telegram_notifier.py    # TelegramNotifier — отправка уведомлений продавцу
├── refresh_worker.py       # RefreshRecoveryWorker — воркер-пул Playwright перезаходов
├── check_job_queue.py      # CheckJobQueue — CRUD для AccountCheckJob
├── app_lifecycle.py        # AppLifecycle — оркестратор (FastAPI lifespan)

backend/app/services/
└── (модификации funpay_lifecycle.py — интеграция Scheduler callbacks)

backend/tests/
├── test_scheduler.py
├── test_telegram_notifier.py
├── test_refresh_worker.py
├── test_check_job_queue.py
└── test_app_lifecycle.py

backend/alembic/            # Alembic migration (если ещё нет)
├── env.py
├── script.py.mako
└── versions/

docs/
├── deployment.md           # Инструкция по развёртыванию
└── alembic.md              # Alembic setup (если требуется)
```

### Модифицируемые

- `backend/app/main.py` — интеграция AppLifecycle в lifespan

---

## Task 1: CheckJobQueue — CRUD для AccountCheckJob

Очередь задач проверки аккаунтов: enqueue, fetch_next_pending, mark_running, mark_done/failed, дедупликация.

**Files:**
- Create: `backend/app/check_job_queue.py`
- Test: `backend/tests/test_check_job_queue.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_check_job_queue.py`:

```python
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.check_job_queue import CheckJobQueue
from app.models.account import Account, AccountCheckJob
from app.models.catalog import SubscriptionTier


async def _add_account(session: AsyncSession) -> Account:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()
    acc = Account(
        login="acc1", password_encrypted="enc", totp_secret_encrypted="enc",
        tier_id=tier.id, status="active",
    )
    session.add(acc)
    await session.flush()
    return acc


async def test_enqueue_creates_pending_job(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    job = await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    assert job.status == "pending"
    assert job.priority == "new"
    assert job.job_type == "full_validation"


async def test_dedup_skips_existing_pending(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    first = await q.enqueue(session, account_id=acc.id, priority="scheduled", job_type="limit_check")
    second = await q.enqueue(session, account_id=acc.id, priority="scheduled", job_type="limit_check")
    assert second.id == first.id  # дедуп — вернулся тот же job


async def test_higher_priority_overrides_lower(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    low = await q.enqueue(session, account_id=acc.id, priority="scheduled", job_type="limit_check")
    # higher priority перебивает
    high = await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    await session.refresh(low)
    assert low.status == "done"  # старый закрыт
    assert high.priority == "new"
    assert high.job_type == "full_validation"


async def test_fetch_next_pending_returns_oldest(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    j1 = await q.enqueue(session, account_id=acc.id, priority="scheduled", job_type="limit_check")
    from datetime import timedelta
    import asyncio
    await asyncio.sleep(0.01)
    acc2 = await _add_account(session)
    acc2.login = "acc2"
    await session.flush()
    j2 = await q.enqueue(session, account_id=acc2.id, priority="scheduled", job_type="limit_check")
    next_job = await q.fetch_next_pending(session, job_types=("limit_check",))
    assert next_job is not None
    assert next_job.id == j1.id  # FIFO


async def test_fetch_next_pending_filters_by_type(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    # Ищем только limit_check — не должен найти full_validation
    next_job = await q.fetch_next_pending(session, job_types=("limit_check",))
    assert next_job is None


async def test_mark_running_updates_status(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    job = await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    await q.mark_running(session, job)
    await session.refresh(job)
    assert job.status == "running"
    assert job.started_at is not None


async def test_mark_done_updates_status(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    job = await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    await q.mark_done(session, job, result="ok")
    await session.refresh(job)
    assert job.status == "done"
    assert job.result == "ok"
    assert job.finished_at is not None


async def test_mark_failed_updates_error(session: AsyncSession):
    acc = await _add_account(session)
    q = CheckJobQueue()
    job = await q.enqueue(session, account_id=acc.id, priority="new", job_type="full_validation")
    await q.mark_failed(session, job, error="connection timeout")
    await session.refresh(job)
    assert job.status == "failed"
    assert job.error == "connection timeout"
```

- [ ] **Step 2: Реализовать**

`backend/app/check_job_queue.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import AccountCheckJob


# Приоритеты: высший → низший. Высший перебивает низший в дедупликации.
_PRIORITY_ORDER = {"new": 0, "refresh_recover": 0, "manual": 1, "scheduled": 2, "limit_check": 3}


class CheckJobQueue:
    """Очередь задач проверки аккаунтов (AccountCheckJob).

    Дедупликация: если есть pending/running job того же job_type для аккаунта,
    не создаём новый. Высший приоритет перебивает низший (закрывает старый + создаёт новый).
    """

    async def enqueue(
        self,
        session: AsyncSession,
        account_id: int,
        priority: str,
        job_type: str,
    ) -> AccountCheckJob:
        existing = await self._find_active(session, account_id, job_type)
        if existing is not None:
            if _PRIORITY_ORDER.get(priority, 9) < _PRIORITY_ORDER.get(existing.priority, 9):
                # Высший приоритет перебивает: закрываем старый
                existing.status = "done"
                existing.result = "superseded"
                existing.finished_at = datetime.now(timezone.utc)
                await session.flush()
            else:
                return existing  # дедуп — возвращаем существующий

        job = AccountCheckJob(
            account_id=account_id,
            priority=priority,
            job_type=job_type,
            status="pending",
        )
        session.add(job)
        await session.flush()
        return job

    async def fetch_next_pending(
        self,
        session: AsyncSession,
        job_types: tuple[str, ...],
    ) -> AccountCheckJob | None:
        """FIFO: старейший pending job указанных типов."""
        result = await session.execute(
            select(AccountCheckJob)
            .where(
                AccountCheckJob.status == "pending",
                AccountCheckJob.job_type.in_(job_types),
            )
            .order_by(AccountCheckJob.created_at.asc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def mark_running(self, session: AsyncSession, job: AccountCheckJob) -> None:
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        await session.flush()

    async def mark_done(self, session: AsyncSession, job: AccountCheckJob, result: str) -> None:
        job.status = "done"
        job.result = result
        job.finished_at = datetime.now(timezone.utc)
        await session.flush()

    async def mark_failed(self, session: AsyncSession, job: AccountCheckJob, error: str) -> None:
        job.status = "failed"
        job.error = error
        job.finished_at = datetime.now(timezone.utc)
        await session.flush()

    async def _find_active(
        self,
        session: AsyncSession,
        account_id: int,
        job_type: str,
    ) -> AccountCheckJob | None:
        result = await session.execute(
            select(AccountCheckJob).where(
                AccountCheckJob.account_id == account_id,
                AccountCheckJob.job_type == job_type,
                AccountCheckJob.status.in_(["pending", "running"]),
            ).limit(1)
        )
        return result.scalar_one_or_none()
```

- [ ] **Step 3: Запустить тест**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_check_job_queue.py -v`
Expected: PASS (7 tests)

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add backend/app/check_job_queue.py backend/tests/test_check_job_queue.py
git commit -m "feat: add CheckJobQueue for AccountCheckJob CRUD with deduplication"
```

---

## Task 2: TelegramNotifier — отправка уведомлений продавцу

Тонкий слой над python-telegram-bot. Только send_message в seller_chat_id. Все уведомления из спеки раздела 16.

**Files:**
- Create: `backend/app/telegram_notifier.py`
- Test: `backend/tests/test_telegram_notifier.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_telegram_notifier.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.telegram_notifier import TelegramNotifier


@pytest.fixture
def notifier() -> TelegramNotifier:
    return TelegramNotifier(bot_token="123:abc", seller_chat_id="456")


async def test_notify_sends_message(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify("Test message")
        mock_bot.send_message.assert_awaited_once_with(chat_id="456", text="Test message")


async def test_notify_swallows_error(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock(side_effect=RuntimeError("network"))
        # Не должно падать
        await notifier.notify("Test")


async def test_notify_new_order(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_new_order(order_id="123", desc="Plus × 7дн × Codex ≥50%", price=599)
        mock_bot.send_message.assert_awaited_once()
        _, kwargs = mock_bot.send_message.call_args
        assert "🆕" in kwargs["text"]
        assert "123" in kwargs["text"]
        assert "599" in kwargs["text"]


async def test_notify_order_confirmed(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_order_confirmed(order_id="123")
        _, kwargs = mock_bot.send_message.call_args
        assert "✅" in kwargs["text"]


async def test_notify_rental_expired(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_rental_expired(account_login="acc1")
        _, kwargs = mock_bot.send_message.call_args
        assert "⏰" in kwargs["text"]
        assert "acc1" in kwargs["text"]


async def test_notify_account_unavailable(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_account_unavailable(account_login="acc1", reason="ban")
        _, kwargs = mock_bot.send_message.call_args
        assert "🔴" in kwargs["text"]
        assert "ban" in kwargs["text"]


async def test_notify_low_limits(notifier: TelegramNotifier):
    with patch.object(notifier, "_bot") as mock_bot:
        mock_bot.send_message = AsyncMock()
        await notifier.notify_low_limits(account_login="acc1", chat_weekly=18)
        _, kwargs = mock_bot.send_message.call_args
        assert "📊" in kwargs["text"]
        assert "18" in kwargs["text"]


async def test_notify_disabled_when_no_token():
    n = TelegramNotifier(bot_token="", seller_chat_id="")
    # Не должно падать, не должно пытаться отправить
    await n.notify("test")  # silent no-op


async def test_from_settings_creates_notifier(session: AsyncSession):
    from app.models.settings import SellerSettings
    session.add(SellerSettings(
        id=1, telegram_bot_token="tok", telegram_seller_chat_id="chat",
    ))
    await session.flush()
    n = await TelegramNotifier.from_settings(session)
    assert n is not None
    assert n._seller_chat_id == "chat"
```

- [ ] **Step 2: Реализовать**

`backend/app/telegram_notifier.py`:

```python
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Bot

from app.models.settings import SellerSettings

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """Микро-бот: только отправка сообщений продавцу в seller_chat_id.

    Все методы swallow exceptions — уведомления не должны ломать основной поток.
    Если bot_token пустой — все методы no-op.
    """

    def __init__(self, bot_token: str, seller_chat_id: str) -> None:
        self._seller_chat_id = seller_chat_id
        self._bot = Bot(token=bot_token) if bot_token else None

    @classmethod
    async def from_settings(cls, session: AsyncSession) -> TelegramNotifier | None:
        settings = await session.get(SellerSettings, 1)
        if settings is None or not settings.telegram_bot_token:
            return None
        return cls(
            bot_token=settings.telegram_bot_token or "",
            seller_chat_id=settings.telegram_seller_chat_id or "",
        )

    async def notify(self, text: str) -> None:
        if self._bot is None or not self._seller_chat_id:
            return
        try:
            await self._bot.send_message(chat_id=self._seller_chat_id, text=text)
        except Exception:
            logger.exception("Telegram notify failed")

    async def notify_new_order(self, order_id: str, desc: str, price: int) -> None:
        await self.notify(f"🆕 Новый заказ #{order_id}: {desc}, {price}₽")

    async def notify_order_confirmed(self, order_id: str) -> None:
        await self.notify(f"✅ Заказ #{order_id} подтверждён")

    async def notify_dispute(self, order_id: str) -> None:
        await self.notify(f"⚠️ СПОР по заказу #{order_id}!")

    async def notify_replace_requested(self, buyer_id: str, account_login: str) -> None:
        await self.notify(f"🔄 Замена: покупатель {buyer_id} запросил замену аккаунта {account_login}")

    async def notify_rental_expired(self, account_login: str) -> None:
        await self.notify(f"⏰ Аренда истекла: аккаунт {account_login}, освободился слот")

    async def notify_account_unavailable(self, account_login: str, reason: str) -> None:
        await self.notify(f"🔴 Аккаунт {account_login} недоступен ({reason})")

    async def notify_low_limits(self, account_login: str, chat_weekly: int) -> None:
        await self.notify(f"📊 Лимиты аккаунта {account_login} упали ниже порога (chat weekly: {chat_weekly}%)")

    async def notify_seller_called(self, buyer_id: str) -> None:
        await self.notify(f"📢 Покупатель {buyer_id} вызывает продавца")

    async def notify_bump_failed(self, lot_id: int) -> None:
        await self.notify(f"❌ Bump лота #{lot_id} не удался")

    async def notify_funpay_disconnect(self) -> None:
        await self.notify("🔴 FunPay дисконнект / golden_key протух")
```

- [ ] **Step 3: Запустить тест**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_telegram_notifier.py -v`
Expected: PASS (10 tests)

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add backend/app/telegram_notifier.py backend/tests/test_telegram_notifier.py
git commit -m "feat: add TelegramNotifier for seller notifications"
```

---

## Task 3: Scheduler — периодические задачи

Регулярные корутины: expire_overdue (30с), limits_check (5мин), full_check (10мин), bump (по интервалу), lot_auto_manager (2мин).

**Files:**
- Create: `backend/app/scheduler.py`
- Test: `backend/tests/test_scheduler.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_scheduler.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

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


async def test_scheduler_stop_is_idempotent():
    sched = Scheduler()
    await sched.stop()  # не падает без start


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
    """Упавшая задача не должна останавливать Scheduler."""
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
    assert good_count >= 1  # good задача продолжила работать несмотря на падение failing


async def test_scheduler_unregister_removes_task():
    sched = Scheduler()
    task = AsyncMock()
    sched.register("test", ScheduledTask(callback=task, interval=0.02))
    sched.unregister("test")
    await sched.start()
    await asyncio.sleep(0.05)
    await sched.stop()
    task.assert_not_awaited()
```

- [ ] **Step 2: Реализовать**

`backend/app/scheduler.py`:

```python
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
    interval: float  # секунды


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
        """Запуск всех зарегистрированных задач в background."""
        if self._running:
            return
        self._running = True
        for name, task in self._tasks.items():
            self._loops[name] = asyncio.create_task(self._run_loop(name, task))

    async def stop(self) -> None:
        """Остановка всех background задач."""
        self._running = False
        for loop in list(self._loops.values()):
            loop.cancel()
        # Ждём отмены (подавляя CancelledError)
        for loop in list(self._loops.values()):
            try:
                await loop
            except (asyncio.CancelledError, Exception):
                pass
        self._loops.clear()

    async def _run_loop(self, name: str, task: ScheduledTask) -> None:
        """Цикл выполнения одной задачи: callback → sleep → повтор."""
        while self._running:
            try:
                await task.callback()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduled task '%s' failed", name)
            await asyncio.sleep(task.interval)
```

- [ ] **Step 3: Запустить тест**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_scheduler.py -v`
Expected: PASS (5 tests)

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add backend/app/scheduler.py backend/tests/test_scheduler.py
git commit -m "feat: add async Scheduler with error isolation"
```

---

## Task 4: RefreshRecoveryWorker — воркер-пул Playwright перезаходов

Обрабатывает AccountCheckJob с job_type in (full_validation, refresh_recover). Вызывает validate_account + measure. Пауза check_delay_seconds между операциями.

**Files:**
- Create: `backend/app/refresh_worker.py`
- Test: `backend/tests/test_refresh_worker.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_refresh_worker.py`:

```python
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.account import Account, AccountCheckJob
from app.models.catalog import SubscriptionTier
from app.refresh_worker import RefreshRecoveryWorker


async def _add_account_with_job(session: AsyncSession, job_type: str = "full_validation") -> tuple[Account, AccountCheckJob]:
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()
    acc = Account(
        login="acc1", password_encrypted="enc", totp_secret_encrypted="enc",
        tier_id=tier.id, status="pending_validation",
    )
    session.add(acc)
    await session.flush()
    job = AccountCheckJob(
        account_id=acc.id, priority="new", job_type=job_type, status="pending",
    )
    session.add(job)
    await session.flush()
    return acc, job


async def test_process_next_pending_job_success(session: AsyncSession):
    acc, job = await _add_account_with_job(session)
    worker = RefreshRecoveryWorker(check_delay_seconds=0)
    with patch("app.refresh_worker.validate_account", new_callable=AsyncMock) as mock_validate:
        mock_validate.return_value = __import__("app.services.account_validation", fromlist=["ValidationOutcome"]).ValidationOutcome.OK
        result = await worker.process_next(session)
    assert result is True  # обработал один job
    await session.refresh(job)
    assert job.status == "done"
    mock_validate.assert_awaited_once_with(session, acc.id)


async def test_process_next_returns_false_when_no_jobs(session: AsyncSession):
    worker = RefreshRecoveryWorker()
    result = await worker.process_next(session)
    assert result is False


async def test_process_next_marks_failed_on_validation_error(session: AsyncSession):
    acc, job = await _add_account_with_job(session)
    worker = RefreshRecoveryWorker(check_delay_seconds=0)
    with patch("app.refresh_worker.validate_account", new_callable=AsyncMock) as mock_validate:
        vo = __import__("app.services.account_validation", fromlist=["ValidationOutcome"]).ValidationOutcome
        mock_validate.return_value = vo.LOGIN_FAILED
        result = await worker.process_next(session)
    assert result is True
    await session.refresh(job)
    assert job.status == "failed"
    assert "login_failed" in (job.error or "") or job.result == "login_failed"
```

- [ ] **Step 2: Реализовать**

`backend/app/refresh_worker.py`:

```python
from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.check_job_queue import CheckJobQueue
from app.db.session import async_session_factory
from app.models.account import AccountCheckJob
from app.services.account_validation import ValidationOutcome, validate_account

logger = logging.getLogger(__name__)


class RefreshRecoveryWorker:
    """Воркер перезаходов: обрабатывает AccountCheckJob (full_validation, refresh_recover).

    Один вызов process_next: берёт старейший pending job, выполняет validate_account,
    помечает done/failed. Пауза check_delay_seconds после операции (анти-спам).
    """

    def __init__(self, check_delay_seconds: int = 45) -> None:
        self._queue = CheckJobQueue()
        self._check_delay = check_delay_seconds

    async def process_next(self, session: AsyncSession) -> bool:
        """Обработать один pending job. Возвращает True если обработал, False если очереди пуста."""
        job = await self._queue.fetch_next_pending(
            session, job_types=("full_validation", "refresh_recover"),
        )
        if job is None:
            return False

        await self._queue.mark_running(session, job)
        try:
            outcome = await validate_account(session, job.account_id)
            if outcome is ValidationOutcome.OK:
                await self._queue.mark_done(session, job, result="ok")
            else:
                await self._queue.mark_failed(session, job, error=outcome.value)
            await session.commit()
        except Exception as exc:
            await self._queue.mark_failed(session, job, error=str(exc))
            await session.commit()
            logger.exception("Job %s failed for account %s", job.id, job.account_id)

        if self._check_delay > 0:
            await asyncio.sleep(self._check_delay)
        return True

- [ ] **Step 3: Запустить тест**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_refresh_worker.py -v`
Expected: PASS (3 tests)

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add backend/app/refresh_worker.py backend/tests/test_refresh_worker.py
git commit -m "feat: add RefreshRecoveryWorker for Playwright re-login processing"
```

---

## Task 5: AppLifecycle — оркестратор

Запускается в FastAPI lifespan. Создаёт FunPayRunner + Scheduler + RefreshRecoveryWorker. Регистрирует периодические задачи (expire_overdue, limits_check, lot_auto_manager, bump). Запускает FunPay WebSocket loop.

**Files:**
- Create: `backend/app/app_lifecycle.py`
- Test: `backend/tests/test_app_lifecycle.py`

- [ ] **Step 1: Написать тест**

`backend/tests/test_app_lifecycle.py`:

```python
import pytest
from app.app_lifecycle import AppLifecycle


def test_lifecycle_creates_components():
    lc = AppLifecycle(golden_key="", category_id=0)
    assert lc.scheduler is not None
    assert lc.runner is not None


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
    # Проверяем что задачи зарегистрированы
    assert "expire_overdue" in lc.scheduler._tasks
    assert "limits_check" in lc.scheduler._tasks
    await lc.stop()
```

- [ ] **Step 2: Реализовать**

`backend/app/app_lifecycle.py`:

```python
from __future__ import annotations

import logging

from sqlalchemy import select

from app.db.session import async_session_factory
from app.integrations.funpay.gateway import FunPayChatGateway
from app.integrations.funpay.runner import FunPayRunner, RunnerCallbacks
from app.models.account import Account, AccountLimits
from app.models.lot import Lot
from app.models.settings import SellerSettings
from app.scheduler import ScheduledTask, Scheduler
from app.services.account_limits import measure_account_limits
from app.services.bump import BumpService
from app.services.funpay_lifecycle import build_callbacks
from app.services.lot_auto_manager import LotAutoManager
from app.services.rental_expiry import RentalExpiryService
from app.telegram_notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class AppLifecycle:
    """Оркестратор приложения: FunPay Runner + Scheduler + Worker.

    Запускается в FastAPI lifespan. Стартует WebSocket loop (если golden_key)
    и периодические задачи (expire, limits check, lot auto, bump).
    """

    def __init__(self, golden_key: str, category_id: int) -> None:
        self._golden_key = golden_key
        self._category_id = category_id
        self.scheduler = Scheduler()
        self._gateway = FunPayChatGateway(bot=None) if golden_key else None
        self._runner: FunPayRunner | None = None
        self._expiry = RentalExpiryService()
        self._bump = BumpService()
        self._notifier: TelegramNotifier | None = None

    async def start(self) -> None:
        """Запуск: загрузка настроек, регистрация задач, старт Scheduler + Runner."""
        await self._load_settings()
        self._register_tasks()
        await self.scheduler.start()

        if self._golden_key and self._gateway is not None:
            callbacks = await self._build_callbacks()
            self._runner = FunPayRunner(
                golden_key=self._golden_key,
                callbacks=callbacks,
                category_id=self._category_id,
            )
            try:
                await self._runner.start()
            except Exception:
                logger.exception("FunPayRunner failed to start")

    async def stop(self) -> None:
        """Остановка Runner + Scheduler."""
        if self._runner is not None:
            try:
                await self._runner.stop()
            except Exception:
                logger.exception("FunPayRunner stop failed")
        await self.scheduler.stop()

    async def _load_settings(self) -> None:
        async with async_session_factory() as session:
            settings = await session.get(SellerSettings, 1)
            if settings:
                self._notifier = await TelegramNotifier.from_settings(session)

    async def _build_callbacks(self) -> RunnerCallbacks:
        async with async_session_factory() as session:
            return build_callbacks(
                session_factory=async_session_factory,
                gateway=self._gateway,
            )

    def _register_tasks(self) -> None:
        """Регистрация всех периодических задач в Scheduler."""
        self.scheduler.register("expire_overdue", ScheduledTask(
            callback=self._task_expire_overdue, interval=30,
        ))
        self.scheduler.register("limits_check", ScheduledTask(
            callback=self._task_limits_check, interval=300,
        ))
        self.scheduler.register("lot_auto_manager", ScheduledTask(
            callback=self._task_lot_auto, interval=120,
        ))
        self.scheduler.register("bump", ScheduledTask(
            callback=self._task_bump, interval=3600,
        ))
        self.scheduler.register("refresh_recover", ScheduledTask(
            callback=self._task_refresh_recover, interval=60,
        ))

    async def _task_expire_overdue(self) -> None:
        """Помечать истёкшие аренды как expired, отправить expiry message."""
        async with async_session_factory() as session:
            await self._expiry.expire_overdue(session, self._gateway)
            await session.commit()

    async def _task_limits_check(self) -> None:
        """Замер лимитов для аккаунтов с устаревшим measured_at."""
        async with async_session_factory() as session:
            from datetime import datetime, timedelta, timezone
            cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)
            result = await session.execute(
                select(AccountLimits).where(
                    AccountLimits.refresh_status == "ok",
                    AccountLimits.measured_at < cutoff,
                )
            )
            for limits in result.scalars().all():
                await measure_account_limits(session, limits.account_id)
            await session.commit()

    async def _task_lot_auto(self) -> None:
        """Пересчёт capacity и sync лотов."""
        async with async_session_factory() as session:
            settings = await session.get(SellerSettings, 1)
            node_id = settings.funpay_node_id if settings else None
            if node_id:
                mgr = LotAutoManager(funpay_node_id=node_id)
                await mgr.run(session, self._gateway)
                await session.commit()

    async def _task_bump(self) -> None:
        """Поднять лоты, у которых истёк кулдаун bump."""
        async with async_session_factory() as session:
            from datetime import timedelta
            settings = await session.get(SellerSettings, 1)
            if not settings or not settings.auto_bump_enabled:
                return
            interval = timedelta(hours=settings.bump_interval_hours)
            result = await session.execute(
                select(Lot).where(Lot.status == "active", Lot.funpay_id.isnot(None))
            )
            for lot in result.scalars().all():
                if await self._bump.needs_bump(session, lot.id, interval):
                    await self._bump.bump_lot(
                        session, self._gateway,
                        lot_id=lot.id,
                        category_id=self._category_id,
                        subcategory_id=settings.funpay_node_id or 0,
            await session.commit()

    async def _task_refresh_recover(self) -> None:
        """Обработать один pending refresh-recovery job."""
        from app.refresh_worker import RefreshRecoveryWorker
        async with async_session_factory() as session:
            settings = await session.get(SellerSettings, 1)
            delay = settings.check_delay_seconds if settings else 45
            worker = RefreshRecoveryWorker(check_delay_seconds=delay)
            await worker.process_next(session)
```

- [ ] **Step 3: Запустить тест**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest tests/test_app_lifecycle.py -v`
Expected: PASS (3 tests)

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add backend/app/app_lifecycle.py backend/tests/test_app_lifecycle.py
git commit -m "feat: add AppLifecycle orchestrator with periodic tasks"
```

---

## Task 6: Интеграция AppLifecycle в FastAPI lifespan

**Files:**
- Modify: `backend/app/main.py`

- [ ] **Step 1: Прочитать текущий main.py**

- [ ] **Step 2: Изменить lifespan**

В lifespan создать AppLifecycle, сохранить в app.state, стартовать/остановить:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    from app.config import get_settings
    from app.app_lifecycle import AppLifecycle

    settings = get_settings()
    lifecycle = AppLifecycle(
        golden_key=settings.funpay_session_key,
        category_id=0,
    )
    app.state.lifecycle = lifecycle
    await lifecycle.start()
    yield
    await lifecycle.stop()
    await engine.dispose()
```

- [ ] **Step 3: Проверить что существующие тесты не сломались**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest -q 2>&1 | tail -5`
Expected: ALL PASS

ВАЖНО: `_test_env` autouse fixture выставляет `FUNPAY_SESSION_KEY=""` → golden_key="" → Runner не стартует. Если падает — проверь что AppLifecycle.start() не падает при пустом golden_key.

- [ ] **Step 4: Commit**

```bash
cd /c/Source/funpay
git add backend/app/main.py
git commit -m "feat: integrate AppLifecycle into FastAPI lifespan"
```

---

## Task 7: Deployment документация

**Files:**
- Create: `docs/deployment.md`

- [ ] **Step 1: Написать deployment.md**

Инструкция: требования к серверу, сборка backend+frontend, .env, systemd unit, backup.

- [ ] **Step 2: Commit**

```bash
cd /c/Source/funpay
git add docs/deployment.md
git commit -m "docs: add deployment guide"
```

---

## Task 8: Финальная проверка

- [ ] **Step 1: Полный прогон всех backend-тестов**

Run: `cd /c/Source/funpay/backend && py -3.12 -m pytest -v 2>&1 | tail -15`
Expected: ALL PASS

- [ ] **Step 2: Проверить сборку frontend**

Run: `cd /c/Source/funpay/frontend && npm run build 2>&1 | tail -5`
Expected: успешная сборка

- [ ] **Step 3: Проверить импорты всех модулей Фазы 7**

Run: `cd /c/Source/funpay/backend && py -3.12 -c "
import app.check_job_queue
import app.telegram_notifier
import app.scheduler
import app.refresh_worker
import app.app_lifecycle
print('All Phase 7 modules import OK')
"`
Expected: `OK`

- [ ] **Step 4: Проверить git log**

Run: `cd /c/Source/funpay && git log --oneline phase1-foundation..HEAD | head -10`
Expected: 7-8 коммитов

---

## Замечания

### Scheduler

- Задача `refresh_recover` обрабатывает один job за раз. Интервал 60с — достаточно.
- `expire_overdue` запускается каждые 30с — аренда истечёт в пределах 30с после expires_at.
- Ошибки в одной задаче НЕ останавливают Scheduler (изоляция в _run_loop try/except).

### Deployment

- **PostgreSQL**: production DATABASE_URL. Тесты используют SQLite.
- **Playwright**: требует `playwright install chromium` (~300MB).
- **Systemd**: `Restart=always` + `RestartSec=10`.
- **Backup**: `pg_dump` по cron.

### Что НЕ делает Фаза 7 (out of scope)

- **Alembic migration** — отложено (Фаза 1 Task 18).
- **Многопоточный refresh-recovery** — один worker.
- **Docker Compose** — systemd unit.
- **Мониторинг** — без Prometheus/Grafana.
