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
    OAuthErrorCode,
    OAuthLoginError,
    _do_post_password_steps,
    _parse_oauth_callback,
    raise_if_cloudflare,
)
from app.integrations.email.provider import EmailErrorCode, EmailProviderError


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


def _make_page(inputs_spec, *, step_kinds: list[str] | None = None):
    """Замокированная Playwright Page.

    inputs_spec — список bool: True = поле появится (wait_for ок), False = поле
    отсутствует (wait_for падает по таймауту). Каждый вызов `page.locator(...).first`
    выдаёт следующий input из очереди. Созданные input-ы сохраняются в `page.inputs`.
    """
    page = MagicMock()
    page.url = "https://auth.openai.com/"
    page.title = AsyncMock(return_value="Sign in")
    page.query_selector = AsyncMock(return_value=None)
    step_state = {"index": 0}
    marker_text = {
        "email": "Check your email. We sent a code to your email.",
        "totp": "Enter the code from your authenticator app.",
        "unknown": "Enter verification code.",
    }

    async def _page_text(*_args, **_kwargs):
        kinds = step_kinds or ["unknown"]
        kind = kinds[min(step_state["index"], len(kinds) - 1)]
        return marker_text[kind]

    page.text_content = AsyncMock(side_effect=_page_text)
    inputs = [_make_input_locator(raises_on_wait=not appears) for appears in inputs_spec]
    page.available_inputs = inputs
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

    async def _continue_click(*_args, **_kwargs):
        step_state["index"] += 1

    continue_btn.click = AsyncMock(side_effect=_continue_click)
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
    page = _make_page([True], step_kinds=["totp"])
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
    page = _make_page([True, False], step_kinds=["email"])
    provider = _make_email_provider(code="987654")

    await _do_post_password_steps(page, totp_secret="", email_provider=provider)

    provider.fetch_verification_code.assert_awaited_once()
    page.inputs[0].fill.assert_awaited_once_with("987654")
    page.continue_btn.click.assert_awaited_once()


async def test_post_password_email_code_then_totp():
    """email_provider + totp_secret → оба шага: сначала email-code, затем TOTP."""
    page = _make_page([True, True], step_kinds=["email", "totp"])
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


async def test_post_password_existing_totp_does_not_mistake_it_for_email_code():
    page = _make_page([True], step_kinds=["totp"])
    page.url = "https://auth.openai.com/mfa"
    page.text_content = AsyncMock(return_value="Enter code from your authenticator app")
    provider = _make_email_provider(code="111111")

    await _do_post_password_steps(
        page,
        "JBSWY3DPEHPK3PXP",
        email_provider=provider,
    )

    provider.fetch_verification_code.assert_not_awaited()
    assert len(page.inputs) == 1
    filled_code = page.inputs[0].fill.call_args.args[0]
    assert filled_code.isdigit() and len(filled_code) == 6


async def test_optional_email_preflight_failure_does_not_block_totp():
    page = _make_page([True], step_kinds=["totp"])
    page.url = "https://auth.openai.com/mfa"
    page.text_content = AsyncMock(return_value="Enter code from your authenticator app")
    provider = _make_email_provider(code="111111")
    preflight_error = EmailProviderError(
        EmailErrorCode.AUTH_FAILED,
        "Старый пароль почты больше не работает.",
    )

    await _do_post_password_steps(
        page,
        "JBSWY3DPEHPK3PXP",
        email_provider=provider,
        email_preflight_error=preflight_error,
    )

    provider.fetch_verification_code.assert_not_awaited()
    filled_code = page.inputs[0].fill.call_args.args[0]
    assert filled_code.isdigit() and len(filled_code) == 6


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
    page = _make_page([True], step_kinds=["totp"])
    totp = "JBSWY3DPEHPK3PXP"

    await _do_post_password_steps(page, totp, email_provider=None)

    assert len(page.inputs) == 1
    code = page.inputs[0].fill.call_args.args[0]
    assert code.isdigit() and len(code) == 6


async def test_email_code_without_provider_never_receives_totp():
    page = _make_page([True], step_kinds=["email"])

    with pytest.raises(OAuthLoginError) as error:
        await _do_post_password_steps(
            page,
            "JBSWY3DPEHPK3PXP",
            email_provider=None,
        )

    assert error.value.code is OAuthErrorCode.EMAIL_PROVIDER_UNSUPPORTED
    assert error.value.stage == "email_code"
    page.inputs[0].fill.assert_not_awaited()
    page.continue_btn.click.assert_not_awaited()


async def test_unknown_code_step_never_receives_totp():
    page = _make_page([True], step_kinds=["unknown"])

    with pytest.raises(OAuthLoginError) as error:
        await _do_post_password_steps(
            page,
            "JBSWY3DPEHPK3PXP",
            email_provider=None,
        )

    assert error.value.code is OAuthErrorCode.OAUTH_REJECTED
    assert error.value.stage == "verification_code"
    page.inputs[0].fill.assert_not_awaited()
    page.continue_btn.click.assert_not_awaited()


async def test_rejected_totp_is_reported_without_callback_timeout():
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    page = _make_page([True], step_kinds=["totp"])

    async def wait_for(*_args, **kwargs):
        if kwargs.get("state") == "hidden":
            raise PlaywrightTimeoutError("code form remains")

    page.available_inputs[0].wait_for = AsyncMock(side_effect=wait_for)
    page.text_content = AsyncMock(return_value="Incorrect code from authenticator app")

    with pytest.raises(OAuthLoginError) as error:
        await _do_post_password_steps(
            page,
            "JBSWY3DPEHPK3PXP",
            email_provider=None,
        )

    assert error.value.code is OAuthErrorCode.INVALID_TOTP
    assert error.value.stage == "totp"


async def test_remaining_totp_form_without_rejection_marker_is_not_terminal():
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    page = _make_page([True], step_kinds=["totp"])

    async def wait_for(*_args, **kwargs):
        if kwargs.get("state") == "hidden":
            raise PlaywrightTimeoutError("form is still loading")

    page.available_inputs[0].wait_for = AsyncMock(side_effect=wait_for)
    page.text_content = AsyncMock(return_value="Enter code from authenticator app")

    await _do_post_password_steps(
        page,
        "JBSWY3DPEHPK3PXP",
        email_provider=None,
    )

    page.available_inputs[0].fill.assert_awaited_once()


async def test_rejected_email_code_is_reported_without_callback_timeout():
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    page = _make_page([True], step_kinds=["email"])

    async def wait_for(*_args, **kwargs):
        if kwargs.get("state") == "hidden":
            raise PlaywrightTimeoutError("code form remains")

    page.available_inputs[0].wait_for = AsyncMock(side_effect=wait_for)
    page.text_content = AsyncMock(return_value="The email verification code is invalid")
    provider = _make_email_provider("123456")

    with pytest.raises(OAuthLoginError) as error:
        await _do_post_password_steps(
            page,
            "",
            email_provider=provider,
        )

    assert error.value.code is OAuthErrorCode.EMAIL_CODE_REJECTED
    assert error.value.stage == "email_code"


def test_invalid_totp_oauth_error_maps_to_validation_code():
    from app.services.account_validation import _from_oauth_error

    mapped = _from_oauth_error(OAuthLoginError(
        "OpenAI отклонил TOTP-код.",
        code=OAuthErrorCode.INVALID_TOTP,
        stage="totp",
    ))

    assert mapped.code == "invalid_totp"
    assert mapped.stage == "totp"


async def test_post_password_email_code_none_raises():
    """email-code требуется (поле есть), но provider вернул None → OAuthLoginError."""
    page = _make_page([True], step_kinds=["email"])
    provider = _make_email_provider(code=None)

    with pytest.raises(OAuthLoginError):
        await _do_post_password_steps(page, totp_secret="", email_provider=provider)


async def test_email_provider_error_keeps_safe_machine_code():
    page = _make_page([True], step_kinds=["email"])
    provider = _make_email_provider()
    provider.fetch_verification_code.side_effect = EmailProviderError(
        EmailErrorCode.AUTH_FAILED,
        "Почтовый сервер отклонил логин или пароль приложения.",
    )

    with pytest.raises(OAuthLoginError) as error:
        await _do_post_password_steps(page, totp_secret="", email_provider=provider)
    assert error.value.code is OAuthErrorCode.EMAIL_AUTH_FAILED
    assert error.value.stage == "email_code"


async def test_cloudflare_is_detected_without_bypass_attempt():
    page = MagicMock()
    page.url = "https://auth.openai.com/cdn-cgi/challenge-platform/"
    page.title = AsyncMock(return_value="Just a moment...")
    page.text_content = AsyncMock(return_value="Verify you are human")
    page.query_selector = AsyncMock(return_value=object())

    with pytest.raises(OAuthLoginError) as error:
        await raise_if_cloudflare(page, "authorize")
    assert error.value.code is OAuthErrorCode.CLOUDFLARE_CHALLENGE
    assert error.value.stage == "authorize"


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


async def test_login_supports_email_code_before_password():
    """Passwordless OpenAI accounts may show the mailbox code immediately."""
    from app.integrations.playwright import oauth_login
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    page = MagicMock()
    page.url = "https://auth.openai.com/log-in"
    request_handlers = []
    page.on = MagicMock(
        side_effect=lambda event, handler: request_handlers.append(handler)
        if event == "request" else None
    )
    page.route = AsyncMock()
    page.goto = AsyncMock()
    page.close = AsyncMock()
    page.title = AsyncMock(return_value="Sign in")
    page.text_content = AsyncMock(return_value="Check your email")
    page.query_selector = AsyncMock(return_value=None)

    email_input = _make_input_locator()
    password_input = _make_input_locator(raises_on_wait=True)
    code_input = _make_input_locator()
    locators = [email_input, password_input, code_input]

    def locator_factory(_selector: str):
        locator = MagicMock()
        locator.first = locators.pop(0)
        return locator

    page.locator = MagicMock(side_effect=locator_factory)
    click_count = 0

    async def continue_click():
        nonlocal click_count
        click_count += 1
        if click_count == 2:
            auth_url = page.goto.await_args.args[0]
            state = parse_qs(urlparse(auth_url).query)["state"][0]
            request_handlers[0](SimpleNamespace(
                url=(
                    "http://localhost:1455/auth/callback"
                    f"?code=passwordless-code&state={state}"
                )
            ))

    continue_button = MagicMock()
    continue_button.click = AsyncMock(side_effect=continue_click)
    page.get_by_role = MagicMock(return_value=continue_button)
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    provider = _make_email_provider("654321")
    provider.preflight = AsyncMock()

    code, verifier = await oauth_login.login_and_get_auth_code(
        context,
        "passwordless@example.com",
        "unused-password",
        email_provider=provider,
    )

    assert code == "passwordless-code"
    assert verifier
    provider.preflight.assert_awaited_once()
    provider.fetch_verification_code.assert_awaited_once()
    code_input.fill.assert_awaited_once_with("654321")
    password_input.fill.assert_not_awaited()


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
    with pytest.raises(OAuthLoginError, match="отказал в авторизации") as error:
        _parse_oauth_callback(
            "http://localhost:1455/auth/callback?error=access_denied"
            "&error_description=access%20denied&state=expected",
            "expected",
        )
    assert "access denied" not in error.value.detail
