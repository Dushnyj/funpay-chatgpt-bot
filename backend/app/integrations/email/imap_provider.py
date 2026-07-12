from __future__ import annotations

import asyncio
import logging
import ssl

import aioimaplib

from app.integrations.email.provider import parse_verification_code

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
        lines = getattr(response, "lines", None)
        raise IMAPResponseError(f"IMAP {operation} failed: {result} {lines}")


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
    ) -> None:
        self.email = email
        self._password = password
        self.imap_host = imap_host
        self.imap_port = imap_port

    async def fetch_verification_code(self, timeout: float = 60.0) -> str | None:
        """Подключается к IMAP, ищет свежие письма от OpenAI, извлекает код.

        Возвращает код или None при таймауте/ошибке/отсутствии писем.
        """
        try:
            return await asyncio.wait_for(self._do_fetch(), timeout=timeout)
        except Exception:
            logger.warning("IMAP fetch_verification_code failed for %s", self.email, exc_info=True)
            return None

    async def _do_fetch(self) -> str | None:
        client = aioimaplib.IMAP4_SSL(
            host=self.imap_host,
            port=self.imap_port,
            ssl_context=ssl.create_default_context(),
        )
        try:
            await client.wait_hello_from_server()
            _require_ok(await client.login(self.email, self._password), "login")
            _require_ok(await client.select("INBOX"), "select")

            # Ищем свежие непрочитанные от OpenAI
            result = await client.search("UNSEEN", "FROM", "openai.com")
            _require_ok(result, "search")
            if not result.lines:
                return None

            # Берём последнее (старший UID)
            uid_list: list[str] = []
            for line in result.lines:
                if isinstance(line, bytes):
                    line = line.decode(errors="ignore")
                uid_list.extend(token for token in str(line).split() if token.isdigit())
            if not uid_list:
                return None
            last_uid = uid_list[-1]

            # Загружаем тело
            fetch_result = await client.fetch(last_uid, "(BODY.PEEK[TEXT])")
            _require_ok(fetch_result, "fetch")
            for line in fetch_result.lines:
                if isinstance(line, bytes):
                    text = line.decode("utf-8", errors="ignore")
                else:
                    text = str(line)
                code = parse_verification_code(text)
                if code:
                    return code
            return None
        finally:
            try:
                await client.logout()
            except Exception:
                pass


def detect_imap_provider(
    email: str,
    password: str,
    fallback_host: str = _DEFAULT_FALLBACK_HOST,
) -> IMAPProvider:
    """По email-домену определяет IMAP-сервер и создаёт провайдера.

    Для неизвестных доменов используется fallback_host
    (настраивается через SellerSettings в будущем).
    """
    domain = email.split("@")[-1].lower() if "@" in email else ""
    host = _KNOWN_HOSTS.get(domain, fallback_host)
    return IMAPProvider(email=email, password=password, imap_host=host)
