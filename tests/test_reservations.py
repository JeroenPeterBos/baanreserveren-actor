import pytest

from src.reservations import extract_player_names, extract_tables, parse_reservation_page

LEGACY_TBODY = """
<table>
  <tbody>
    <tr><th colspan="5">Reserveringen</th></tr>
    <tr class="tblTitle">
      <th>Datum</th><th>Begintijd</th><th>Eindtijd</th><th>Baan</th><th></th>
    </tr>
    <tr class="odd">
      <td><a href="/reservations/101">15-10-2026</a></td>
      <td>19:45</td><td>20:30</td><td>Court 3</td>
      <td><a href="/reservations/101/cancel">Annuleren</a></td>
    </tr>
    <tr class="even">
      <td><a href="/reservations/102">19-10-2026</a></td>
      <td>21:15</td><td>22:00</td><td>Court 5</td><td></td>
    </tr>
  </tbody>
</table>
"""

# Title and column headers live in thead. Body rows have no odd/even class.
# ancestor::tbody from the "Reserveringen" cell matches nothing on this page.
CURRENT_THEAD = """
<table class="card data-table">
  <thead>
    <tr class="tblUpTitle"><th colspan="4">Reserveringen</th></tr>
    <tr class="tblTitle">
      <th>Datum</th><th>Begintijd</th><th>Eindtijd</th><th>Baan</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td><a href="/reservations/101">do 15-10-2026</a></td>
      <td>19:45</td><td>20:30</td><td>Court 3</td>
    </tr>
    <tr>
      <td><a href="/reservations/102">19-10-2026</a></td>
      <td>21:15</td><td>22:00</td><td>Court 5</td>
    </tr>
  </tbody>
</table>
"""

HEADER_CELLS_AS_TD = """
<table>
  <tbody>
    <tr><th>Reserveringen</th></tr>
    <tr class="tblTitle">
      <td>Datum</td><td>Begin tijd</td><td>Baan</td>
    </tr>
    <tr class="odd">
      <td>2026-10-15</td><td>9:45</td><td>Court 2</td>
    </tr>
  </tbody>
</table>
"""


def test_legacy_tbody_table_parses_future_reservations():
    parsed = parse_reservation_page(LEGACY_TBODY, page_url="https://squtrecht.baanreserveren.nl/user/future")

    assert parsed.title == "Reserveringen"
    assert [row["datum"] for row in parsed.reservations] == ["15-10-2026", "19-10-2026"]
    assert parsed.reservations[0]["begintijd"] == "19:45"
    assert parsed.reservations[0]["baan"] == "Court 3"
    assert parsed.reservations[1]["begintijd"] == "21:15"
    assert parsed.reservations[0]["eindtijd"] == "20:30"


def test_thead_card_table_without_odd_even_classes_parses():
    # The old XPath started at the Reserveringen cell and required ancestor::tbody.
    # On this table that cell is in thead, so the old lookup matches nothing.
    title_rows = [
        row
        for table in extract_tables(CURRENT_THEAD)
        for row in table.rows
        if "tbluptitle" in {cls.lower() for cls in row.classes}
    ]
    assert title_rows
    assert {row.section for row in title_rows} == {"thead"}

    parsed = parse_reservation_page(CURRENT_THEAD)

    assert len(parsed.reservations) == 2
    assert parsed.reservations[0] == {
        "eindtijd": "20:30",
        "datum": "15-10-2026",
        "begintijd": "19:45",
        "baan": "Court 3",
    }
    assert parsed.reservations[1]["datum"] == "19-10-2026"
    assert parsed.reservations[1]["begintijd"] == "21:15"
    assert parsed.reservations[1]["baan"] == "Court 5"
    assert parsed.matches[0] == {"datum": "do 15-10-2026", "begintijd": "19:45", "baan": "Court 3"}


def test_spaced_header_and_iso_date_are_normalized():
    parsed = parse_reservation_page(HEADER_CELLS_AS_TD)

    assert parsed.reservations == [
        {"datum": "15-10-2026", "begintijd": "09:45", "baan": "Court 2"},
    ]


def test_decoy_matrix_does_not_hide_the_reservations_table():
    html = (
        "<table class='matrix'><tr class='tblTitle'><th>Tijd</th><th>Court 1</th></tr>"
        "<tr class='odd'><td>19:45</td><td>vrij</td></tr></table>"
        + CURRENT_THEAD
    )

    parsed = parse_reservation_page(html)

    assert parsed.title == "Reserveringen"
    assert len(parsed.reservations) == 2


def test_empty_overview_with_headers_returns_no_rows():
    html = """
    <table class="card data-table">
      <thead>
        <tr class="tblUpTitle"><th>Mijn reserveringen</th></tr>
        <tr class="tblTitle"><th>Datum</th><th>Begintijd</th><th>Baan</th></tr>
      </thead>
      <tbody>
        <tr><td colspan="3">Geen reserveringen gevonden</td></tr>
      </tbody>
    </table>
    """

    parsed = parse_reservation_page(html)

    assert parsed.reservations == []
    assert parsed.title == "Mijn reserveringen"


def test_missing_columns_raise_layout_error_with_what_was_found():
    html = """
    <table>
      <tr class="tblTitle"><th>Tijd</th><th>Court</th></tr>
      <tr><td>19:45</td><td>vrij</td></tr>
    </table>
    """

    with pytest.raises(RuntimeError) as caught:
        parse_reservation_page(html, page_url="https://squtrecht.baanreserveren.nl/user/future")

    message = str(caught.value)
    assert "Expected column 'datum' not found" in message
    assert "BaanReserveren likely changed their layout" in message
    assert "tijd" in message
    assert "https://squtrecht.baanreserveren.nl/user/future" in message


def test_page_with_no_table_reports_empty_columns():
    with pytest.raises(RuntimeError) as caught:
        parse_reservation_page("<div>niets</div>")

    assert "Parsed columns: []" in str(caught.value)
    assert "(no tables)" in str(caught.value)


def test_login_form_is_a_different_error_from_a_layout_change():
    html = """
    <form id="login-form"><input type="email" name="username"></form>
    <p>Inloggen</p>
    """

    with pytest.raises(RuntimeError) as caught:
        parse_reservation_page(html, page_url="https://squtrecht.baanreserveren.nl/?reason=LOGGED_IN")

    message = str(caught.value)
    assert "login form" in message
    assert "Expected column 'datum'" not in message


def test_unreadable_data_row_raises_instead_of_being_dropped():
    html = """
    <table class="card data-table">
      <thead>
        <tr class="tblTitle"><th>Datum</th><th>Begintijd</th><th>Baan</th></tr>
      </thead>
      <tbody>
        <tr><td>ergens volgende week</td><td>avond</td><td>Court 1</td></tr>
      </tbody>
    </table>
    """

    with pytest.raises(RuntimeError) as caught:
        parse_reservation_page(html)

    assert "could not be read" in str(caught.value)
    assert "ergens volgende week" in str(caught.value)


def test_player_names_from_legacy_class():
    html = """
    <div class="lightbox">
      <div class="res-info-player-name">Jeroen Bos</div>
      <div class="res-info-player-name">Vera Sweere</div>
    </div>
    """

    assert extract_player_names(html) == ["Jeroen Bos", "Vera Sweere"]


def test_player_names_from_speler_label_cells():
    html = """
    <table class="card info-table">
      <tr><th>Speler 1</th><td>Jeroen Bos</td></tr>
      <tr><th>Speler 2</th><td>Vera Sweere</td></tr>
    </table>
    """

    assert extract_player_names(html) == ["Jeroen Bos", "Vera Sweere"]


def test_player_names_from_member_list():
    html = """
    <ul class="member-list-members">
      <li>Jeroen Bos</li>
      <li>Vera Sweere</li>
    </ul>
    """

    assert extract_player_names(html) == ["Jeroen Bos", "Vera Sweere"]
