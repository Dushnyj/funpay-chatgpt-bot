import httpx
import pytest

from app.integrations.playwright.proxy import BrowserProxy, ProxyUnavailableError


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
    assert usage.primary_window_seconds == 18000
    assert usage.secondary_window_seconds == 604800


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
                "acc-1": {
                    "account": {"plan_type": "pro"},
                    "entitlement": {
                        "has_active_subscription": True,
                        "expires_at": "2026-09-01T00:00:00Z",
                    },
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


@pytest.mark.asyncio
async def test_client_uses_the_selected_proxy_transport(monkeypatch):
    from app.integrations.openai.client import OpenAIClient

    selected = BrowserProxy(
        71,
        "socks5",
        "home-relay.internal",
        1080,
        username="relay-user",
        password="relay-password",
        config_revision=9,
    )
    observed: list[tuple[BrowserProxy | None, float]] = []

    class FakeTransport:
        async def aclose(self):
            return None

    def fake_http_client(*, proxy=None, timeout=30.0):
        observed.append((proxy, timeout))
        return FakeTransport()

    monkeypatch.setattr(
        "app.integrations.openai.client.openai_http_client",
        fake_http_client,
    )

    async with OpenAIClient("access", "account", proxy=selected):
        pass

    assert observed == [(selected, 30.0)]


@pytest.mark.asyncio
async def test_transport_failure_is_a_fail_closed_proxy_error(monkeypatch):
    from app.integrations.openai.client import OpenAIClient

    selected = BrowserProxy(72, "http", "home-relay.internal", 3128)

    def fail_transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret-bearing transport detail", request=request)

    def fake_http_client(*, proxy=None, timeout=30.0):
        assert proxy is selected
        return httpx.AsyncClient(
            transport=httpx.MockTransport(fail_transport),
            timeout=timeout,
            trust_env=False,
        )

    monkeypatch.setattr(
        "app.integrations.openai.client.openai_http_client",
        fake_http_client,
    )

    async with OpenAIClient("access", "account", proxy=selected) as client:
        with pytest.raises(ProxyUnavailableError) as failure:
            await client.get_usage()

    assert failure.value.code == "proxy_unavailable"
    assert "secret-bearing" not in str(failure.value)
