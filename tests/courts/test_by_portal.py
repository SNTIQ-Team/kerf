"""BAYERN.RECHT decision pages (Wayback captures of gesetze-bayern.de) - the parser behind ingest-capture."""
import gzip
import re
from pathlib import Path

import pytest

from courts.paragraphs import assemble
from courts.sources import get_adapter
from courts.sources.base import Capture
from courts.sources.by_portal import (canonical_url, page_doc_ids, parse_portal_html, parse_portal_pdf,
                                      restate_norm_chain)
from courts.store import decision_identity, finalise

ROOT = Path(__file__).resolve().parents[2]
DECISIONS = ROOT / "conformance" / "fixtures" / "decisions"


def page(name: str) -> bytes:
    path = DECISIONS / f"bayern-recht-{name}"
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def item(doc: str) -> dict:
    return {"source_doc_id": f"Y-300-Z-BECKRS-{doc}", "url": f"https://www.gesetze-bayern.de/Content/Document/"
            f"Y-300-Z-BECKRS-{doc}", "extra": {"route": "portal_capture"}}


def parsed_html(doc: str):
    return finalise(parse_portal_html(page(f"{doc}.html.gz"), item(doc), item(doc)["url"]))


def test_bayvgh_urteil_identity_sections_and_rn():
    d = parsed_html("B-2023-N-10142")
    assert (d.court_id, d.court_name, d.decision_type, d.decision_date, d.case_numbers_raw) == (
        "vgh-by", "Bayerischer Verwaltungsgerichtshof", "Urteil", "2023-01-18", "16b D 22.2183")
    assert decision_identity(d) == "court:vgh-by/2023-01-18/16b_D_22.2183"
    assert [(s.name, s.authorship) for s in d.sections] == [
        ("titel", "editorial"), ("orientierungssatz", "editorial"), ("tenor", "court"), ("tatbestand", "court"),
        ("entscheidungsgruende", "court")]
    paras, numbering = assemble("D", d.sections)
    assert numbering == "rn"
    assert [p.rn for p in paras if p.rn] == [str(n) for n in range(1, 37)]
    tenor = [p for p in paras if p.section == "tenor"]
    assert [p.id for p in tenor] == [f"D#tenor-{n}" for n in range(1, 5)] and tenor[0].text.startswith("I. Das Urteil")
    assert d.metadata["numbering_source"] == "bayern_recht" and d.metadata["beckrs"] == "BeckRS 2023, 10142"
    assert d.procedural[0]["court"] == "Verwaltungsgericht Ansbach" and d.procedural[0]["date"] == "2018-10-16"
    assert "§ 61 Abs. 1 S. 3 BBG" in d.metadata["norms"]


def test_court_leitsatz_and_editorial_leitsatz_are_separate_sections():
    d = parsed_html("B-2023-N-10253")        # OLG Bamberg: three court Leitsätze, three redaktionelle
    sections = {s.name: s for s in d.sections}
    assert sections["leitsatz"].authorship == "court" and len(sections["leitsatz"].blocks) == 3
    assert sections["orientierungssatz"].authorship == "editorial" and len(sections["orientierungssatz"].blocks) == 3
    assert all("redaktioneller Leitsatz" in b.text for b in sections["orientierungssatz"].blocks)
    assert not any("redaktioneller Leitsatz" in b.text for b in sections["leitsatz"].blocks)


def test_court_names_as_the_portal_abbreviates_them():
    assert parsed_html("B-2023-N-10068").court_id == "lag-muenchen"     # "LArbG München"
    assert parsed_html("B-2025-N-12316").court_id == "vg-muenchen"
    assert parsed_html("B-2025-N-15010").court_id == "verfgh-by"        # "VerfGH München"


def test_definition_list_items_continue_the_rn_with_their_marker():
    d = parsed_html("B-2025-N-15010")
    paras, _ = assemble("D", d.sections)
    rn3 = next(p for p in paras if p.rn == "3")
    assert [b.text[:3] for b in rn3.blocks[2:5]] == ["1. ", "180", "2. "]   # after "Nach ihren Angaben …"


def test_image_only_blocks_are_reported_not_invented():
    d = parsed_html("B-2025-N-15353")
    assert len(d.metadata["images_omitted"]) == 6
    assert all(i["src"].startswith("/Content/Resource") for i in d.metadata["images_omitted"])


def test_subsequent_instance_without_date():
    proc = parsed_html("B-2025-N-13206").procedural
    assert proc[1] == {"direction": "subsequent", "court": "Bundesgerichtshof", "date": None,
                       "case_number": "XII ZB 279/25", "decision_type": None, "raw": "BGH vom -- – XII ZB 279/25",
                       "field": "BAYERN.RECHT:Rechtsmittelinstanz", "origin": "editorial_metadata"}


@pytest.mark.parametrize("doc", ["B-2023-N-10142", "B-2023-N-10080"])
def test_the_portal_pdf_yields_the_same_paragraph_ids(doc):
    html = parsed_html(doc)
    pdf = finalise(parse_portal_pdf(page(f"{doc}.pdf"), item(doc), item(doc)["url"]))
    a, _ = assemble("D", html.sections)
    b, _ = assemble("D", pdf.sections)
    assert [p.id for p in a] == [p.id for p in b]
    norm = lambda t: re.sub(r"[\s-]", "", t)  # noqa: E731 - line-end hyphens differ in the PDF
    assert sum(norm(x.text) != norm(y.text) for x, y in zip(a, b)) <= 1


def test_adapter_dispatches_portal_captures():
    body = page("B-2023-N-10142.html.gz")
    row = item("B-2023-N-10142") | {"extra": '{"route": "portal_capture", "capture_origin": "user_browser", '
                                             '"captured_at": "2026-09-28T10:00:00Z"}'}
    d = get_adapter("by").parse(Capture(body, row["url"], "text/html", "html"), row)
    assert d.metadata["capture_origin"] == "user_browser" and d.metadata["capture"]["captured_at"].startswith("2026")


def test_canonical_urls():
    doc = "Y-300-Z-BECKRS-B-2023-N-10142"
    for url in (f"https://www.gesetze-bayern.de/Content/Document/{doc}", f"https://gesetze-bayern.de/Content/Document/{doc}?hl=true",
                f"https://www.gesetze-bayern.de/Content/Pdf/{doc}?all=False"):
        assert canonical_url(url) == (doc, f"https://www.gesetze-bayern.de/Content/Document/{doc}")
    for url in ("https://www.gesetze-bayern.de/Search/Suggest?query=16b&suggester=azSuggest",
                "https://www.gesetze-bayern.de/Content/Document/BayBO", "https://openjur.de/u/123.html",
                "https://www.gesetze-bayern.de/Search/Hitlist"):
        with pytest.raises(ValueError):
            canonical_url(url)
    assert doc in page_doc_ids(page("B-2023-N-10142.html.gz"))


def test_norm_chains_are_restated_act_last():
    assert restate_norm_chain("BBG § 61 Abs. 1 S. 3, § 77 Abs. 1 S. 2") == ["§ 61 Abs. 1 S. 3 BBG", "§ 77 Abs. 1 S. 2 BBG"]
    assert restate_norm_chain("BayBO Art. 6 Abs. 1 S. 3, S. 4, Art. 63 Abs. 1 S. 1") == [
        "Art. 6 Abs. 1 S. 3, S. 4 BayBO", "Art. 63 Abs. 1 S. 1 BayBO"]
