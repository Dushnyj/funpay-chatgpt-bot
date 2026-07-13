from unittest.mock import AsyncMock, MagicMock

import pytest

from app.integrations.playwright.kick import KickError, _logout_everywhere


def _button(target: MagicMock) -> MagicMock:
    locator = MagicMock()
    locator.first = target
    return locator


def _logout_page(body: str) -> tuple[MagicMock, MagicMock, MagicMock]:
    page = MagicMock()
    page.url = "https://chatgpt.com/#settings"
    page.goto = AsyncMock()
    page.title = AsyncMock(return_value="Settings")
    page.query_selector = AsyncMock(return_value=None)
    page.text_content = AsyncMock(return_value=body)

    logout = MagicMock()
    logout.click = AsyncMock()
    confirm = MagicMock()
    confirm.click = AsyncMock()
    dialog = MagicMock()
    dialog.get_by_role = MagicMock(return_value=_button(confirm))
    dialog_wrapper = MagicMock()
    dialog_wrapper.last = dialog

    def get_by_role(role: str, **_kwargs):
        if role == "dialog":
            return dialog_wrapper
        return _button(logout)

    page.get_by_role = MagicMock(side_effect=get_by_role)
    page.locator = MagicMock(return_value=_button(logout))
    return page, logout, confirm


async def test_logout_everywhere_requires_explicit_success_signal():
    page, logout, confirm = _logout_page("Settings")

    with pytest.raises(KickError, match="не подтвердил"):
        await _logout_everywhere(page, timeout_ms=100)

    logout.click.assert_awaited_once()
    confirm.click.assert_awaited_once()


async def test_logout_everywhere_accepts_success_notification():
    page, logout, confirm = _logout_page("Successfully logged out of all sessions")

    await _logout_everywhere(page, timeout_ms=1_000)

    logout.click.assert_awaited_once()
    confirm.click.assert_awaited_once()
