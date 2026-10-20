"""Opening-time booking window.

Member courts open at a clock time in Europe/Amsterdam. The actor can log in
before that time, sit on the target date, and then poll until a preferred
slot is free.

Dates are computed in Europe/Amsterdam. The container clock is UTC, so
``datetime.today()`` books the wrong day when Amsterdam has already crossed
midnight and UTC has not.

Preference policy: every poll walks the configured times in order and books
the first time that has a bookable court. A lower preference is taken on the
poll that finds it, including the first poll after opening. Waiting for a
better time to appear later would give the free court to someone else.
"""

from __future__ import annotations

import logging
import math
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

AMSTERDAM = ZoneInfo("Europe/Amsterdam")

DEFAULT_ACTOR_TIMEOUT_SECONDS = 3 * 60
TIMEOUT_BUFFER_SECONDS = 120
DEFAULT_RETRY_INTERVAL_SECONDS = 2
MAX_RETRY_FOR_SECONDS = 900
MIN_RETRY_INTERVAL_SECONDS = 1
MAX_RETRY_INTERVAL_SECONDS = 60
MIN_TIMEOUT_SECONDS = 30
MAX_TIMEOUT_SECONDS = 3600

_CLOCK = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")

log = logging.getLogger(__name__)


def amsterdam_now() -> datetime:
    return datetime.now(AMSTERDAM)


def normalize_clock(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("wait_until must be a Europe/Amsterdam time like 06:30:00")
    match = _CLOCK.fullmatch(value.strip())
    if match is None:
        raise ValueError("wait_until must be a Europe/Amsterdam time like 06:30:00")
    second = match.group(3) or "00"
    return ":".join((match.group(1), match.group(2), second))


def opening_datetime(wait_until: str, now: datetime) -> datetime:
    """Today's opening instant in Europe/Amsterdam, even if ``now`` is UTC."""
    clock = normalize_clock(wait_until)
    hour, minute, second = (int(part) for part in clock.split(":"))
    local = now.astimezone(AMSTERDAM)
    return local.replace(hour=hour, minute=minute, second=second, microsecond=0)


def seconds_until_opening(wait_until: str | None, now: datetime) -> float:
    if not wait_until:
        return 0.0
    remaining = (opening_datetime(wait_until, now) - now.astimezone(AMSTERDAM)).total_seconds()
    return max(0.0, remaining)


def resolve_booking_date(
    *,
    reservation_date: str | None,
    reservation_default: str,
    now: datetime,
) -> datetime:
    """Naive midnight datetime of the court day, on the Amsterdam calendar.

    An explicit ``reservation_date`` is that calendar day. ``next_week`` and
    ``today`` are seven days after, or equal to, today's date in Amsterdam.
    """
    today = now.astimezone(AMSTERDAM).date()
    if reservation_date:
        try:
            booked = datetime.strptime(reservation_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("reservation_date must be yyyy-mm-dd") from exc
    elif reservation_default == "today":
        booked = today
    elif reservation_default == "next_week":
        booked = today + timedelta(days=7)
    else:
        raise ValueError(f"Unknown reservation_default '{reservation_default}'")
    return datetime(booked.year, booked.month, booked.day)


def automatic_timeout_seconds(*, seconds_until_opening: float, retry_for_seconds: int | None) -> int:
    """Timeout that fits the wait and the poll, or the historical 3 minutes."""
    waiting = max(0.0, seconds_until_opening)
    polling = retry_for_seconds or 0
    if waiting == 0 and polling == 0:
        return DEFAULT_ACTOR_TIMEOUT_SECONDS
    needed = math.ceil(waiting + polling + TIMEOUT_BUFFER_SECONDS)
    return max(DEFAULT_ACTOR_TIMEOUT_SECONDS, needed)


def resolve_actor_timeout_seconds(
    *,
    timeout_seconds: int | None,
    seconds_until_opening: float,
    retry_for_seconds: int | None,
) -> int:
    """Use an explicit timeout when given, otherwise extend the 3 minute default."""
    if timeout_seconds is None:
        return automatic_timeout_seconds(
            seconds_until_opening=seconds_until_opening,
            retry_for_seconds=retry_for_seconds,
        )
    return timeout_seconds


def confirms_booking(*, settings_dry_run: bool, input_dry_run: bool) -> bool:
    """Confirm a reservation only when the environment and the input both allow it."""
    return not settings_dry_run and not input_dry_run


def choose_preferred_time(preferences: list[str], available: list[str]) -> str | None:
    """Best free time on this poll.

    The first preference that is free wins, including on the first poll after
    opening. A free 21:15 is booked when 20:30 and 19:45 are not free. Holding
    out for a better time would lose the court.
    """
    available_set = set(available)
    for time in preferences:
        if time in available_set:
            return time
    return None


def calendar_day_selectors(target: date) -> list[str]:
    """Links in the club calendar. Month and day are unpadded, with a padded fallback."""
    unpadded = "_".join(str(part) for part in (target.year, target.month, target.day))
    padded = "_".join(
        (
            str(target.year),
            str(target.month).zfill(2),
            str(target.day).zfill(2),
        )
    )
    selectors = [f"#cal_{unpadded} > a.cal-link"]
    if padded != unpadded:
        selectors.append(f"#cal_{padded} > a.cal-link")
    return selectors


def coerce_optional_int(value, *, name: str, minimum: int, maximum: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "":
            return None
        if not stripped.lstrip("-").isdigit():
            raise ValueError(f"{name} must be an integer")
        value = int(stripped)
    if not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


async def poll_for_slot(
    *,
    attempt,
    refresh,
    retry_for_seconds: int | None,
    retry_interval_seconds: float,
    sleep,
    now,
) -> bool:
    """Try ``attempt`` once, or until a slot is selected or the retry window ends.

    The first attempt does not sleep and does not refresh. The caller has
    already opened the page, and has refreshed it when it waited for opening.
    Later attempts sleep, then refresh, then check again. Sleep is capped so
    a new check does not start after the deadline.
    """
    if retry_interval_seconds <= 0:
        raise ValueError("retry_interval_seconds must be greater than zero")

    deadline = None
    if retry_for_seconds:
        deadline = now() + timedelta(seconds=retry_for_seconds)

    saw_clean_miss = False
    last_error: Exception | None = None
    attempt_number = 0

    while True:
        if deadline is not None and now() >= deadline:
            break

        if attempt_number:
            remaining = (deadline - now()).total_seconds()
            if remaining <= 0:
                break
            pause = min(float(retry_interval_seconds), remaining)
            log.info("Retrying in %s seconds", pause)
            await sleep(pause)
            if now() >= deadline:
                break
            try:
                await refresh()
            except Exception as exc:
                last_error = exc
                log.warning("Could not refresh the court matrix: %s", exc)

        attempt_number += 1
        try:
            if await attempt():
                return True
            saw_clean_miss = True
            last_error = None
            log.info("No free slot in preference order (attempt %s)", attempt_number)
        except Exception as exc:
            last_error = exc
            log.warning("Slot check %s failed: %s", attempt_number, exc)

        if deadline is None:
            break

    if last_error is not None and not saw_clean_miss:
        raise last_error
    return False
