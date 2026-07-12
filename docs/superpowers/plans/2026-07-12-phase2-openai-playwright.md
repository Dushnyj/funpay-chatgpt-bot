# Phase 2: OpenAI + Playwright Integration — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Реализовать интеграцию с OpenAI: HTTP-клиент к backend-api (замеры лимитов, подписка), OAuth refresh-флоу, Playwright-автоматизацию (первичная валидация аккаунта → refresh_token, кик через logout all, перезаход при протухании refresh_token). Все за изолирующими интерфейсами (`OpenAIClient`, `BrowserAutomation`).

**Architecture:** Два изолированных слоя. `OpenAIClient` (httpx) — чистый HTTP к backend-api, обновление access_token через refresh_token, парсинг JWT. `BrowserAutomation` (Playwright) — headless Chromium для операций, требующих реального браузера: OAuth device flow при добавлении аккаунта, logout all при кике, перезаход при протухании refresh_token. Слои не зависят друг от друга и тестируются независимо (OpenAIClient — через моки/responses, BrowserAutomation — через ручные smoke-тесты, т.к. требует живого аккаунта).

**Tech Stack:** httpx (async), Playwright (Chromium), pyjwt или ручной парсинг JWT, pyotp (уже есть из Фазы 1).

**Spec:** `docs/superpowers/specs/2026-07-11-funpay-chatgpt-rental-bot-design.md` (секции 5, 6, 13, 17)

**Зависимости от Фазы 1:** `Account`, `AccountLimits` модели, `FernetEncrypted`, `services/crypto.py`, `services/totp.py`.

---

## Важное архитектурное уточнение (rate limits)

Из анализа codex-switcher: эндпоинт `/backend-api/wham/usage` возвращает **один** `rate_limit` с `primary_window` (5h) и `secondary_window` (weekly). Разделения на chat/codex в этом эндпоинте **нет** — это общие лимиты аккаунта.

Спека описывает 4 значения (chat_5h, chat_weekly, codex_5h, codex_weekly). Реальность: 2 окна (5h, weekly), применимые к обоим типам. Решение:

- `AccountLimits` хранит 4 поля, но **chat и codex замеры равны** (берутся из одного `rate_limit`).
- При замере заполняем все 4 поля одинаковыми значениями из primary/secondary window.
- Это сохраняет гибкость: если OpenAI когда-нибудь разделит лимиты — поменяем только парсер, не модель.
- Логика выдачи (4-мерная) продолжает работать как задумано, т.к. пороги chat/codex будут совпадать.

---

## File Structure

```
backend/app/
├── integrations/
│   ├── __init__.py
│   ├── openai/
│   │   ├── __init__.py
│   │   ├── client.py          OpenAIClient — HTTP к backend-api
│   │   ├── oauth.py           OAuth refresh + PKCE + JWT parsing
│   │   ├── types.py           Pydantic-схемы ответов (UsageInfo, AccountMetadata)
│   │   └── exceptions.py      OpenAIError, TokenExpiredError, RefreshFailedError
│   └── playwright/
│       ├── __init__.py
│       ├── browser.py         BrowserAutomation — менеджер Chromium
│       ├── oauth_login.py     OAuth device flow через Playwright → refresh_token
│       └── kick.py            logout all через Playwright
└── services/
    └── account_limits.py      Сервис замеров: связывает OpenAIClient + AccountLimits модель
```

```
backend/tests/
├── test_openai_oauth.py       JWT парсинг, refresh-флоу (моки)
├── test_openai_client.py      backend-api запросы (моки httpx)
└── test_account_limits_service.py  связывание замеров с БД
```

---

## Task 1: Pydantic-схемы ответов OpenAI

**Files:**
- Create: `backend/app/integrations/__init__.py`
- Create: `backend/app/integrations/openai/__init__.py`
- Create: `backend/app/integrations/openai/types.py`
- Create: `backend/app/integrations/openai/exceptions.py`
- Test: `backend/tests/test_openai_types.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_openai_types.py
from datetime import datetime, timezone

from app.integrations.openai.types import AccountMetadata, UsageInfo


def test_usage_info_from_api_response():
    """Парсинг ответа /wham/usage с обоими окнами."""
    raw = {
        "plan_type": "plus",
        "rate_limit": {
            "primary_window": {
                "used_percent": 18,
                "limit_window_seconds": 18000,
                "reset_at": "2026-07-12T18:00:00Z",
            },
            "secondary_window": {
                "used_percent": 33,
                "limit_window_seconds": 604800,
                "reset_at": "2026-07-14T00:00:00Z",
            },
        },
    }
    info = UsageInfo.from_api_response(raw)
    assert info.plan_type == "plus"
    assert info.primary_remaining_pct == 82  # 100 - 18
    assert info.secondary_remaining_pct == 67  # 100 - 33


def test_usage_info_handles_missing_windows():
    """Ответ без rate_limit — все pct = None."""
    raw = {"plan_type": "free", "rate_limit": None}
    info = UsageInfo.from_api_response(raw)
    assert info.plan_type == "free"
    assert info.primary_remaining_pct is None
    assert info.secondary_remaining_pct is None


def test_account_metadata_from_accounts_check():
    raw = {
        "accounts": {
            "default": {
                "account": {"plan_type": "plus"},
                "entitlement": {"expires_at": "2026-08-15T00:00:00Z"},
            }
        }
    }
    meta = AccountMetadata.from_accounts_check(raw)
    assert meta.plan_type == "plus"
    assert meta.subscription_expires_at == datetime(2026, 8, 15, tzinfo=timezone.utc)


def test_account_metadata_picks_first_account_if_no_default():
    raw = {
        "accounts": {
            "acc-123": {
                "account": {"plan_type": "pro"},
                "entitlement": {"expires_at": None},
            }
        }
    }
    meta = AccountMetadata.from_accounts_check(raw)
    assert meta.plan_type == "pro"
    assert meta.subscription_expires_at is None
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && py -3.12 -m pytest tests/test_openai_types.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать exceptions.py**

```python
# backend/app/integrations/openai/exceptions.py


class OpenAIError(Exception):
    """Базовая ошибка интеграции с OpenAI backend-api."""


class TokenExpiredError(OpenAIError):
    """Access_token протух и не обновляется."""


class RefreshFailedError(OpenAIError):
    """Refresh_token протух — требуется перезаход через Playwright."""


class BackendApiError(OpenAIError):
    """HTTP-ошибка от backend-api (не 401)."""

    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"backend-api error {status}: {body[:200]}")
```

- [ ] **Step 4: Реализовать types.py**

```python
# backend/app/integrations/openai/types.py
from datetime import datetime

from pydantic import BaseModel


class UsageInfo(BaseModel):
    """Результат замера лимитов из /wham/usage."""

    plan_type: str | None = None
    primary_remaining_pct: int | None = None  # 5h окно
    secondary_remaining_pct: int | None = None  # weekly окно
    primary_resets_at: datetime | None = None
    secondary_resets_at: datetime | None = None

    @classmethod
    def from_api_response(cls, raw: dict) -> "UsageInfo":
        rate_limit = raw.get("rate_limit") or {}
        primary = rate_limit.get("primary_window") or {}
        secondary = rate_limit.get("secondary_window") or {}

        primary_used = primary.get("used_percent")
        secondary_used = secondary.get("used_percent")

        return cls(
            plan_type=raw.get("plan_type"),
            primary_remaining_pct=(100 - primary_used) if primary_used is not None else None,
            secondary_remaining_pct=(100 - secondary_used) if secondary_used is not None else None,
            primary_resets_at=_parse_dt(primary.get("reset_at")),
            secondary_resets_at=_parse_dt(secondary.get("reset_at")),
        )


class AccountMetadata(BaseModel):
    """Метаданные аккаунта из /accounts/check."""

    plan_type: str | None = None
    subscription_expires_at: datetime | None = None

    @classmethod
    def from_accounts_check(cls, raw: dict) -> "AccountMetadata":
        accounts = raw.get("accounts") or {}
        entry = accounts.get("default") or next(iter(accounts.values()), None)
        if entry is None:
            return cls()
        account = entry.get("account") or {}
        entitlement = entry.get("entitlement") or {}
        return cls(
            plan_type=account.get("plan_type"),
            subscription_expires_at=_parse_dt(entitlement.get("expires_at")),
        )


def _parse_dt(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
```

- [ ] **Step 5: Создать пустые __init__.py**

`backend/app/integrations/__init__.py` и `backend/app/integrations/openai/__init__.py` — пустые.

- [ ] **Step 6: Run — verify pass**

Run: `cd backend && py -3.12 -m pytest tests/test_openai_types.py -v`
Expected: PASS (4 passed)

- [ ] **Step 7: Commit**

```bash
cd C:/Source/funpay
git add backend/app/integrations/ backend/tests/test_openai_types.py
git commit -m "feat: add OpenAI types and exceptions for backend-api"
```

---

## Task 2: JWT-парсинг id_token

**Files:**
- Create: `backend/app/integrations/openai/oauth.py` (часть 1: парсинг JWT)
- Test: `backend/tests/test_openai_oauth.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_openai_oauth.py
import base64
import json
from datetime import datetime, timezone

from app.integrations.openai.oauth import IdTokenClaims, parse_id_token


def _make_jwt(payload: dict) -> str:
    """Создаёт минимальный валидный по структуре JWT (без подписи)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."


def test_parse_id_token_extracts_claims():
    jwt = _make_jwt({
        "email": "user@example.com",
        "https://api.openai.com/auth": {"plan_type": "plus"},
        "https://api.openai.com/profile": {"subscription_expires_at": 1723680000},
        "https://api.openai.com/account": {"account_id": "acc-xyz-123"},
    })
    claims = parse_id_token(jwt)
    assert claims.email == "user@example.com"
    assert claims.plan_type == "plus"
    assert claims.account_id == "acc-xyz-123"
    assert claims.subscription_expires_at is not None


def test_parse_id_token_handles_missing_claims():
    jwt = _make_jwt({"email": "minimal@example.com"})
    claims = parse_id_token(jwt)
    assert claims.email == "minimal@example.com"
    assert claims.plan_type is None
    assert claims.account_id is None
    assert claims.subscription_expires_at is None


def test_parse_id_token_invalid_jwt_returns_empty_claims():
    claims = parse_id_token("not.a.valid.jwt.token")
    assert claims.email is None
    assert claims.plan_type is None


def test_id_token_claims_defaults():
    claims = IdTokenClaims()
    assert claims.email is None
    assert claims.plan_type is None
    assert claims.account_id is None
    assert claims.subscription_expires_at is None
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && py -3.12 -m pytest tests/test_openai_oauth.py -v`
Expected: FAIL

- [ ] **Step 3: Реализовать oauth.py (часть 1: JWT)**

```python
# backend/app/integrations/openai/oauth.py
import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Константы из codex-switcher (реверс-инжиниринг OpenAI OAuth)
OPENAI_ISSUER = "https://auth.openai.com"
OPENAI_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_USER_AGENT = "codex-cli/1.0.0"

# Пространства имён claims в id_token OpenAI
_AUTH_NS = "https://api.openai.com/auth"
_PROFILE_NS = "https://api.openai.com/profile"
_ACCOUNT_NS = "https://api.openai.com/account"


@dataclass
class IdTokenClaims:
    email: str | None = None
    plan_type: str | None = None
    account_id: str | None = None
    subscription_expires_at: datetime | None = None


def parse_id_token(token: str) -> IdTokenClaims:
    """Парсит JWT id_token без проверки подписи — извлекает claims.

    Подпись не проверяем: токен получен напрямую от auth.openai.com по TLS,
    доверяем каналу. Проверка подписи добавила бы зависимость от публичного ключа,
    который OpenAI не документирует.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return IdTokenClaims()

    try:
        payload = _decode_jwt_part(parts[1])
    except Exception:
        return IdTokenClaims()

    return IdTokenClaims(
        email=payload.get("email"),
        plan_type=_get_nested(payload, _AUTH_NS, "plan_type"),
        account_id=_get_nested(payload, _ACCOUNT_NS, "account_id"),
        subscription_expires_at=_parse_unix(_get_nested(payload, _PROFILE_NS, "subscription_expires_at")),
    )


def _decode_jwt_part(part: str) -> dict:
    # JWT использует base64url без padding
    padded = part + "=" * (4 - len(part) % 4)
    decoded = base64.urlsafe_b64decode(padded)
    return json.loads(decoded)


def _get_nested(payload: dict, namespace: str, key: str) -> str | int | None:
    nested = payload.get(namespace)
    if not isinstance(nested, dict):
        return None
    return nested.get(key)


def _parse_unix(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)
```

- [ ] **Step 4: Run — verify pass**

Run: `cd backend && py -3.12 -m pytest tests/test_openai_oauth.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/integrations/openai/oauth.py backend/tests/test_openai_oauth.py
git commit -m "feat: add JWT id_token parsing for OpenAI OAuth claims"
```

---

## Task 3: OAuth refresh — обновление access_token

**Files:**
- Modify: `backend/app/integrations/openai/oauth.py` (добавить refresh_access_token)
- Modify: `backend/tests/test_openai_oauth.py` (добавить тесты refresh)

- [ ] **Step 1: Добавить тесты refresh в test_openai_oauth.py**

Добавить в конец файла:

```python
@pytest.mark.asyncio
async def test_refresh_access_token_success(httpx_mock):
    from app.integrations.openai.oauth import refresh_access_token

    httpx_mock.add_response(
        url="https://auth.openai.com/oauth/token",
        method="POST",
        json={
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "id_token": _make_jwt({"email": "u@e.com"}),
        },
    )

    result = await refresh_access_token("old-refresh-token")
    assert result.access_token == "new-access-token"
    assert result.refresh_token == "new-refresh-token"
    assert result.id_token is not None


@pytest.mark.asyncio
async def test_refresh_access_token_keeps_old_refresh_if_missing(httpx_mock):
    from app.integrations.openai.oauth import refresh_access_token

    httpx_mock.add_response(
        url="https://auth.openai.com/oauth/token",
        method="POST",
        json={
            "access_token": "new-access",
            # refresh_token отсутствует — OpenAI иногда не возвращает его
        },
    )

    result = await refresh_access_token("original-refresh")
    assert result.access_token == "new-access"
    assert result.refresh_token == "original-refresh"  # fallback на старый


@pytest.mark.asyncio
async def test_refresh_access_token_raises_on_401(httpx_mock):
    from app.integrations.openai.exceptions import RefreshFailedError
    from app.integrations.openai.oauth import refresh_access_token

    httpx_mock.add_response(
        url="https://auth.openai.com/oauth/token",
        method="POST",
        status_code=401,
        text="invalid_grant",
    )

    with pytest.raises(RefreshFailedError):
        await refresh_access_token("expired-token")
```

И добавить импорты pytest и pytest_asyncio сверху файла (если ещё нет):
```python
import pytest
```

- [ ] **Step 2: Добавить pytest-httpx в dev-зависимости**

В `backend/pyproject.toml`, секция `[project.optional-dependencies] dev`, добавить:
```toml
    "pytest-httpx>=0.30",
```

Run: `cd backend && py -3.12 -m pip install -e ".[dev]"`

- [ ] **Step 3: Run — verify failure**

Run: `cd backend && py -3.12 -m pytest tests/test_openai_oauth.py::test_refresh_access_token_success -v`
Expected: FAIL — `ImportError: cannot import name 'refresh_access_token'`

- [ ] **Step 4: Реализовать refresh в oauth.py**

Добавить в конец `backend/app/integrations/openai/oauth.py`:

```python
from dataclasses import dataclass

import httpx

from app.integrations.openai.exceptions import RefreshFailedError

# Существующие константы и функции выше...


@dataclass
class RefreshedTokens:
    access_token: str
    refresh_token: str
    id_token: str | None


async def refresh_access_token(refresh_token: str) -> RefreshedTokens:
    """Обновляет access_token через refresh_token.

    POST https://auth.openai.com/oauth/token
    Возвращает новые токены. При 401/400 — RefreshFailedError (нужен перезаход).
    """
    body = (
        f"grant_type=refresh_token"
        f"&refresh_token={refresh_token}"
        f"&client_id={OPENAI_CLIENT_ID}"
    )

    async with httpx.AsyncClient() as client:
        for attempt in range(1, 4):
            try:
                response = await client.post(
                    f"{OPENAI_ISSUER}/oauth/token",
                    content=body,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                break
            except httpx.HTTPError:
                if attempt == 3:
                    raise
                await _short_backoff(attempt)

    if response.status_code in (400, 401):
        raise RefreshFailedError(f"refresh failed: {response.status_code} {response.text}")
    response.raise_for_status()

    data = response.json()
    return RefreshedTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token") or refresh_token,
        id_token=data.get("id_token"),
    )


async def _short_backoff(attempt: int) -> None:
    import asyncio

    await asyncio.sleep(0.25 * attempt)
```

Примечание: импорты `dataclass` (уже есть в начале файла для IdTokenClaims — переместить наверх) и `httpx` добавить в начало файла. Переместить `from dataclasses import dataclass` к существующему импорту.

- [ ] **Step 5: Run — verify pass**

Run: `cd backend && py -3.12 -m pytest tests/test_openai_oauth.py -v`
Expected: PASS (7 passed: 4 JWT + 3 refresh)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat: add OAuth refresh_token flow for access_token renewal"
```

---

## Task 4: OpenAIClient — замеры лимитов и подписки

**Files:**
- Create: `backend/app/integrations/openai/client.py`
- Test: `backend/tests/test_openai_client.py`

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_openai_client.py
import pytest


WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
ACCOUNTS_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"


@pytest.mark.asyncio
async def test_get_usage_success(httpx_mock):
    from app.integrations.openai.client import OpenAIClient

    httpx_mock.add_response(
        url=WHAM_USAGE_URL,
        method="GET",
        json={
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 20, "limit_window_seconds": 18000, "reset_at": "2026-07-12T18:00:00Z"},
                "secondary_window": {"used_percent": 40, "limit_window_seconds": 604800, "reset_at": "2026-07-14T00:00:00Z"},
            },
        },
    )

    async with OpenAIClient(access_token="tok", account_id="acc-1") as client:
        usage = await client.get_usage()

    assert usage.plan_type == "plus"
    assert usage.primary_remaining_pct == 80
    assert usage.secondary_remaining_pct == 60


@pytest.mark.asyncio
async def test_get_usage_401_raises_token_expired(httpx_mock):
    from app.integrations.openai.client import OpenAIClient
    from app.integrations.openai.exceptions import TokenExpiredError

    httpx_mock.add_response(url=WHAM_USAGE_URL, method="GET", status_code=401, text="unauthorized")

    async with OpenAIClient(access_token="expired", account_id="acc-1") as client:
        with pytest.raises(TokenExpiredError):
            await client.get_usage()


@pytest.mark.asyncio
async def test_get_usage_429_raises_backend_api_error(httpx_mock):
    from app.integrations.openai.client import OpenAIClient
    from app.integrations.openai.exceptions import BackendApiError

    httpx_mock.add_response(url=WHAM_USAGE_URL, method="GET", status_code=429, text="rate limited")

    async with OpenAIClient(access_token="tok", account_id="acc-1") as client:
        with pytest.raises(BackendApiError) as exc_info:
            await client.get_usage()
    assert exc_info.value.status == 429


@pytest.mark.asyncio
async def test_get_account_metadata_success(httpx_mock):
    from app.integrations.openai.client import OpenAIClient

    httpx_mock.add_response(
        url=ACCOUNTS_CHECK_URL,
        method="GET",
        json={
            "accounts": {
                "default": {
                    "account": {"plan_type": "pro"},
                    "entitlement": {"expires_at": "2026-09-01T00:00:00Z"},
                }
            }
        },
    )

    async with OpenAIClient(access_token="tok", account_id="acc-1") as client:
        meta = await client.get_account_metadata()

    assert meta.plan_type == "pro"
    assert meta.subscription_expires_at is not None


@pytest.mark.asyncio
async def test_client_sends_correct_headers(httpx_mock):
    from app.integrations.openai.client import OpenAIClient

    httpx_mock.add_response(
        url=WHAM_USAGE_URL,
        method="GET",
        json={"plan_type": "plus", "rate_limit": None},
    )

    async with OpenAIClient(access_token="my-token", account_id="my-acc") as client:
        await client.get_usage()

    request = httpx_mock.get_requests()[0]
    assert request.headers["authorization"] == "Bearer my-token"
    assert request.headers["chatgpt-account-id"] == "my-acc"
    assert request.headers["user-agent"] == "codex-cli/1.0.0"
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && py -3.12 -m pytest tests/test_openai_client.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать client.py**

```python
# backend/app/integrations/openai/client.py
import httpx

from app.integrations.openai.exceptions import BackendApiError, TokenExpiredError
from app.integrations.openai.oauth import CODEX_USER_AGENT
from app.integrations.openai.types import AccountMetadata, UsageInfo

WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
ACCOUNTS_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"


class OpenAIClient:
    """HTTP-клиент к OpenAI backend-api для замеров лимитов и подписки.

    Не управляет refresh_token — это ответственность вызывающего.
    При 401 выбрасывает TokenExpiredError, вызывавший код обновляет токен и ретраит.
    """

    def __init__(self, access_token: str, account_id: str | None = None) -> None:
        self._access_token = access_token
        self._account_id = account_id
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "OpenAIClient":
        self._client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._client is not None:
            await self._client.aclose()

    async def get_usage(self) -> UsageInfo:
        response = await self._request("GET", WHAM_USAGE_URL)
        return UsageInfo.from_api_response(response.json())

    async def get_account_metadata(self) -> AccountMetadata:
        response = await self._request("GET", ACCOUNTS_CHECK_URL)
        return AccountMetadata.from_accounts_check(response.json())

    async def _request(self, method: str, url: str) -> httpx.Response:
        assert self._client is not None, "используй async with OpenAIClient(...) as client"
        headers = self._build_headers()
        response = await self._client.request(method, url, headers=headers)

        if response.status_code == 401:
            raise TokenExpiredError("access_token отклонён (401)")
        if not response.is_success:
            raise BackendApiError(response.status_code, response.text)

        return response

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": CODEX_USER_AGENT,
        }
        if self._account_id:
            headers["chatgpt-account-id"] = self._account_id
        return headers
```

- [ ] **Step 4: Run — verify pass**

Run: `cd backend && py -3.12 -m pytest tests/test_openai_client.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/integrations/openai/client.py backend/tests/test_openai_client.py
git commit -m "feat: add OpenAIClient for backend-api usage and subscription queries"
```

---

## Task 5: Сервис замеров — связывание OpenAIClient с БД

**Files:**
- Create: `backend/app/services/account_limits.py`
- Test: `backend/tests/test_account_limits_service.py`

Этот сервис инкапсулирует полный цикл замера: refresh access_token (если протух) → get_usage → get_account_metadata → обновление AccountLimits в БД. Возвращает результат (ok/refresh_failed/...).

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_account_limits_service.py
import pytest
from datetime import datetime, timezone
from sqlalchemy import select


@pytest.mark.asyncio
async def test_measure_and_update_success(session, httpx_mock):
    """Полный цикл замера: refresh + usage + metadata → запись в AccountLimits."""
    from app.models.account import Account, AccountLimits
    from app.models.catalog import SubscriptionTier
    from app.services.account_limits import measure_account_limits, MeasureResult
    from app.services.crypto import decrypt, encrypt

    # Подготовка: аккаунт с протухшим access_token (нужен refresh)
    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="u@e.com",
        password_encrypted=encrypt("pass"),
        totp_secret_encrypted=encrypt("JBSWY3DPEHPK3PXP"),
        tier_id=tier.id,
        status="active",
    )
    session.add(acc)
    await session.flush()

    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted=encrypt("valid-refresh-token"),
        access_token_encrypted=encrypt("old-expired-access"),
        access_token_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),  # протух
        account_id_openai="acc-openai-1",
        refresh_status="ok",
    )
    session.add(limits)
    await session.commit()

    # Мок: refresh возвращает новые токены
    httpx_mock.add_response(
        url="https://auth.openai.com/oauth/token",
        method="POST",
        json={
            "access_token": "fresh-access",
            "refresh_token": "fresh-refresh",
            "id_token": _make_jwt({"email": "u@e.com", "https://api.openai.com/auth": {"plan_type": "plus"}}),
        },
    )
    # Мок: wham/usage
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/wham/usage",
        method="GET",
        json={
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 20, "reset_at": "2026-07-12T18:00:00Z"},
                "secondary_window": {"used_percent": 50, "reset_at": "2026-07-14T00:00:00Z"},
            },
        },
    )
    # Мок: accounts/check
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        method="GET",
        json={
            "accounts": {
                "default": {
                    "account": {"plan_type": "plus"},
                    "entitlement": {"expires_at": "2026-08-15T00:00:00Z"},
                }
            }
        },
    )

    result = await measure_account_limits(session, acc.id)
    assert result == MeasureResult.OK

    # Проверяем обновлённые поля
    reloaded = await session.get(AccountLimits, acc.id)
    assert decrypt(reloaded.access_token_encrypted) == "fresh-access"
    assert decrypt(reloaded.refresh_token_encrypted) == "fresh-refresh"
    assert reloaded.chat_5h_remaining_pct == 80  # 100 - 20
    assert reloaded.codex_5h_remaining_pct == 80  # то же (общий лимит)
    assert reloaded.chat_weekly_remaining_pct == 50
    assert reloaded.codex_weekly_remaining_pct == 50
    assert reloaded.plan_type == "plus"
    assert reloaded.measured_at is not None
    assert reloaded.refresh_status == "ok"


@pytest.mark.asyncio
async def test_measure_refresh_failed_sets_status(session, httpx_mock):
    """Протухший refresh_token → RefreshFailedError → refresh_status=expired."""
    from app.models.account import Account, AccountLimits
    from app.models.catalog import SubscriptionTier
    from app.services.account_limits import MeasureResult, measure_account_limits
    from app.services.crypto import encrypt

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="bad@e.com",
        password_encrypted=encrypt("pass"),
        totp_secret_encrypted=encrypt("totp"),
        tier_id=tier.id,
        status="active",
    )
    session.add(acc)
    await session.flush()

    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted=encrypt("expired-refresh"),
        access_token_encrypted=encrypt("expired-access"),
        access_token_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        refresh_status="ok",
    )
    session.add(limits)
    await session.commit()

    httpx_mock.add_response(
        url="https://auth.openai.com/oauth/token",
        method="POST",
        status_code=401,
        text="invalid_grant",
    )

    result = await measure_account_limits(session, acc.id)
    assert result == MeasureResult.REFRESH_FAILED

    reloaded = await session.get(AccountLimits, acc.id)
    assert reloaded.refresh_status == "expired"
    assert reloaded.refresh_failed_at is not None


@pytest.mark.asyncio
async def test_measure_skips_refresh_if_token_fresh(session, httpx_mock):
    """Свежий access_token — refresh не вызывается, только usage+metadata."""
    from app.models.account import Account, AccountLimits
    from app.models.catalog import SubscriptionTier
    from app.services.account_limits import MeasureResult, measure_account_limits
    from app.services.crypto import decrypt, encrypt

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(login="fresh@e.com", password_encrypted=encrypt("p"), totp_secret_encrypted=encrypt("t"), tier_id=tier.id, status="active")
    session.add(acc)
    await session.flush()

    # access_token истекает через час — свежий
    future = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    limits = AccountLimits(
        account_id=acc.id,
        refresh_token_encrypted=encrypt("rt"),
        access_token_encrypted=encrypt("valid-access"),
        access_token_expires_at=future,
        account_id_openai="acc-1",
        refresh_status="ok",
    )
    session.add(limits)
    await session.commit()

    # Только usage и metadata — refresh НЕ мокаем (если вызовется, тест упадёт)
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/wham/usage",
        method="GET",
        json={"plan_type": "plus", "rate_limit": None},
    )
    httpx_mock.add_response(
        url="https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        method="GET",
        json={"accounts": {"default": {"account": {"plan_type": "plus"}, "entitlement": {"expires_at": None}}}},
    )

    result = await measure_account_limits(session, acc.id)
    assert result == MeasureResult.OK

    reloaded = await session.get(AccountLimits, acc.id)
    # access_token не изменился
    assert decrypt(reloaded.access_token_encrypted) == "valid-access"


# Вспомогательная для JWT
import base64
import json
from datetime import timedelta


def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && py -3.12 -m pytest tests/test_account_limits_service.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Реализовать services/account_limits.py**

```python
# backend/app/services/account_limits.py
import enum
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.openai.client import OpenAIClient
from app.integrations.openai.exceptions import BackendApiError, RefreshFailedError, TokenExpiredError
from app.integrations.openai.oauth import refresh_access_token
from app.models.account import AccountLimits
from app.services.crypto import decrypt, encrypt

# access_token считается свежим, если истекает не раньше чем через это время
_TOKEN_FRESH_THRESHOLD = timedelta(minutes=5)
# Скв: при 401 от backend-api делаем refresh и ретраим замер один раз
_MAX_RETRIES = 1


class MeasureResult(enum.Enum):
    OK = "ok"
    REFRESH_FAILED = "refresh_failed"
    BACKEND_ERROR = "backend_error"


async def measure_account_limits(session: AsyncSession, account_id: int) -> MeasureResult:
    """Замеряет лимиты и подписку аккаунта, обновляет AccountLimits.

    Цикл: refresh access_token (если протух) → get_usage + get_account_metadata → запись в БД.
    При RefreshFailedError → refresh_status=expired, возврат REFRESH_FAILED.
    """
    limits = await session.get(AccountLimits, account_id)
    if limits is None:
        raise ValueError(f"AccountLimits not found for account_id={account_id}")

    access_token = decrypt(limits.access_token_encrypted) if limits.access_token_encrypted else None
    if access_token is None or _is_token_expired(limits.access_token_expires_at):
        refreshed = await _do_refresh(session, limits)
        if refreshed is None:
            return MeasureResult.REFRESH_FAILED
        access_token = refreshed

    # Замер с retry при 401
    for attempt in range(_MAX_RETRIES + 1):
        try:
            async with OpenAIClient(access_token, limits.account_id_openai) as client:
                usage = await client.get_usage()
                metadata = await client.get_account_metadata()
            break
        except TokenExpiredError:
            if attempt >= _MAX_RETRIES:
                raise
            refreshed = await _do_refresh(session, limits)
            if refreshed is None:
                return MeasureResult.REFRESH_FAILED
            access_token = refreshed
        except BackendApiError:
            return MeasureResult.BACKEND_ERROR

    # Запись результатов: chat/codex равны (общий rate_limit)
    primary = usage.primary_remaining_pct
    secondary = usage.secondary_remaining_pct
    limits.chat_5h_remaining_pct = primary
    limits.codex_5h_remaining_pct = primary
    limits.chat_weekly_remaining_pct = secondary
    limits.codex_weekly_remaining_pct = secondary
    limits.plan_type = metadata.plan_type or usage.plan_type
    limits.subscription_expires_at = metadata.subscription_expires_at
    limits.measured_at = datetime.now(timezone.utc)
    limits.refresh_status = "ok"
    limits.refresh_failed_at = None

    await session.commit()
    return MeasureResult.OK


def _is_token_expired(expires_at: datetime | None) -> bool:
    if expires_at is None:
        return True
    return expires_at <= datetime.now(timezone.utc) + _TOKEN_FRESH_THRESHOLD


async def _do_refresh(session: AsyncSession, limits: AccountLimits) -> str | None:
    """Обновляет access_token. При провале — ставит refresh_status=expired, возвращает None."""
    try:
        refreshed = await refresh_access_token(decrypt(limits.refresh_token_encrypted))
    except RefreshFailedError:
        limits.refresh_status = "expired"
        limits.refresh_failed_at = datetime.now(timezone.utc)
        limits.refresh_recover_attempts += 1
        await session.commit()
        return None

    limits.access_token_encrypted = encrypt(refreshed.access_token)
    limits.refresh_token_encrypted = encrypt(refreshed.refresh_token)
    limits.access_token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    limits.refresh_recover_attempts = 0
    limits.refresh_status = "ok"
    await session.commit()
    return refreshed.access_token
```

- [ ] **Step 4: Run — verify pass**

Run: `cd backend && py -3.12 -m pytest tests/test_account_limits_service.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run all tests**

Run: `cd backend && py -3.12 -m pytest -v`
Expected: all PASS (35 + новые)

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/account_limits.py backend/tests/test_account_limits_service.py
git commit -m "feat: add account limits measurement service with refresh handling"
```

---

## Task 6: Playwright — установка и browser manager

**Files:**
- Create: `backend/app/integrations/playwright/__init__.py`
- Create: `backend/app/integrations/playwright/browser.py`
- Modify: `backend/pyproject.toml` (playwright уже в deps, нужен `playwright install chromium`)

- [ ] **Step 1: Установить браузер Playwright**

Run: `cd backend && py -3.12 -m playwright install chromium`
Expected: Chromium загружается и устанавливается (~150 МБ)

- [ ] **Step 2: Реализовать browser.py**

```python
# backend/app/integrations/playwright/browser.py
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from playwright.async_api import Browser, BrowserContext, async_playwright


@asynccontextmanager
async def browser_context(headless: bool = True) -> AsyncGenerator[BrowserContext, None]:
    """Создаёт изолированный incognito-контекст Chromium.

    Каждый вызов — новый контекст с чистыми cookies. После выхода — контекст закрывается,
    cookies уничтожаются. Сессии арендаторов не затрагиваются.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context()
        try:
            yield context
        finally:
            await context.close()
            await browser.close()
```

- [ ] **Step 3: Создать пустой __init__.py**

`backend/app/integrations/playwright/__init__.py` — пустой.

- [ ] **Step 4: Smoke-test: проверить, что браузер запускается**

Создать временный скрипт:
```python
# backend/scripts/smoke_browser.py
import asyncio
from app.integrations.playwright.browser import browser_context


async def main():
    async with browser_context() as ctx:
        page = await ctx.new_page()
        await page.goto("https://example.com")
        title = await page.title()
        print(f"Title: {title}")


asyncio.run(main())
```

Run: `cd backend && py -3.12 scripts/smoke_browser.py`
Expected: `Title: Example Domain`

Удалить `scripts/smoke_browser.py` после проверки.

- [ ] **Step 5: Commit**

```bash
git add backend/app/integrations/playwright/
git commit -m "feat: add Playwright browser context manager"
```

---

## Task 7: Playwright — OAuth login (получение refresh_token)

**Files:**
- Create: `backend/app/integrations/playwright/oauth_login.py`

Этот модуль логинится в ChatGPT через Playwright (login:pass + TOTP) и проходит OAuth flow для получения refresh_token. Используется при первичной валидации аккаунта и при перезаходе (refresh протух).

Внимание: точные селекторы OpenAI login-страницы могут меняться. Код пишем с устойчивыми селекторами и placeholder-комментариями, готовыми к тестированию на реальном аккаунте. Это smoke-level — unit-тесты без живого аккаунта невозможны.

- [ ] **Step 1: Реализовать oauth_login.py**

```python
# backend/app/integrations/playwright/oauth_login.py
import asyncio
import base64
import hashlib
import secrets

from playwright.async_api import BrowserContext, TimeoutError as PlaywrightTimeoutError

from app.integrations.openai.oauth import OPENAI_CLIENT_ID, OPENAI_ISSUER
from app.services.totp import generate_totp

# URL OAuth-авторизации Codex CLI
_AUTHORIZE_BASE = f"{OPENAI_ISSUER}/oauth/authorize"
_REDIRECT_URI = "http://localhost:1455/auth/callback"
_SCOPE = "openid profile email offline_access"


class OAuthLoginError(Exception):
    """Сбой логина через Playwright (неверные данные / бан / таймаут)."""


def _generate_pkce() -> tuple[str, str]:
    """Генерирует PKCE code_verifier и code_challenge (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _build_authorize_url(code_challenge: str, state: str) -> str:
    from urllib.parse import urlencode

    params = {
        "response_type": "code",
        "client_id": OPENAI_CLIENT_ID,
        "redirect_uri": _REDIRECT_URI,
        "scope": _SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "state": state,
        "originator": "codex_cli_rs",
    }
    return f"{_AUTHORIZE_BASE}?{urlencode(params)}"


async def login_and_get_auth_code(
    context: BrowserContext,
    login: str,
    password: str,
    totp_secret: str,
    timeout_ms: int = 60_000,
) -> str:
    """Логинится на auth.openai.com и возвращает authorization_code из callback.

    Селекторы OpenAI могут меняться — при сбое на реальном аккаунте уточнять тут.
    """
    verifier, challenge = _generate_pkce()
    state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    auth_url = _build_authorize_url(challenge, state)

    page = await context.new_page()
    auth_code_holder: dict[str, str] = {}

    # Ловим redirect на localhost — там будет authorization_code
    async def _capture_code(response):
        if "/auth/callback" in response.url and "code=" in response.url:
            from urllib.parse import parse_qs, urlparse

            params = parse_qs(urlparse(response.url).query)
            code = params.get("code", [None])[0]
            if code:
                auth_code_holder["code"] = code

    page.on("response", _capture_code)

    try:
        await page.goto(auth_url, wait_until="networkidle", timeout=timeout_ms)

        # Шаг 1: ввод email
        # Селектор уточнять на реальной странице
        email_input = page.locator('input[name="email"], input[type="email"]').first
        await email_input.fill(login)
        await page.get_by_role("button", name="Continue").click()

        # Шаг 2: ввод пароля
        password_input = page.locator('input[name="password"], input[type="password"]').first
        await password_input.fill(password)
        await page.get_by_role("button", name="Continue").click()

        # Шаг 3: 2FA (TOTP), если OpenAI требует
        try:
            otp_input = page.locator('input[name="code"], input[inputmode="numeric"]').first
            await otp_input.wait_for(timeout=15_000)
            code = generate_totp(totp_secret)
            await otp_input.fill(code)
            await page.get_by_role("button", name="Continue").click()
        except PlaywrightTimeoutError:
            # 2FA не потребовалось — нормальный сценарий для некоторых аккаунтов
            pass

        # Ждём capture кода
        await asyncio.wait_for(_wait_for_code(auth_code_holder), timeout=timeout_ms / 1000)

    except PlaywrightTimeoutError as e:
        raise OAuthLoginError(f"таймаут при логине: {e}") from e
    except asyncio.TimeoutError as e:
        raise OAuthLoginError("не получен authorization_code за отведённое время") from e
    finally:
        await page.close()

    return auth_code_holder["code"]


async def _wait_for_code(holder: dict[str, str]) -> None:
    while "code" not in holder:
        await asyncio.sleep(0.5)
```

- [ ] **Step 2: Реализовать exchange_code_for_tokens в oauth.py**

Добавить в конец `backend/app/integrations/openai/oauth.py`:

```python
async def exchange_code_for_tokens(
    code: str, code_verifier: str, redirect_uri: str
) -> "RefreshedTokens":
    """Обменивает authorization_code на токены (первичный вход)."""
    import httpx

    body = (
        f"grant_type=authorization_code"
        f"&code={code}"
        f"&redirect_uri={redirect_uri}"
        f"&client_id={OPENAI_CLIENT_ID}"
        f"&code_verifier={code_verifier}"
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{OPENAI_ISSUER}/oauth/token",
            content=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if not response.is_success:
        raise RefreshFailedError(f"token exchange failed: {response.status_code} {response.text}")

    data = response.json()
    return RefreshedTokens(
        access_token=data["access_token"],
        refresh_token=data["refresh_token"],
        id_token=data.get("id_token"),
    )
```

- [ ] **Step 3: Проверить импорт без ошибок**

Run: `cd backend && py -3.12 -c "from app.integrations.playwright.oauth_login import login_and_get_auth_code; print('OK')"`
Expected: `OK`

(Unit-тест невозможен без живого аккаунта — smoke-тест на реальном аккаунте проводится вручную при наличии тестовых учётных данных.)

- [ ] **Step 4: Commit**

```bash
git add backend/app/integrations/playwright/oauth_login.py backend/app/integrations/openai/oauth.py
git commit -m "feat: add Playwright OAuth login flow for account validation"
```

---

## Task 8: Playwright — kick (logout all)

**Files:**
- Create: `backend/app/integrations/playwright/kick.py`

Кик: логин в ChatGPT → Settings → "Log out everywhere". Сбрасывает все сессии (включая арендаторов). Используется при истечении аренды.

- [ ] **Step 1: Реализовать kick.py**

```python
# backend/app/integrations/playwright/kick.py
import asyncio

from playwright.async_api import BrowserContext, TimeoutError as PlaywrightTimeoutError

from app.integrations.openai.oauth import OPENAI_ISSUER
from app.services.totp import generate_totp

_LOGIN_URL = f"{OPENAI_ISSUER}/oauth/authorize"
_SETTINGS_URL = "https://chatgpt.com/#settings"


class KickError(Exception):
    """Сбой кика (логин не удался / страница настроек недоступна)."""


async def kick_account(
    context: BrowserContext,
    login: str,
    password: str,
    totp_secret: str,
    timeout_ms: int = 60_000,
) -> None:
    """Логинится в аккаунт и нажимает 'Выйти на всех устройствах'.

    Селекторы OpenAI могут меняться — при сбое уточнять по фактической DOM.
    """
    page = await context.new_page()
    try:
        await _login(page, login, password, totp_secret, timeout_ms)
        await _logout_everywhere(page, timeout_ms)
    finally:
        await page.close()


async def _login(page, login: str, password: str, totp_secret: str, timeout_ms: int) -> None:
    from app.integrations.playwright.oauth_login import _build_authorize_url, _generate_pkce
    import base64
    import secrets

    verifier, challenge = _generate_pkce()
    state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    auth_url = _build_authorize_url(challenge, state)

    await page.goto(auth_url, wait_until="networkidle", timeout=timeout_ms)

    email_input = page.locator('input[name="email"], input[type="email"]').first
    await email_input.fill(login)
    await page.get_by_role("button", name="Continue").click()

    password_input = page.locator('input[name="password"], input[type="password"]').first
    await password_input.fill(password)
    await page.get_by_role("button", name="Continue").click()

    # 2FA, если требуется
    try:
        otp_input = page.locator('input[name="code"], input[inputmode="numeric"]').first
        await otp_input.wait_for(timeout=15_000)
        await otp_input.fill(generate_totp(totp_secret))
        await page.get_by_role("button", name="Continue").click()
    except PlaywrightTimeoutError:
        pass  # 2FA не потребовалось

    # Ждём перехода в ChatGPT
    await page.wait_for_url("**/chatgpt.com/**", timeout=timeout_ms)


async def _logout_everywhere(page, timeout_ms: int) -> None:
    """Открывает настройки и нажимает 'Log out everywhere'."""
    from urllib.parse import urlencode

    await page.goto("https://chatgpt.com/#settings", wait_until="networkidle", timeout=timeout_ms)

    # Кнопка может называться по-разному в зависимости от локали
    logout_btn = page.get_by_role("button", name="Log out of all sessions")
    try:
        await logout_btn.click(timeout=10_000)
    except PlaywrightTimeoutError:
        # Fallback: ищем по тексту
        alt_btn = page.locator("button:has-text('all devices'), button:has-text('всех устройствах')").first
        await alt_btn.click(timeout=10_000)

    # Подтверждение, если есть диалог
    try:
        confirm = page.get_by_role("button", name="Confirm, Log out, ОК").first
        await confirm.click(timeout=5_000)
    except PlaywrightTimeoutError:
        pass  # подтверждения не было
```

- [ ] **Step 2: Проверить импорт**

Run: `cd backend && py -3.12 -c "from app.integrations.playwright.kick import kick_account; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backend/app/integrations/playwright/kick.py
git commit -m "feat: add Playwright logout-all for account kick"
```

---

## Task 9: Сервис первичной валидации аккаунта

**Files:**
- Create: `backend/app/services/account_validation.py`
- Test: `backend/tests/test_account_validation.py`

Связывает Playwright OAuth login + exchange + первичный замер лимитов. Используется при добавлении аккаунта и при перезаходе.

- [ ] **Step 1: Написать failing test**

```python
# backend/tests/test_account_validation.py
import pytest
from datetime import datetime, timezone
from sqlalchemy import select


@pytest.mark.asyncio
async def test_validate_account_success(session, monkeypatch):
    """Первичная валидация: Playwright логин → токены → замер лимитов."""
    from app.models.account import Account, AccountLimits
    from app.models.catalog import SubscriptionTier
    from app.services.account_validation import ValidationOutcome, validate_account
    from app.services.crypto import decrypt, encrypt

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="new@e.com",
        password_encrypted=encrypt("pass123"),
        totp_secret_encrypted=encrypt("JBSWY3DPEHPK3PXP"),
        tier_id=tier.id,
        status="pending_validation",
    )
    session.add(acc)
    await session.commit()

    # Мокаем Playwright-логин и exchange
    async def fake_login_and_get_auth_code(context, login, password, totp_secret, **kw):
        assert login == "new@e.com"
        assert password == "pass123"
        return "fake-auth-code"

    async def fake_exchange(code, verifier, redirect_uri):
        from app.integrations.openai.oauth import RefreshedTokens
        assert code == "fake-auth-code"
        return RefreshedTokens(
            access_token="initial-access",
            refresh_token="initial-refresh",
            id_token=_make_jwt({
                "email": "new@e.com",
                "https://api.openai.com/auth": {"plan_type": "plus"},
                "https://api.openai.com/account": {"account_id": "openai-acc-1"},
            }),
        )

    # Мокаем замер (чтобы не дёргать реальный backend-api)
    async def fake_measure(session, account_id):
        from app.services.account_limits import MeasureResult
        return MeasureResult.OK

    monkeypatch.setattr("app.services.account_validation.login_and_get_auth_code", fake_login_and_get_auth_code)
    monkeypatch.setattr("app.services.account_validation.exchange_code_for_tokens", fake_exchange)
    monkeypatch.setattr("app.services.account_validation.measure_account_limits", fake_measure)

    outcome = await validate_account(session, acc.id)
    assert outcome == ValidationOutcome.OK

    # Проверяем: аккаунт active, AccountLimits создан с токенами
    reloaded_acc = await session.get(Account, acc.id)
    assert reloaded_acc.status == "active"

    limits = await session.get(AccountLimits, acc.id)
    assert limits is not None
    assert decrypt(limits.refresh_token_encrypted) == "initial-refresh"
    assert limits.account_id_openai == "openai-acc-1"
    assert limits.refresh_status == "ok"


@pytest.mark.asyncio
async def test_validate_account_login_failure(session, monkeypatch):
    """Playwright логин не удался → аккаунт остаётся в pending_validation."""
    from app.integrations.playwright.oauth_login import OAuthLoginError
    from app.models.account import Account
    from app.models.catalog import SubscriptionTier
    from app.services.account_validation import ValidationOutcome, validate_account
    from app.services.crypto import encrypt

    tier = SubscriptionTier(name="Plus", is_active=True)
    session.add(tier)
    await session.flush()

    acc = Account(
        login="bad@e.com",
        password_encrypted=encrypt("wrong"),
        totp_secret_encrypted=encrypt("t"),
        tier_id=tier.id,
        status="pending_validation",
    )
    session.add(acc)
    await session.commit()

    async def failing_login(context, login, password, totp_secret, **kw):
        raise OAuthLoginError("invalid credentials")

    monkeypatch.setattr("app.services.account_validation.login_and_get_auth_code", failing_login)

    outcome = await validate_account(session, acc.id)
    assert outcome == ValidationOutcome.LOGIN_FAILED

    reloaded = await session.get(Account, acc.id)
    assert reloaded.status == "pending_validation"


import base64
import json


def _make_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}."
```

- [ ] **Step 2: Run — verify failure**

Run: `cd backend && py -3.12 -m pytest tests/test_account_validation.py -v`
Expected: FAIL

- [ ] **Step 3: Реализовать services/account_validation.py**

```python
# backend/app/services/account_validation.py
import enum

from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.openai.oauth import IdTokenClaims, exchange_code_for_tokens, parse_id_token
from app.integrations.playwright.browser import browser_context
from app.integrations.playwright.oauth_login import OAuthLoginError, login_and_get_auth_code
from app.models.account import Account, AccountLimits
from app.services.account_limits import MeasureResult, measure_account_limits
from app.services.crypto import decrypt, encrypt

_REDIRECT_URI = "http://localhost:1455/auth/callback"


class ValidationOutcome(enum.Enum):
    OK = "ok"
    LOGIN_FAILED = "login_failed"
    MEASURE_FAILED = "measure_failed"


async def validate_account(session: AsyncSession, account_id: int) -> ValidationOutcome:
    """Первичная валидация аккаунта через Playwright OAuth flow.

    Шаги: логин → auth code → exchange → сохранение токенов → первичный замер лимитов.
    При успехе аккаунт → active. При сбое логина — остаётся в текущем статусе.
    """
    account = await session.get(Account, account_id)
    if account is None:
        raise ValueError(f"Account not found: {account_id}")

    login = account.login
    password = decrypt(account.password_encrypted)
    totp_secret = decrypt(account.totp_secret_encrypted)

    # Playwright OAuth flow
    try:
        async with browser_context() as context:
            auth_code = await login_and_get_auth_code(context, login, password, totp_secret)
            code_verifier = _get_last_verifier()  # см. реализацию ниже
            tokens = await exchange_code_for_tokens(auth_code, code_verifier, _REDIRECT_URI)
    except OAuthLoginError:
        return ValidationOutcome.LOGIN_FAILED

    # Парсинг id_token → claims
    claims = parse_id_token(tokens.id_token) if tokens.id_token else IdTokenClaims()

    # Создание/обновление AccountLimits
    limits = await session.get(AccountLimits, account_id)
    if limits is None:
        limits = AccountLimits(account_id=account_id, refresh_token_encrypted=encrypt(tokens.refresh_token))
        session.add(limits)

    limits.refresh_token_encrypted = encrypt(tokens.refresh_token)
    limits.access_token_encrypted = encrypt(tokens.access_token)
    limits.account_id_openai = claims.account_id
    limits.refresh_status = "ok"
    limits.refresh_recover_attempts = 0

    # Обновляем поля аккаунта из claims
    if claims.plan_type:
        account.tier_id  # tier остаётся, plan_type в limits
    if claims.subscription_expires_at:
        account.subscription_expires_at = claims.subscription_expires_at

    await session.commit()

    # Первичный замер лимитов
    result = await measure_account_limits(session, account_id)
    if result != MeasureResult.OK:
        # Аккаунт валиден, но замер не прошёл — это не блокер
        return ValidationOutcome.MEASURE_FAILED

    account.status = "active"
    await session.commit()
    return ValidationOutcome.OK


def _get_last_verifier() -> str:
    """Возвращает code_verifier из последнего вызова login_and_get_auth_code.

    PKCE verifier генерируется внутри login_and_get_auth_code, но нужен для exchange.
    Решение: модуль-level хранилище последнего verifier.
    """
    return _last_verifier_holder.get("verifier", "")


_last_verifier_holder: dict[str, str] = {}
```

Внимание: `_get_last_verifier()` — временное решение через модуль-level state. В Task 7 `login_and_get_auth_code` генерирует verifier, но не возвращает его. Нужно изменить сигнатуру, чтобы возвращать `(code, verifier)`. Обновлю oauth_login.py.

- [ ] **Step 4: Обновить oauth_login.py — возвращать verifier**

Изменить сигнатуру `login_and_get_auth_code` в `backend/app/integrations/playwright/oauth_login.py`:

Было:
```python
async def login_and_get_auth_code(
    context: BrowserContext,
    login: str,
    password: str,
    totp_secret: str,
    timeout_ms: int = 60_000,
) -> str:
```

Стало:
```python
async def login_and_get_auth_code(
    context: BrowserContext,
    login: str,
    password: str,
    totp_secret: str,
    timeout_ms: int = 60_000,
) -> tuple[str, str]:
    """Возвращает (authorization_code, code_verifier)."""
```

И в конце функции, где `return auth_code_holder["code"]`, заменить на:
```python
    return auth_code_holder["code"], verifier
```

- [ ] **Step 5: Обновить account_validation.py под новую сигнатуру**

Заменить блок:
```python
    try:
        async with browser_context() as context:
            auth_code = await login_and_get_auth_code(context, login, password, totp_secret)
            code_verifier = _get_last_verifier()
            tokens = await exchange_code_for_tokens(auth_code, code_verifier, _REDIRECT_URI)
```
на:
```python
    try:
        async with browser_context() as context:
            auth_code, code_verifier = await login_and_get_auth_code(context, login, password, totp_secret)
            tokens = await exchange_code_for_tokens(auth_code, code_verifier, _REDIRECT_URI)
```

И удалить `_get_last_verifier` и `_last_verifier_holder`.

- [ ] **Step 6: Обновить test — await возвращает tuple**

В `test_validate_account_success` изменить мок:
```python
    async def fake_login_and_get_auth_code(context, login, password, totp_secret, **kw):
        assert login == "new@e.com"
        return "fake-auth-code", "fake-verifier"

    async def fake_exchange(code, verifier, redirect_uri):
        assert code == "fake-auth-code"
        assert verifier == "fake-verifier"
        ...
```

- [ ] **Step 7: Run — verify pass**

Run: `cd backend && py -3.12 -m pytest tests/test_account_validation.py -v`
Expected: PASS (2 passed)

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat: add account validation service with Playwright OAuth flow"
```

---

## Task 10: Финальная проверка Фазы 2

- [ ] **Step 1: Run full test suite**

Run: `cd backend && py -3.12 -m pytest -v`
Expected: all PASS (35 Фаза 1 + ~14 Фаза 2 = ~49)

- [ ] **Step 2: Проверить структуру integrations**

Run: `find backend/app/integrations -name "*.py" | sort`
Expected:
```
backend/app/integrations/__init__.py
backend/app/integrations/openai/__init__.py
backend/app/integrations/openai/client.py
backend/app/integrations/openai/exceptions.py
backend/app/integrations/openai/oauth.py
backend/app/integrations/openai/types.py
backend/app/integrations/playwright/__init__.py
backend/app/integrations/playwright/browser.py
backend/app/integrations/playwright/kick.py
backend/app/integrations/playwright/oauth_login.py
```

- [ ] **Step 3: Git log**

Run: `git log --oneline | head -15`
Expected: ~10 коммитов Фазы 2 сверху

- [ ] **Step 4: Commit финальный (если нужен)**

```bash
git commit --allow-empty -m "chore: phase 2 openai and playwright integration complete"
```

---

## Итог Фазы 2

После завершения:
- ✅ `OpenAIClient` — HTTP к backend-api (wham/usage, accounts/check) с retry при 401
- ✅ OAuth refresh — обновление access_token через refresh_token
- ✅ JWT парсинг id_token → email, plan_type, account_id, subscription_expires_at
- ✅ Сервис замеров — связывает OpenAIClient с AccountLimits (refresh → usage → metadata → БД)
- ✅ Playwright browser context — изолированный incognito для каждой операции
- ✅ Playwright OAuth login — логин+2FA → authorization_code (для первичной валидации)
- ✅ Playwright kick — logout all (для кика арендаторов)
- ✅ Сервис первичной валидации — полный цикл добавления аккаунта
- ✅ ~14 тестов, все проходят (моки для HTTP, monkeypatch для Playwright)

**Не покрыто unit-тестами (требует живого аккаунта):**
- Реальный OAuth flow на auth.openai.com (селекторы, 2FA)
- Реальный logout all на chatgpt.com (селекторы)
- Реальный замер лимитов с настоящим access_token

Эти компоненты smoke-тестируются вручную при наличии тестового аккаунта OpenAI.

Фаза 3 (FunPay интеграция) — следующий план.
