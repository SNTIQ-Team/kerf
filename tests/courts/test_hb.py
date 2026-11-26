"""Bremen adapter (OVG/VG/OLG websites, PDF decisions) on real captured lists and PDFs, offline."""
import re
import shutil

import pytest

from courts import http as http_module
from courts.errors import UpstreamFailure
from courts.http import PoliteHttp
from courts.paragraphs import assemble, locate_quote
from courts.sources import get_adapter
from courts.sources.base import Capture
from courts.sources.hb import COURT_BY_KEY, PAGE_NO, BremenAdapter, parse_list
from courts.store import connect, init_db, store_decision

pytestmark = pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler pdftotext not installed")
FURNITURE = re.compile(r"^(?:-?\s*\d{1,3}\s*-?|Seite\s+\d+\s*(?:/|von)\s*\d+)$")


def listed(fixture, court, name, az):
    _, items = parse_list(fixture(f"hb/{name}.html.gz").decode(), COURT_BY_KEY[court])
    return next(i for i in items if i["listed_case_numbers"] == az)


def parse_pdf(fixture, pdf, item):
    return BremenAdapter().parse(Capture(fixture(f"hb/{pdf}"), item["url"], "application/pdf", "pdf", {}), item)


@pytest.fixture
def vg_item(fixture):
    return listed(fixture, "vg", "list-vg-max20-skip0", "2 K 1343/24")


@pytest.fixture
def olg_item(fixture):
    return listed(fixture, "olg", "list-olg-max20-skip0", "1 W 58/25")


def test_registered():
    assert isinstance(get_adapter("hb"), BremenAdapter)


def test_list_parse(fixture, vg_item, olg_item):
    total, items = parse_list(fixture("hb/list-vg-max20-skip0.html.gz").decode(), COURT_BY_KEY["vg"])
    assert total == 818 and len(items) == 20
    assert [i["listed_date"] for i in items] == sorted((i["listed_date"] for i in items), reverse=True)
    assert vg_item["source_doc_id"] == "bremen73.c.27121"
    assert vg_item["url"] == "https://www.verwaltungsgericht.bremen.de/sixcms/media.php/13/24_1343_K_2%20.pdf"
    assert (vg_item["listed_date"], vg_item["listed_court"], vg_item["priority"]) == ("2026-08-26", "vg-bremen", 0)
    assert vg_item["extra"]["decision_type"] == "Urteil" and vg_item["extra"]["rechtsgebiet"] == "Ausländerrecht"
    assert vg_item["extra"]["norms"] == ["AufenthG § 104c", "BremAufenthZVO § 3 Abs 4", "VwGO § 75", "VwVfG § 35"]
    assert vg_item["extra"]["detail_url"].endswith("detail.php?gsid=bremen73.c.27121.de&asl=bremen73.c.13039.de")
    assert (olg_item["listed_court"], olg_item["priority"], olg_item["source_doc_id"]) == (
        "olg-bremen", 1, "bremen88.c.25847")
    assert olg_item["url"].endswith("/1-W-25-058%20anonymisiert.pdf")


class ListHttp:
    def __init__(self, fixture):
        self.fixture, self.urls, self.requests_made = fixture, [], 0

    def get(self, url, **kw):
        self.urls.append(url)
        self.requests_made += 1
        name = {"11265": "list-ovg-max20-skip20", "13039": "list-vg-max20-skip0",
                "2335": "list-olg-max20-skip0"}[re.search(r"uebersicht-(\d+)", url).group(1)]
        return http_module.Response(url=url, status=200, body=self.fixture(f"hb/{name}.html.gz"),
                                    headers={"content-type": "text/html; charset=utf-8"}, elapsed=0.0)


def test_list_items_limit_admin_courts_first(fixture):
    fake = ListHttp(fixture)
    listing = BremenAdapter().list_items(fake, None, since=None, limit=5, courts=None)
    assert fake.urls == ["https://www.oberverwaltungsgericht.bremen.de/entscheidungen/"
                         "entscheidungsuebersicht-11265?max=20&skip=0"]      # parameter order: max, then skip
    assert len(listing.items) == 20 and listing.complete is False


def test_list_items_since_stops_paging(fixture):
    fake = ListHttp(fixture)
    listing = BremenAdapter().list_items(fake, None, since="2026-08-18", limit=None, courts=None)
    # newest first: a page reaching below `since` ends that court's listing (OVG page starts at 2026-08-04)
    assert [u.split("?")[1] for u in fake.urls] == ["max=100&skip=0"] * 3
    assert all(i["listed_date"] >= "2026-08-18" for i in listing.items)
    assert [i["listed_court"] for i in listing.items] == ["vg-bremen"] * 19 + ["olg-bremen"] * 2
    assert [i["priority"] for i in listing.items] == sorted(i["priority"] for i in listing.items)
    assert listing.complete is False and listing.upstream_total is None


def test_vg_urteil_sections_and_page_breaks(fixture, vg_item):
    parsed = parse_pdf(fixture, "vg-2_K_1343-24.pdf", vg_item)
    assert parsed.court_name == "Verwaltungsgericht der Freien Hansestadt Bremen"
    assert (parsed.court_id, parsed.jurisdiction, parsed.land) == ("vg-bremen", "verwaltung", "hb")
    assert parsed.decision_date == "2026-08-26" and parsed.decision_type == "Urteil"
    assert parsed.case_numbers_raw == "2 K 1343/24" and parsed.case_numbers == ["2 K 1343/24"]
    assert parsed.ecli is None
    assert [(s.name, s.authorship, s.heading) for s in parsed.sections] == [
        ("tenor", "court", None), ("tatbestand", "court", "Tatbestand"),
        ("entscheidungsgruende", "court", "Entscheidungsgründe")]
    assert parsed.metadata["identity"]["date_in_rubrum"] == "2026-08-26"
    assert parsed.metadata["pdf"]["pages"] == 7 and parsed.metadata["pdf"]["page_break_joins"] == 3
    assert all(b.label is None for s in parsed.sections for b in s.blocks)

    paragraphs, numbering = assemble("D", parsed.sections)
    assert numbering == "none"
    assert [p.id for p in paragraphs][:4] == ["D#tenor-1", "D#tenor-2", "D#tenor-3", "D#p-1"]
    body = [p for p in paragraphs if p.section != "tenor"]
    assert [p.id for p in body] == [f"D#p-{n}" for n in range(1, len(body) + 1)]
    # page numbers never become (or end) a paragraph
    assert not any(FURNITURE.match(p.text) or PAGE_NO.fullmatch(p.text) for p in paragraphs)
    assert not any(re.search(r"(?:^|\s)[2-7]\s*$", p.text) and len(p.text) < 4 for p in paragraphs)
    # a sentence running over the break from page 3 to page 4 stays in one paragraph
    hit = locate_quote(paragraphs, "Es fehlen zureichende Anhaltspunkte für die Existenz einer konkludenten "
                                   "Ablehnung")
    assert hit == ["D#p-15"]
    para = next(p for p in paragraphs if p.id == "D#p-15")
    assert para.blocks[0].locator == "pdf:p3:l44-p4:l5"
    # the tenor sentence and the heading "Tatbestand" on the next page stay apart
    assert paragraphs[2].text.endswith("des jeweils zu vollstreckenden Betrages leistet.")
    assert paragraphs[3].text == "Der Kläger begehrt die Erteilung einer Aufenthaltserlaubnis."
    assert parsed.metadata["norms"] == ["AufenthG § 104c", "BremAufenthZVO § 3 Abs 4", "VwGO § 75", "VwVfG § 35"]


def test_olg_footer_is_dropped_and_cross_page_quote(fixture, olg_item):
    parsed = parse_pdf(fixture, "olg-1_W_58-25.pdf", olg_item)
    assert (parsed.court_id, parsed.court_name) == ("olg-bremen", "Hanseatisches Oberlandesgericht in Bremen")
    assert parsed.decision_date == "2025-11-19" and parsed.case_numbers == ["1 W 58/25"]
    paragraphs, numbering = assemble("D", parsed.sections)
    assert numbering == "none"
    assert not any("Seite 2/3" in p.text or "Seite 3/3" in p.text for p in paragraphs)
    assert parsed.metadata["pdf"]["page_furniture_lines_dropped"] == 2
    ids = locate_quote(paragraphs, "weniger als 70 Kilometer betragen würde und eine mit der Anreise verbundene "
                                   "besondere Erschwernis für die Zeugen der Akte nicht zu entnehmen sei")
    assert ids == ["D#p-3"]
    assert next(p for p in paragraphs if p.id == "D#p-3").blocks[0].locator == "pdf:p1:l40-p2:l4"
    heading = next(p for p in paragraphs if p.id == "D#p-1")
    assert heading.text == "I." and heading.blocks[0].kind == "heading"


@pytest.mark.parametrize("over,detail", [
    ({"listed_case_numbers": "2 K 1344/24"}, "not found in the PDF"),
    ({"listed_date": "2026-08-27"}, "listed date"),
])
def test_identity_is_validated(fixture, vg_item, over, detail):
    with pytest.raises(UpstreamFailure) as err:
        parse_pdf(fixture, "vg-2_K_1343-24.pdf", vg_item | over)
    assert err.value.kind == "identity_mismatch" and detail in err.value.detail


def test_stored_decision(fixture, vg_item, tmp_path):
    conn = connect(tmp_path / "c.sqlite")
    init_db(conn)
    decision_id, _ = store_decision(conn, parse_pdf(fixture, "vg-2_K_1343-24.pdf", vg_item), content_hash="x",
                                    capture_path="raw/x", media_type="application/pdf")
    row = conn.execute("SELECT numbering, paragraph_count, court_id FROM decisions WHERE id=?",
                       (decision_id,)).fetchone()
    assert (row["numbering"], row["paragraph_count"], row["court_id"]) == ("none", 38, "vg-bremen")


def test_html_instead_of_pdf_is_typed(vg_item):
    with pytest.raises(UpstreamFailure) as err:
        BremenAdapter().parse(Capture(b"<html><body>Fehler</body></html>", vg_item["url"], "text/html", "html"),
                              vg_item)
    assert err.value.kind == "unexpected_representation"


class FakeResponse:
    def __init__(self, status, body, headers, url):
        self.status_code, self.content, self.headers, self.url = status, body, headers, url


class ChallengeSession:
    headers: dict = {}

    def request(self, method, url, **kw):
        body = (b"<html><head><title>Just a moment...</title></head><body><div class='cf-chl-widget'>"
                b"</div></body></html>")
        return FakeResponse(503, body, {"Content-Type": "text/html"}, url)


def test_challenge_instead_of_pdf_is_typed(vg_item):
    http = PoliteHttp(respect_robots=False, session=ChallengeSession(), min_interval=1.0)
    with pytest.raises(UpstreamFailure) as err:
        BremenAdapter().fetch(http, vg_item)
    assert err.value.kind == "challenge"


def test_bremen_pdf_form_keeps_a_conflicting_listing_for_inspection(fixture, vg_item):
    item = {**vg_item, "extra": {**vg_item["extra"], "decision_type": "Beschluss"}}
    parsed = parse_pdf(fixture, "vg-2_K_1343-24.pdf", item)
    assert parsed.decision_type == "Urteil"
    assert parsed.metadata["decision_type_evidence"]["catalog"] == "Beschluss"


def test_cover_form_requires_a_date_and_docket_and_keeps_specific_catalogue_terms():
    from courts.sources.pdftext import decision_form_from_cover

    cover = "Verwaltungsgericht Bremen\nAz. 5 K 123/20\nB e s c h l u s s\nIn der Verwaltungsrechtssache\nam 21. März 2021 beschlossen:\nUrteil\n"
    assert decision_form_from_cover(cover, "5 K 123/20", "2021-03-21", "Urteil", {}) == "Beschluss"
    with pytest.raises(UpstreamFailure, match="2021-03-21"):
        decision_form_from_cover(cover, "5 K 123/20", "2020-03-21", "Urteil", {})
    assert decision_form_from_cover(cover, "5 K 321/20", "2021-03-21", "Urteil", {}) == "Urteil"
    assert decision_form_from_cover(cover.replace("5 K", "15 K"), "5 K 123/20", "2021-03-21", "Urteil", {}) == "Urteil"
    assert decision_form_from_cover(cover, "5 K 123/20", "2021-03-21", "Streitwertbeschluss", {}) == "Streitwertbeschluss"


def test_cover_form_uses_the_pronouncement_and_the_primary_docket():
    from courts.sources.pdftext import decision_form_from_cover

    cover = ("Geschäftszeichen: 3 U 35/13 = 6 O 1985/12 Landgericht Bremen\nVerkündet am: 6. Februar 2014\n"
             "Urteil\nIn dem Rechtsstreit\naufgrund der mündlichen Verhandlung vom 15. Januar 2014\n")
    assert decision_form_from_cover(cover, "6 O 1985/12", "2014-02-06", "Beschluss", {}) == "Beschluss"
    with pytest.raises(UpstreamFailure, match="2014-02-06"):
        decision_form_from_cover(cover, "3 U 35/13", "2014-01-15", "Beschluss", {})
    assert decision_form_from_cover(cover, "3 U 35/13", "2014-02-06", "Beschluss", {}) == "Urteil"
