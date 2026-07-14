from types import SimpleNamespace

import pytest
from aiohttp import ClientSession
from yarl import URL

from app.integrations.funpay.secure_session import (
    FunPayRedirectSecurityError,
    SecureFunPaySession,
    _reject_cross_origin_redirect,
)


async def test_funpay_cookies_are_host_scoped():
    adapter = SecureFunPaySession()
    session = await adapter.session()
    try:
        adapter.prepare_cookies(
            session,
            SimpleNamespace(
                golden_key="secret-key",
                phpsessid="php-session",
                golden_seal="seal",
            ),
        )

        funpay = session.cookie_jar.filter_cookies(URL("https://funpay.com/"))
        plaintext = session.cookie_jar.filter_cookies(URL("http://funpay.com/"))
        attacker = session.cookie_jar.filter_cookies(URL("https://example.com/"))
        assert funpay["golden_key"].value == "secret-key"
        assert funpay["PHPSESSID"].value == "php-session"
        assert "golden_key" not in plaintext
        assert "PHPSESSID" not in plaintext
        assert "golden_key" not in attacker
        assert "PHPSESSID" not in attacker
    finally:
        await session.close()
        await adapter.close()


@pytest.mark.parametrize(
    "location",
    [
        "https://example.com/steal",
        "//example.com/steal",
        "http://funpay.com/downgrade",
    ],
)
async def test_cross_origin_or_downgrade_redirect_is_blocked(location: str):
    params = SimpleNamespace(
        response=SimpleNamespace(
            url=URL("https://funpay.com/orders/"),
            headers={"Location": location},
        )
    )

    with pytest.raises(FunPayRedirectSecurityError):
        await _reject_cross_origin_redirect(None, None, params)  # type: ignore[arg-type]


async def test_same_origin_redirect_is_allowed():
    params = SimpleNamespace(
        response=SimpleNamespace(
            url=URL("https://funpay.com/orders/"),
            headers={"Location": "/login/"},
        )
    )

    await _reject_cross_origin_redirect(None, None, params)  # type: ignore[arg-type]
