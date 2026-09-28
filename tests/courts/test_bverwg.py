"""Bundesverwaltungsgericht (bverwg.de): real captured pages, offline."""
import pytest

from courts import http as http_module
from courts.detect import classify_page
from courts.errors import UpstreamFailure
from courts.http import PoliteHttp, Response
from courts.paragraphs import assemble, locate_quote, normalise
from courts.sources.base import Capture
from courts.sources.bverwg import (RSS_URL, SITEMAP_INDEX_URL, BverwgAdapter, id_facts, parse_decision_html,
                                   parse_rss, parse_sitemap, parse_sitemap_index)
from courts.sources.rii import parse_xml_decision, read_zip_xml
from courts.store import connect, finalise, init_db, store_decision

URL = "https://www.bverwg.de/de/{}"


def item(doc, date=None, az=None):
    facts = id_facts(doc)
    return {"source_doc_id": doc, "url": URL.format(doc), "listed_date": date or facts["date"],
            "listed_court": "bverwg", "listed_case_numbers": az or facts["case_number"], "listed_modified": None,
            "priority": 0, "extra": {}}


def capture(fixture, doc):
    return Capture(fixture(f"bverwg/{doc}.html"), URL.format(doc), "text/html", "html",
                   {"content-type": "text/html; charset=utf-8"})


def parsed(fixture, doc):
    return finalise(BverwgAdapter().parse(capture(fixture, doc), item(doc)))


@pytest.fixture
def urteil(fixture):
    return parsed(fixture, "080726U8C5.25.0")


@pytest.fixture
def beschluss(fixture):
    return parsed(fixture, "160316B1B19.16.0")


def test_metadata_of_urteil(urteil):
    d = urteil
    assert (d.court_name, d.court_id, d.jurisdiction, d.land) == ("Bundesverwaltungsgericht", "bverwg", "verwaltung", "de")
    assert d.decision_date == "2026-07-08" and d.decision_type == "Urteil"
    assert d.case_numbers_raw == "8 C 5.25" and d.case_numbers == ["8 C 5.25"]
    assert d.metadata["az_published"] == "BVerwG 8 C 5.25"
    assert d.ecli == "ECLI:DE:BVerwG:2026:080726U8C5.25.0"
    assert d.chamber == "8. Senat"
    assert d.title == "Ausdrückliche gesetzliche Regelung der Insolvenzunfähigkeit im Betriebsrentenrecht"
    assert d.metadata["norms"] == ["§§ 10, 17 BetrAVG", "§§ 1a, 13 KfWG"]
    assert d.procedural == [
        {"direction": "previous", "court": "VG Frankfurt am Main", "date": "2020-11-11", "case_number": "2 K 1703/19.F",
         "decision_type": None, "raw": "VG Frankfurt am Main - 11.11.2020 - AZ: 2 K 1703/19.F",
         "field": "bverwg:instanzenzug", "origin": "source_metadata"},
        {"direction": "previous", "court": "VGH Kassel", "date": "2024-12-04", "case_number": "10 A 1152/24",
         "decision_type": None, "raw": "VGH Kassel - 04.12.2024 - AZ: 10 A 1152/24",
         "field": "bverwg:instanzenzug", "origin": "source_metadata"}]


def test_sections_and_authorship(urteil):
    assert [(s.name, s.authorship) for s in urteil.sections] == [
        ("titel", "editorial"), ("leitsatz", "court"), ("tenor", "court"), ("gruende", "court")]
    assert all(s.authorship_basis for s in urteil.sections)
    leitsatz = urteil.sections[1].blocks[0].text
    assert leitsatz.startswith("Das Erfordernis einer ausdrücklichen gesetzlichen Regelung der Insolvenzunfähigkeit")
    tenor = [b.text for b in urteil.sections[2].blocks]
    assert tenor == ["Das Urteil des Hessischen Verwaltungsgerichtshofs vom 4. Dezember 2024 wird geändert. Die Berufung "
                     "der Klägerin gegen das Urteil des Verwaltungsgerichts Frankfurt am Main vom 11. November 2020 wird "
                     "zurückgewiesen.", "Die Klägerin trägt die Kosten des Berufungs- und des Revisionsverfahrens."]
    # Verfahrensinformation (press summary) and rubrum are not decision sections.
    texts = " ".join(b.text for s in urteil.sections for b in s.blocks)
    assert "Dagegen wendet sich der Beklagte mit der vom Verwaltungsgerichtshof" not in texts
    assert "für Recht erkannt" in urteil.metadata["rubrum"] and "für Recht erkannt" not in texts


def test_randnummern_assembly_and_quote(urteil):
    paragraphs, numbering = assemble(urteil.ecli, urteil.sections)
    assert numbering == "rn"
    rns = [p.rn for p in paragraphs if p.rn]
    assert rns == [str(n) for n in range(1, 17)]
    ids = {p.id: p for p in paragraphs}
    assert locate_quote(paragraphs, "Die Kostenentscheidung folgt aus § 154 Abs. 1 VwGO.") == [urteil.ecli + "#rn-16"]
    assert locate_quote(paragraphs, "Seit 1975 zahlt die Klägerin jeweils jährlich festgesetzte Beiträge "
                                    "zur Insolvenzsicherung") == [urteil.ecli + "#rn-2"]
    # The Rn marker itself is not part of the text; the heading "II" continues Rn 7 (no own Rn).
    assert ids[urteil.ecli + "#rn-2"].text.startswith("Seit 1975")
    rn7 = ids[urteil.ecli + "#rn-7"]
    assert [b.kind for b in rn7.blocks] == ["text", "heading"] and rn7.blocks[1].text == "II"
    assert ids[urteil.ecli + "#gruende-u1"].text == "I"
    assert [p.id.split("#")[1] for p in paragraphs[:4]] == ["titel-1", "leitsatz-1", "tenor-1", "tenor-2"]


def test_randnummern_equal_rii_xml_of_same_decision(fixture, beschluss):
    """The same decision from RII (official XML): identical Rn numbers and Rn texts."""
    rii = parse_xml_decision(read_zip_xml(fixture("rii/jb-WBRE201600193.zip"), "jb-WBRE201600193"),
                             "jb-WBRE201600193", "x")
    assert rii.ecli == beschluss.ecli
    ours = {p.rn: normalise(p.text) for p in assemble("X", beschluss.sections)[0] if p.rn}
    theirs = {p.rn: normalise(p.text) for p in assemble("X", rii.sections)[0] if p.rn}
    assert list(ours) == [str(n) for n in range(1, 13)]
    assert ours == theirs


def test_beschluss_metadata_and_court_prefixed_lower_instance_az(beschluss):
    d = beschluss
    assert (d.decision_type, d.decision_date, d.case_numbers_raw, d.chamber) == ("Beschluss", "2016-03-16", "1 B 19.16",
                                                                                 "1. Senat")
    assert [(p["court"], p["date"], p["case_number"]) for p in d.procedural] == [
        ("VG Stuttgart", "2015-02-03", "A 6 K 3840/14"), ("VGH Mannheim", "2015-12-01", "A 11 S 490/15")]
    assert [s.name for s in d.sections] == ["tenor", "gruende"]
    assert len(d.sections[0].blocks) == 3


def test_short_beschluss_with_cloudflare_beacon_is_not_a_challenge(fixture):
    body = fixture("bverwg/240826B8AV1.26.0.html")
    # Regression: the passive Cloudflare beacon (/cdn-cgi/challenge-platform/scripts/jsd/main.js) on a
    # short real decision must not be classified as a challenge.
    assert b"/cdn-cgi/challenge-platform/scripts/jsd/main.js" in body
    assert classify_page(body, status=200) is None
    d = parsed(fixture, "240826B8AV1.26.0")
    assert (d.decision_type, d.case_numbers, d.chamber, d.procedural) == ("Beschluss", ["8 AV 1.26"], "8. Senat", [])
    assert [p.id.split("#")[1] for p in assemble("X", d.sections)[0]] == ["tenor-1", "rn-1", "rn-2", "rn-3"]


class FakeResponse:
    def __init__(self, status, body, url):
        self.status_code, self.content, self.url = status, body, url
        self.headers = {"Content-Type": "text/html; charset=utf-8"}


class FakeSession:
    def __init__(self, pages):
        self.headers, self.pages, self.calls = {}, pages, []

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append(url)
        return FakeResponse(200, self.pages[url], url)


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda s: None)


def test_fetch_short_beschluss_needs_one_request(fixture, no_sleep):
    doc = "240826B8AV1.26.0"
    session = FakeSession({URL.format(doc): fixture(f"bverwg/{doc}.html")})
    http = PoliteHttp(session=session, respect_robots=False)
    cap = BverwgAdapter().fetch(http, item(doc))
    assert cap.body == fixture(f"bverwg/{doc}.html") and len(session.calls) == 1


CHALLENGE = (b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
             b"<script>window._cf_chl_opt={cvId:'3'};</script>"
             b"<script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'></script></body></html>")


def test_real_challenge_is_a_typed_failure(no_sleep):
    doc = "080726U8C5.25.0"
    session = FakeSession({URL.format(doc): CHALLENGE})
    http = PoliteHttp(session=session, respect_robots=False)
    with pytest.raises(UpstreamFailure) as err:
        BverwgAdapter().fetch(http, item(doc))
    assert err.value.kind == "challenge" and len(session.calls) == 1
    with pytest.raises(UpstreamFailure) as err:
        BverwgAdapter().parse(Capture(CHALLENGE, URL.format(doc), "text/html", "html", {}), item(doc))
    assert err.value.kind == "challenge"


def test_rss_page_served_as_decision_is_rejected(fixture):
    with pytest.raises(UpstreamFailure) as err:
        parse_decision_html(fixture("bverwg/sitemap_index_dyn.xml"), "080726U8C5.25.0", "x")
    assert err.value.kind == "unexpected_representation"


@pytest.mark.parametrize("date,az", [("2026-07-09", "8 C 5.25"), ("2026-07-08", "8 C 6.25")])
def test_identity_mismatch(fixture, date, az):
    doc = "080726U8C5.25.0"
    with pytest.raises(UpstreamFailure) as err:
        BverwgAdapter().parse(capture(fixture, doc), item(doc, date, az))
    assert err.value.kind == "identity_mismatch"


def test_page_of_another_decision_is_identity_mismatch(fixture):
    with pytest.raises(UpstreamFailure) as err:
        parse_decision_html(fixture("bverwg/080726U8C5.25.0.html"), "240826B8AV1.26.0", "x")
    assert err.value.kind == "identity_mismatch"


def test_rss_listing(fixture):
    items = parse_rss(fixture("bverwg/entscheidungen.rss"))
    assert len(items) == 50
    first = items[0]
    assert first["source_doc_id"] == "080726U8C5.25.0" and first["url"] == "https://www.bverwg.de/de/080726U8C5.25.0"
    assert (first["listed_date"], first["listed_court"], first["listed_case_numbers"]) == ("2026-07-08", "bverwg", "8 C 5.25")
    assert first["priority"] == 0 and first["listed_modified"] is None
    assert first["extra"]["published"] == "2026-09-24T11:00:00+02:00" and first["extra"]["listed_type"] == "Urteil"
    wd = next(i for i in items if i["source_doc_id"] == "200826B2WDB6.26.0")
    assert (wd["listed_date"], wd["listed_case_numbers"]) == ("2026-08-20", "2 WDB 6.26")


def test_sitemap_listing(fixture):
    locs = parse_sitemap_index(fixture("bverwg/sitemap_index_dyn.xml"))
    assert len(locs) == 13 and locs[0] == "https://www.bverwg.de/sitemap_ent_1.xml"
    items = parse_sitemap(fixture("bverwg/sitemap_ent_11.xml"), locs[10])
    first = items[0]
    assert (first["source_doc_id"], first["listed_date"], first["listed_case_numbers"]) == (
        "181223B11VR2.23.0", "2023-12-18", "11 VR 2.23")
    assert all(i["listed_court"] == "bverwg" and i["priority"] == 0 for i in items)


class ListingHttp:
    def __init__(self, fixture):
        self.fixture, self.urls, self.requests_made = fixture, [], 0

    def get(self, url, **kwargs):
        self.urls.append(url)
        if url == RSS_URL:
            body = self.fixture("bverwg/entscheidungen.rss")
        elif url == SITEMAP_INDEX_URL:
            body = self.fixture("bverwg/sitemap_index_dyn.xml")
        else:
            body = self.fixture("bverwg/sitemap_ent_11.xml")
        return Response(url, 200, body, {"content-type": "application/xml"}, 0.0)


def test_list_items_limit_uses_only_the_feed(fixture):
    http = ListingHttp(fixture)
    conn = connect()
    init_db(conn)
    listing = BverwgAdapter().list_items(http, conn, since=None, limit=2, courts=None)
    assert http.urls == [RSS_URL] and listing.complete is False
    dates = [i["listed_date"] for i in listing.items]
    assert dates == sorted(dates, reverse=True) and dates[0] == "2026-08-27"


def test_list_items_since_inside_feed_window(fixture):
    http = ListingHttp(fixture)
    listing = BverwgAdapter().list_items(http, None, since="2026-09-01", limit=None, courts=None)
    assert http.urls == [RSS_URL]
    assert listing.items == [] or all(i["listed_date"] >= "2026-09-01" for i in listing.items)


def test_list_items_walks_sitemaps_when_feed_is_exhausted(fixture):
    http = ListingHttp(fixture)
    conn = connect()
    init_db(conn)
    rss = parse_rss(fixture("bverwg/entscheidungen.rss"))
    conn.executemany("INSERT INTO source_items(source_id, source_doc_id, url, state) VALUES('bverwg', ?, ?, 'stored')",
                     [(i["source_doc_id"], i["url"]) for i in rss])
    conn.commit()
    listing = BverwgAdapter().list_items(http, conn, since=None, limit=2, courts=None)
    assert http.urls[:2] == [RSS_URL, SITEMAP_INDEX_URL] and len(http.urls) == 15
    assert listing.complete is True and listing.upstream_total == len(listing.items) > 50


def test_court_filter_excludes_bverwg():
    assert BverwgAdapter().list_items(None, None, since=None, limit=None, courts=["bsg"]).items == []


def test_store_roundtrip(urteil):
    conn = connect()
    init_db(conn)
    decision_id, changed = store_decision(conn, urteil, content_hash="x", capture_path="raw/x", media_type="text/html")
    assert changed and decision_id == urteil.ecli
    row = conn.execute("SELECT numbering, paragraph_count FROM decisions WHERE id=?", (decision_id,)).fetchone()
    assert row["numbering"] == "rn" and row["paragraph_count"] == 21
    rel = conn.execute("SELECT origin, detail FROM relations WHERE decision_id=? AND relation='procedural_reference'",
                       (decision_id,)).fetchall()
    assert len(rel) == 2 and {r["origin"] for r in rel} == {"source_metadata"}
