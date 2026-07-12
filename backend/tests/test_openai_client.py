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
