from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from playwright.async_api import BrowserContext, async_playwright


@asynccontextmanager
async def browser_context(headless: bool = True) -> AsyncGenerator[BrowserContext, None]:
    """Создаёт изолированный incognito-контекст Chromium.

    Каждый вызов — новый контекст с чистыми cookies. После выхода контекст
    закрывается, cookies уничтожаются — сессии арендаторов не затрагиваются,
    а операции на разных аккаунтах не протекают друг в друга.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context()
        try:
            yield context
        finally:
            await context.close()
            await browser.close()
