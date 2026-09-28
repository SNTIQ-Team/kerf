"""Regression: BVerwG 16.03.2016 - 1 B 19.16 (RII jb-WBRE201600193, real capture).

Rn. 10 consists of three blocks (the two following blocks have an empty
<dt/>).  The quote "Streitgegenstand … Mitteilung" is in the third block and
must resolve to Rn. 10, not Rn. 11.
"""
import pytest

from courts.paragraphs import assemble, locate_quote
from courts.sources.base import Capture
from courts.sources.rii import RiiAdapter, parse_toc

DOC = "jb-WBRE201600193"
ITEM = {"source_doc_id": DOC, "url": f"https://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/{DOC}.zip",
        "listed_date": "2016-03-16", "listed_court": "bverwg"}
QUOTE = ("Streitgegenstand … ist nicht eine Untätigkeitsklage gemäß § 75 VwGO, sondern lediglich eine "
         "Klage auf Erteilung einer Mitteilung")


@pytest.fixture
def parsed(fixture):
    body = fixture(f"rii/{DOC}.zip")
    return RiiAdapter().parse(Capture(body, ITEM["url"], "application/zip", "zip"), ITEM)


@pytest.fixture
def paragraphs(parsed):
    paras, numbering = assemble(parsed.ecli, parsed.sections)
    assert numbering == "rn"
    return {p.id.split("#", 1)[1]: p for p in paras}


def test_rn10_has_three_blocks_and_rn11_one(paragraphs):
    assert len(paragraphs["rn-10"].blocks) == 3
    assert len(paragraphs["rn-11"].blocks) == 1
    assert paragraphs["rn-10"].blocks[1].text.startswith('"inwieweit die ausreichende Mitteilung')
    assert paragraphs["rn-10"].blocks[2].text.startswith("rechtfertigt diese ebenfalls nicht")


def test_quote_resolves_to_rn10_not_rn11(parsed):
    paras, _ = assemble(parsed.ecli, parsed.sections)
    assert locate_quote(paras, QUOTE) == [f"{parsed.ecli}#rn-10"]


def test_rn6_absorbs_three_unnumbered_blocks(paragraphs):
    assert len(paragraphs["rn-6"].blocks) == 4


def test_unnumbered_heading_before_first_rn_gets_stable_positional_id(paragraphs):
    assert paragraphs["gruende-u1"].text == "I"
    assert paragraphs["gruende-u1"].rn is None
    # "II" between Rn 2 and Rn 3 continues Rn 2 (spec: unnumbered blocks continue the previous Rn)
    assert paragraphs["rn-2"].blocks[-1].text == "II"


def test_rn_sequence_complete(paragraphs):
    assert [k for k in paragraphs if k.startswith("rn-")] == [f"rn-{n}" for n in range(1, 13)]


def test_identity_and_metadata(parsed):
    assert parsed.ecli == "ECLI:DE:BVerwG:2016:160316B1B19.16.0"
    assert parsed.case_numbers_raw == "1 B 19/16, 1 B 19/16, 1 PKH 55/16"
    assert parsed.case_numbers == ["1 B 19/16", "1 PKH 55/16"]
    assert parsed.court_id == "bverwg" and parsed.jurisdiction == "verwaltung"
    assert parsed.decision_type == "Beschluss" and parsed.decision_date == "2016-03-16"
    assert parsed.metadata["norms"] == ["§ 24 Abs 4 AsylVfG 1992"]
    assert [(p["court"], p["case_number"]) for p in parsed.procedural] == [
        ("Verwaltungsgerichtshof Baden-Württemberg", "A 11 S 490/15"), ("VG Stuttgart", "A 6 K 3840/14")]


def test_sections_separate_editorial_title_from_court_reasons(parsed):
    by_name = {s.name: s for s in parsed.sections}
    assert by_name["titel"].authorship == "editorial"
    assert by_name["gruende"].authorship == "court"
    assert "tenor" not in by_name  # empty <tenor/> is not a section


def test_identity_mismatch_is_typed(fixture):
    from courts.errors import UpstreamFailure
    body = fixture(f"rii/{DOC}.zip")
    wrong = dict(ITEM, listed_date="2016-03-17")
    with pytest.raises(UpstreamFailure) as err:
        RiiAdapter().parse(Capture(body, ITEM["url"], "application/zip", "zip"), wrong)
    assert err.value.kind == "identity_mismatch"


def test_toc_parsing_and_priority():
    toc = (b"<?xml version='1.0'?><!DOCTYPE items [<!ELEMENT items (item)*>]><items>"
           b"<item><gericht>BVerwG 1. Senat</gericht><entsch-datum>20160316</entsch-datum>"
           b"<aktenzeichen>1 B 19/16, 1 B 19/16, 1 PKH 55/16</aktenzeichen>"
           b"<link>http://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/jb-WBRE201600193.zip</link>"
           b"<modified>2025-06-23T21:55:54.378Z</modified></item>"
           b"<item><gericht>BGH 9. Zivilsenat</gericht><entsch-datum>20100114</entsch-datum>"
           b"<aktenzeichen>IX ZB 72/08</aktenzeichen>"
           b"<link>http://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/jb-JURE100055033.zip</link>"
           b"<modified>2026-04-14T21:14:20.772Z</modified></item></items>")
    items = parse_toc(toc)
    assert items[0]["url"].startswith("https://") and items[0]["listed_court"] == "bverwg"
    assert items[0]["priority"] == 0 and items[1]["priority"] == 1
    assert items[0]["listed_modified"] == "2025-06-23T21:55:54.378Z"
