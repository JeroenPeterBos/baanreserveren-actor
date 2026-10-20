import asyncio
import json
from datetime import date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.baanreserveren import create_calendar
from src.calendar_sync import combine_reservations, normalize_removal_stamp, placeholder_anchor
from src.models import Input
from src.placeholders import generate_placeholders

ROOT = Path(__file__).resolve().parents[1]

PAST = {
    "datum": "01-10-2026",
    "begintijd": "19:45",
    "baan": "Court 3",
    "spelers": ["Jeroen Bos", "Vera Sweere"],
}
CANCELLED = {
    "datum": "19-10-2026",
    "begintijd": "21:15",
    "baan": "Court 9",
    "spelers": ["Jeroen Bos", "Vera Sweere"],
}
UPCOMING = {
    "datum": "27-10-2026",
    "begintijd": "20:30",
    "baan": "Court 5",
    "spelers": ["Jeroen Bos", "Vera Sweere"],
}


def _event_starts(calendar) -> list[tuple]:
    found = []
    for event in calendar.walk("VEVENT"):
        start = event.decoded("dtstart")
        found.append((start.year, start.month, start.day, start.hour, start.minute, str(event.get("summary"))))
    return found


def test_empty_scrape_uses_a_week_ahead_as_the_placeholder_anchor():
    # Sunday 18 Oct 2026 is the day the sync crashed on max() of an empty list.
    now = datetime(2026, 10, 18, 9, 0)
    anchor = placeholder_anchor([], now)

    assert anchor == datetime(2026, 10, 25, 9, 0)
    placeholders = generate_placeholders(anchor, placeholder_weeks=2)
    assert [item["datum"] for item in placeholders] == [
        "27-10-2026",
        "29-10-2026",
        "03-11-2026",
        "05-11-2026",
    ]


def test_latest_booking_still_pushes_the_placeholder_anchor_out():
    now = datetime(2026, 10, 18, 9, 0)
    anchor = placeholder_anchor([UPCOMING], now)
    assert anchor == datetime(2026, 10, 27, 0, 0)
    assert "27-10-2026" not in [item["datum"] for item in generate_placeholders(anchor, placeholder_weeks=1)]


def test_cancelled_future_booking_is_dropped_when_the_scrape_is_empty():
    # On 18 Oct the 19 Oct court is still in the future, so it is not history.
    # An empty scrape must not copy it forward.
    kept, removed, missing = combine_reservations(
        [PAST, CANCELLED],
        [],
        today=date(2026, 10, 18),
    )

    assert kept == [PAST]
    assert removed == 0
    assert missing == []


def test_played_history_is_kept_when_the_scrape_is_empty():
    kept, _, _ = combine_reservations([PAST, UPCOMING], [], today=date(2026, 10, 20))
    assert kept == [PAST]


def test_remove_reservations_drops_the_cancelled_court_after_its_date_passed():
    # By 20 Oct the failed syncs have let 19 Oct age into the past bucket.
    kept, removed, missing = combine_reservations(
        [PAST, CANCELLED],
        [],
        today=date(2026, 10, 20),
        remove=["19-10-2026 21:15"],
    )

    assert kept == [PAST]
    assert removed == 1
    assert missing == []


def test_remove_reservations_reports_a_stamp_that_is_not_stored():
    kept, removed, missing = combine_reservations(
        [PAST],
        [],
        today=date(2026, 10, 20),
        remove=["19-10-2026 21:15"],
    )

    assert kept == [PAST]
    assert removed == 0
    assert missing == ["19-10-2026 21:15"]


def test_empty_scrape_calendar_has_placeholders_and_not_the_cancelled_court():
    now = datetime(2026, 10, 20, 8, 0)
    kept, _, _ = combine_reservations(
        [PAST, CANCELLED],
        [],
        today=now.date(),
        remove=["19-10-2026 21:15"],
    )
    placeholders = generate_placeholders(
        placeholder_anchor([], now),
        placeholder_weeks=8,
        weekdays=["tuesday", "thursday"],
        placeholder_time="20:30",
    )

    calendar = asyncio.run(create_calendar(kept + placeholders, player="jeroen"))
    starts = _event_starts(calendar)
    summaries = [item[-1] for item in starts]

    assert (2026, 10, 19, 21, 15) not in [item[:5] for item in starts]
    assert (2026, 10, 1, 19, 45) in [item[:5] for item in starts]
    assert sum("Placeholder" in summary for summary in summaries) == len(placeholders) == 16
    assert all(item[3] == 20 and item[4] == 30 for item in starts if "Placeholder" in item[-1])


def test_player_file_keeps_only_that_player_upcoming_rows():
    other = {
        "datum": "29-10-2026",
        "begintijd": "19:45",
        "baan": "Court 4",
        "spelers": ["Vera Sweere", "Koen"],
    }
    kept, _, _ = combine_reservations(
        [PAST],
        [UPCOMING, other],
        today=date(2026, 10, 20),
        player="jeroen",
    )

    assert kept == [PAST, UPCOMING]


def test_remove_reservations_input_defaults_to_empty_and_checks_the_stamp():
    assert Input().remove_reservations == []
    assert Input(remove_reservations=["19-10-2026 21:15"]).remove_reservations == ["19-10-2026 21:15"]
    assert normalize_removal_stamp("19-10-2026 21:15") == "19-10-2026 21:15"

    with pytest.raises(ValidationError):
        Input(remove_reservations=["2026-10-19 21:15"])
    with pytest.raises(ValidationError):
        Input(remove_reservations=["31-02-2026 21:15"])


def test_input_schema_remove_reservations_is_a_string_list_without_enum():
    schema = json.loads((ROOT / ".actor" / "input_schema.json").read_text())
    field = schema["properties"]["remove_reservations"]

    assert field["editor"] == "stringList"
    assert field["type"] == "array"
    assert field["default"] == []
    assert "enum" not in field["items"]
    assert "enumTitles" not in field["items"]
    assert field["items"]["type"] == "string"
    assert "remove_reservations" not in schema.get("required", [])
