from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.integrations.email.provider import (
    EmailErrorCode,
    EmailProviderError,
    parse_verification_code,
)


MICROSOFT_AUTHORITY = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
MICROSOFT_GRAPH_SCOPES = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "https://graph.microsoft.com/User.Read",
    "https://graph.microsoft.com/Mail.Read",
)
MICROSOFT_GRAPH_SCOPE_STRING = " ".join(MICROSOFT_GRAPH_SCOPES)

_TOKEN_URL = f"{MICROSOFT_AUTHORITY}/token"
_MESSAGES_URL = (
    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages"
)
_OPENAI_DOMAINS = frozenset({"openai.com", "tm.openai.com"})


RefreshTokenUpdate = Callable[[str], Awaitable[None]]
CredentialStatusUpdate = Callable[[], Awaitable[None]]


class MicrosoftGraphEmailProvider:
    """Read only newly-arrived OpenAI codes through delegated Graph access."""

    def __init__(
        self,
        email: str,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        on_refresh_token: RefreshTokenUpdate,
        on_reauthorization_required: CredentialStatusUpdate | None = None,
        poll_interval_s: float = 2.0,
        request_timeout_s: float = 15.0,
    ) -> None:
        self.email = email
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._on_refresh_token = on_refresh_token
        self._on_reauthorization_required = on_reauthorization_required
        self._poll_interval_s = poll_interval_s
        self._request_timeout_s = request_timeout_s
        self._access_token: str | None = None
        self._baseline_ids: set[str] = set()
        self._preflight_complete = False

    async def preflight(self) -> None:
        """Refresh authorization and snapshot messages before code delivery."""

        messages = await self._list_messages()
        self._baseline_ids = {
            message_id
            for message in messages
            if (message_id := self._message_id(message)) is not None
        }
        self._preflight_complete = True

    async def fetch_verification_code(self, timeout: float = 60.0) -> str | None:
        if not self._preflight_complete:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Перед ожиданием кода не выполнена проверка Microsoft Graph.",
            )

        deadline = time.monotonic() + timeout
        while True:
            messages = await self._list_messages()
            for message in messages:
                message_id = self._message_id(message)
                if message_id is None or message_id in self._baseline_ids:
                    continue

                # Consume every new revision before inspecting its body. A
                # non-code message must not be retried indefinitely.
                self._baseline_ids.add(message_id)
                if not self._is_openai_message(message):
                    continue
                code = parse_verification_code(self._message_text(message))
                if code is not None:
                    return code

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EmailProviderError(
                    EmailErrorCode.NO_CODE,
                    "Новое письмо с кодом OpenAI не пришло за отведённое время.",
                )
            await asyncio.sleep(min(self._poll_interval_s, remaining))

    async def _get_access_token(self) -> str:
        if self._access_token is not None:
            return self._access_token

        try:
            async with httpx.AsyncClient(timeout=self._request_timeout_s) as client:
                response = await client.post(
                    _TOKEN_URL,
                    data={
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": self._refresh_token,
                        "scope": MICROSOFT_GRAPH_SCOPE_STRING,
                    },
                )
        except httpx.HTTPError as exc:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft не ответил при обновлении доступа к почте.",
            ) from exc

        if response.status_code in {400, 401, 403}:
            await self._mark_reauthorization_required()
            raise EmailProviderError(
                EmailErrorCode.AUTH_FAILED,
                "Подключение Outlook истекло. Подключите почту повторно.",
            )
        if response.is_error:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft временно не выдал доступ к почте.",
            )

        payload = self._response_object(response)
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft вернул неполный ответ авторизации почты.",
            )

        rotated_token = payload.get("refresh_token")
        if (
            isinstance(rotated_token, str)
            and rotated_token
            and rotated_token != self._refresh_token
        ):
            try:
                # Do not consume an access token obtained alongside a rotated
                # refresh token until the replacement is durably committed.
                await self._on_refresh_token(rotated_token)
            except Exception as exc:
                raise EmailProviderError(
                    EmailErrorCode.CONNECTION_FAILED,
                    "Не удалось сохранить обновлённое подключение Outlook.",
                ) from exc
            self._refresh_token = rotated_token

        self._access_token = access_token
        return access_token

    async def _list_messages(self) -> list[dict[str, Any]]:
        access_token = await self._get_access_token()
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout_s) as client:
                response = await client.get(
                    _MESSAGES_URL,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Prefer": 'outlook.body-content-type="text"',
                    },
                    params={
                        "$select": "id,receivedDateTime,subject,bodyPreview,body,from",
                        "$orderby": "receivedDateTime desc",
                        "$top": "50",
                    },
                )
        except httpx.HTTPError as exc:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft Graph не ответил при чтении почты.",
            ) from exc

        if response.status_code in {401, 403}:
            await self._mark_reauthorization_required()
            raise EmailProviderError(
                EmailErrorCode.AUTH_FAILED,
                "Microsoft Graph отклонил доступ к почте. Подключите Outlook повторно.",
            )
        if response.is_error:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft Graph временно не прочитал почту.",
            )

        payload = self._response_object(response)
        values = payload.get("value")
        if not isinstance(values, list) or not all(
            isinstance(item, dict) for item in values
        ):
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft Graph вернул неполный список писем.",
            )
        return values

    @staticmethod
    def _response_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft вернул некорректный ответ почтового API.",
            ) from exc
        if not isinstance(payload, dict):
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft вернул некорректный ответ почтового API.",
            )
        return payload

    async def _mark_reauthorization_required(self) -> None:
        if self._on_reauthorization_required is None:
            return
        try:
            await self._on_reauthorization_required()
        except Exception:
            # Preserve the actionable authorization error. A failed status
            # update must never expose its database or token details.
            pass

    @staticmethod
    def _message_id(message: dict[str, Any]) -> str | None:
        value = message.get("id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _is_openai_message(message: dict[str, Any]) -> bool:
        sender = message.get("from")
        if not isinstance(sender, dict):
            return False
        email_address = sender.get("emailAddress")
        if not isinstance(email_address, dict):
            return False
        address = email_address.get("address")
        if not isinstance(address, str) or "@" not in address:
            return False
        domain = address.rsplit("@", 1)[-1].lower().rstrip(".")
        return domain in _OPENAI_DOMAINS or domain.endswith(".openai.com")

    @staticmethod
    def _message_text(message: dict[str, Any]) -> str:
        body = message.get("body")
        body_content = body.get("content", "") if isinstance(body, dict) else ""
        return "\n".join(
            str(value or "")
            for value in (
                message.get("subject"),
                message.get("bodyPreview"),
                body_content,
            )
        )
