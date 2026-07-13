from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from app.integrations.email.provider import (
    EmailErrorCode,
    EmailProviderError,
    FreshVerificationCode,
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
_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/mailFolders/{folder_id}/messages"
_MESSAGE_URL = "https://graph.microsoft.com/v1.0/me/messages/{message_id}"
_MAIL_FOLDER_IDS = ("inbox", "junkemail")
_OPENAI_DOMAINS = frozenset({"openai.com", "tm.openai.com"})
_MESSAGE_BODY_RESPONSE_LIMIT = 256 * 1024
_MAX_HYDRATED_MESSAGES_PER_LOOKUP = 5


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

        messages = await self._list_messages(_MAIL_FOLDER_IDS)
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
        body_fetches_remaining = _MAX_HYDRATED_MESSAGES_PER_LOOKUP
        while True:
            messages = await self._list_messages(_MAIL_FOLDER_IDS)
            for message in messages:
                message_id = self._message_id(message)
                if message_id is None or message_id in self._baseline_ids:
                    continue

                # Consume every new revision before inspecting its body. A
                # non-code message must not be retried indefinitely.
                self._baseline_ids.add(message_id)
                if not self._is_openai_message(message):
                    continue
                if self._needs_body_fetch(message):
                    if body_fetches_remaining <= 0:
                        continue
                    body_fetches_remaining -= 1
                code = await self._verification_code(message)
                if code is not None:
                    return code

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EmailProviderError(
                    EmailErrorCode.NO_CODE,
                    "Новое письмо с кодом OpenAI не пришло за отведённое время.",
                )
            await asyncio.sleep(min(self._poll_interval_s, remaining))

    async def fetch_fresh_verification_code(
        self,
        *,
        not_before: datetime,
        timeout: float = 10.0,
    ) -> FreshVerificationCode:
        """Read an existing Graph message only when its timestamp is proven."""
        cutoff = (
            not_before.replace(tzinfo=timezone.utc)
            if not_before.tzinfo is None
            else not_before.astimezone(timezone.utc)
        )
        deadline = time.monotonic() + timeout
        body_fetches_remaining = _MAX_HYDRATED_MESSAGES_PER_LOOKUP
        while True:
            messages = await self._list_messages(_MAIL_FOLDER_IDS)
            messages.sort(
                key=lambda message: self._message_received_at(message)
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            for message in messages:
                message_id = self._message_id(message)
                received_at = self._message_received_at(message)
                if (
                    message_id is None
                    or received_at is None
                    or received_at < cutoff
                    or not self._is_openai_message(message)
                ):
                    continue
                if self._needs_body_fetch(message):
                    if body_fetches_remaining <= 0:
                        continue
                    body_fetches_remaining -= 1
                code = await self._verification_code(message)
                if code is not None:
                    material = f"graph|{message_id}|{received_at.isoformat()}"
                    return FreshVerificationCode(
                        code=code,
                        received_at=received_at,
                        fingerprint=hashlib.sha256(material.encode()).hexdigest(),
                    )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EmailProviderError(
                    EmailErrorCode.NO_CODE,
                    "Свежее письмо с кодом OpenAI не найдено.",
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

    async def _list_messages(
        self,
        folder_ids: tuple[str, ...] = ("inbox",),
    ) -> list[dict[str, Any]]:
        access_token = await self._get_access_token()
        messages: list[dict[str, Any]] = []
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout_s) as client:
                for folder_id in folder_ids:
                    response = await client.get(
                        _MESSAGES_URL.format(folder_id=folder_id),
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Prefer": 'outlook.body-content-type="text"',
                        },
                        params={
                            "$select": "id,receivedDateTime,subject,bodyPreview,from",
                            "$orderby": "receivedDateTime desc",
                            "$top": "50",
                        },
                    )
                    if response.status_code in {401, 403}:
                        await self._mark_reauthorization_required()
                        raise EmailProviderError(
                            EmailErrorCode.AUTH_FAILED,
                            "Microsoft Graph отклонил доступ к почте. "
                            "Подключите Outlook повторно.",
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
                    messages.extend(values)
        except httpx.HTTPError as exc:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft Graph не ответил при чтении почты.",
            ) from exc
        return messages

    async def _verification_code(self, message: dict[str, Any]) -> str | None:
        """Parse metadata first and fetch one bounded body only when necessary."""

        code = parse_verification_code(self._message_text(message))
        if code is not None or "body" in message:
            return code
        message_id = self._message_id(message)
        if message_id is None:
            return None
        body = await self._get_message_body(message_id)
        hydrated = dict(message)
        hydrated["body"] = body
        return parse_verification_code(self._message_text(hydrated))

    @classmethod
    def _needs_body_fetch(cls, message: dict[str, Any]) -> bool:
        return "body" not in message and parse_verification_code(
            cls._message_text(message)
        ) is None

    async def _get_message_body(self, message_id: str) -> dict[str, Any]:
        access_token = await self._get_access_token()
        try:
            async with httpx.AsyncClient(timeout=self._request_timeout_s) as client:
                async with client.stream(
                    "GET",
                    _MESSAGE_URL.format(message_id=quote(message_id, safe="")),
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Prefer": 'outlook.body-content-type="text"',
                    },
                    params={"$select": "body"},
                ) as response:
                    if response.status_code in {401, 403}:
                        await self._mark_reauthorization_required()
                        raise EmailProviderError(
                            EmailErrorCode.AUTH_FAILED,
                            "Microsoft Graph отклонил доступ к почте. "
                            "Подключите Outlook повторно.",
                        )
                    if response.is_error:
                        raise EmailProviderError(
                            EmailErrorCode.CONNECTION_FAILED,
                            "Microsoft Graph временно не прочитал письмо.",
                        )
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            if int(declared) > _MESSAGE_BODY_RESPONSE_LIMIT:
                                raise EmailProviderError(
                                    EmailErrorCode.CONNECTION_FAILED,
                                    "Письмо с кодом превышает безопасный размер.",
                                )
                        except ValueError:
                            raise EmailProviderError(
                                EmailErrorCode.CONNECTION_FAILED,
                                "Microsoft Graph вернул некорректный размер письма.",
                            )
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > _MESSAGE_BODY_RESPONSE_LIMIT:
                            raise EmailProviderError(
                                EmailErrorCode.CONNECTION_FAILED,
                                "Письмо с кодом превышает безопасный размер.",
                            )
        except httpx.HTTPError as exc:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft Graph не ответил при чтении письма.",
            ) from exc

        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, ValueError) as exc:
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft Graph вернул некорректное письмо.",
            ) from exc
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Microsoft Graph вернул письмо без содержимого.",
            )
        return body

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
    def _message_received_at(message: dict[str, Any]) -> datetime | None:
        value = message.get("receivedDateTime")
        if not isinstance(value, str) or not value:
            return None
        try:
            received_at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if received_at.tzinfo is None:
            return None
        return received_at.astimezone(timezone.utc)

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
