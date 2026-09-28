"""Bundessozialgericht (bsg.bund.de): real captured pages, offline."""
import pytest

from courts import http as http_module
from courts.errors import UpstreamFailure
from courts.http import PoliteHttp, Response
from courts.paragraphs import assemble, locate_quote
from courts.sources import bsg
from courts.sources.base import Capture
from courts.sources.bsg import OVERVIEW_URL, BsgAdapter, parse_decision_html, parse_overview
from courts.store import connect, finalise, init_db, store_decision

URL = "https://www.bsg.bund.de/SharedDocs/Entscheidungen/DE/{}/{}.html"
OVERVIEW_2026 = "bsg/verhandlungstermine_uebersicht_2026.html.gz"


def item(doc, date, az, extra=None):
    return {"source_doc_id": doc, "url": URL.format(doc[:4], doc), "listed_date": date, "listed_court": "bsg",
            "listed_case_numbers": az, "listed_modified": None, "priority": 0,
            "extra": extra if extra is not None else {"senat": "4. Senat", "sachgebiete": ["Grundsicherung"]}}


def capture(fixture, doc):
    return Capture(fixture(f"bsg/{doc}.html.gz"), URL.format(doc[:4], doc), "text/html", "html",
                   {"content-type": "text/html;charset=utf-8"})


@pytest.fixture
def urteil(fixture):
    doc = "2026_06_18_B_04_AS_10_25_R"
    return finalise(BsgAdapter().parse(capture(fixture, doc), item(doc, "2026-06-18", "B 4 AS 10/25 R")))


def test_metadata(urteil):
    d = urteil
    assert (d.court_name, d.court_id, d.jurisdiction, d.land) == ("Bundessozialgericht", "bsg", "sozial", "de")
    assert d.decision_date == "2026-06-18" and d.decision_type == "Urteil"
    assert d.case_numbers_raw == "B 4 AS 10/25 R" and d.case_numbers == ["B 4 AS 10/25 R"]
    assert d.ecli == "ECLI:DE:BSG:2026:180626UB4AS1025R0"
    assert d.chamber == "4. Senat"
    assert d.title.startswith("Grundsicherung für Arbeitsuchende - Bürgergeld - britischer Staatsangehöriger")
    assert d.metadata["norms"] == [] and d.procedural == []
    assert d.metadata["pdf_version"] == "3" and d.metadata["sachgebiete"] == ["Grundsicherung"]


def test_sections_and_authorship(urteil):
    assert [(s.name, s.authorship) for s in urteil.sections] == [
        ("tenor", "court"), ("tatbestand", "court"), ("entscheidungsgruende", "court")]
    assert [b.text for b in urteil.sections[0].blocks] == [
        "Auf die Revision des Beklagten werden das Urteil des Landessozialgerichts Berlin-Brandenburg vom 18. März 2025 "
        "aufgehoben, der Gerichtsbescheid des Sozialgerichts Berlin vom 9. April 2024 abgeändert und die Klage insgesamt "
        "abgewiesen.",
        "Die Beteiligten haben einander außergerichtliche Kosten des Rechtsstreits in allen Rechtszügen nicht zu erstatten."]
    # <br/> inside a paragraph is a line break, abbreviations keep their published text.
    antrag = next(b.text for b in urteil.sections[1].blocks if b.text.startswith("Der Beklagte beantragt"))
    assert antrag.startswith("Der Beklagte beantragt,\ndas Urteil des Landessozialgerichts")


def test_no_randnummern_are_invented(urteil):
    assert all(b.label is None for s in urteil.sections for b in s.blocks)
    paragraphs, numbering = assemble(urteil.ecli, urteil.sections)
    assert numbering == "none" and all(p.rn is None for p in paragraphs)
    assert [p.id.split("#")[1] for p in paragraphs[:4]] == ["tenor-1", "tenor-2", "p-1", "p-2"]
    hit = locate_quote(paragraphs, "Die Kostenentscheidung beruht auf § 193 Abs 1 Satz 1, Abs 4 SGG.")
    assert hit == [urteil.ecli + "#p-48"]
    last = next(p for p in paragraphs if p.id == hit[0])
    assert last.section == "entscheidungsgruende" and last.text.startswith("D. Die Kostenentscheidung")
    quote = "Der Kläger ist Staatsangehöriger des Vereinigten Königreichs Großbritannien und Nordirland."
    assert locate_quote(paragraphs, quote) == [urteil.ecli + "#p-2"]


def test_file_name_typo_is_not_an_identity_mismatch(fixture):
    """The overview links '..._18_23_B.html' with text 'B 2 U 18/23 R'; the page is that Urteil."""
    doc = "2026_03_24_B_02_U_18_23_B"
    d = BsgAdapter().parse(capture(fixture, doc), item(doc, "2026-03-24", "B 2 U 18/23 R", extra={}))
    assert (d.case_numbers, d.decision_type, d.chamber) == (["B 2 U 18/23 R"], "Urteil", "2. Senat")
    assert d.ecli == "ECLI:DE:BSG:2026:240326UB2U1823R0"


@pytest.mark.parametrize("date,az", [("2026-06-19", "B 4 AS 10/25 R"), ("2026-06-18", "B 4 AS 32/24 R")])
def test_identity_mismatch(fixture, date, az):
    doc = "2026_06_18_B_04_AS_10_25_R"
    with pytest.raises(UpstreamFailure) as err:
        BsgAdapter().parse(capture(fixture, doc), item(doc, date, az))
    assert err.value.kind == "identity_mismatch"


def test_overview_listing(fixture):
    items = parse_overview(fixture(OVERVIEW_2026), OVERVIEW_URL.format(year=2026))
    assert len(items) == 52
    first = items[0]
    assert first["source_doc_id"] == "2026_06_18_B_04_AS_10_25_R"
    assert first["url"] == URL.format("2026", "2026_06_18_B_04_AS_10_25_R")
    assert (first["listed_date"], first["listed_court"], first["listed_case_numbers"]) == ("2026-06-18", "bsg",
                                                                                           "B 4 AS 10/25 R")
    assert first["priority"] == 0 and first["listed_modified"] is None
    assert first["extra"]["senat"] == "4. Senat" and first["extra"]["sachgebiete"] == ["Grundsicherung für Arbeitsuchende"]
    ueg = next(i for i in items if i["listed_case_numbers"] == "B 10 ÜG 3/24 R")
    assert ueg["source_doc_id"] == "2026_03_26_B_10_ÜG_03_24_R" and "%C3%9CG" in ueg["url"]
    typo = next(i for i in items if i["source_doc_id"].endswith("_B"))
    assert typo["listed_case_numbers"] == "B 2 U 18/23 R"
    # hearings without published decisions (Verhandlungen links) are not items
    assert all("/Entscheidungen/DE/" in i["url"] for i in items)


class YearHttp:
    def __init__(self, fixture):
        self.fixture, self.urls, self.requests_made = fixture, [], 0

    def get(self, url, **kwargs):
        self.urls.append(url)
        return Response(url, 200, self.fixture(OVERVIEW_2026), {"content-type": "text/html"}, 0.0)


@pytest.fixture
def year_2026(monkeypatch):
    monkeypatch.setattr(bsg, "current_year", lambda: 2026)


def test_list_items_limit_fetches_only_the_current_year(fixture, year_2026):
    http = YearHttp(fixture)
    conn = connect()
    init_db(conn)
    listing = BsgAdapter().list_items(http, conn, since=None, limit=2, courts=None)
    assert http.urls == [OVERVIEW_URL.format(year=2026)] and listing.complete is False
    dates = [i["listed_date"] for i in listing.items]
    assert dates == sorted(dates, reverse=True) and dates[0] == "2026-06-18"


def test_list_items_since_stops_at_its_year(fixture, year_2026):
    http = YearHttp(fixture)
    listing = BsgAdapter().list_items(http, None, since="2026-05-01", limit=None, courts=None)
    assert http.urls == [OVERVIEW_URL.format(year=2026)]
    assert listing.items and all(i["listed_date"] >= "2026-05-01" for i in listing.items)


def test_list_items_goes_back_a_year_when_current_items_are_stored(fixture, year_2026):
    http = YearHttp(fixture)
    conn = connect()
    init_db(conn)
    stored = parse_overview(fixture(OVERVIEW_2026), "x")
    conn.executemany("INSERT INTO source_items(source_id, source_doc_id, url, state) VALUES('bsg', ?, ?, 'stored')",
                     [(i["source_doc_id"], i["url"]) for i in stored])
    conn.commit()
    BsgAdapter().list_items(http, conn, since=None, limit=2, courts=None)
    assert http.urls[:2] == [OVERVIEW_URL.format(year=2026), OVERVIEW_URL.format(year=2025)]


def test_court_filter_excludes_bsg():
    assert BsgAdapter().list_items(None, None, since=None, limit=None, courts=["bverwg"]).items == []


CHALLENGE = (b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
             b"<div id='cf-chl-widget'>Checking your browser</div></body></html>")


class FakeResponse:
    def __init__(self, status, body, url):
        self.status_code, self.content, self.url = status, body, url
        self.headers = {"Content-Type": "text/html;charset=utf-8"}


class FakeSession:
    def __init__(self, status, body):
        self.headers, self.status, self.body, self.calls = {}, status, body, []

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append(url)
        return FakeResponse(self.status, self.body, url)


@pytest.mark.parametrize("status", [200, 403])
def test_challenge_is_a_typed_failure_not_a_decision(monkeypatch, status):
    monkeypatch.setattr(http_module.time, "sleep", lambda s: None)
    doc = "2026_06_18_B_04_AS_10_25_R"
    http = PoliteHttp(session=FakeSession(status, CHALLENGE), respect_robots=False)
    with pytest.raises(UpstreamFailure) as err:
        BsgAdapter().fetch(http, item(doc, "2026-06-18", "B 4 AS 10/25 R"))
    assert err.value.kind == "challenge"
    with pytest.raises(UpstreamFailure) as err:
        BsgAdapter().parse(Capture(CHALLENGE, "x", "text/html", "html", {}), item(doc, "2026-06-18", "B 4 AS 10/25 R"))
    assert err.value.kind == "challenge"


def test_overview_page_served_as_decision_is_rejected(fixture):
    with pytest.raises(UpstreamFailure) as err:
        parse_decision_html(fixture(OVERVIEW_2026), "2026_06_18_B_04_AS_10_25_R", "x")
    assert err.value.kind == "unexpected_representation"


def test_store_roundtrip(urteil):
    conn = connect()
    init_db(conn)
    decision_id, changed = store_decision(conn, urteil, content_hash="x", capture_path="raw/x", media_type="text/html")
    assert changed and decision_id == urteil.ecli
    row = conn.execute("SELECT numbering, paragraph_count, chamber FROM decisions WHERE id=?", (decision_id,)).fetchone()
    assert (row["numbering"], row["paragraph_count"], row["chamber"]) == ("none", 50, "4. Senat")
