"""Placeholder squash evenings for weeks that are not booked yet.

Placeholders start strictly after the anchor date. That anchor is the later of
the latest real reservation and a week from today, so an evening that already
has a booking is not also given a placeholder.

The default evenings are Tuesday and Thursday at 20:30. Weekdays and time can
be overridden from the actor input.
"""

from __future__ import annotations

from datetime import datetime, timedelta

WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
DEFAULT_PLACEHOLDER_WEEKDAYS = ["tuesday", "thursday"]
DEFAULT_PLACEHOLDER_TIME = "20:30"


def generate_placeholders(
    start: datetime,
    placeholder_weeks: int,
    weekdays: list[str] | None = None,
    placeholder_time: str = DEFAULT_PLACEHOLDER_TIME,
) -> list[dict]:
    """Return placeholder reservations for the next `placeholder_weeks` weeks.

    `start` is exclusive. The first placeholder is the earliest requested
    weekday after that date, then the other requested weekdays in that same
    seven-day window, repeated weekly.
    """
    if placeholder_weeks < 0:
        raise ValueError("placeholder_weeks must be zero or greater")

    weekday_numbers = {
        WEEKDAY_NAMES.index(name)
        for name in normalize_weekdays(DEFAULT_PLACEHOLDER_WEEKDAYS if weekdays is None else weekdays)
    }
    hour, minute = _time_parts(normalize_placeholder_time(placeholder_time))
    start_date = start.date()

    first_cycle = []
    for offset in range(1, 8):
        day = start_date + timedelta(days=offset)
        if day.weekday() in weekday_numbers:
            first_cycle.append(datetime(day.year, day.month, day.day, hour, minute))

    placeholders = []
    for week in range(placeholder_weeks):
        for day in first_cycle:
            when = day + timedelta(weeks=week)
            placeholders.append(
                {
                    "datum": when.strftime("%d-%m-%Y"),
                    "weekdag": when.strftime("%A"),
                    "begintijd": when.strftime("%H:%M"),
                    "baan": "🚧 Placeholder",
                    "spelers": ["Jeroen Bos", "Vera Sweere"],
                }
            )
    return placeholders


def normalize_weekdays(weekdays: list[str]) -> list[str]:
    if not weekdays:
        raise ValueError("placeholder_weekdays must list at least one weekday")

    normalized = []
    for weekday in weekdays:
        if not isinstance(weekday, str):
            raise ValueError(f"placeholder weekday must be one of {list(WEEKDAY_NAMES)}")
        name = weekday.strip().lower()
        if name not in WEEKDAY_NAMES:
            raise ValueError(f"Unknown placeholder weekday '{weekday}'. Expected one of {list(WEEKDAY_NAMES)}")
        if name not in normalized:
            normalized.append(name)
    return normalized


def normalize_placeholder_time(placeholder_time: str) -> str:
    try:
        parsed = datetime.strptime(placeholder_time.strip(), "%H:%M")
    except (AttributeError, ValueError) as exc:
        raise ValueError("placeholder_time must be HH:MM") from exc
    return parsed.strftime("%H:%M")


def _time_parts(placeholder_time: str) -> tuple[int, int]:
    parsed = datetime.strptime(placeholder_time, "%H:%M")
    return parsed.hour, parsed.minute
