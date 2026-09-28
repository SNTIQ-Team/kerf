"""Bavaria: court PDFs (real captures) — page breaks never create or split a Randnummer."""
import pytest

from courts.paragraphs import assemble, locate_quote
from courts.sources.by import parse_pdf_decision, parse_verfgh_list, parse_vgh_press
from courts.sources.pdftext import layout_blocks, pdf_text
from courts.store import finalise

PDF = "by/verfgh_15-vii-25_entscheidung.pdf"
ITEM = {"source_doc_id": "verfgh:/media/images/bayverfgh/15-vii-25_entscheidung.pdf", "listed_date": "2026-07-31",
        "listed_case_numbers": None,
        "extra": {"route": "verfgh", "court_name": "Bayerischer Verfassungsgerichtshof", "title": "Popularklage"}}


@pytest.fixture
def decision(fixture):
    return finalise(parse_pdf_decision(fixture(PDF), dict(ITEM), "https://www.bayern.verfassungsgerichtshof.de/x.pdf"))


@pytest.fixture
def paragraphs(decision):
    paras, numbering = assemble("D", decision.sections)
    assert numbering == "rn"
    return paras


def test_metadata(decision):
    assert decision.court_id == "verfgh-by" and decision.land == "by"
    assert decision.case_numbers_raw == "Vf. 15-VII-25" and decision.case_numbers == ["Vf. 15-VII-25"]
    assert decision.decision_date == "2026-07-31" and decision.ecli is None
    assert [s.name for s in decision.sections] == ["leitsatz", "tenor", "gruende"]


def test_page_markers_never_become_randnummern(fixture, paragraphs):
    text = pdf_text(fixture(PDF))
    assert "-3-" in text  # the capture really contains page markers
    rns = [p.rn for p in paragraphs if p.rn]
    assert rns == [str(n) for n in range(1, len(rns) + 1)]
    assert all("-3-" not in p.text and "-4-" not in p.text for p in paragraphs)
    blocks = layout_blocks(text)
    assert any(b.kind == "pagebreak" for b in blocks)


def test_rn_continues_across_page_break(paragraphs):
    # Rn 2 quotes Art. 82 BayBO; its last line is on the next page.
    assert locate_quote(paragraphs, "Voraussetzung ist, dass der Wald bereits am 16. November 2022 bestanden hat") \
        == ["D#rn-2"]
    # Rn 4: the word "Landschaft" is hyphenated across a page break.
    assert locate_quote(paragraphs, "natürliche Eigenart der Landschaft und ihren Erholungswert") == ["D#rn-4"]
    assert locate_quote(paragraphs, "Art. 82 Abs. 5 wurde durch § 1 Nr. 2 Buchst. b") == ["D#rn-3"]


def test_justified_numbers_are_not_mistaken_for_rn():
    text = "Gründe:\n\n1   Erster Absatz.\n\n12  Monate später geschah etwas.\n\n2   Zweiter Absatz.\n"
    blocks = layout_blocks(text, headings={"gründe": "gruende"})
    assert [b.label for b in blocks if b.label] == ["1", "2"]


def test_lists(fixture):
    verfgh = parse_verfgh_list(fixture("by/verfgh_ausgewaehlte.html.gz").decode("utf-8"))
    assert len(verfgh) == 121 and verfgh[0]["listed_date"] == "2026-07-31"
    assert verfgh[0]["url"] == "https://www.bayern.verfassungsgerichtshof.de/media/images/bayverfgh/15-vii-25_entscheidung.pdf"
    press = parse_vgh_press(fixture("by/vgh_pm_index.html.gz").decode("utf-8"), "bayvgh")
    assert press[0]["listed_court"] == "vgh-by" and press[0]["priority"] == 0
    assert any(p["listed_case_numbers"] == "10 ZB 24.2079" for p in press)
    assert all("%" in p["url"] or p["url"].isascii() for p in press)  # umlauts percent-encoded
