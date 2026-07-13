from __future__ import annotations

import asyncio
from email import policy
from email.parser import BytesParser
import logging
import ssl
import time

import aioimaplib

from app.integrations.email.provider import (
    EmailErrorCode,
    EmailProvider,
    EmailProviderError,
    parse_verification_code,
)

logger = logging.getLogger(__name__)


# Маппинг домен → IMAP-сервер
_KNOWN_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "outlook.com": "outlook.office365.com",
    "hotmail.com": "outlook.office365.com",
    "live.com": "outlook.office365.com",
    "msn.com": "outlook.office365.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "yahoo.co.uk": "imap.mail.yahoo.com",
    "icloud.com": "imap.mail.me.com",
    "me.com": "imap.mail.me.com",
    "mac.com": "imap.mail.me.com",
}

_DEFAULT_PORT = 993
_DEFAULT_FALLBACK_HOST = "imap.gmail.com"


class IMAPResponseError(RuntimeError):
    """An IMAP command completed with a non-OK protocol response."""


def _require_ok(response, operation: str) -> None:
    result = getattr(response, "result", "")
    if isinstance(result, bytes):
        result = result.decode(errors="replace")
    if str(result).upper() != "OK":
        if operation == "login":
            raise EmailProviderError(
                EmailErrorCode.AUTH_FAILED,
                "Почтовый сервер отклонил логин или пароль приложения.",
            )
        raise IMAPResponseError(f"IMAP {operation} failed")


def _message_text(lines: list[object]) -> str:
    """Decode an IMAP RFC822 response, including multipart/encoded MIME bodies."""
    byte_lines = [line for line in lines if isinstance(line, bytes)]
    raw = max(byte_lines, key=len, default=b"")
    if not raw:
        return " ".join(str(line) for line in lines)

    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
        chunks = [str(message.get("subject", ""))]
        if message.is_multipart():
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get_content_type() not in {"text/plain", "text/html"}:
                    continue
                try:
                    chunks.append(part.get_content())
                except (LookupError, UnicodeError):
                    payload = part.get_payload(decode=True) or b""
                    chunks.append(payload.decode("utf-8", errors="replace"))
        else:
            try:
                chunks.append(message.get_content())
            except (LookupError, UnicodeError):
                payload = message.get_payload(decode=True) or raw
                chunks.append(payload.decode("utf-8", errors="replace"))
        return "\n".join(str(chunk) for chunk in chunks)
    except Exception:
        return raw.decode("utf-8", errors="replace")


class IMAPProvider:
    """IMAP-источник кодов подтверждения.

    Работает с любым провайдером, поддерживающим IMAP (Gmail, Outlook, Yahoo, кастомные).
    Требует App Password (если на почте включена 2FA).
    """

    def __init__(
        self,
        email: str,
        password: str,
        imap_host: str,
        imap_port: int = _DEFAULT_PORT,
        *,
        basic_auth_supported: bool = True,
    ) -> None:
        self.email = email
        self._password = password
        self.imap_host = imap_host
        self.imap_port = imap_port
        self._basic_auth_supported = basic_auth_supported
        self._baseline_ids: set[str] = set()

    def _ensure_supported(self) -> None:
        if not self._basic_auth_supported:
            raise EmailProviderError(
                EmailErrorCode.UNSUPPORTED,
                "Outlook/Hotmail не поддерживает используемый вход по IMAP-паролю; нужен OAuth2.",
            )

    async def preflight(self) -> None:
        """Validate credentials and remember messages that predate the login flow."""
        self._ensure_supported()
        try:
            client = await self._connect()
            try:
                self._baseline_ids = await self._search_openai_messages(client)
            finally:
                await self._logout(client)
        except EmailProviderError:
            raise
        except Exception as exc:
            logger.warning("IMAP preflight failed for %s", self.email, exc_info=True)
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Не удалось проверить доступ к почте через IMAP.",
            ) from exc

    async def fetch_verification_code(self, timeout: float = 60.0) -> str | None:
        """Подключается к IMAP, ищет свежие письма от OpenAI, извлекает код.

        Polls all matching messages rather than only ``UNSEEN``: mail clients can
        mark a brand-new verification message as read before this worker sees it.
        """
        self._ensure_supported()
        try:
            return await self._do_fetch(timeout)
        except EmailProviderError:
            raise
        except Exception as exc:
            logger.warning("IMAP failure for %s", self.email, exc_info=True)
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Не удалось получить письмо через IMAP.",
            ) from exc

    async def _connect(self):
        client = aioimaplib.IMAP4_SSL(
            host=self.imap_host,
            port=self.imap_port,
            ssl_context=ssl.create_default_context(),
        )
        try:
            await client.wait_hello_from_server()
            _require_ok(await client.login(self.email, self._password), "login")
            _require_ok(await client.select("INBOX"), "select")
            return client
        except Exception:
            await self._logout(client)
            raise

    async def _search_openai_messages(self, client) -> set[str]:
        result = await client.search("ALL", "FROM", "openai.com")
        _require_ok(result, "search")
        ids: set[str] = set()
        for line in result.lines or []:
            if isinstance(line, bytes):
                line = line.decode(errors="ignore")
            ids.update(token for token in str(line).split() if token.isdigit())
        return ids

    async def _do_fetch(self, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        client = await self._connect()
        try:
            while True:
                message_ids = await self._search_openai_messages(client)
                candidates = message_ids - self._baseline_ids

                # If preflight was not used (e.g. a standalone provider call),
                # inspect the current messages too, preserving API usefulness.
                if not self._baseline_ids:
                    candidates = message_ids

                for message_id in sorted(candidates, key=int, reverse=True):
                    fetch_result = await client.fetch(message_id, "(BODY.PEEK[])")
                    _require_ok(fetch_result, "fetch")
                    code = parse_verification_code(_message_text(fetch_result.lines or []))
                    if code:
                        return code

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EmailProviderError(
                        EmailErrorCode.NO_CODE,
                        "Новое письмо с кодом OpenAI не пришло за отведённое время.",
                    )
                await asyncio.sleep(min(2.0, remaining))
        finally:
            await self._logout(client)

    @staticmethod
    async def _logout(client) -> None:
        try:
            await client.logout()
        except Exception:
            pass


def detect_imap_provider(
    email: str,
    password: str,
    fallback_host: str = _DEFAULT_FALLBACK_HOST,
) -> EmailProvider:
    """По email-домену выбирает безопасный источник кодов подтверждения.

    Microsoft consumer mail uses Outlook Web because password-based IMAP is
    disabled.  Other domains use IMAP; unknown domains use ``fallback_host``.
    """
    domain = email.split("@")[-1].lower() if "@" in email else ""
    if domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}:
        # Microsoft disabled IMAP Basic authentication.  Use Outlook Web with
        # an ephemeral browser session instead; no mailbox cookies are written
        # to disk or persisted in the database.
        from app.integrations.email.outlook_web_provider import OutlookWebProvider

        return OutlookWebProvider(email, password)

    host = _KNOWN_HOSTS.get(domain, fallback_host)
    return IMAPProvider(
        email=email,
        password=password,
        imap_host=host,
    )
