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
    assert len(verfgh) == 245 and verfgh[0]["listed_date"] == "2026-07-31"
    assert verfgh[0]["url"] == "https://www.bayern.verfassungsgerichtshof.de/media/images/bayverfgh/15-vii-25_entscheidung.pdf"
    press = parse_vgh_press(fixture("by/vgh_pm_index.html.gz").decode("utf-8"), "bayvgh")
    assert press[0]["listed_court"] == "vgh-by" and press[0]["priority"] == 0
    assert any(p["listed_case_numbers"] == "10 ZB 24.2079" for p in press)
    assert all("%" in p["url"] or p["url"].isascii() for p in press)  # umlauts percent-encoded


def test_paragraph_split_by_page_break_is_rejoined_without_rn():
    text = ("Tatbestand:\n\nDie Klägerin begehrt die Zahlung von Mehrarbeits-\nzuschlägen für die Monate\n"
            "2\n\f7 SLa 272/25\n\n                 -3-\n\nJanuar bis März 2024.\n\nDer Beklagte beantragt Abweisung.\n"
            "\n\f   Seite 4/9\n\naa) Neuer Gliederungspunkt beginnt.\n")
    blocks = [b for b in layout_blocks(text, running_headers={"7 SLa 272/25"}, headings={"tatbestand": "tatbestand"})
              if b.kind != "pagebreak"]
    texts = [b.text for b in blocks]
    assert texts == ["Tatbestand:",
                     "Die Klägerin begehrt die Zahlung von Mehrarbeitszuschlägen für die Monate Januar bis März 2024.",
                     "Der Beklagte beantragt Abweisung.", "aa) Neuer Gliederungspunkt beginnt."]
    assert all(b.label is None for b in blocks)


def test_lag_list_attributes_first_instance_entries_to_the_arbg():
    from courts.sources.by import parse_lag_list
    markup = ('<div class="item odd"><div class="info"><h2>ArbG München - 6 BV 41/19</h2><span><strong>12.03.2021'
              '</strong></span><br><p><a href="/die-arbeitsgerichtsbarkeit-in-bayern/lag-muenchen/entscheidungen/neue/'
              '46586/index.php">x</a></p></div></div><div class="item even"><div class="info"><h2>4 Sa 871/20</h2>'
              '<span><strong>01.02.2021</strong></span><br><p><a href="/die-arbeitsgerichtsbarkeit-in-bayern/'
              'lag-muenchen/entscheidungen/neue/46590/index.php">y</a></p></div></div>')
    first, second = parse_lag_list(markup, "muenchen")
    assert (first["listed_court"], first["listed_case_numbers"]) == ("arbg-muenchen", "6 BV 41/19")
    assert first["extra"]["court_name"] == "Arbeitsgericht München"
    assert (second["listed_court"], second["listed_case_numbers"]) == ("lag-muenchen", "4 Sa 871/20")


def test_wrong_list_date_is_tolerated_only_when_the_docket_matches_the_file(fixture):
    from courts.errors import UpstreamFailure
    wrong = dict(ITEM, listed_date="2008-08-27")
    d = parse_pdf_decision(fixture(PDF), wrong, "https://www.bayern.verfassungsgerichtshof.de/media/images/"
                                                "bayverfgh/15-vii-25_entscheidung.pdf")
    assert d.decision_date == "2026-07-31"
    assert d.metadata["listed_date_conflict"] == {"listed": "2008-08-27", "document": "2026-07-31"}
    with pytest.raises(UpstreamFailure) as err:
        parse_pdf_decision(fixture(PDF), wrong, "https://www.bayern.verfassungsgerichtshof.de/media/images/"
                                                "bayverfgh/16-vii-25_entscheidung.pdf")
    assert err.value.kind == "identity_mismatch"


def test_verfgh_dates_and_nested_citations_stay_in_source_entry():
    from courts.sources.by import parse_verfgh_list
    listing = """
    <li class="pdf"><a href="/media/images/bayverfgh/one.pdf">First</a>
      <span>1. August 1997<br>VerfGHE 50, 156</span></li>
    <li class="pdf"><a href="/media/images/bayverfgh/two.pdf">Second</a>
      <span>2. August 1997</span></li>
    """
    assert [(i["extra"]["title"], i["listed_date"]) for i in parse_verfgh_list(listing)] == [
        ("First", "1997-08-01"), ("Second", "1997-08-02")]

def test_historical_verfgh_short_title_must_match_directory_date(monkeypatch):
    from courts.sources import by
    text = "Vf. 2-VII-00\n\nEntscheidung vom 4. Juli 2001\n\nAktenzeichen: Vf. 2-VII-00\n    Vf. 3-VII-00\n\nGründe :\n\nDie Popularklage ist zulässig."
    monkeypatch.setattr(by, "pdf_text", lambda pdf, url: text)
    item = dict(ITEM, listed_date="2001-07-04", source_doc_id="verfgh:/media/images/bayverfgh/2-vii-00.pdf")
    url = by.VERFGH + "/media/images/bayverfgh/2-vii-00.pdf"
    decision = by.parse_pdf_decision(b"fixture", item, url)
    assert decision.decision_date == "2001-07-04"
    assert decision.case_numbers == ["Vf. 2-VII-00", "Vf. 3-VII-00"]
    with pytest.raises(by.UpstreamFailure, match="without decision date"):
        by.parse_pdf_decision(b"fixture", dict(item, listed_date="2001-07-05"), url)
