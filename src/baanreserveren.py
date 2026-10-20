"""
This module defines the `main()` coroutine for the Apify Actor, executed from the `__main__.py` file.

Feel free to modify this file to suit your specific needs.

To build Apify Actors, utilize the Apify SDK toolkit, read more at the official documentation:
https://docs.apify.com/sdk/python
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timedelta
from itertools import zip_longest
from multiprocessing import Value

import boto3
import pytz

# Apify SDK - toolkit for building Apify Actors, read more at https://docs.apify.com/sdk/python
from apify import Actor
from icalendar import Calendar, Event, Timezone, TimezoneDaylight, TimezoneStandard, vCalAddress, vText

# We use playwright for scraping, read more at https://playwright.dev/python/docs/api/class-playwright
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from src.booking_window import (
    DEFAULT_RETRY_INTERVAL_SECONDS,
    amsterdam_now,
    automatic_timeout_seconds,
    calendar_day_selectors,
    confirms_booking,
    poll_for_slot,
    resolve_actor_timeout_seconds,
    resolve_booking_date,
    seconds_until_opening,
)
from src.models import Input, Settings
from src.placeholders import generate_placeholders
from src.reservations import extract_player_names, parse_reservation_page
from src.utils import to_snake_case

# We use pydantic for parsing te input and loading the environment variables, read more at https://pydantic-docs.helpmanual.io/


log = logging.getLogger(__name__)
print(log.name)

URL_LOGIN = "https://squtrecht.baanreserveren.nl/"
URL_RESERVATIONS = "https://squtrecht.baanreserveren.nl/user/future"
CALENDAR_BUCKET = "apify-squash-utrecht"
DEVICE = "Desktop Chrome"
OPPONENTS = {
    "vera": "1409256",
    "koen": "1340920",
    "jeroen": "1148695",
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


async def select_date(settings: Settings, args: Input, page: Page, target: datetime | None = None) -> datetime:
    if target is None:
        now = amsterdam_now()
        if args.reservation_date is None:
            log.info("No reservation date specified")
            target = resolve_booking_date(
                reservation_date=None,
                reservation_default=args.reservation_default,
                now=now,
            )
            log.info(
                "Reservation default: %s. Defaulting to %s",
                args.reservation_default,
                target.strftime("%Y-%m-%d"),
            )
        else:
            target = resolve_booking_date(
                reservation_date=args.reservation_date,
                reservation_default=args.reservation_default,
                now=now,
            )

        if target.date() < now.date():
            raise ValueError("Requested date is in the past")

        if target.date().strftime("%Y-%m-%d") in args.reservation_skip:
            raise ValueError("Requested date is in the skip list")

    await open_date(page, target)
    current_date = await read_date(page)
    log.info(
        "Selected date %s is equal to the desired date %s",
        current_date.strftime("%Y-%m-%d"),
        target.strftime("%Y-%m-%d"),
    )
    return target


async def open_date(page: Page, target: datetime) -> None:
    """Land on ``target``. A calendar link is one request; day arrows are the fallback."""
    if await click_calendar_day(page, target):
        if await wait_until_on_date(page, target, timeout=3):
            return
        log.info("Calendar link did not land on %s, stepping day by day", target.date().isoformat())

    for _ in range(40):
        current_date = await read_date(page)
        if current_date.date() == target.date():
            return
        offset = "+1" if current_date.date() < target.date() else "-1"
        await page.click(f'a.matrix-date-nav[data-offset="{offset}"]')
        await asyncio.sleep(0.5)

    raise RuntimeError(f"Could not open {target.date().isoformat()}")


async def click_calendar_day(page: Page, target: datetime) -> bool:
    for selector in calendar_day_selectors(target.date()):
        link = page.locator(selector).locator("visible=true")
        try:
            if await link.count() == 0:
                continue
            await link.first.click(timeout=3000)
            log.info("Opened %s from the calendar", target.date().isoformat())
            return True
        except Exception:
            log.info("Calendar link %s was not clickable", selector)
    return False


async def wait_until_on_date(page: Page, target: datetime, timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        current_date = await read_date_if_present(page)
        if current_date is not None and current_date.date() == target.date():
            return True
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.2)


async def read_date_if_present(page: Page) -> datetime | None:
    if await page.locator("#matrix_date_title").count() == 0:
        return None
    return await read_date(page)


async def session_alive(page: Page) -> bool:
    try:
        return await page.locator('a[href="/auth/logout"]').count() > 0
    except Exception:
        return False


async def ensure_ready(settings: Settings, args: Input, page: Page, target: datetime) -> None:
    """Log in again and reopen the date when the session or the matrix has gone away."""
    logged_in = await session_alive(page)
    on_date = False
    if logged_in:
        current_date = await read_date_if_present(page)
        on_date = current_date is not None and current_date.date() == target.date()
    if logged_in and on_date:
        return

    if not logged_in:
        log.warning("Session expired, logging in again")
        await login(settings, page)
    if not on_date:
        log.info("Target date %s is not on screen, opening it again", target.date().isoformat())
        await select_date(settings, args, page, target=target)


async def select_slot(settings: Settings, args: Input, page: Page):
    """Book the best court that is free right now.

    Times stay in preference order (20:30, then 19:45, then 21:15 when that
    is the input). Court 1 is skipped because reception has to book it. The
    first remaining free court is booked on this poll. A lower preference is
    not deferred to a later poll.
    """
    times = ordered_times(args)
    for time in times:
        slots = await page.locator(f'tr[data-time="{time}"] td[type="free"]').all()

        if len(slots) == 0:
            log.info("No slots available at %s", time)
            continue

        if args.leden_only:
            # For members only we want the hall in the back to have preference
            slots = slots[::-1]

        for slot in slots:
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

    if confirms_booking(settings_dry_run=settings.dry_run, input_dry_run=args.dry_run):
        await page.click('input#__make_submit2[type="submit"]')
        log.info("Succesfully placed the reservation with %s", args.opponent)
    else:
        log.info("[DRY RUN] Not actually placing the reservation")
        await asyncio.sleep(1)

    return True


async def wait_until_opening(settings: Settings, args: Input, page: Page, target: datetime) -> None:
    """Sleep until the Amsterdam opening time. Do not reload the court matrix."""
    remaining = seconds_until_opening(args.wait_until, amsterdam_now())
    if remaining <= 0:
        log.info(
            "Opening time %s Europe/Amsterdam has already passed, checking slots now",
            args.wait_until,
        )
        return

    log.info(
        "On %s and logged in. Waiting %.0f seconds until %s Europe/Amsterdam before checking slots",
        target.date().isoformat(),
        remaining,
        args.wait_until,
    )
    while True:
        remaining = seconds_until_opening(args.wait_until, amsterdam_now())
        if remaining <= 0:
            break
        await asyncio.sleep(min(remaining, 30))
        if not await session_alive(page):
            log.warning("Session expired while waiting for opening, logging in again")
            await login(settings, page)
            await select_date(settings, args, page, target=target)

    log.info("Opening time %s Europe/Amsterdam reached", args.wait_until)


def _is_club_xhr(response) -> bool:
    return response.request.resource_type in ("xhr", "fetch") and "baanreserveren.nl" in response.url


async def refresh_slots(settings: Settings, args: Input, page: Page, target: datetime) -> None:
    """Reload the matrix for the date we are already on.

    A same-day calendar click is not used here. The date title would already
    match, so the actor could read the stale grid from before opening.
    ``reloadMatrix`` counts only when a club request returns. Otherwise the
    actor steps one day away and back, which is the navigation that already
    updates the grid.
    """
    if await _invoke_reload_matrix(page):
        log.info("Refreshed the court matrix")
    else:
        log.info("Refreshed slots by stepping off the date and back")
        await page.click('a.matrix-date-nav[data-offset="+1"]')
        await asyncio.sleep(0.5)
        await page.click('a.matrix-date-nav[data-offset="-1"]')
        if not await wait_until_on_date(page, target, timeout=5):
            log.info("Date did not return to %s after refresh", target.date().isoformat())

    await _wait_for_matrix_idle(page)
    await ensure_ready(settings, args, page, target)


async def _invoke_reload_matrix(page: Page) -> bool:
    try:
        available = await page.evaluate("() => typeof window.reloadMatrix === 'function' && !window.reloadMatrix.fake")
    except Exception:
        return False
    if not available:
        return False

    try:
        async with page.expect_response(_is_club_xhr, timeout=4000):
            await page.evaluate("() => { window.reloadMatrix(); }")
    except PlaywrightTimeoutError:
        log.info("reloadMatrix did not return a court request, refreshing another way")
        return False
    except Exception as exc:
        log.info("reloadMatrix failed (%s), refreshing another way", exc)
        return False
    return True


async def _wait_for_matrix_idle(page: Page) -> None:
    """Wait until a matrix reload finishes.

    networkidle is not used: the club page loads analytics that keep the
    network busy, and an 8 second idle wait would miss the opening.
    """
    loading = page.locator("#ajax_loading")
    try:
        if await loading.count() and await loading.is_visible():
            await loading.wait_for(state="hidden", timeout=5000)
            return
    except PlaywrightTimeoutError:
        log.info("Loading indicator stayed visible, checking the slots that are on the page")
        return
    await asyncio.sleep(0.3)


async def run_reserver(settings: Settings, args: Input, page: Page):
    await login(settings, page)
    target = await select_date(settings, args, page)

    if args.wait_until or args.retry_for_seconds:
        log.info(
            "Opening watch: wait_until=%s retry_for_seconds=%s retry_interval_seconds=%s",
            args.wait_until,
            args.retry_for_seconds,
            args.retry_interval_seconds or DEFAULT_RETRY_INTERVAL_SECONDS,
        )

    if args.wait_until:
        await wait_until_opening(settings, args, page, target)
        await refresh_slots(settings, args, page, target)
    else:
        # The original single check waited for the matrix to finish rendering.
        await asyncio.sleep(0.5)

    interval = args.retry_interval_seconds or DEFAULT_RETRY_INTERVAL_SECONDS

    async def attempt() -> bool:
        await ensure_ready(settings, args, page, target)
        log.info("Checking slots at %s Europe/Amsterdam", amsterdam_now().strftime("%H:%M:%S"))
        return await select_slot(settings, args, page)

    async def refresh() -> None:
        await refresh_slots(settings, args, page, target)

    success = await poll_for_slot(
        attempt=attempt,
        refresh=refresh,
        retry_for_seconds=args.retry_for_seconds or None,
        retry_interval_seconds=interval,
        sleep=asyncio.sleep,
        now=amsterdam_now,
    )

    if not success:
        raise Exception("Failed to select a slot")

    success = await place_reservation(settings, args, page)

    if not success:
        raise Exception("Failed to place reservation")

    log.info("Placed reservation successfully")


async def get_future_reservations(page: Page) -> list[dict]:
    await page.goto(URL_RESERVATIONS)
    await _wait_for_reservations_markup(page)

    # Parse the browser DOM. page.content() is the live document, so a thead
    # title row and body rows without odd/even classes are both visible here.
    parsed = parse_reservation_page(await page.content(), page_url=page.url)
    columns = [to_snake_case(header) for header in parsed.columns if to_snake_case(header)]
    log.info("Found %s reservations", len(parsed.reservations))
    log.info("Parsed reservation columns: %s", columns)
    if parsed.title:
        log.info("Reservations table title: %s", parsed.title)

    for reservation, match in zip(parsed.reservations, parsed.matches):
        reservation["spelers"] = await _read_reservation_players(page, match)

    return parsed.reservations


async def _wait_for_reservations_markup(page: Page) -> None:
    table_selector = "table tr.tblTitle, table thead th"
    if await page.locator("#login-form").count() and await page.locator(table_selector).count() == 0:
        return
    try:
        await page.wait_for_selector(table_selector, timeout=10000)
    except PlaywrightTimeoutError:
        log.info("Reservations table header did not appear on %s", page.url)


async def _read_reservation_players(page: Page, match: dict) -> list[str]:
    label = f"{match['datum']} {match['begintijd']} {match['baan']}"
    row = await _find_reservation_row(page, match)
    if row is None:
        raise RuntimeError(
            f"Parsed reservation {label} is not on the reservations page. "
            "BaanReserveren likely changed their layout."
        )

    link = await _reservation_detail_link(row)
    if link is None:
        raise RuntimeError(
            f"Reservation {label} has no detail link to read players from. "
            "BaanReserveren likely changed their layout."
        )

    await link.click()
    try:
        await page.get_by_text(re.compile(r"Speler\s*1", re.I)).first.wait_for(state="visible", timeout=10000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            f"Opened reservation {label} but did not find 'Speler 1'. "
            "BaanReserveren likely changed the reservation detail layout."
        ) from exc

    lightbox = page.locator("#overlays .lightbox")
    detail_html = await lightbox.last.inner_html() if await lightbox.count() else await page.content()
    names = extract_player_names(detail_html)
    if not names:
        raise RuntimeError(
            f"Opened reservation {label} but could not read player names. "
            "BaanReserveren likely changed the reservation detail layout."
        )

    await row.evaluate("el => el.setAttribute('data-reservation-read', '1')")
    await _close_reservation_detail(page)
    return names


async def _find_reservation_row(page: Page, match: dict):
    """Find the overview row by exact cell text, so Court 1 does not match Court 10."""
    handle = await page.evaluate_handle(
        """(target) => {
            const text = (el) => (el.textContent || '').replace(/\\s+/g, ' ').trim();
            const visible = (el) => {
                for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
                    const style = getComputedStyle(node);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                }
                return true;
            };
            for (const row of document.querySelectorAll('tr:not([data-reservation-read])')) {
                if (!visible(row)) continue;
                const cells = [...row.children]
                    .filter((el) => el.tagName === 'TD' || el.tagName === 'TH')
                    .map(text);
                if (
                    cells.includes(target.datum)
                    && cells.includes(target.begintijd)
                    && cells.includes(target.baan)
                ) {
                    return row;
                }
            }
            return null;
        }""",
        match,
    )
    element = handle.as_element()
    if element is None:
        await handle.dispose()
    return element


async def _reservation_detail_link(row):
    """Prefer the details link over an annuleren/cancel control in the same row."""
    links = await row.query_selector_all("a")
    for link in links:
        href = (await link.get_attribute("href") or "").lower()
        text = (await link.inner_text() or "").lower()
        if "cancel" in href or "annul" in href or "annul" in text:
            continue
        return link
    return links[0] if links else None


async def _close_reservation_detail(page: Page) -> None:
    for selector in (
        "input[value='Terug']",
        "button:has-text('Terug')",
        "a:has-text('Terug')",
        "a.top-close",
        "a[tooltip='Sluiten']",
    ):
        control = page.locator(selector)
        if await control.count():
            await control.first.click()
            break
    else:
        await page.keyboard.press("Escape")

    lightbox = page.locator("#overlays .lightbox")
    if await lightbox.count():
        try:
            await lightbox.first.wait_for(state="hidden", timeout=5000)
        except PlaywrightTimeoutError:
            log.warning("Reservation detail overlay stayed open")


async def create_calendar(reservations: list[dict], player: str):
    # Create a calendar
    cal = Calendar()

    # Add some properties to the calendar
    cal.add("prodid", "-//Jeroen Squash Utrecht//mxm.dk//")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", "Squash Reserveringen")

    # Create a VTIMEZONE component for Europe/Amsterdam
    tz = Timezone()
    tz.add("tzid", "Europe/Amsterdam")
    tz.add("x-lic-location", "Europe/Amsterdam")

    # Standard Time Component (assuming standard time here, adjust as necessary)
    std = TimezoneStandard()
    std.add("dtstart", datetime(1970, 10, 25, 3, 0, 0))
    std.add("tzoffsetfrom", timedelta(hours=2))
    std.add("tzoffsetto", timedelta(hours=1))
    std.add("tzname", "CET")
    tz.add_component(std)

    # Daylight Saving Time Component (if applicable, adjust as necessary)
    dst = TimezoneDaylight()
    dst.add("dtstart", datetime(1970, 3, 29, 2, 0, 0))
    dst.add("tzoffsetfrom", timedelta(hours=1))
    dst.add("tzoffsetto", timedelta(hours=2))
    dst.add("tzname", "CEST")
    tz.add_component(dst)

    # Add the VTIMEZONE component to your calendar before adding events
    cal.add_component(tz)

    amsterdam_tz = pytz.timezone("Europe/Amsterdam")

    for reservation in reservations:
        event = Event()

        # Parse date and time
        date_str = reservation["datum"] + " " + reservation["begintijd"]
        start_datetime = amsterdam_tz.localize(datetime.strptime(date_str, "%d-%m-%Y %H:%M"))
        # Assuming the duration of each reservation is 1 hour
        end_datetime = start_datetime + timedelta(minutes=45)

        # Format summary
        summary = f"{reservation['baan']} 🏆⚫️"

        # Set event properties
        event.add("summary", summary)
        event.add("dtstart", start_datetime)
        event.add("dtend", end_datetime)
        event.add("location", "Squash Utrecht, Taagdreef 130, 3561 VL Utrecht")
        event.add("url", f"https://squtrecht.baanreserveren.nl/user/{OPPONENTS[player]}/future")

        if "spelers" in reservation:
            event.add("description", "Spelers: " + ", ".join(reservation["spelers"]))
            for speler in reservation["spelers"]:
                # Adding attendees
                attendee = vCalAddress("MAILTO:jeroenbosleusden@mail.com")
                attendee.params["cn"] = vText(speler)
                attendee.params["ROLE"] = vText("REQ-PARTICIPANT")
                event.add("attendee", attendee, encode=0)

        # Generate a UID for each event, for example using the start datetime and court number
        uid = f"squash-{reservation['datum'].replace('-', '')}-{reservation['begintijd'].replace(':', '')}-{reservation['baan'].replace(' ', '')}@example.com"
        event.add("uid", uid)

        # Add the current timestamp as dtstamp
        event.add("dtstamp", datetime.now())

        # Setting status and transparency
        event.add("status", "CONFIRMED")
        event.add("transp", "OPAQUE")

        # Increment number to invalidate old values
        event.add("sequence", int(datetime.now().strftime("%Y%m%d")))

        # Add the event to the calendar
        cal.add_component(event)
        event_type = "placeholder" if "placeholder" in reservation["baan"].lower() else "reservation"
        log.info("Added %s for %s to calendar", event_type, start_datetime.strftime("%Y-%m-%d %H:%M"))

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

        def _upload():
            s3_client.put_object(
                Bucket=CALENDAR_BUCKET,
                Key=key,
                Body=bytes,
                ContentType=content_type,
            )

        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _upload)

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


async def generate_upload_files(
    future_reservations: list[dict],
    player: str,
    placeholder_weeks: int,
    placeholder_weekdays: list[str] | None = None,
    placeholder_time: str = "20:30",
):
    calendar_key = "calendar/reservations.ics" if player is None else f"calendar/reservations-{player}.ics"
    reservations_key = "calendar/reservations.json" if player is None else f"calendar/reservations-{player}.json"
    reservations_placeholders_key = (
        "calendar/reservations_placeholders.json"
        if player is None
        else f"calendar/reservations_placeholders-{player}.json"
    )

    latest_reservation = max(datetime.strptime(reservation["datum"], "%d-%m-%Y") for reservation in future_reservations)
    placeholders = generate_placeholders(
        start=max(latest_reservation, datetime.now() + timedelta(days=7)),
        placeholder_weeks=placeholder_weeks,
        weekdays=placeholder_weekdays,
        placeholder_time=placeholder_time,
    )
    reservations = await combine_with_old_reservations(reservations_key, future_reservations, player=player)

    json_bytes = str.encode(json.dumps(reservations, indent=4), "utf-8")
    upload_reservations = upload_bytes_to_s3(reservations_key, json_bytes, content_type="application/json")

    json_bytes = str.encode(json.dumps(reservations + placeholders, indent=4), "utf-8")
    upload_reservations_placeholders = upload_bytes_to_s3(
        reservations_placeholders_key, json_bytes, content_type="application/json"
    )

    calendar = await create_calendar(reservations=reservations + placeholders, player=player or "jeroen")
    upload_calendar = upload_bytes_to_s3(calendar_key, calendar.to_ical(), content_type="text/calendar")

    await asyncio.gather(
        upload_reservations,
        upload_reservations_placeholders,
        upload_calendar,
    )


async def run_calendar_updater(settings: Settings, args: Input, page: Page):
    await login(settings, page)
    future_reservations = await get_future_reservations(page)

    await asyncio.gather(
        *[
            generate_upload_files(
                future_reservations=future_reservations,
                player=player,
                placeholder_weeks=placeholder_weeks,
                placeholder_weekdays=args.placeholder_weekdays,
                placeholder_time=args.placeholder_time,
            )
            for player, placeholder_weeks in (
                (None, 8),
                ("jeroen", 8),
                ("vera", 8),
            )
        ]
    )


async def main() -> None:
    """main() is executed when the module is run"""
    settings = Settings()
    async with Actor as actor:
        args = Input(**await actor.get_input() or {})
        waiting = seconds_until_opening(args.wait_until, amsterdam_now())
        automatic = automatic_timeout_seconds(
            seconds_until_opening=waiting,
            retry_for_seconds=args.retry_for_seconds,
        )
        timeout_seconds = resolve_actor_timeout_seconds(
            timeout_seconds=args.timeout_seconds,
            seconds_until_opening=waiting,
            retry_for_seconds=args.retry_for_seconds,
        )
        if args.timeout_seconds is not None and args.timeout_seconds < automatic:
            log.warning(
                "timeout_seconds=%s is shorter than the %s seconds the wait and retry loop needs",
                args.timeout_seconds,
                automatic,
            )
        log.info("Actor timeout is %s seconds", timeout_seconds)
        try:
            async with asyncio.timeout(timeout_seconds):
                await _run_with_browser(settings, args)
        except TimeoutError:
            log.error("Actor exceeded its %s second timeout", timeout_seconds)
            raise


async def _run_with_browser(settings: Settings, args: Input) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.headless)
        device = playwright.devices[DEVICE]
        context = await browser.new_context(**device)
        page = await context.new_page()

        if args.update_calendar:
            await run_calendar_updater(settings=settings, args=args, page=page)
        else:
            await run_reserver(settings=settings, args=args, page=page)

        await browser.close()
