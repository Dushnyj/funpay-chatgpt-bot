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
