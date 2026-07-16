from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
import hashlib
import logging
import re
import socket
import ssl
import time

import aioimaplib
from python_socks import ProxyError, ProxyType
from python_socks.async_.asyncio import Proxy as AsyncioProxy

from app.integrations.email.provider import (
    EmailErrorCode,
    EmailProvider,
    EmailProviderError,
    FreshVerificationCode,
    parse_verification_code,
)
from app.integrations.playwright.proxy import BrowserProxy, ProxyUnavailableError

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
_JUNK_FOLDER_CANDIDATES: dict[str, tuple[str, ...]] = {
    "imap.gmail.com": ("[Gmail]/Spam",),
    "outlook.office365.com": ("Junk Email", "Junk"),
    "imap.mail.yahoo.com": ("Bulk Mail", "Spam"),
    "imap.mail.me.com": ("Junk",),
}
_GENERIC_JUNK_FOLDER_CANDIDATES = ("Junk", "Junk Email", "Spam")
_MAX_MESSAGE_BYTES = 512 * 1024
_MAX_CANDIDATES_PER_FOLDER = 20
_PROXY_CONNECT_TIMEOUT_SECONDS = 10.0


class IMAPResponseError(RuntimeError):
    """An IMAP command completed with a non-OK protocol response."""


class _SocketIMAP4SSL(aioimaplib.IMAP4_SSL):
    """Start aioimaplib TLS on an already proxy-connected TCP socket."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sock: socket.socket,
        ssl_context: ssl.SSLContext,
    ) -> None:
        self._connected_socket = sock
        super().__init__(host=host, port=port, ssl_context=ssl_context)

    def create_client(
        self,
        host: str,
        port: int,
        loop: asyncio.AbstractEventLoop | None,
        conn_lost_cb=None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        del port
        local_loop = loop if loop is not None else asyncio.get_running_loop()
        context = ssl_context or ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        self.protocol = aioimaplib.IMAP4ClientProtocol(local_loop, conn_lost_cb)
        self._client_task = local_loop.create_task(
            local_loop.create_connection(
                lambda: self.protocol,
                sock=self._connected_socket,
                ssl=context,
                server_hostname=host,
            )
        )


def _proxy_connector(proxy: BrowserProxy) -> AsyncioProxy:
    is_socks5 = proxy.proxy_type == "socks5"
    proxy_type = ProxyType.SOCKS5 if is_socks5 else ProxyType.HTTP
    return AsyncioProxy(
        proxy_type=proxy_type,
        host=proxy.host,
        port=proxy.port,
        username=None if is_socks5 else proxy.username,
        password=None if is_socks5 else proxy.password,
        rdns=True,
    )


async def _connect_imap_over_proxy(
    proxy: BrowserProxy,
    *,
    imap_host: str,
    imap_port: int,
    ssl_context: ssl.SSLContext,
) -> _SocketIMAP4SSL:
    """Open HTTP CONNECT/SOCKS5, then authenticate TLS to the IMAP host."""

    sock: socket.socket | None = None
    try:
        sock = await _proxy_connector(proxy).connect(
            dest_host=imap_host,
            dest_port=imap_port,
            timeout=_PROXY_CONNECT_TIMEOUT_SECONDS,
        )
        client = _SocketIMAP4SSL(
            host=imap_host,
            port=imap_port,
            sock=sock,
            ssl_context=ssl_context,
        )
        # Await the TLS transport task explicitly. aioimaplib normally starts
        # it in the background; doing so here lets a dead route fail now and
        # never fall back to a direct connection.
        await asyncio.wait_for(
            client._client_task,
            timeout=_PROXY_CONNECT_TIMEOUT_SECONDS,
        )
        return client
    except (ProxyError, OSError, ssl.SSLError, asyncio.TimeoutError) as exc:
        if sock is not None:
            sock.close()
        raise ProxyUnavailableError(
            "Маршрут входа в почту через прокси недоступен."
        ) from exc


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


def _raw_message(lines: list[object]) -> bytes:
    byte_lines = [line for line in lines if isinstance(line, bytes)]
    return max(byte_lines, key=len, default=b"")


def _message_text(lines: list[object]) -> str:
    """Decode an IMAP RFC822 response, including multipart/encoded MIME bodies."""
    raw = _raw_message(lines)
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


def _message_received_at(lines: list[object]) -> datetime | None:
    """Return the server-provided IMAP INTERNALDATE, never sender Date."""
    for line in lines:
        if not isinstance(line, bytes):
            continue
        match = re.search(rb'INTERNALDATE\s+"([^"]+)"', line, re.IGNORECASE)
        if match is None:
            continue
        try:
            received_at = parsedate_to_datetime(match.group(1).decode("ascii"))
        except (UnicodeError, TypeError, ValueError, OverflowError):
            return None
        if received_at.tzinfo is None:
            return None
        return received_at.astimezone(timezone.utc)
    return None


def _message_size(lines: list[object]) -> int | None:
    for line in lines:
        if not isinstance(line, bytes):
            continue
        match = re.search(rb"RFC822\.SIZE\s+(\d+)", line, re.IGNORECASE)
        if match is not None:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def _message_identity(lines: list[object]) -> str | None:
    raw = _raw_message(lines)
    if not raw:
        return None
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        value = str(message.get("message-id") or "").strip()
    except Exception:
        return None
    return value or None


def _is_openai_sender(lines: list[object]) -> bool:
    raw = _raw_message(lines)
    if not raw:
        return False
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
        addresses = getaddresses(message.get_all("from", []))
    except Exception:
        return False
    for _name, address in addresses:
        domain = address.rsplit("@", 1)[-1].lower().rstrip(".")
        if domain == "openai.com" or domain.endswith(".openai.com"):
            return True
    return False


def _fresh_fingerprint(message_identity: str, received_at: datetime) -> str:
    material = f"imap|{message_identity}|{received_at.isoformat()}"
    return hashlib.sha256(material.encode()).hexdigest()


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
        proxy: BrowserProxy | None = None,
    ) -> None:
        self.email = email
        self._password = password
        self.imap_host = imap_host
        self.imap_port = imap_port
        self._basic_auth_supported = basic_auth_supported
        self._proxy = proxy
        self._baseline_ids: set[tuple[str, str]] = set()

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
                baseline_ids: set[tuple[str, str]] = set()
                async for mailbox in self._selected_mailboxes(client):
                    message_ids = await self._search_openai_messages(client)
                    baseline_ids.update(
                        (mailbox, message_id) for message_id in message_ids
                    )
                self._baseline_ids = baseline_ids
            finally:
                await self._logout(client)
        except (EmailProviderError, ProxyUnavailableError):
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
        except (EmailProviderError, ProxyUnavailableError):
            raise
        except Exception as exc:
            logger.warning("IMAP failure for %s", self.email, exc_info=True)
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Не удалось получить письмо через IMAP.",
            ) from exc

    async def fetch_fresh_verification_code(
        self,
        *,
        not_before: datetime,
        timeout: float = 10.0,
    ) -> FreshVerificationCode:
        """Read a timestamp-proven OpenAI code without a post-arrival preflight."""
        self._ensure_supported()
        cutoff = (
            not_before.replace(tzinfo=timezone.utc)
            if not_before.tzinfo is None
            else not_before.astimezone(timezone.utc)
        )
        try:
            return await self._do_fetch_fresh(cutoff, timeout)
        except (EmailProviderError, ProxyUnavailableError):
            raise
        except Exception as exc:
            logger.warning("IMAP fresh-code lookup failed for %s", self.email, exc_info=True)
            raise EmailProviderError(
                EmailErrorCode.CONNECTION_FAILED,
                "Не удалось получить свежее письмо через IMAP.",
            ) from exc

    async def _connect(self):
        ssl_context = ssl.create_default_context()
        if self._proxy is None:
            client = aioimaplib.IMAP4_SSL(
                host=self.imap_host,
                port=self.imap_port,
                ssl_context=ssl_context,
            )
        else:
            client = await _connect_imap_over_proxy(
                self._proxy,
                imap_host=self.imap_host,
                imap_port=self.imap_port,
                ssl_context=ssl_context,
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
                async for mailbox in self._selected_mailboxes(client):
                    message_ids = await self._search_openai_messages(client)
                    candidates = {
                        message_id
                        for message_id in message_ids
                        if (mailbox, message_id) not in self._baseline_ids
                    }

                    for message_id in sorted(candidates, key=int, reverse=True)[
                        :_MAX_CANDIDATES_PER_FOLDER
                    ]:
                        header_result = await client.fetch(
                            message_id,
                            "(RFC822.SIZE BODY.PEEK[HEADER.FIELDS "
                            "(FROM SUBJECT MESSAGE-ID)])",
                        )
                        _require_ok(header_result, "fetch")
                        header_lines = header_result.lines or []
                        size = _message_size(header_lines)
                        if (
                            size is None
                            or size > _MAX_MESSAGE_BYTES
                            or not _is_openai_sender(header_lines)
                        ):
                            self._baseline_ids.add((mailbox, message_id))
                            continue
                        fetch_result = await client.fetch(message_id, "(BODY.PEEK[])")
                        _require_ok(fetch_result, "fetch")
                        self._baseline_ids.add((mailbox, message_id))
                        code = parse_verification_code(
                            _message_text(fetch_result.lines or [])
                        )
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

    async def _do_fetch_fresh(
        self,
        not_before: datetime,
        timeout: float,
    ) -> FreshVerificationCode:
        deadline = time.monotonic() + timeout
        client = await self._connect()
        examined: set[tuple[str, str]] = set()
        try:
            while True:
                async for mailbox in self._selected_mailboxes(client):
                    message_ids = await self._search_openai_messages(client)
                    for message_id in sorted(message_ids, key=int, reverse=True)[
                        :_MAX_CANDIDATES_PER_FOLDER
                    ]:
                        identity = (mailbox, message_id)
                        if identity in examined:
                            continue
                        header_result = await client.fetch(
                            message_id,
                            "(RFC822.SIZE INTERNALDATE BODY.PEEK[HEADER.FIELDS "
                            "(FROM SUBJECT MESSAGE-ID)])",
                        )
                        _require_ok(header_result, "fetch")
                        header_lines = header_result.lines or []
                        received_at = _message_received_at(header_lines)
                        message_identity = _message_identity(header_lines)
                        size = _message_size(header_lines)
                        if (
                            received_at is None
                            or received_at < not_before
                            or message_identity is None
                            or size is None
                            or size > _MAX_MESSAGE_BYTES
                            or not _is_openai_sender(header_lines)
                        ):
                            examined.add(identity)
                            continue
                        fetch_result = await client.fetch(message_id, "(BODY.PEEK[])")
                        _require_ok(fetch_result, "fetch")
                        examined.add(identity)
                        code = parse_verification_code(
                            _message_text(fetch_result.lines or [])
                        )
                        if code is not None:
                            return FreshVerificationCode(
                                code=code,
                                received_at=received_at,
                                fingerprint=_fresh_fingerprint(
                                    message_identity, received_at,
                                ),
                            )

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise EmailProviderError(
                        EmailErrorCode.NO_CODE,
                        "Свежее письмо с кодом OpenAI не найдено.",
                    )
                await asyncio.sleep(min(2.0, remaining))
        finally:
            await self._logout(client)

    async def _selected_mailboxes(self, client) -> AsyncIterator[str]:
        """Select Inbox and the first supported provider-specific junk alias."""
        for mailbox in ("INBOX", *self._junk_folder_candidates()):
            selected = await client.select(self._mailbox_argument(mailbox))
            if mailbox == "INBOX":
                _require_ok(selected, "select")
            elif not self._response_is_ok(selected):
                continue

            yield mailbox
            if mailbox != "INBOX":
                return

    @staticmethod
    def _mailbox_argument(mailbox: str) -> str:
        if mailbox.upper() == "INBOX":
            return "INBOX"
        escaped = mailbox.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

    def _junk_folder_candidates(self) -> tuple[str, ...]:
        return _JUNK_FOLDER_CANDIDATES.get(
            self.imap_host.lower(),
            _GENERIC_JUNK_FOLDER_CANDIDATES,
        )

    @staticmethod
    def _response_is_ok(response) -> bool:
        result = getattr(response, "result", "")
        if isinstance(result, bytes):
            result = result.decode(errors="replace")
        return str(result).upper() == "OK"

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
    *,
    browser_proxy: BrowserProxy | None = None,
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

        return OutlookWebProvider(email, password, proxy=browser_proxy)

    host = _KNOWN_HOSTS.get(domain, fallback_host)
    return IMAPProvider(
        email=email,
        password=password,
        imap_host=host,
        proxy=browser_proxy,
    )
