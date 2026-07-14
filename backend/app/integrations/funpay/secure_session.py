from __future__ import annotations

from contextlib import suppress
from http.cookies import SimpleCookie
from typing import Any

from aiohttp import ClientSession, CookieJar, TCPConnector, TraceConfig
from aiohttp_socks import ProxyConnector
from funpaybotengine.client.session.aiohttp_session import AioHttpSession
from funpaybotengine.exceptions.bot_exceptions import BotNotInitializedError
from yarl import URL


_FUNPAY_ORIGIN = URL("https://funpay.com/")


class FunPayRedirectSecurityError(RuntimeError):
    """A credential-bearing FunPay request attempted to leave its origin."""


async def _reject_cross_origin_redirect(
    _session: ClientSession,
    _trace_context: Any,
    params: Any,
) -> None:
    location = params.response.headers.get("Location")
    if not location:
        return
    target = params.response.url.join(URL(location))
    if target.origin() != _FUNPAY_ORIGIN.origin():
        raise FunPayRedirectSecurityError(
            f"Blocked FunPay redirect to untrusted origin {target.origin()}"
        )


class SecureFunPaySession(AioHttpSession):
    """FunPay transport with host-scoped secrets and redirect confinement.

    FunPayBotEngine 0.7 creates domain-less cookies by calling
    ``CookieJar.update_cookies`` without a response URL.  This adapter keeps
    Golden Key/PHP session cookies host-only and aborts before aiohttp follows
    a cross-origin or HTTPS-to-HTTP redirect.
    """

    async def session(self) -> ClientSession:
        if self._connector is None or self._connector.closed:
            self._connector = (
                ProxyConnector.from_url(self._proxy)
                if self._proxy
                else TCPConnector()
            )
        trace = TraceConfig()
        trace.on_request_redirect.append(_reject_cross_origin_redirect)
        return ClientSession(
            base_url=_FUNPAY_ORIGIN,
            connector=self._connector,
            connector_owner=False,
            cookie_jar=CookieJar(),
            trace_configs=[trace],
        )

    def prepare_cookies(
        self,
        session: ClientSession,
        bot: Any,
        skip_session_cookies: bool = False,
    ) -> None:
        cookies = {"cookie_prefs": "1"}
        with suppress(BotNotInitializedError):
            if bot.golden_key:
                cookies["golden_key"] = bot.golden_key
        with suppress(BotNotInitializedError):
            if bot.phpsessid and not skip_session_cookies:
                cookies["PHPSESSID"] = bot.phpsessid
        with suppress(BotNotInitializedError):
            if bot.golden_seal and not skip_session_cookies:
                cookies["golden_seal"] = bot.golden_seal
        secure_cookies = SimpleCookie()
        for name, value in cookies.items():
            secure_cookies[name] = value
            secure_cookies[name]["secure"] = True
            secure_cookies[name]["httponly"] = True
            secure_cookies[name]["samesite"] = "Lax"
        session.cookie_jar.update_cookies(
            secure_cookies,
            response_url=_FUNPAY_ORIGIN,
        )
