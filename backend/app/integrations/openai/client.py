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
        return AccountMetadata.from_accounts_check(
            response.json(), account_id=self._account_id
        )

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
