from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from playwright.async_api import BrowserContext, async_playwright

from app.integrations.playwright.proxy import (
    BrowserProxy,
    ProxyUnavailableError,
    is_proxy_failure,
)


@asynccontextmanager
async def browser_context(
    headless: bool = True,
    *,
    proxy: BrowserProxy | None = None,
) -> AsyncGenerator[BrowserContext, None]:
    """Создаёт изолированный incognito-контекст Chromium.

    Каждый вызов — новый контекст с чистыми cookies. После выхода контекст
    закрывается, cookies уничтожаются — сессии арендаторов не затрагиваются,
    а операции на разных аккаунтах не протекают друг в друга.
    """
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                headless=headless,
                proxy=proxy.as_playwright() if proxy is not None else None,
            )
            context = await browser.new_context()
        except Exception as exc:
            if proxy is not None and is_proxy_failure(exc):
                raise ProxyUnavailableError() from exc
            raise
        try:
            yield context
        except Exception as exc:
            if proxy is not None and is_proxy_failure(exc):
                raise ProxyUnavailableError() from exc
            raise
        finally:
            await context.close()
            await browser.close()
