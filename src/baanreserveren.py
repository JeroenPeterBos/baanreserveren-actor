"""
This module defines the `main()` coroutine for the Apify Actor, executed from the `__main__.py` file.

Feel free to modify this file to suit your specific needs.

To build Apify Actors, utilize the Apify SDK toolkit, read more at the official documentation:
https://docs.apify.com/sdk/python
"""

import asyncio
import logging
from itertools import zip_longest
from datetime import datetime, timedelta

# Apify SDK - toolkit for building Apify Actors, read more at https://docs.apify.com/sdk/python
from apify import Actor

# We use playwright for scraping, read more at https://playwright.dev/python/docs/api/class-playwright
from playwright.async_api import Page, async_playwright

# We use pydantic for parsing te input and loading the environment variables, read more at https://pydantic-docs.helpmanual.io/

from src.models import Input, Settings


log = logging.getLogger(__name__)
print(log.name)

URL_LOGIN = "https://squtrecht.baanreserveren.nl/"
DEVICE = "Desktop Chrome"
OPPONENTS = {
    "vera": "1073483",
    "koen": "1340920",
}


def ordered_times(args: Input) -> list[timedelta]:
    if args.leden_only:
        return args.times

    return [
        time
        for time_pairs in zip_longest(args.times, args.non_leden_times, fillvalue=None)
        for time in time_pairs
        if time is not None
    ]


async def login(settings: Settings, page: Page):
    await page.goto(URL_LOGIN)

    await page.fill('#login-form input[type="email"]', settings.username)
    await page.fill('#login-form input[type="password"]', settings.password)

    await page.click("#login-form button")

    # Wait until we have been redirected to the my-account page
    for _ in range(20):
        if await page.query_selector_all('a[href="/auth/logout"]'):
            break
        await asyncio.sleep(0.5)
    else:
        raise Exception("Login failed")

    log.info("Succesfully logged in")


async def read_date(page: Page) -> datetime:
    current_date_str = await page.text_content("#matrix_date_title")
    current_date = datetime.strptime(current_date_str.split(" ")[1], "%d-%m-%Y")

    return current_date


async def select_date(settings: Settings, args: Input, page: Page):
    date = args.date

    if date < datetime.today():
        raise ValueError("Requested date is in the past")

    current_date = await read_date(page)
    while current_date < date:
        await page.click('a.matrix-date-nav[data-offset="+1"]')
        await asyncio.sleep(0.5)
        current_date = await read_date(page)

    log.info(
        "Current date %s is equal to the desired date %s",
        current_date.strftime("%Y-%m-%d"),
        date.strftime("%Y-%m-%d"),
    )


async def select_slot(settings: Settings, args: Input, page: Page):
    times = ordered_times(args)
    await asyncio.sleep(0.5)
    for time in times:
        slots = await page.locator(f'tr[data-time="{time}"] td[type="free"]').all()

        if len(slots) == 0:
            log.info("No slots available at %s", time)
            continue

        for slot in slots[::-1]:
            await slot.click()

            row = page.locator('td.tblTitle:has-text("Baan")').locator("..")
            court = await row.locator("td").nth(1).text_content()

            if court.strip().lower().startswith("court 1 "):
                # Court 1 needs to be booked via the reception
                await page.click('a[tooltip="Sluiten"]')
                log.info("Skipping court 1 slot at %s", time)
            else:
                break

    log.info("Selected one of the %s slots available at %s", len(slots), time)


async def place_reservation(settings: Settings, args: Input, page: Page):
    await page.select_option('select[name="players[2]"]', value=OPPONENTS[args.opponent])
    await page.click('input#__make_submit[type="submit"]')

    if not settings.dry_run and not args.dry_run:
        await page.click('input#__make_submit2[type="submit"]')
        log.info("Succesfully placed the reservation with %s", args.opponent)
    else:
        log.info("Dry run, not placing the reservation")
        await asyncio.sleep(3)


async def run(settings: Settings, args: Input, page: Page):
    await login(settings, page)
    await select_date(settings, args, page)
    await select_slot(settings, args, page)
    await place_reservation(settings, args, page)

    log.info("Placed reservation successfully")


async def main() -> None:
    """main() is executed when the module is run"""
    settings = Settings()
    async with Actor as actor, async_playwright() as playwright:
        args = Input(**await actor.get_input() or {})
        browser = await playwright.chromium.launch(headless=settings.headless)
        device = playwright.devices[DEVICE]
        context = await browser.new_context(**device)
        page = await context.new_page()

        await run(settings=settings, args=args, page=page)

        await browser.close()
