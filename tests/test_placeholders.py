import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.placeholders import generate_placeholders

ROOT = Path(__file__).resolve().parents[1]


def _dates(start, weeks=2, weekdays=None, placeholder_time="20:30"):
    return [
        item["datum"]
        for item in generate_placeholders(
            start,
            weeks,
            weekdays=weekdays,
            placeholder_time=placeholder_time,
        )
    ]


def _legacy_monday_wednesday(start: datetime, weeks: int) -> list[str]:
    """The previous hardcoded Monday/Wednesday schedule, kept as a reference."""
    weekday = start.weekday()
    if weekday < 2:
        next_wednesday = start + timedelta(days=(2 - weekday))
        days = [
            datetime(next_wednesday.year, next_wednesday.month, next_wednesday.day, 20, 30),
            datetime(next_wednesday.year, next_wednesday.month, next_wednesday.day, 20, 30) + timedelta(days=5),
        ]
    else:
        next_monday = start + timedelta(days=(7 - weekday))
        days = [
            datetime(next_monday.year, next_monday.month, next_monday.day, 20, 30),
            datetime(next_monday.year, next_monday.month, next_monday.day, 20, 30) + timedelta(days=2),
        ]
    return [(day + timedelta(weeks=i)).strftime("%d-%m-%Y") for i in range(weeks) for day in days]


def test_default_placeholders_are_tuesday_and_thursday_at_2030():
    # Sunday 25 Oct 2026 is an anchor that used to emit Mon 26 Oct and Wed 28 Oct.
    start = datetime(2026, 10, 25)
    placeholders = generate_placeholders(start, placeholder_weeks=3)

    assert [item["datum"] for item in placeholders] == [
        "27-10-2026",
        "29-10-2026",
        "03-11-2026",
        "05-11-2026",
        "10-11-2026",
        "12-11-2026",
    ]
    assert {item["begintijd"] for item in placeholders} == {"20:30"}
    assert {datetime.strptime(item["datum"], "%d-%m-%Y").weekday() for item in placeholders} == {1, 3}
    assert placeholders[0]["baan"] == "🚧 Placeholder"
    assert placeholders[0]["spelers"] == ["Jeroen Bos", "Vera Sweere"]
    assert "26-10-2026" not in _dates(start, weeks=8)
    assert "28-10-2026" not in _dates(start, weeks=8)


def test_booked_anchor_day_is_not_also_a_placeholder():
    tuesday = datetime(2026, 10, 27)
    thursday = datetime(2026, 10, 29)

    after_tuesday = _dates(tuesday, weeks=2)
    after_thursday = _dates(thursday, weeks=2)

    assert after_tuesday[0] == "29-10-2026"
    assert "27-10-2026" not in after_tuesday
    assert after_thursday[0] == "03-11-2026"
    assert "29-10-2026" not in after_thursday


def test_monday_wednesday_input_matches_the_old_schedule():
    start = datetime(2026, 10, 18)
    for offset in range(21):
        anchor = start + timedelta(days=offset)
        assert _dates(anchor, weeks=8, weekdays=["monday", "wednesday"]) == _legacy_monday_wednesday(anchor, 8)


def test_logged_monday_wednesday_run_moves_to_tuesday_thursday():
    # A Wednesday anchor used to log 26 Oct, 28 Oct, then 2 Nov.
    start = datetime(2026, 10, 21)
    assert _dates(start, weeks=2, weekdays=["monday", "wednesday"]) == [
        "26-10-2026",
        "28-10-2026",
        "02-11-2026",
        "04-11-2026",
    ]
    assert _dates(start, weeks=2) == [
        "22-10-2026",
        "27-10-2026",
        "29-10-2026",
        "03-11-2026",
    ]


def test_placeholder_time_is_configurable_and_normalized():
    placeholders = generate_placeholders(
        datetime(2026, 10, 25),
        placeholder_weeks=1,
        placeholder_time="9:45",
    )

    assert [item["begintijd"] for item in placeholders] == ["09:45", "09:45"]


def test_duplicate_weekdays_are_not_repeated():
    dates = _dates(datetime(2026, 10, 25), weeks=1, weekdays=["Thursday", "tuesday", "thursday"])

    assert dates == ["27-10-2026", "29-10-2026"]


def test_invalid_weekday_or_time_raises():
    with pytest.raises(ValueError, match="Unknown placeholder weekday"):
        generate_placeholders(datetime(2026, 10, 25), 1, weekdays=["tues"])
    with pytest.raises(ValueError, match="HH:MM"):
        generate_placeholders(datetime(2026, 10, 25), 1, placeholder_time="2030")
    with pytest.raises(ValueError, match="at least one"):
        generate_placeholders(datetime(2026, 10, 25), 1, weekdays=[])


def test_input_schema_defaults_to_tuesday_thursday_at_2030():
    schema = json.loads((ROOT / ".actor" / "input_schema.json").read_text())
    weekdays = schema["properties"]["placeholder_weekdays"]
    placeholder_time = schema["properties"]["placeholder_time"]

    assert weekdays["default"] == ["tuesday", "thursday"]
    assert weekdays["items"]["enum"][0] == "monday"
    assert "thursday" in weekdays["items"]["enum"]
    assert placeholder_time["default"] == "20:30"
    assert "45 minutes" in placeholder_time["description"]
