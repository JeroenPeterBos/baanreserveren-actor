Book squash courts on [Squash Utrecht's BaanReserveren](https://squtrecht.baanreserveren.nl/) and publish the upcoming reservations as an iCal file.

The weekday booking task runs on an Apify schedule. Member courts for the date one week ahead open at **06:30 Europe/Amsterdam**. The actor can log in before that time and poll until a court in the preferred time is free.

## What does Baanreserveren Actor do?

It logs into squtrecht.baanreserveren.nl with Playwright and either:

- books one court for a configured opponent and time, or
- reads future reservations and uploads an iCal calendar.

It does not search the public web. It uses the club account in the actor environment (`BR_USERNAME`, `BR_PASSWORD`).

## Why book before opening?

A run that starts at 06:30 spends most of its first half minute pulling the container image, then logging in and opening the date. By the time it reads the matrix, the preferred member courts are often already taken. Starting a few minutes early moves that work to before 06:30. The first slot check then happens as the courts open.

## How to book a court at opening

1. Put the club username and password in the actor environment as `BR_USERNAME` and `BR_PASSWORD`. Set `DRY_RUN` to false only when a real booking should be confirmed. The input `dry_run` flag must also be false. Either flag still skips the final confirm click.
2. On the booking task, set the input below. Leave `timeout_seconds` empty so the actor extends its own 180 second limit to fit the wait and the poll.
3. Set the task timeout to **600 seconds**. The actor timeout does not include the image pull, and the platform kills the run at the task timeout.
4. Schedule the task at **06:27 Europe/Amsterdam** on Tuesday and Thursday: cron `27 6 * * 2,4` with timezone `Europe/Amsterdam`. Three minutes covers the image pull (about 17 seconds in recent runs) plus login and opening the date (about 7 seconds).

Recommended task input:

```json
{
  "dry_run": false,
  "leden_only": true,
  "opponent": "vera",
  "times": ["20:30", "19:45", "21:15"],
  "reservation_default": "next_week",
  "wait_until": "06:30:00",
  "retry_for_seconds": 180,
  "retry_interval_seconds": 2
}
```

Leave the new fields empty to keep the previous behaviour: one check as soon as the date page is open, and a 180 second actor timeout.

## Input

See the input tab for the full form. Dates are calculated in Europe/Amsterdam, not from the container's UTC clock.

| Field | Role |
| --- | --- |
| `wait_until` | Local opening time, `HH:MM` or `HH:MM:SS`, Europe/Amsterdam. Before this time the actor only logs in and opens the target date. It does not reload the matrix. |
| `retry_for_seconds` | How long to keep checking after `wait_until` (or immediately, when `wait_until` is empty). Empty means a single check. |
| `retry_interval_seconds` | Pause between checks. Use 2 or 3. The default while polling is 2. Ignored when `retry_for_seconds` is empty. |
| `timeout_seconds` | Optional overall limit. Empty keeps 180 seconds, or longer when the wait and the poll need it. |
| `times` | Preference order for member courts. The task uses 20:30, then 19:45, then 21:15. |
| `reservation_default` | `next_week` is seven days after today's date in Amsterdam. |
| `dry_run` | When true, the actor selects a court and fills the opponent, then stops before the confirm button. |

`reservation_skip` and `times` are plain string lists. Weekday names are checked in code. Apify rejects `items.enum` on a `stringList` field.

## Preference order

At opening the actor reloads the court matrix before it reads slots, so it does not book from the page it loaded a few minutes earlier. Each poll then walks `times` from first to last and books the first time that has a free court other than Court 1 (Court 1 has to be booked at reception). Member courts prefer the hall at the back of that time.

A lower preference is booked on the poll that finds it, including the first poll after 06:30. If 20:30 and 19:45 are already gone and 21:15 is free, the actor books 21:15 immediately. Waiting for a better time to show up on a later poll would lose the free court.

## Session expiry

A few minutes of waiting is within a normal club session. If the logout link disappears while waiting, or a check hits a logged-out page, the actor logs in again, reopens the same date, and keeps polling until the retry window ends. A dead session on every attempt fails the run with the last error instead of looking like a quiet "no slot".

## Output

A booking run logs the chosen time and opponent. It does not write a dataset row. A calendar run uploads the iCal and JSON reservations to the configured S3 bucket. Placeholder events for Tuesday and Thursday at 20:30 fill evenings that are not booked yet.

A sync with no upcoming reservations still writes those files. Placeholders then start a week from the run. A booking that is still in the future and missing from the club page is left out. Past rows stay, because the club page only lists what is still ahead. `remove_reservations` drops specific `DD-MM-YYYY HH:MM` rows from that stored history before the ICS is written. It defaults to an empty list.

## Schedule notes

The cron timezone has to be Europe/Amsterdam. The same expression in UTC fires at 08:27 CEST.

Do not start much earlier than 06:27 unless the task timeout is raised. The actor timeout becomes `seconds until 06:30 + retry_for_seconds + 120`, and the task timeout must be larger than that plus the image pull. At 06:27 the actor timeout lands around 460 seconds when the process starts about two and a half minutes before opening, so a 600 second task timeout still has room.

## Local run

```bash
apify run
```

Put sample input in `storage/key_value_stores/default/INPUT.json`. Local storage stays on disk. A dry run never clicks the confirm button.
