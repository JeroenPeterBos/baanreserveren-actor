"""Parse the BaanReserveren future-reservations overview.

The July 2026 page kept the column-header row and the "Reserveringen" title in
the same ``tbody``. The selectors then were:

    //th[contains(text(), 'Reserveringen')]/ancestor::tbody/tr[@class='odd' or @class='even']
    //th[contains(text(), 'Reserveringen')]/ancestor::tbody/tr[@class='tblTitle'][1]/...

Build 2.9 styles overview tables as ``table.card.data-table``: the title
(``tr.tblUpTitle``) and the column headers (``tr.tblTitle``) sit in ``thead``,
and body rows are plain ``tbody tr`` elements. ``ancestor::tbody`` matches
nothing on that page, which is why the updater logged ``Found 0 reservations``
and ``Parsed reservation columns: []``.

This parser keys off the column headers themselves, in either ``thead`` or
``tbody``, and still raises when those columns are not on the page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser

from src.utils import to_snake_case

REQUIRED_COLUMNS = ("datum", "begintijd", "baan")

# Compact header text (snake_case, underscores removed) -> canonical field.
COLUMN_ALIASES = {
    "datum": ("datum",),
    "begintijd": ("begintijd", "starttijd", "tijd"),
    "baan": ("baan", "baannaam"),
}

_DATE_DMY = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})")
_DATE_YMD = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_TIME = re.compile(r"(\d{1,2}):(\d{2})")
_PLAYER_LABEL = re.compile(r"^speler\s*\d+\b", re.IGNORECASE)
_PLAYER_LABEL_ONLY = re.compile(r"^speler\s*\d+\s*$", re.IGNORECASE)
_EMPTY_MARKER = re.compile(r"geen\s+reserver|no\s+reservation", re.IGNORECASE)


@dataclass
class HtmlRow:
    classes: set[str]
    section: str
    cells: list[str] = field(default_factory=list)
    cell_tags: list[str] = field(default_factory=list)


@dataclass
class HtmlTable:
    classes: set[str]
    section: str = "tbody"
    rows: list[HtmlRow] = field(default_factory=list)


@dataclass
class ReservationParse:
    reservations: list[dict]
    columns: list[str]
    title: str
    # Raw cell text, parallel to reservations, used to find the same row in the live DOM.
    matches: list[dict] = field(default_factory=list)


def parse_reservation_page(html: str, page_url: str = "") -> ReservationParse:
    """Return future reservations from a reservations-overview document.

    Raises ``RuntimeError`` when the page is the login form, or when no table
    has readable datum / begintijd / baan columns.
    """
    tables = extract_tables(html)
    candidates: list[dict] = []
    best: tuple[int, ReservationParse] | None = None
    closest_columns: list[str] = []
    closest_hits = -1

    for table in tables:
        header_row = choose_header_row(table)
        headers = header_row.cells if header_row is not None else []
        title = table_title(table)
        candidates.append({"title": title, "headers": headers})
        indexes = {column: _column_index(headers, column) for column in REQUIRED_COLUMNS}
        hits = sum(index is not None for index in indexes.values())
        if hits > closest_hits:
            closest_hits = hits
            closest_columns = [to_snake_case(header) for header in headers if to_snake_case(header)]
        if hits < len(REQUIRED_COLUMNS) or header_row is None:
            continue

        reservations, matches = _reservations_from_rows(table, header_row, headers, indexes, page_url)
        score = (10 if "reserver" in title.lower() else 0) + len(reservations)
        parsed = ReservationParse(reservations=reservations, columns=headers, title=title, matches=matches)
        if best is None or score > best[0]:
            best = (score, parsed)

    if best is not None:
        return best[1]

    if _is_login_page(html):
        url = f" ({page_url})" if page_url else ""
        raise RuntimeError(
            "Reservations page is showing the login form"
            f"{url}. The session did not reach the reservations overview."
        )

    url = f" Page: {page_url}." if page_url else ""
    raise RuntimeError(
        "Expected column 'datum' not found in reservations table. "
        "BaanReserveren likely changed their layout. "
        f"Parsed columns: {closest_columns}.{url} Tables: {_format_candidates(candidates)}"
    )


def extract_player_names(html: str) -> list[str]:
    """Player names from a reservation-detail overlay.

    Prefers the historical ``res-info-player-name`` nodes, then a ``Speler N``
    label with the name in the next cell, then membership-list items.
    """
    names = _texts_with_class(html, "res-info-player-name")
    if not names:
        names = _names_from_player_rows(html)
    if not names:
        names = _member_list_names(html)
    return _clean_names(names)


def extract_tables(html: str) -> list[HtmlTable]:
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    return parser.tables


def choose_header_row(table: HtmlTable) -> HtmlRow | None:
    candidates = [row for row in table.rows if _is_header_row(row)]
    if not candidates:
        candidates = [
            row
            for row in table.rows
            if row.section == "thead" and not _is_title_row(row) and len(row.cells) >= 2
        ]
    if not candidates:
        return None

    def score(row: HtmlRow) -> tuple[int, int]:
        hits = sum(_column_index(row.cells, column) is not None for column in REQUIRED_COLUMNS)
        return (hits, len(row.cells))

    return max(candidates, key=score)


def table_title(table: HtmlTable) -> str:
    for row in table.rows:
        if _is_title_row(row) and row.cells:
            return row.cells[0]
    return ""


def _reservations_from_rows(
    table: HtmlTable,
    header_row: HtmlRow,
    headers: list[str],
    indexes: dict[str, int | None],
    page_url: str,
) -> tuple[list[dict], list[dict]]:
    reservations = []
    matches = []
    for row in table.rows:
        if row is header_row or _is_title_row(row) or _is_header_row(row):
            continue
        if row.section == "tfoot":
            continue
        if {cls.lower() for cls in row.classes} & {"tbltotal", "tblsorting", "spacer", "footer"}:
            continue
        if sum(tag == "td" for tag in row.cell_tags) < 1 or len(row.cells) < 2:
            continue
        if _is_empty_marker(row.cells):
            continue

        raw_date = _cell(row, indexes["datum"])
        raw_time = _cell(row, indexes["begintijd"])
        raw_court = _cell(row, indexes["baan"]).strip()
        if not raw_date.strip() and not raw_time.strip():
            continue

        datum = _normalize_date(raw_date)
        begintijd = _normalize_time(raw_time)
        if datum is None or begintijd is None or not raw_court:
            url = f" Page: {page_url}." if page_url else ""
            raise RuntimeError(
                "Reservations table has datum, begintijd and baan columns, but a row "
                "could not be read. BaanReserveren likely changed their layout. "
                f"Parsed columns: {headers}. Sample row: {row.cells[:8]}.{url}"
            )

        reservation = {}
        for header, value in zip(headers, row.cells):
            key = to_snake_case(header)
            if key and _canonical_column(header) is None:
                reservation[key] = value.strip()
        reservation["datum"] = datum
        reservation["begintijd"] = begintijd
        reservation["baan"] = raw_court
        reservations.append(reservation)
        matches.append({"datum": raw_date.strip(), "begintijd": raw_time.strip(), "baan": raw_court})
    return reservations, matches


def _is_login_page(html: str) -> bool:
    return 'id="login-form"' in html or "id='login-form'" in html


def _is_header_row(row: HtmlRow) -> bool:
    return "tbltitle" in {cls.lower() for cls in row.classes} and len(row.cells) >= 2


def _is_title_row(row: HtmlRow) -> bool:
    classes = {cls.lower() for cls in row.classes}
    if "tbluptitle" in classes:
        return True
    if "tbltitle" in classes:
        return False
    return len(row.cells) <= 1 and bool(row.cell_tags) and row.cell_tags[0] == "th"


def _is_empty_marker(cells: list[str]) -> bool:
    return _EMPTY_MARKER.search(" ".join(cells)) is not None


def _canonical_column(header: str) -> str | None:
    compact = _compact_header(header)
    for canonical, aliases in COLUMN_ALIASES.items():
        if compact in aliases:
            return canonical
    return None


def _column_index(headers: list[str], canonical: str) -> int | None:
    aliases = COLUMN_ALIASES[canonical]
    normalized = [_compact_header(header) for header in headers]
    for alias in aliases:
        if alias in normalized:
            return normalized.index(alias)
    return None


def _compact_header(header: str) -> str:
    return to_snake_case(header).replace("_", "")


def _cell(row: HtmlRow, index: int | None) -> str:
    if index is None or index >= len(row.cells):
        return ""
    return row.cells[index]


def _normalize_date(value: str) -> str | None:
    match = _DATE_DMY.search(value)
    if match:
        day, month, year = (int(part) for part in match.groups())
    else:
        match = _DATE_YMD.search(value)
        if not match:
            return None
        year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day).strftime("%d-%m-%Y")
    except ValueError:
        return None


def _normalize_time(value: str) -> str | None:
    match = _TIME.search(value)
    if not match:
        return None
    hour, minute = (int(part) for part in match.groups())
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def _format_candidates(candidates: list[dict]) -> str:
    if not candidates:
        return "(no tables)"
    parts = []
    for candidate in candidates[:8]:
        title = (candidate["title"] or "(no title)")[:80]
        headers = candidate["headers"][:12]
        parts.append(f"{title}: {headers}")
    suffix = f" (+{len(candidates) - 8} more)" if len(candidates) > 8 else ""
    return "; ".join(parts) + suffix


def _clean_names(names: list[str]) -> list[str]:
    cleaned = []
    for name in names:
        text = re.sub(r"\s+", " ", name).strip()
        text = re.sub(r"^speler\s*\d+\s*[:\-]\s*", "", text, flags=re.IGNORECASE).strip()
        if not text or _PLAYER_LABEL_ONLY.match(text):
            continue
        cleaned.append(text)
    return cleaned


def _names_from_player_rows(html: str) -> list[str]:
    names = []
    for table in extract_tables(html):
        for row in table.rows:
            if not row.cells or not _PLAYER_LABEL.match(row.cells[0].strip()):
                continue
            if len(row.cells) >= 2 and row.cells[1].strip():
                names.append(row.cells[1])
            else:
                names.append(row.cells[0])
    return names


def _member_list_names(html: str) -> list[str]:
    return _texts_with_class(html, "member-list-members", item_tag="li")


def _texts_with_class(html: str, class_name: str, item_tag: str | None = None) -> list[str]:
    parser = _ClassTextParser(class_name, item_tag=item_tag)
    parser.feed(html)
    parser.close()
    return parser.texts


def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
    for key, value in attrs:
        if key == "class" and value:
            return set(value.split())
    return set()


def _attr(attrs: list[tuple[str, str | None]], name: str) -> str | None:
    for key, value in attrs:
        if key == name:
            return value
    return None


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[HtmlTable] = []
        self._table_stack: list[HtmlTable] = []
        self._paused: list[tuple[list[str] | None, str | None, HtmlRow | None]] = []
        self._row: HtmlRow | None = None
        self._cell: list[str] | None = None
        self._cell_tag: str | None = None
        self._script = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs)
        self._end(tag.lower())

    def handle_endtag(self, tag: str) -> None:
        self._end(tag.lower())

    def handle_data(self, data: str) -> None:
        if self._script or self._cell is None:
            return
        self._cell.append(data)

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._script += 1
            return
        if self._script:
            return
        if tag == "table":
            self._paused.append((self._cell, self._cell_tag, self._row))
            self._cell = None
            self._cell_tag = None
            self._row = None
            self._table_stack.append(HtmlTable(classes=_classes(attrs)))
            return
        if not self._table_stack:
            return
        table = self._table_stack[-1]
        if tag in {"thead", "tbody", "tfoot"}:
            table.section = tag
            return
        if tag == "tr":
            self._row = HtmlRow(classes=_classes(attrs), section=table.section)
            return
        if tag in {"td", "th"} and self._row is not None:
            self._cell = []
            self._cell_tag = tag
            return
        if tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def _end(self, tag: str) -> None:
        if tag in {"script", "style"} and self._script:
            self._script -= 1
            return
        if self._script:
            return
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.cells.append(text)
            self._row.cell_tags.append(self._cell_tag or tag)
            self._cell = None
            self._cell_tag = None
            return
        if tag == "tr" and self._row is not None and self._table_stack:
            self._table_stack[-1].rows.append(self._row)
            self._row = None
            return
        if tag in {"thead", "tbody", "tfoot"} and self._table_stack:
            self._table_stack[-1].section = "tbody"
            return
        if tag == "table" and self._table_stack:
            self._flush_open_row()
            self.tables.append(self._table_stack.pop())
            if self._paused:
                self._cell, self._cell_tag, self._row = self._paused.pop()

    def _flush_open_row(self) -> None:
        if self._cell is not None and self._row is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            self._row.cells.append(text)
            self._row.cell_tags.append(self._cell_tag or "td")
            self._cell = None
            self._cell_tag = None
        if self._row is not None and self._table_stack:
            self._table_stack[-1].rows.append(self._row)
            self._row = None


@dataclass
class _Frame:
    tag: str
    region: bool
    capture: bool


class _ClassTextParser(HTMLParser):
    """Collect text of elements carrying ``class_name``.

    When ``item_tag`` is set, collect that tag only while it is nested in an
    element with ``class_name`` (used for ``li`` items in a member list).
    """

    def __init__(self, class_name: str, item_tag: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.class_name = class_name
        self.item_tag = item_tag
        self.texts: list[str] = []
        self._frames: list[_Frame] = []
        self._region_depth = 0
        self._capture_depth = 0
        self._buf: list[str] | None = None
        self._script = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._start(tag.lower(), attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "br" and self._buf is not None:
            self._buf.append(" ")
            return
        self._start(tag, attrs)
        self._end(tag)

    def handle_endtag(self, tag: str) -> None:
        self._end(tag.lower())

    def handle_data(self, data: str) -> None:
        if self._script or self._buf is None:
            return
        self._buf.append(data)

    def _start(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._script += 1
            return
        if self._script:
            return
        if tag == "br":
            if self._buf is not None:
                self._buf.append(" ")
            return
        region = self.class_name in _classes(attrs)
        capture = False
        if self._capture_depth:
            self._capture_depth += 1
            capture = True
        else:
            own_text = self.item_tag is None and region
            child_text = self.item_tag is not None and tag == self.item_tag and self._region_depth > 0
            if own_text or child_text:
                self._capture_depth = 1
                self._buf = []
                capture = True
        if region:
            self._region_depth += 1
        self._frames.append(_Frame(tag=tag, region=region, capture=capture))

    def _end(self, tag: str) -> None:
        if tag in {"script", "style"} and self._script:
            self._script -= 1
            return
        if self._script or tag == "br":
            return
        index = next((i for i in range(len(self._frames) - 1, -1, -1) if self._frames[i].tag == tag), None)
        if index is None:
            return
        closing = self._frames[index:]
        del self._frames[index:]
        for frame in reversed(closing):
            if frame.capture and self._capture_depth:
                self._capture_depth -= 1
                if self._capture_depth == 0 and self._buf is not None:
                    text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
                    if text:
                        self.texts.append(text)
                    self._buf = None
            if frame.region and self._region_depth:
                self._region_depth -= 1
