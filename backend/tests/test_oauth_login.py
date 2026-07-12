"""Unit-тесты oauth_login с замокированным Playwright (без реального браузера).

Покрывают:
  - обратная совместимость: totp_secret + без email_provider → старое поведение.
  - email_provider задан, totp_secret пуст → запрашивается email-code.
  - email_provider + totp_secret → оба шага подтверждения.
  - отсутствие подтверждений → пропуск без обращений к provider.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlparse

import pytest

from app.integrations.playwright.oauth_login import (
    OAuthLoginError,
    _do_post_password_steps,
    _parse_oauth_callback,
)


def _make_input_locator(*, raises_on_wait: bool = False) -> MagicMock:
    """Замокированный locator(input), имитирующий wait_for + fill.

    raises_on_wait=True имитирует отсутствие поля (Playwright TimeoutError).
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    loc = MagicMock()
    loc.wait_for = AsyncMock(
        side_effect=PlaywrightTimeoutError("no code field") if raises_on_wait else None
    )
    loc.fill = AsyncMock(return_value=None)
    return loc


def _make_page(inputs_spec):
    """Замокированная Playwright Page.

    inputs_spec — список bool: True = поле появится (wait_for ок), False = поле
    отсутствует (wait_for падает по таймауту). Каждый вызов `page.locator(...).first`
    выдаёт следующий input из очереди. Созданные input-ы сохраняются в `page.inputs`.
    """
    page = MagicMock()
    inputs = [_make_input_locator(raises_on_wait=not appears) for appears in inputs_spec]
    page.inputs = []  # те, что реально были выданы через .first
    queue = list(inputs)

    def _locator_factory(_selector: str):
        loc = MagicMock()
        if queue:
            loc.first = queue.pop(0)
            page.inputs.append(loc.first)
        else:
            # Больше не ожидалось — поле отсутствует.
            extra = _make_input_locator(raises_on_wait=True)
            loc.first = extra
            page.inputs.append(extra)
        return loc

    page.locator = MagicMock(side_effect=_locator_factory)

    continue_btn = MagicMock()
    continue_btn.click = AsyncMock(return_value=None)
    page.get_by_role = MagicMock(return_value=continue_btn)
    page.continue_btn = continue_btn
    return page


def _make_email_provider(code: str | None = "123456") -> MagicMock:
    """Замокированный EmailProvider: fetch_verification_code возвращает фиксированный код."""
    provider = MagicMock()
    provider.fetch_verification_code = AsyncMock(return_value=code)
    return provider


async def test_post_password_totp_only_backward_compatible():
    """totp_secret задан, email_provider=None → вводится TOTP-код (старое поведение)."""
    page = _make_page([True])  # один input появится (TOTP)
    totp = "JBSWY3DPEHPK3PXP"

    await _do_post_password_steps(page, totp, email_provider=None)

    # Один input заполнен TOTP-кодом (6 цифр).
    assert len(page.inputs) == 1
    page.inputs[0].fill.assert_awaited_once()
    filled_code = page.inputs[0].fill.call_args.args[0]
    assert filled_code.isdigit() and len(filled_code) == 6
    page.continue_btn.click.assert_awaited_once()


async def test_post_password_email_code_only():
    """email_provider задан, totp_secret пуст → запрашивается email-code."""
    page = _make_page([True, False])  # email-code появится, 2FA нет (totp пуст)
    provider = _make_email_provider(code="987654")

    await _do_post_password_steps(page, totp_secret="", email_provider=provider)

    provider.fetch_verification_code.assert_awaited_once()
    page.inputs[0].fill.assert_awaited_once_with("987654")
    page.continue_btn.click.assert_awaited_once()


async def test_post_password_email_code_then_totp():
    """email_provider + totp_secret → оба шага: сначала email-code, затем TOTP."""
    page = _make_page([True, True])  # оба input появятся
    provider = _make_email_provider(code="111111")
    totp = "JBSWY3DPEHPK3PXP"

    await _do_post_password_steps(page, totp, email_provider=provider)

    provider.fetch_verification_code.assert_awaited_once()
    # Оба input-а заполнены: первый — email-code, второй — TOTP.
    assert len(page.inputs) == 2
    page.inputs[0].fill.assert_awaited_once_with("111111")
    totp_code = page.inputs[1].fill.call_args.args[0]
    assert totp_code.isdigit() and len(totp_code) == 6
    # Continue кликнут дважды (для каждого шага).
    assert page.continue_btn.click.await_count == 2


async def test_post_password_no_email_code_skips_provider():
    """email_provider задан, но input не появился → провайдер не вызывается."""
    page = _make_page([False, False])  # email-code не появился; totp пуст → 2FA не идём
    provider = _make_email_provider(code="123456")

    await _do_post_password_steps(page, totp_secret="", email_provider=provider)

    # Поле не появилось → email-code не нужен.
    provider.fetch_verification_code.assert_not_awaited()
    page.continue_btn.click.assert_not_awaited()


async def test_post_password_no_code_no_totp_noop():
    """Ни provider, ни totp_secret, поле не появилось → пустой no-op."""
    page = _make_page([False, False])

    await _do_post_password_steps(page, totp_secret="", email_provider=None)

    page.continue_btn.click.assert_not_awaited()


async def test_post_password_email_provider_none_skips_email_step():
    """email_provider=None → блок email-code пропускается, идёт сразу к TOTP."""
    page = _make_page([True])
    totp = "JBSWY3DPEHPK3PXP"

    await _do_post_password_steps(page, totp, email_provider=None)

    assert len(page.inputs) == 1
    code = page.inputs[0].fill.call_args.args[0]
    assert code.isdigit() and len(code) == 6


async def test_post_password_email_code_none_raises():
    """email-code требуется (поле есть), но provider вернул None → OAuthLoginError."""
    page = _make_page([True])  # поле появится
    provider = _make_email_provider(code=None)

    with pytest.raises(OAuthLoginError):
        await _do_post_password_steps(page, totp_secret="", email_provider=provider)


async def test_login_and_get_auth_code_backward_compat_positional(monkeypatch):
    """Позиционный вызов login_and_get_auth_code(ctx, login, pass, totp) совместим."""
    from app.integrations.playwright import oauth_login
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    page = MagicMock()
    request_handlers = []
    page.on = MagicMock(
        side_effect=lambda event, handler: request_handlers.append(handler)
        if event == "request" else None
    )
    page.close = AsyncMock()
    page.route = AsyncMock()

    async def _goto(url, **_kwargs):
        state = parse_qs(urlparse(url).query)["state"][0]
        request_handlers[0](SimpleNamespace(
            url=f"http://localhost:1455/auth/callback?code=auth-code-123&state={state}"
        ))

    page.goto = AsyncMock(side_effect=_goto)

    def _noop_locator(_selector: str):
        loc = MagicMock()
        loc.first = MagicMock()
        # Ни email-code, ни 2FA поля нет → логин без подтверждений.
        loc.first.wait_for = AsyncMock(side_effect=PlaywrightTimeoutError("no field"))
        loc.first.fill = AsyncMock()
        return loc

    page.locator = MagicMock(side_effect=_noop_locator)
    continue_btn = MagicMock()
    continue_btn.click = AsyncMock()
    page.get_by_role = MagicMock(return_value=continue_btn)

    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)

    # Позиционный вызов как раньше: (context, login, password, totp_secret)
    code, verifier = await oauth_login.login_and_get_auth_code(
        context, "user@example.com", "pass", "JBSWY3DPEHPK3PXP"
    )

    assert code == "auth-code-123"
    assert isinstance(verifier, str) and verifier
    page.close.assert_awaited_once()


def test_parse_oauth_callback_validates_state_and_endpoint():
    assert _parse_oauth_callback(
        "http://localhost:1455/auth/callback?code=abc&state=expected",
        "expected",
    ) == "abc"
    assert _parse_oauth_callback("https://example.com/?code=abc", "expected") is None
    with pytest.raises(OAuthLoginError, match="state mismatch"):
        _parse_oauth_callback(
            "http://localhost:1455/auth/callback?code=abc&state=wrong",
            "expected",
        )


def test_parse_oauth_callback_propagates_provider_error():
    with pytest.raises(OAuthLoginError, match="access denied"):
        _parse_oauth_callback(
            "http://localhost:1455/auth/callback?error=access_denied"
            "&error_description=access%20denied&state=expected",
            "expected",
        )
