"""Merge scraped reservations with the history stored in S3.

The club page only lists upcoming reservations. A successful sync therefore:

- keeps stored rows whose date is already in the past
- replaces everything from today onward with the rows just scraped

An upcoming booking that disappears from the page is left out of the new file.
If the sync crashes before that write, and the date then passes, the cancelled
row is indistinguishable from a game that was played. ``remove_reservations``
is the explicit way to drop those rows. It matches ``DD-MM-YYYY HH:MM`` and
does not look at the court name.

Placeholders still need an anchor when the scrape is empty. That anchor is a
week from the run, which is the same horizon used when every real booking is
closer than a week out.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


def placeholder_anchor(future_reservations: list[dict], now: datetime) -> datetime:
    """Exclusive start for placeholders: later of the latest booking and a week ahead.

    An empty scrape has no latest booking, so the anchor is a week from ``now``.
    """
    horizon = now + timedelta(days=7)
    if not future_reservations:
        return horizon
    latest = max(datetime.strptime(reservation["datum"], "%d-%m-%Y") for reservation in future_reservations)
    return max(latest, horizon)


def reservation_key(reservation: dict) -> str:
    return reservation["datum"] + " " + reservation["begintijd"]


def normalize_removal_stamp(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("remove_reservations entries must be DD-MM-YYYY HH:MM")
    try:
        parsed = datetime.strptime(value.strip(), "%d-%m-%Y %H:%M")
    except ValueError as exc:
        raise ValueError("remove_reservations entries must be DD-MM-YYYY HH:MM") from exc
    return parsed.strftime("%d-%m-%Y %H:%M")


def combine_reservations(
    previous: list[dict],
    future: list[dict],
    *,
    today: date,
    player: str | None = None,
    remove: list[str] | None = None,
) -> tuple[list[dict], int, list[str]]:
    """Return kept rows, how many matched ``remove``, and stamps that matched nothing.

    ``player`` filters the scraped upcoming rows only, matching the per-player
    calendar files. Stored history is already that file's own contents.
    """
    past = [
        reservation for reservation in previous if datetime.strptime(reservation["datum"], "%d-%m-%Y").date() < today
    ]
    upcoming = future
    if player is not None:
        upcoming = [
            reservation
            for reservation in upcoming
            if any(player in person.lower() for person in reservation["spelers"])
        ]

    selected = past + upcoming
    remove = remove or []
    if not remove:
        return selected, 0, []

    remove_set = set(remove)
    found: set[str] = set()
    kept = []
    removed = 0
    for reservation in selected:
        key = reservation_key(reservation)
        if key in remove_set:
            found.add(key)
            removed += 1
            continue
        kept.append(reservation)
    missing = [stamp for stamp in remove if stamp not in found]
    return kept, removed, missing
