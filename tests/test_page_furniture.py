"""Page furniture never becomes command wording (audit finding a, BGBl side).

Fixtures are real amendment commands from Lexgraph's retrospective store
whose text layer contains running headers: the 2023+ electronic BGBl
("Seite 3 von 4 Bundesgesetzblatt …" in the middle of a line) and the
printed BGBl (page number + header, plus the "im Internet" footer).
"""
import json
from pathlib import Path

from api.page_furniture import find_page_furniture, strip_page_furniture

ROWS = json.loads((Path(__file__).parent / "fixtures"
                   / "page_furniture_bgbl_commands.json").read_text())


def _row(document_id):
    return next(row for row in ROWS if row["document_id"] == document_id)


def test_mid_line_electronic_bgbl_header_is_removed():
    row = _row("bgbl-1-2023-406")
    clean, removed = strip_page_furniture(row["raw"])
    assert [span["kind"] for span in removed] == ["bgbl_running_header"]
    assert "Bundesgesetzblatt" not in clean
    assert clean.rstrip().endswith("durch die Angabe „29. Dezember 2023“ "
                                   "ersetzt.")
    # Offsets refer to the original extraction.
    span = removed[0]
    assert row["raw"][span["start"]:span["end"]] == span["text"]


def test_printed_bgbl_header_and_footer_are_removed():
    for document_id in ("bgbl-1-2021-s1534", "bgbl-1-2009-s1102"):
        row = _row(document_id)
        clean, removed = strip_page_furniture(row["raw"])
        kinds = {span["kind"] for span in removed}
        assert kinds == {"bgbl_running_header", "bgbl_footer"}
        assert "ausgegeben zu Bonn" not in clean
        assert "im Internet" not in clean


def test_drucksache_running_headers():
    text = ("Die Bundesregierung stimmt zu.\nDrucksache 19/13824"
            "                                 – 314 –             Deutscher "
            "Bundestag – 19. Wahlperiode\nZu Ziffer 10")
    spans = find_page_furniture(text)
    assert [span["kind"] for span in spans] == ["drucksache_running_header"]
    clean, _ = strip_page_furniture(text)
    assert clean == "Die Bundesregierung stimmt zu.\nZu Ziffer 10"


def test_ordinary_wording_is_untouched():
    text = ("In § 7 wird die Angabe „Bundesgesetzblatt I S. 12“ durch die "
            "Angabe „BGBl. I Nr. 5“ ersetzt.")
    assert strip_page_furniture(text) == (text, [])


def test_backfill_parser_strips_mid_line_headers():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
    from backfill_bgbl_history import parse_amendment_commands
    row = _row("bgbl-1-2023-406")
    commands, _ = parse_amendment_commands({
        "article": "1", "heading": "Änderung",
        "text": "Artikel 1\nÄnderung\n" + row["raw"]})
    assert commands
    assert all("Bundesgesetzblatt" not in command["raw"]
               for command in commands)


def test_broken_text_layers_of_printed_bgbl():
    """Real pre-2006 text layers split "am" across lines and put the page
    number on its own line before or after the header."""
    for document_id in ("bgbl-1-2004-s2198", "bgbl-1-2005-s837",
                        "bgbl-1-2004-s1950", "bgbl-1-2005-s721"):
        row = _row(document_id)
        clean, removed = strip_page_furniture(row["raw"])
        assert removed, document_id
        assert "Bundesgesetzblatt Jahrgang" not in clean, document_id
        assert "ausgegeben zu Bonn" not in clean, document_id
        for span in removed:
            assert row["raw"][span["start"]:span["end"]] == span["text"]
    clean, removed = strip_page_furniture(_row("bgbl-1-2004-s2198")["raw"])
    assert removed[0]["text"].startswith("2208\nBundesgesetzblatt")


def test_numbers_that_are_not_page_furniture_survive():
    text = ("1. § 3 wird aufgehoben.\n2208\n2. § 4 wird aufgehoben.")
    assert strip_page_furniture(text) == (text, [])
