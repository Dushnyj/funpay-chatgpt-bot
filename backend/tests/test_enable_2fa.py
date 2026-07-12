"""Unit-тесты enable_2fa с замокированным Playwright (без реального браузера).

Покрывают:
  - happy path: логин → settings → QR → decode → подтверждение кодом → secret возвращён.
  - кнопка 2FA не найдена → Enable2FAError.
  - QR не найден → Enable2FAError.
  - QR не декодирован → Enable2FAError.
  - _login вызывается с totp_secret="" и email_provider.
  - page закрывается в finally даже при ошибке.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.integrations.playwright.enable_2fa import Enable2FAError, enable_2fa


SECRET = "JBSWY3DPEHPK3PXP"


def _async_raises(exc):
    """AsyncMock, бросающий exc."""
    return AsyncMock(side_effect=exc)


def _make_selector_chain(*, click_ok=True, wait_for_ok=True, fill_ok=True,
                         screenshot_ok=True, click_raises=False):
    """Замокированный результат page.locator(...) / page.get_by_role(...).

    Возвращает объект, у которого `.first` имеет wait_for/fill/click/screenshot.
    Все awaitable — AsyncMock, чтобы корректно работали await.
    """
    element = MagicMock()
    element.wait_for = (
        AsyncMock(return_value=None) if wait_for_ok
        else _async_raises(PlaywrightTimeoutError("timeout"))
    )
    element.fill = AsyncMock(return_value=None) if fill_ok else _async_raises(RuntimeError)
    element.click = (
        AsyncMock(return_value=None) if click_ok
        else _async_raises(PlaywrightTimeoutError("timeout") if not click_raises else RuntimeError())
    )
    element.screenshot = AsyncMock(return_value=b"fake-qr-png") if screenshot_ok else _async_raises(RuntimeError)
    wrapper = MagicMock()
    wrapper.first = element
    return wrapper


def _make_page(
    *,
    qr_found: bool = True,
    enable_button_found: bool = True,
) -> MagicMock:
    """Замокированная Page для _enable_authenticator.

    Маршрутизация:
      - page.get_by_role("button", name=...) → enable-кнопки (get_by_role в коде).
      - page.locator("button:has-text('authenticator')"/"a:...") → enable-кнопки (locator).
      - page.locator QR-селекторы → QR-элементы.
      - page.locator input → код-инпут.
    """
    page = MagicMock()
    page.goto = AsyncMock(return_value=None)
    page.close = AsyncMock(return_value=None)

    enable_btn = _make_selector_chain(
        click_ok=enable_button_found,
        wait_for_ok=True, fill_ok=True, screenshot_ok=False,
    )

    # Все QR-селекторы и input идут через page.locator. Различаем по тексту селектора.
    def _locator_factory(selector: str):
        # QR-селекторы в коде.
        qr_markers = ("data:image", "canvas", "svg", "alt*='QR'")
        if any(m in selector for m in qr_markers):
            return _make_selector_chain(
                click_ok=True,
                wait_for_ok=qr_found,
                fill_ok=True, screenshot_ok=True,
            )
        # Enable-кнопки через locator (button/anchor :has-text).
        if "authenticator" in selector:
            return enable_btn
        # Код-инпут подтверждения.
        if "code" in selector or "numeric" in selector:
            return _make_selector_chain(
                click_ok=True, wait_for_ok=True, fill_ok=True, screenshot_ok=False,
            )
        # Прочее — падающий селектор.
        return _make_selector_chain(click_ok=False, wait_for_ok=False)

    page.locator = MagicMock(side_effect=_locator_factory)

    # get_by_role: кнопки enable (в коде первые 2 попытки) + финальный Continue.
    def _get_by_role(role, name=None):
        # Кнопки enable.
        if role == "button" and name and "two-factor" in str(name):
            return enable_btn
        # Финальная кнопка подтверждения.
        if role == "button" and name and "Continue" in str(name):
            confirm = MagicMock()
            confirm_btn = MagicMock()
            confirm_btn.click = AsyncMock(return_value=None)
            confirm.first = confirm_btn
            page._confirm_btn = confirm_btn
            return confirm
        # По умолчанию — падающий.
        return _make_selector_chain(click_ok=False)

    page.get_by_role = MagicMock(side_effect=_get_by_role)
    return page


def _make_context(page: MagicMock) -> MagicMock:
    ctx = MagicMock()
    ctx.new_page = AsyncMock(return_value=page)
    return ctx


def _make_email_provider() -> MagicMock:
    provider = MagicMock()
    provider.fetch_verification_code = AsyncMock(return_value="123456")
    return provider


async def test_enable_2fa_happy_path():
    """Логин → settings → QR декодирован → код введён → secret возвращён."""
    page = _make_page(qr_found=True, enable_button_found=True)
    context = _make_context(page)
    provider = _make_email_provider()

    with patch(
        "app.integrations.playwright.enable_2fa._login", new_callable=AsyncMock
    ) as mock_login, patch(
        "app.integrations.playwright.enable_2fa.decode_qr_secret",
        return_value=SECRET,
    ) as mock_decode:
        result = await enable_2fa(context, "u@e.com", "pass", provider)

    assert result == SECRET
    # _login вызван с пустым totp_secret и email_provider.
    mock_login.assert_awaited_once()
    call = mock_login.call_args
    assert call.args[0] is page
    assert call.args[1] == "u@e.com"
    assert call.args[2] == "pass"
    assert call.args[3] == ""  # totp_secret пуст
    assert call.args[5] is provider  # email_provider
    # decode_qr_secret получил байты скриншота.
    mock_decode.assert_called_once()
    assert mock_decode.call_args.args[0] == b"fake-qr-png"
    # Кнопка подтверждения кликнута.
    assert getattr(page, "_confirm_btn", None) is not None
    page._confirm_btn.click.assert_awaited_once()
    # page закрыт.
    page.close.assert_awaited_once()


async def test_enable_2fa_enable_button_not_found():
    """Кнопка 2FA не найдена → Enable2FAError, page всё равно закрывается."""
    page = _make_page(enable_button_found=False)
    context = _make_context(page)
    provider = _make_email_provider()

    with patch("app.integrations.playwright.enable_2fa._login", new_callable=AsyncMock):
        with pytest.raises(Enable2FAError, match="Не найдена кнопка"):
            await enable_2fa(context, "u@e.com", "pass", provider)

    page.close.assert_awaited_once()


async def test_enable_2fa_qr_not_found():
    """Кнопка нажата, но QR не появился → Enable2FAError."""
    page = _make_page(qr_found=False, enable_button_found=True)
    context = _make_context(page)
    provider = _make_email_provider()

    with patch("app.integrations.playwright.enable_2fa._login", new_callable=AsyncMock):
        with pytest.raises(Enable2FAError, match="QR-код не найден"):
            await enable_2fa(context, "u@e.com", "pass", provider)

    page.close.assert_awaited_once()


async def test_enable_2fa_qr_not_decoded():
    """QR найден, но decode_qr_secret вернул None → Enable2FAError."""
    page = _make_page(qr_found=True, enable_button_found=True)
    context = _make_context(page)
    provider = _make_email_provider()

    with patch("app.integrations.playwright.enable_2fa._login", new_callable=AsyncMock), \
         patch("app.integrations.playwright.enable_2fa.decode_qr_secret", return_value=None):
        with pytest.raises(Enable2FAError, match="не декодирован"):
            await enable_2fa(context, "u@e.com", "pass", provider)

    page.close.assert_awaited_once()


async def test_enable_2fa_passes_email_provider_to_login():
    """enable_2fa передаёт email_provider в _login для обработки email-code."""
    page = _make_page()
    context = _make_context(page)
    provider = _make_email_provider()

    with patch("app.integrations.playwright.enable_2fa._login", new_callable=AsyncMock) as mock_login, \
         patch("app.integrations.playwright.enable_2fa.decode_qr_secret", return_value=SECRET):
        await enable_2fa(context, "u@e.com", "pass", provider)

    mock_login.assert_awaited_once()
    args, kwargs = mock_login.call_args
    # _login(page, login, password, totp_secret, timeout_ms, email_provider)
    assert args[5] is provider or kwargs.get("email_provider") is provider
