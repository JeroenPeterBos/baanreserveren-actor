import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from src.baanreserveren import ordered_times
from src.booking_window import (
    DEFAULT_ACTOR_TIMEOUT_SECONDS,
    TIMEOUT_BUFFER_SECONDS,
    automatic_timeout_seconds,
    calendar_day_selectors,
    choose_preferred_time,
    confirms_booking,
    normalize_clock,
    opening_datetime,
    poll_for_slot,
    resolve_actor_timeout_seconds,
    resolve_booking_date,
    seconds_until_opening,
)
from src.models import Input

ROOT = Path(__file__).resolve().parents[1]
UTC = ZoneInfo("UTC")
AMSTERDAM = ZoneInfo("Europe/Amsterdam")
PREFERENCES = ["20:30", "19:45", "21:15"]


def test_next_week_on_the_scheduled_morning_is_seven_days_ahead():
    # 20 Oct 2026 04:30 UTC is 06:30 CEST, the run that targeted 27 Oct.
    now = datetime(2026, 10, 20, 4, 30, tzinfo=UTC)
    booked = resolve_booking_date(reservation_date=None, reservation_default="next_week", now=now)
    assert booked.date().isoformat() == "2026-10-27"


def test_next_week_uses_the_amsterdam_date_after_utc_midnight():
    # 20 Oct 2026 22:30 UTC is already 21 Oct 2026 00:30 in Amsterdam.
    now = datetime(2026, 10, 20, 22, 30, tzinfo=UTC)
    booked = resolve_booking_date(reservation_date=None, reservation_default="next_week", now=now)
    assert booked.date().isoformat() == "2026-10-28"


def test_today_uses_the_amsterdam_date():
    now = datetime(2026, 10, 20, 22, 30, tzinfo=UTC)
    booked = resolve_booking_date(reservation_date=None, reservation_default="today", now=now)
    assert booked.date().isoformat() == "2026-10-21"


def test_explicit_reservation_date_is_that_calendar_day():
    now = datetime(2026, 10, 20, 22, 30, tzinfo=UTC)
    booked = resolve_booking_date(reservation_date="2026-11-03", reservation_default="next_week", now=now)
    assert booked.date().isoformat() == "2026-11-03"


def test_wait_until_is_an_amsterdam_clock_time():
    now = datetime(2026, 10, 20, 4, 27, tzinfo=UTC)
    opening = opening_datetime("06:30", now)
    assert opening.tzinfo == AMSTERDAM
    assert opening.hour == 6 and opening.minute == 30 and opening.second == 0
    assert seconds_until_opening("06:30:00", now) == 3 * 60


def test_wait_until_in_the_past_does_not_wait():
    now = datetime(2026, 10, 20, 5, 0, tzinfo=UTC)  # 07:00 CEST
    assert seconds_until_opening("06:30:00", now) == 0
    assert seconds_until_opening(None, now) == 0


def test_normalize_clock_accepts_hours_and_seconds():
    assert normalize_clock("06:30") == "06:30:00"
    assert normalize_clock("06:30:05") == "06:30:05"


def test_timeout_stays_three_minutes_without_the_new_inputs():
    assert (
        resolve_actor_timeout_seconds(
            timeout_seconds=None,
            seconds_until_opening=0,
            retry_for_seconds=None,
        )
        == DEFAULT_ACTOR_TIMEOUT_SECONDS
        == 180
    )


def test_timeout_grows_to_fit_the_opening_wait_and_the_poll():
    # Process starts about 2.5 minutes before 06:30, then polls for 180 seconds.
    timeout = resolve_actor_timeout_seconds(
        timeout_seconds=None,
        seconds_until_opening=160,
        retry_for_seconds=180,
    )
    assert timeout == 160 + 180 + TIMEOUT_BUFFER_SECONDS
    assert timeout < 600


def test_explicit_timeout_is_used_as_given():
    assert (
        resolve_actor_timeout_seconds(
            timeout_seconds=240,
            seconds_until_opening=160,
            retry_for_seconds=180,
        )
        == 240
    )
    assert automatic_timeout_seconds(seconds_until_opening=160, retry_for_seconds=180) == 460


def test_lower_preference_is_taken_as_soon_as_it_is_free():
    # 20:30 and 19:45 are gone on this poll. Book 21:15 now.
    assert choose_preferred_time(PREFERENCES, ["21:15"]) == "21:15"
    assert choose_preferred_time(PREFERENCES, ["19:45", "21:15"]) == "19:45"
    assert choose_preferred_time(PREFERENCES, ["20:30", "21:15"]) == "20:30"
    assert choose_preferred_time(PREFERENCES, []) is None


def test_member_times_keep_input_order():
    args = Input(times=PREFERENCES, leden_only=True)
    assert ordered_times(args) == PREFERENCES


def test_dry_run_never_confirms():
    assert confirms_booking(settings_dry_run=False, input_dry_run=False) is True
    assert confirms_booking(settings_dry_run=True, input_dry_run=False) is False
    assert confirms_booking(settings_dry_run=False, input_dry_run=True) is False
    assert confirms_booking(settings_dry_run=True, input_dry_run=True) is False


def test_absent_opening_inputs_stay_absent():
    args = Input()
    assert args.wait_until is None
    assert args.retry_for_seconds is None
    assert args.retry_interval_seconds is None
    assert args.timeout_seconds is None
    assert args.dry_run is True


def test_opening_inputs_are_normalized():
    args = Input(
        wait_until="06:30",
        retry_for_seconds="180",
        retry_interval_seconds=3,
        timeout_seconds=600,
    )
    assert args.wait_until == "06:30:00"
    assert args.retry_for_seconds == 180
    assert args.retry_interval_seconds == 3
    assert args.timeout_seconds == 600


@pytest.mark.parametrize(
    "payload",
    [
        {"wait_until": "6:30"},
        {"wait_until": "25:00"},
        {"retry_for_seconds": -1},
        {"retry_interval_seconds": 0},
        {"timeout_seconds": 10},
    ],
)
def test_opening_inputs_reject_bad_values(payload):
    with pytest.raises(ValidationError):
        Input(**payload)


def test_empty_opening_strings_mean_unset():
    args = Input(wait_until="", retry_for_seconds="", retry_interval_seconds="", timeout_seconds="")
    assert args.wait_until is None
    assert args.retry_for_seconds is None
    assert args.retry_interval_seconds is None
    assert args.timeout_seconds is None


def test_calendar_selectors_cover_unpadded_and_padded_days():
    assert calendar_day_selectors(datetime(2026, 11, 3).date()) == [
        "#cal_2026_11_3 > a.cal-link",
        "#cal_2026_11_03 > a.cal-link",
    ]
    assert calendar_day_selectors(datetime(2026, 10, 27).date()) == ["#cal_2026_10_27 > a.cal-link"]


def _clock(start: datetime):
    state = {"now": start}

    def now():
        return state["now"]

    async def sleep(seconds):
        state["now"] = state["now"] + timedelta(seconds=seconds)

    return now, sleep


def test_poll_checks_once_when_retry_is_disabled():
    async def scenario():
        calls = {"attempt": 0, "refresh": 0}

        async def attempt():
            calls["attempt"] += 1
            return False

        async def refresh():
            calls["refresh"] += 1

        now, sleep = _clock(datetime(2026, 10, 20, 6, 30, tzinfo=AMSTERDAM))
        found = await poll_for_slot(
            attempt=attempt,
            refresh=refresh,
            retry_for_seconds=None,
            retry_interval_seconds=2,
            sleep=sleep,
            now=now,
        )
        assert found is False
        assert calls == {"attempt": 1, "refresh": 0}

    asyncio.run(scenario())


def test_poll_books_on_the_first_check_without_waiting():
    async def scenario():
        events = []

        async def attempt():
            events.append("check")
            return True

        async def refresh():
            events.append("refresh")

        async def sleep(seconds):
            events.append(("sleep", seconds))

        found = await poll_for_slot(
            attempt=attempt,
            refresh=refresh,
            retry_for_seconds=180,
            retry_interval_seconds=2,
            sleep=sleep,
            now=lambda: datetime(2026, 10, 20, 6, 30, tzinfo=AMSTERDAM),
        )
        assert found is True
        assert events == ["check"]

    asyncio.run(scenario())


def test_poll_retries_until_a_slot_appears_then_stops():
    async def scenario():
        events = []

        async def attempt():
            events.append("check")
            return len(events) >= 4

        async def refresh():
            events.append("refresh")

        now, sleep = _clock(datetime(2026, 10, 20, 6, 30, tzinfo=AMSTERDAM))

        async def recorded_sleep(seconds):
            events.append(("sleep", seconds))
            await sleep(seconds)

        found = await poll_for_slot(
            attempt=attempt,
            refresh=refresh,
            retry_for_seconds=30,
            retry_interval_seconds=2,
            sleep=recorded_sleep,
            now=now,
        )
        assert found is True
        assert events == ["check", ("sleep", 2), "refresh", "check"]

    asyncio.run(scenario())


def test_poll_does_not_start_a_check_after_the_deadline():
    async def scenario():
        events = []

        async def attempt():
            events.append(("check", now()))
            return False

        async def refresh():
            events.append("refresh")

        now, sleep = _clock(datetime(2026, 10, 20, 6, 30, tzinfo=AMSTERDAM))
        found = await poll_for_slot(
            attempt=attempt,
            refresh=refresh,
            retry_for_seconds=5,
            retry_interval_seconds=2,
            sleep=sleep,
            now=now,
        )
        assert found is False
        check_times = [event[1] for event in events if event[0] == "check"]
        assert len(check_times) == 3
        assert all((check_times[-1] - check_times[0]).total_seconds() < 5 for _ in [0])
        assert events[-1] == ("check", check_times[-1])

    asyncio.run(scenario())


def test_poll_keeps_going_after_a_session_error():
    async def scenario():
        calls = {"n": 0}

        async def attempt():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("session expired")
            return True

        async def refresh():
            return None

        now, sleep = _clock(datetime(2026, 10, 20, 6, 30, tzinfo=AMSTERDAM))
        found = await poll_for_slot(
            attempt=attempt,
            refresh=refresh,
            retry_for_seconds=10,
            retry_interval_seconds=2,
            sleep=sleep,
            now=now,
        )
        assert found is True
        assert calls["n"] == 2

    asyncio.run(scenario())


def test_poll_raises_when_every_check_fails():
    async def scenario():
        async def attempt():
            raise RuntimeError("session expired")

        async def refresh():
            return None

        now, sleep = _clock(datetime(2026, 10, 20, 6, 30, tzinfo=AMSTERDAM))
        await poll_for_slot(
            attempt=attempt,
            refresh=refresh,
            retry_for_seconds=4,
            retry_interval_seconds=2,
            sleep=sleep,
            now=now,
        )

    with pytest.raises(RuntimeError, match="session expired"):
        asyncio.run(scenario())


def test_input_schema_accepts_the_opening_fields_and_rejects_stringlist_enums():
    schema = json.loads((ROOT / ".actor" / "input_schema.json").read_text())
    properties = schema["properties"]
    for name in ("wait_until", "retry_for_seconds", "retry_interval_seconds", "timeout_seconds"):
        assert name in properties
        assert name not in schema.get("required", [])

    assert properties["wait_until"]["editor"] == "textfield"
    assert "enum" not in properties["wait_until"]
    assert properties["retry_interval_seconds"]["minimum"] >= 1

    for prop in properties.values():
        if prop.get("editor") == "stringList":
            items = prop.get("items") or {}
            assert "enum" not in items
            assert "enumTitles" not in items
