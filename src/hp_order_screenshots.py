"""
This module defines the `main()` coroutine for the Apify Actor, executed from the `__main__.py` file.

Feel free to modify this file to suit your specific needs.

To build Apify Actors, utilize the Apify SDK toolkit, read more at the official documentation:
https://docs.apify.com/sdk/python
"""

import asyncio
import base64
import logging

# Apify SDK - toolkit for building Apify Actors, read more at https://docs.apify.com/sdk/python
from apify import Actor

# We use playwright for scraping, read more at https://playwright.dev/python/docs/api/class-playwright
from playwright.async_api import Page, async_playwright

# We use pydantic for parsing te input and loading the environment variables, read more at https://pydantic-docs.helpmanual.io/

from src.models import Input, Output, Screenshot, Settings


log = logging.getLogger(__name__)

URL_ORDER = "https://www.helloprint.co.uk/orderdetail?id_order={id_order}"
URL_LOGIN = "https://www.helloprint.co.uk/authentication"
DEVICE = "Desktop Chrome"


async def login(settings: Settings, page: Page):
    await page.goto(URL_LOGIN)

    # Check if we were redirected to the my-account page
    if page.url.lower().startswith(URL_LOGIN.lower()):
        await page.fill("#email-login", settings.hp_username)
        await page.fill("#password-login", settings.hp_password)
        await asyncio.sleep(settings.execution_speed * 1)
        await page.click("#SubmitLogin")

        if page.url.lower().startswith(URL_LOGIN.lower()):
            await asyncio.sleep(settings.execution_speed * 1)
            await page.click("#SubmitLogin")

        # Wait until we have been redirected to the my-account page
        for _ in range(20):
            if not page.url.lower().startswith(URL_LOGIN.lower()):
                break
            await asyncio.sleep(settings.execution_speed * 0.5)
        else:
            raise Exception("Login failed")


async def get_order(settings: Settings, page: Page, order_number):
    await page.goto(URL_ORDER.format(id_order=order_number))

    # Wait until we have been redirected to the my-account page
    for _ in range(20):
        order_items = await page.locator(".order-item").count()
        if order_items > 0:
            break
        await asyncio.sleep(settings.execution_speed * 0.5)
    else:
        raise Exception("Get order failed")


async def run(settings: Settings, args: Input, page: Page):
    await login(settings, page)
    await get_order(settings, page, args.onl_number)

    await asyncio.sleep(settings.execution_speed * 1)

    screenshot_bytes = await page.screenshot(
        full_page=True, type="png", style="#foot, header, .footer-help, a.open-chat, div:has(> .my-account__sidebar) { display: none; }"
    )

    return [
        Screenshot(
            name="screenshot.png",
            content=base64.b64encode(screenshot_bytes).decode(),
            contentType="image/png",
        )
    ]


async def main() -> None:
    """main() is executed when the module is run"""
    settings = Settings()
    async with Actor as actor, async_playwright() as playwright:
        args = Input(**await actor.get_input() or {})
        browser = await playwright.chromium.launch(headless=settings.headless)
        device = playwright.devices[DEVICE]
        context = await browser.new_context(**device)
        page = await context.new_page()

        screenshots = await run(settings=settings, args=args, page=page)

        await actor.set_value("OUTPUT", Output(screenshots=screenshots).dict())

        if args.save_screenshots:
            for i, screenshot in enumerate(screenshots):
                await actor.set_value(
                    key=f"screenshot_{i}.{screenshot.contentType.split('/')[1]}",
                    value=base64.b64decode(screenshot.content.encode()),
                    content_type=screenshot.contentType,
                )

        await browser.close()
