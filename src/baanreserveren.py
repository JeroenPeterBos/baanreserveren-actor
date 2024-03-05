"""
This module defines the `main()` coroutine for the Apify Actor, executed from the `__main__.py` file.

Feel free to modify this file to suit your specific needs.

To build Apify Actors, utilize the Apify SDK toolkit, read more at the official documentation:
https://docs.apify.com/sdk/python
"""

import asyncio
import json
import logging
from itertools import zip_longest
from datetime import datetime, timedelta
from multiprocessing import Value

# Apify SDK - toolkit for building Apify Actors, read more at https://docs.apify.com/sdk/python
from apify import Actor
from icalendar import Calendar, Event, vText

import boto3

# We use playwright for scraping, read more at https://playwright.dev/python/docs/api/class-playwright
from playwright.async_api import Page, async_playwright
import pytz

# We use pydantic for parsing te input and loading the environment variables, read more at https://pydantic-docs.helpmanual.io/

from src.models import Input, Settings
from src.utils import to_snake_case


log = logging.getLogger(__name__)
print(log.name)

URL_LOGIN = "https://squtrecht.baanreserveren.nl/"
URL_RESERVATIONS = "https://squtrecht.baanreserveren.nl/user/future"
CALENDAR_BUCKET = "apify-squash-utrecht"
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
    if args.reservation_date is None:
        log.info("No reservation date specified")

        if args.reservation_default == "next_week":
            date = datetime.today() + timedelta(days=7)
        elif args.reservation_default == "today":
            date = datetime.today()

        log.info("Reservation default: %s. Defaulting to %s", args.reservation_default, date.strftime("%Y-%m-%d"))
    else:
        date = datetime.strptime(args.reservation_date, "%Y-%m-%d")

    if date.date() < datetime.today().date():
        raise ValueError("Requested date is in the past")

    if date.date().strftime("%Y-%m-%d") in args.reservation_skip:
        raise ValueError("Requested date is in the skip list")

    current_date = await read_date(page)
    while current_date.date() < date.date():
        await page.click('a.matrix-date-nav[data-offset="+1"]')
        await asyncio.sleep(0.5)
        current_date = await read_date(page)

    log.info(
        "Selected date %s is equal to the desired date %s",
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
                log.info("Selected one of the %s slots available at %s", len(slots), time)
                return True

    return False


async def place_reservation(settings: Settings, args: Input, page: Page):
    await page.select_option('select[name="players[2]"]', value=OPPONENTS[args.opponent])
    await page.click('input#__make_submit[type="submit"]')

    if not settings.dry_run and not args.dry_run:
        await page.click('input#__make_submit2[type="submit"]')
        log.info("Succesfully placed the reservation with %s", args.opponent)
    else:
        log.info("[DRY RUN] Not actually placing the reservation")
        await asyncio.sleep(1)

    return True


async def run_reserver(settings: Settings, args: Input, page: Page):
    await login(settings, page)
    await select_date(settings, args, page)
    success = await select_slot(settings, args, page)

    if not success:
        raise Exception("Failed to select a slot")

    success = await place_reservation(settings, args, page)

    if not success:
        raise Exception("Failed to place reservation")

    log.info("Placed reservation successfully")


async def get_future_reservations(page: Page) -> list[dict]:
    await page.goto(URL_RESERVATIONS)

    reservations_locator = page.locator(
        "//th[contains(text(), 'Reserveringen')]/ancestor::tbody/tr[@class='odd' or @class='even']"
    )

    log.info("Found %s reservations", await reservations_locator.count())
    headers = [
        to_snake_case(header)
        for header in await page.locator(
            "//th[contains(text(), 'Reserveringen')]/ancestor::tbody/tr[@class='tblTitle'][1]/td"
        ).all_inner_texts()
    ]

    reservations = []
    for reservation_index in range(await reservations_locator.count()):
        reservation = {
            header: value.strip()
            for header, value in zip(
                headers,
                await reservations_locator.nth(reservation_index).locator("td").all_inner_texts(),
            )
        }

        await reservations_locator.nth(reservation_index).locator("a").click()
        await page.wait_for_selector("//*[contains(text(), 'Speler 1')]")

        reservation["spelers"] = await page.locator("//div[@class='res-info-player-name']").all_inner_texts()
        reservations.append(reservation)

        await page.click("//input[@value='Terug']")

    return reservations


async def create_calendar(reservations: list[dict]):
    # Create a calendar
    cal = Calendar()

    # Add some properties to the calendar
    cal.add("prodid", "-//Jeroen Squash Utrecht//mxm.dk//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Squash Reserveringen")

    amsterdam_tz = pytz.timezone("Europe/Amsterdam")

    for reservation in reservations:
        event = Event()

        # Parse date and time
        date_str = reservation["datum"] + " " + reservation["begintijd"]
        start_datetime = amsterdam_tz.localize(datetime.strptime(date_str, "%d-%m-%Y %H:%M"))
        # Assuming the duration of each reservation is 1 hour
        end_datetime = start_datetime + timedelta(hours=1)

        # Format summary
        squash_emoji = "\U0001F3F8"
        summary = f"{squash_emoji} {reservation['baan']} {reservation['begintijd']}"

        # Set event properties
        event.add("summary", summary)
        event.add("dtstart", start_datetime)
        event.add("dtend", end_datetime)
        event.add("location", vText("Squash Utrecht"))

        # Generate a UID for each event, for example using the start datetime and court number
        uid = f"squash-{reservation['datum'].replace('-', '')}-{reservation['begintijd'].replace(':', '')}-{reservation['baan'].replace(' ', '')}@example.com"
        event.add("uid", uid)

        # Add the current timestamp as dtstamp
        event.add("dtstamp", datetime.now())

        # Add the event to the calendar
        cal.add_component(event)

    return cal


async def combine_with_old_reservations(reservations_key, future_reservations, player: str = None):
    previous_reservations = json.loads((await load_bytes_from_s3(reservations_key)).decode("utf-8"))
    today = datetime.now().date()
    previous_reservations = [
        reservation
        for reservation in previous_reservations
        if datetime.strptime(reservation["datum"], "%d-%m-%Y").date() < today
    ]

    if player is not None:
        future_reservations = [
            reservation
            for reservation in future_reservations
            if any(player in p.lower() for p in reservation["spelers"])
        ]

    reservations = previous_reservations + future_reservations
    log.info(
        "Combined %s previous and %s future reservations with a resulting %s reservations",
        len(previous_reservations),
        len(future_reservations),
        len(reservations),
    )

    return reservations


async def upload_bytes_to_s3(key, bytes, content_type):
    s3_client = boto3.client("s3")
    try:
        s3_client.put_object(
            Bucket=CALENDAR_BUCKET,
            Key=key,
            Body=bytes,
            ContentType=content_type,
        )
        log.info("File %s of type %s uploaded successfully.", key, content_type)
    except Exception as e:
        log.error(f"Upload failed: {e}")
        raise e


async def load_bytes_from_s3(key):
    s3_client = boto3.client("s3")
    try:
        response = s3_client.get_object(
            Bucket=CALENDAR_BUCKET,
            Key=key,
        )
        file_bytes = response["Body"].read()
        log.info("File %s downloaded successfully.", key)
        return file_bytes
    except Exception as e:
        log.error(f"Download failed: {e}")
        raise e


async def run_calendar_updater(settings: Settings, args: Input, page: Page):
    await login(settings, page)
    future_reservations = await get_future_reservations(page)

    for player in (None, "vera"):
        calendar_key = "calendar/reservations.ics" if player is None else f"calendar/reservations-{player}.ics"
        reservations_key = "calendar/reservations.json" if player is None else f"calendar/reservations-{player}.json"

        reservations = await combine_with_old_reservations(reservations_key, future_reservations, player=player)

        json_bytes = str.encode(json.dumps(reservations, indent=4), "utf-8")
        await upload_bytes_to_s3(reservations_key, json_bytes, content_type="application/json")

        calendar = await create_calendar(reservations=reservations)
        await upload_bytes_to_s3(calendar_key, calendar.to_ical(), content_type="text/calendar")


async def main() -> None:
    """main() is executed when the module is run"""
    settings = Settings()
    async with Actor as actor, async_playwright() as playwright:
        args = Input(**await actor.get_input() or {})
        browser = await playwright.chromium.launch(headless=settings.headless)
        device = playwright.devices[DEVICE]
        context = await browser.new_context(**device)
        page = await context.new_page()

        if args.update_calendar:
            await run_calendar_updater(settings=settings, args=args, page=page)
        else:
            await run_reserver(settings=settings, args=args, page=page)

        await browser.close()
