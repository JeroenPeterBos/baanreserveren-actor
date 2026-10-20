from typing import Literal, Optional

from pydantic import BaseModel, BaseSettings, Field, validator

from src.booking_window import (
    MAX_RETRY_FOR_SECONDS,
    MAX_RETRY_INTERVAL_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_RETRY_INTERVAL_SECONDS,
    MIN_TIMEOUT_SECONDS,
    coerce_optional_int,
    normalize_clock,
)
from src.placeholders import (
    DEFAULT_PLACEHOLDER_TIME,
    DEFAULT_PLACEHOLDER_WEEKDAYS,
    normalize_placeholder_time,
    normalize_weekdays,
)


class Settings(BaseSettings):
    dry_run: bool = Field(ENV="DRY_RUN", default=True, description="Don't actually place the reservation")
    username: str = Field(env="BR_USERNAME", description="Baanreserveren username")
    password: str = Field(env="BR_PASSWORD", description="Baanreserveren password")
    headless: bool = Field(env="HEADLESS", default=True, description="Run browser in headless mode")


class Input(BaseModel):
    update_calendar: bool = Field(
        default=False,
        description=(
            "Scrape the reservations and update the ical file. If true, booking fields are ignored. "
            "Placeholder weekdays and time still apply."
        ),
    )
    dry_run: bool = Field(default=True, description="Don't actually place the reservation")
    reservation_date: str = Field(
        default=None, description="The date to book a slot on, format: yyyy-mm-dd. Defaults to one week from now"
    )
    reservation_default: Literal["next_week", "today"] = Field(
        default="next_week", description="The default date to book a slot on if no explicit date is given"
    )
    reservation_skip: list[str] = Field(
        default=[], description="The dates to skip when trying to book a slot, format: yyyy-mm-dd"
    )
    opponent: Literal["vera", "koen"] = Field(default="vera", description="The opponent to book a slot with")
    times: list[str] = Field(
        default=["20:30", "19:45"],
        description="The times to try to book a slot on the leden banen",
    )
    leden_only: bool = Field(default=True, description="Only try to book a slot on the leden banen")
    non_leden_times: list[str] = Field(
        default=["20:15", "19:30"],
        description="The times to try to book a slot on the non-leden banen",
    )
    placeholder_weekdays: list[str] = Field(
        default=DEFAULT_PLACEHOLDER_WEEKDAYS,
        description=(
            "Weekdays for placeholder events on evenings that are not booked yet. "
            "Use monday, tuesday, wednesday, thursday, friday, saturday, or sunday. "
            "Defaults to tuesday and thursday."
        ),
    )
    placeholder_time: str = Field(
        default=DEFAULT_PLACEHOLDER_TIME,
        description="Start time for placeholder events, HH:MM. The event stays 45 minutes.",
    )
    wait_until: Optional[str] = Field(
        default=None,
        description=(
            "Europe/Amsterdam time (HH:MM:SS) when courts open. Before this time the actor "
            "logs in and opens the target date, and does not reload the court matrix. "
            "Example: 06:30:00. Leave empty to check as soon as the page is ready."
        ),
    )
    retry_for_seconds: Optional[int] = Field(
        default=None,
        description=(
            "After wait_until (or immediately, when wait_until is empty), keep reloading "
            "and checking for a free slot for this many seconds. Leave empty for a single check."
        ),
    )
    retry_interval_seconds: Optional[int] = Field(
        default=None,
        description=(
            "Seconds between slot checks while retry_for_seconds is running. "
            "Use 2 or 3. Ignored when retry_for_seconds is empty. Defaults to 2."
        ),
    )
    timeout_seconds: Optional[int] = Field(
        default=None,
        description=(
            "Overall actor timeout in seconds. Leave empty to keep 180 seconds, extended "
            "automatically so wait_until plus retry_for_seconds still fits."
        ),
    )

    @validator("wait_until", pre=True)
    def wait_until_is_amsterdam_clock(cls, value):
        if value is None or (isinstance(value, str) and value.strip() == ""):
            return None
        return normalize_clock(value)

    @validator("retry_for_seconds", pre=True)
    def retry_for_seconds_range(cls, value):
        return coerce_optional_int(
            value,
            name="retry_for_seconds",
            minimum=0,
            maximum=MAX_RETRY_FOR_SECONDS,
        )

    @validator("retry_interval_seconds", pre=True)
    def retry_interval_seconds_range(cls, value):
        return coerce_optional_int(
            value,
            name="retry_interval_seconds",
            minimum=MIN_RETRY_INTERVAL_SECONDS,
            maximum=MAX_RETRY_INTERVAL_SECONDS,
        )

    @validator("timeout_seconds", pre=True)
    def timeout_seconds_range(cls, value):
        return coerce_optional_int(
            value,
            name="timeout_seconds",
            minimum=MIN_TIMEOUT_SECONDS,
            maximum=MAX_TIMEOUT_SECONDS,
        )

    @validator("placeholder_weekdays", pre=True)
    def placeholder_weekdays_are_known(cls, value):
        if value is None:
            return DEFAULT_PLACEHOLDER_WEEKDAYS
        return normalize_weekdays(value)

    @validator("placeholder_time", pre=True)
    def placeholder_time_is_hhmm(cls, value):
        if value is None:
            return DEFAULT_PLACEHOLDER_TIME
        return normalize_placeholder_time(value)


if __name__ == "__main__":
    import json

    json_schema = Input.schema()

    # depending on the type set `editor` and depending on the default set `prefill``
    for field in json_schema["properties"].values():
        if field["type"] == "string":
            if "enum" in field:
                field["editor"] = "select"
            elif "date" in field["title"].lower():
                field["editor"] = "datepicker"
            else:
                field["editor"] = "textfield"
            if field.get("default"):
                field["prefill"] = field["default"]

        if field["type"] == "array":
            if field.get("items", {}).get("type") == "string":
                field["editor"] = "stringList"
            # stringList rejects items.enum. Weekday names are checked in code.
            if field.get("editor") == "stringList" and isinstance(field.get("items"), dict):
                field["items"].pop("enum", None)
                field["items"].pop("enumTitles", None)

    json_schema["title"] = "Baanreserveren Actor"
    json_schema["schemaVersion"] = 1

    with open(".actor/input_schema.json", "w") as f:
        f.write(json.dumps(json_schema, indent=4))
