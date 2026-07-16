from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
from typing import Any


_PROXY_ERROR_MARKERS = (
    "err_proxy_connection_failed",
    "err_tunnel_connection_failed",
    "err_socks_connection_failed",
    "err_no_supported_proxies",
    "err_invalid_auth_credentials",
    "err_proxy_auth_requested",
    "proxy connection",
    "proxy authentication required",
    "407 proxy authentication",
    "proxyconnect",
    "socks connection",
    "socks5",
)


class ProxyUnavailableError(RuntimeError):
    """Secret-free signal that a configured browser route cannot be used."""

    code = "proxy_unavailable"

    def __init__(self, detail: str = "Маршрут входа через прокси недоступен.") -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True, slots=True)
class BrowserProxy:
    """Decrypted in-memory Playwright proxy configuration.

    ``repr=False`` is intentional: exception logs and debugger output must not
    expose credentials loaded from encrypted database columns.
    """

    route_id: int
    proxy_type: str
    host: str
    port: int
    username: str | None = field(default=None, repr=False)
    password: str | None = field(default=None, repr=False)
    config_revision: int | None = None

    @property
    def server(self) -> str:
        # The UI/provider category "HTTPS CONNECT" means an HTTP proxy that
        # tunnels HTTPS destinations with CONNECT. Playwright guarantees HTTP
        # and SOCKS proxy transports, not TLS-to-the-proxy, so both HTTP
        # categories deliberately use the supported ``http://`` scheme.
        scheme = "http" if self.proxy_type == "https" else self.proxy_type
        host = self.host
        try:
            if ipaddress.ip_address(host).version == 6:
                host = f"[{host}]"
        except ValueError:
            pass
        return f"{scheme}://{host}:{self.port}"

    def as_playwright(self) -> dict[str, Any]:
        result: dict[str, Any] = {"server": self.server}
        if self.username is not None:
            result["username"] = self.username
        if self.password is not None:
            result["password"] = self.password
        return result


def is_proxy_failure(exc: BaseException) -> bool:
    """Recognise Playwright/Chromium proxy errors without logging their URL."""

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ProxyUnavailableError):
            return True
        message = str(current).casefold()
        if any(marker in message for marker in _PROXY_ERROR_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False
