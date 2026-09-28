"""Bundesverfassungsgericht (bundesverfassungsgericht.de): real captured pages, offline."""
import pytest

from courts.errors import UpstreamFailure
from courts.http import Response
from courts.paragraphs import assemble, locate_quote
from courts.sources.base import Capture
from courts.sources.bverfg import (BVERFGE_URL, HOME_URL, PRESS_URL, BverfgAdapter, az_code, parse_bverfge_index,
                                   parse_bverfge_node, parse_home, parse_press_release)
from courts.store import connect, finalise, init_db, store_decision

DOCS = "https://www.bundesverfassungsgericht.de/SharedDocs/Entscheidungen/DE/"
KAMMER = ("rk20260803_1bvr223224", "2026/08", "2026-08-03", "1 BvR 2232/24")
TRIAGE = ("rs20250923_1bvr228423", "2025/09", "2025-09-23", "1 BvR 2284/23 u.a.")
KOPFTUCH = ("rs20150127_1bvr047110", "2015/01", "2015-01-27", "1 BvR 471/10")
VORLAGE = ("ls20260805_2bvl000714", "2026/08", "2026-08-05", "2 BvL 7/14,2 BvL 8/14")


def item(doc, folder, date, az):
    return {"source_doc_id": doc, "url": f"{DOCS}{folder}/{doc}.html", "listed_date": date,
            "listed_court": "bverfg", "listed_case_numbers": az, "listed_modified": None, "priority": 1,
            "extra": {}}


def capture(fixture, doc, folder):
    return Capture(fixture(f"bverfg/{doc}.html.gz"), f"{DOCS}{folder}/{doc}.html", "text/html", "html",
                   {"content-type": "text/html;charset=UTF-8"})


def parsed(fixture, spec):
    doc, folder, date, az = spec
    return finalise(BverfgAdapter().parse(capture(fixture, doc, folder), item(doc, folder, date, az)))


@pytest.fixture
def kammer(fixture):
    return parsed(fixture, KAMMER)


@pytest.fixture
def triage(fixture):
    return parsed(fixture, TRIAGE)


@pytest.fixture
def kopftuch(fixture):
    return parsed(fixture, KOPFTUCH)


def test_kammer_metadata(kammer):
    d = kammer
    assert (d.source_id, d.source_doc_id) == ("bverfg", "rk20260803_1bvr223224")
    assert d.source_url == f"{DOCS}2026/08/rk20260803_1bvr223224.html"
    assert (d.court_name, d.court_id, d.jurisdiction, d.land) == ("Bundesverfassungsgericht", "bverfg",
                                                                   "verfassung", "de")
    assert d.decision_date == "2026-08-03" and d.decision_type == "Beschluss"
    assert d.case_numbers_raw == "1 BvR 2232/24" and d.case_numbers == ["1 BvR 2232/24"]
    assert d.ecli == "ECLI:DE:BVerfG:2026:rk20260803.1bvr223224"
    assert d.chamber == "2. Kammer des Ersten Senats"
    assert d.metadata["judges"] == ["Ott", "Härtel", "Spinner"]
    assert d.metadata["press_releases"] == ["bvg26-056"] and d.metadata["norms"] == []
    assert d.metadata["template"] == "c-decision"


def test_multi_docket_senate_decision_keeps_all_dockets(triage, fixture):
    assert triage.case_numbers_raw == "1 BvR 2284/23, 1 BvR 2285/23"
    assert triage.case_numbers == ["1 BvR 2284/23", "1 BvR 2285/23"]
    assert triage.chamber == "Erster Senat" and triage.metadata["bverfge"] == "BVerfGE 173, 184"
    vorlage = parsed(fixture, VORLAGE)
    assert vorlage.case_numbers == ["2 BvL 7/14", "2 BvL 8/14"] and vorlage.chamber == "Zweiter Senat"
    assert [(p["court"], p["case_number"], p["decision_type"]) for p in vorlage.procedural] == [
        ("Bundesfinanzhof", "VI R 49/12", "Aussetzungs- und Vorlagebeschluss"),
        ("Bundesfinanzhof", "VI R 50/12", "Aussetzungs- und Vorlagebeschluss")]


def test_sections_and_authorship(kammer, triage):
    assert [s.name for s in kammer.sections] == ["tenor", "gruende"]
    assert [s.name for s in triage.sections] == ["titel", "leitsatz", "tenor", "gruende"]
    assert all(s.authorship == "court" for s in triage.sections)
    titel, leitsatz = triage.sections[0], triage.sections[1]
    assert titel.blocks[0].text == "Triage II" and triage.title == "Triage II"
    assert len(leitsatz.blocks) == 3 and leitsatz.blocks[0].text.startswith("1. Art. 12 Abs. 1 GG gewährleistet")
    assert leitsatz.heading == "Leitsätze zum Beschluss des Ersten Senats vom 23. September 2025"
    assert all(b.label is None for s in triage.sections if s.name in ("leitsatz", "tenor") for b in s.blocks)


def test_randnummern_new_template(kammer):
    paras, numbering = assemble(kammer.ecli, kammer.sections)
    assert numbering == "rn"
    e = kammer.ecli
    assert [p.rn for p in paras if p.rn] == [str(n) for n in range(1, 28)]
    assert locate_quote(paras, "Die Durchsuchung erfolgte am 27. April 2022.") == [f"{e}#rn-4"]
    assert locate_quote(paras, "Diese Entscheidung ist unanfechtbar.") == [f"{e}#rn-27"]
    # The heading "II." sits between Rn 5 and the marker of Rn 6: it continues Rn 5.
    rn5 = next(p for p in paras if p.rn == "5")
    assert rn5.blocks[-1].kind == "heading" and rn5.blocks[-1].text == "II."
    # "I." precedes the first marker: positional id, not a Randnummer.
    assert paras[3].id == f"{e}#gruende-u1" and paras[3].rn is None


def test_old_template_with_dissent_restarting_numbering(kopftuch):
    d = kopftuch
    assert d.metadata["template"] == "entscheidung"
    assert d.case_numbers == ["1 BvR 471/10", "1 BvR 1181/10"] and d.decision_type == "Beschluss"
    assert [s.name for s in d.sections] == ["leitsatz", "tenor", "gruende", "abweichende_meinung"]
    dissent = d.sections[-1]
    assert dissent.authorship == "court"
    assert dissent.heading == "Abweichende Meinung des Richters Schluckebier und der Richterin Hermanns"
    paras, numbering = assemble(d.ecli, d.sections)
    assert numbering == "rn"
    e = d.ecli
    assert locate_quote(paras, "Die Verfassungsbeschwerden betreffen gerichtliche Entscheidungen über "
                               "arbeitsrechtliche Sanktionen") == [f"{e}#rn-1"]
    assert locate_quote(paras, "Die Entscheidung vermögen wir in weiten Teilen des Ergebnisses und der "
                               "Begründung nicht mitzutragen.") == [f"{e}#abweichende_meinung-rn-1"]
    assert locate_quote(paras, "Die Entscheidung ist mit 6 : 2 Stimmen ergangen.") == [f"{e}#rn-159"]
    leitsatz = d.sections[0]
    assert len(leitsatz.blocks) == 4 and leitsatz.blocks[0].text.startswith("Der Schutz des Grundrechts")
    assert d.metadata["judges"][:2] == ["Gaier", "Eichberger"]
    assert ("Bundesarbeitsgericht", "2009-08-20", "2 AZR 499/08") in {
        (p["court"], p["date"], p["case_number"]) for p in d.procedural}


def test_rubrum_references_are_lower_instances(kammer):
    assert kammer.procedural == [
        {"direction": "previous", "court": "Landgericht Chemnitz", "date": "2024-08-22",
         "case_number": "2 Qs 232/24", "decision_type": "Beschluss",
         "raw": "Beschluss des Landgerichts Chemnitz vom 22. August 2024 - 2 Qs 232/24 -",
         "field": "bverfg:rubrum", "origin": "decision_text"},
        {"direction": "previous", "court": "Amtsgericht Chemnitz", "date": "2022-02-14",
         "case_number": "1 Gs 495/22", "decision_type": "Beschluss",
         "raw": "Beschluss des Amtsgerichts Chemnitz vom 14. Februar 2022 - 1 Gs 495/22 -",
         "field": "bverfg:rubrum", "origin": "decision_text"}]


def test_az_code():
    assert az_code("1 BvR 2232/24") == "1bvr223224" and az_code("2 BvL 7/14") == "2bvl000714"


def test_home_listing(fixture):
    items, presses = parse_home(fixture("bverfg/home.html.gz"))
    assert [i["source_doc_id"] for i in items] == [
        "ls20260805_2bvl000714", "rk20260803_1bvr223224", "rk20260803_1bvr239924"]
    first = items[0]
    assert first["url"] == f"{DOCS}2026/08/ls20260805_2bvl000714.html"
    assert (first["listed_date"], first["listed_case_numbers"], first["listed_court"], first["priority"]) == (
        "2026-08-05", "2 BvL 7/14,2 BvL 8/14", "bverfg", 1)
    assert first["listed_modified"] is None and first["extra"]["press_release"] == "bvg26-057"
    assert presses[0] == (2026, 59)


def test_press_release_and_bverfge_listing(fixture):
    released, items = parse_press_release(fixture("bverfg/bvg26-057.html.gz"),
                                          PRESS_URL.format(year=2026, yy=26, number=57))
    assert released == "2026-09-23"
    assert [(i["source_doc_id"], i["listed_case_numbers"]) for i in items] == [
        ("ls20260805_2bvl000714", "2 BvL 7/14, 2 BvL 8/14")]
    pages = parse_bverfge_node(fixture("bverfg/bverfge_node.html.gz"))
    assert len(pages) == 18 and pages[0].endswith("/_functions/akkordeon_band170-179.html")
    rows = parse_bverfge_index(fixture("bverfg/akkordeon_band170-179.html.gz"), pages[0])
    assert rows[0]["source_doc_id"] == "ls20250723_2bvl001914"
    assert rows[0]["extra"]["bverfge"] == "BVerfGE 173, 1" and rows[0]["extra"]["name"] == "Mindestgewinnbesteuerung"
    triage = next(r for r in rows if r["source_doc_id"] == "rs20250923_1bvr228423")
    assert triage["listed_case_numbers"] == "1 BvR 2284/23 u.a." and triage["listed_date"] == "2025-09-23"


class SiteHttp:
    """Serves the captured pages; unknown press releases answer like the real 404."""

    def __init__(self, fixture):
        self.fixture, self.urls, self.requests_made = fixture, [], 0

    def get(self, url, **kwargs):
        self.urls.append(url)
        pages = {HOME_URL: "home", BVERFGE_URL: "bverfge_node",
                 PRESS_URL.format(year=2026, yy=26, number=57): "bvg26-057"}
        if url.endswith("akkordeon_band170-179.html"):
            name = "akkordeon_band170-179"
        else:
            name = pages.get(url)
        if name is None:
            raise UpstreamFailure("error_page", "HTTP Status 404", url=url, status=404)
        return Response(url, 200, self.fixture(f"bverfg/{name}.html.gz"), {"content-type": "text/html"}, 0.0)


def test_list_items_limit_three_needs_only_the_home_page(fixture):
    http = SiteHttp(fixture)
    listing = BverfgAdapter().list_items(http, None, since=None, limit=3, courts=None)
    assert http.urls == [HOME_URL] and len(listing.items) == 3


def test_list_items_since_walks_press_releases_then_one_bverfge_page(fixture):
    http = SiteHttp(fixture)
    listing = BverfgAdapter().list_items(http, None, since="2026-01-01", limit=None, courts=None)
    press = [u for u in http.urls if "/Pressemitteilungen/" in u]
    assert press[:3] == [PRESS_URL.format(year=2026, yy=26, number=n) for n in (59, 58, 57)]
    assert all("/2026/" in u for u in press)          # since is in 2026: no earlier year probed
    assert http.urls[-2:] == [BVERFGE_URL, http.urls[-1]] and http.urls[-1].endswith("band170-179.html")
    assert {i["source_doc_id"] for i in listing.items} == {
        "ls20260805_2bvl000714", "rk20260803_1bvr223224", "rk20260803_1bvr239924"}
    merged = next(i for i in listing.items if i["source_doc_id"] == "ls20260805_2bvl000714")
    assert merged["extra"]["via"] == ["home", "press:bvg26-057"] and listing.complete is False


def test_list_items_limit_counts_only_unstored_items(fixture):
    conn = connect()
    init_db(conn)
    items, _ = parse_home(fixture("bverfg/home.html.gz"))
    conn.executemany("INSERT INTO source_items(source_id, source_doc_id, url, state) VALUES('bverfg', ?, ?, "
                     "'stored')", [(i["source_doc_id"], i["url"]) for i in items])
    conn.commit()
    http = SiteHttp(fixture)
    listing = BverfgAdapter().list_items(http, conn, since="2025-06-01", limit=2, courts=None)
    assert http.urls[-1].endswith("band170-179.html")
    assert len({i["source_doc_id"] for i in listing.items} - {i["source_doc_id"] for i in items}) >= 2


def test_court_filter_excludes_bverfg():
    assert BverfgAdapter().list_items(None, None, since=None, limit=None, courts=["bag"]).items == []


CHALLENGE = (b"<!DOCTYPE html><html><head><title>Security Check</title></head><body>"
             b"<p>Please verify you are human.</p><div class='g-recaptcha'></div></body></html>")


def test_challenge_body_is_a_typed_failure_not_a_decision(fixture):
    doc, folder, date, az = KAMMER
    cap = Capture(CHALLENGE, f"{DOCS}{folder}/{doc}.html", "text/html", "html", {"content-type": "text/html"})
    with pytest.raises(UpstreamFailure) as err:
        BverfgAdapter().parse(cap, item(doc, folder, date, az))
    assert err.value.kind == "challenge"


def test_real_404_page_and_press_page_are_not_decisions(fixture):
    doc, folder, date, az = KAMMER
    for name, kind in (("bvg26-060_404", "error_page"), ("bvg26-057", "unexpected_representation")):
        cap = Capture(fixture(f"bverfg/{name}.html.gz"), f"{DOCS}{folder}/{doc}.html", "text/html", "html",
                      {"content-type": "text/html"})
        with pytest.raises(UpstreamFailure) as err:
            BverfgAdapter().parse(cap, item(doc, folder, date, az))
        assert err.value.kind == kind


@pytest.mark.parametrize("override", [
    {"listed_date": "2026-08-04"},
    {"listed_case_numbers": "1 BvR 2399/24"},
    {"source_doc_id": "rk20260803_1bvr239924",
     "url": f"{DOCS}2026/08/rk20260803_1bvr239924.html"},
])
def test_identity_mismatch(fixture, override):
    doc, folder, date, az = KAMMER
    listed = item(doc, folder, date, az) | override
    cap = capture(fixture, doc, folder)
    cap.url = listed["url"]
    with pytest.raises(UpstreamFailure) as err:
        BverfgAdapter().parse(cap, listed)
    assert err.value.kind == "identity_mismatch"


def test_store_roundtrip(kopftuch):
    conn = connect()
    init_db(conn)
    decision_id, changed = store_decision(conn, kopftuch, content_hash="x", capture_path="raw/x",
                                          media_type="text/html")
    assert changed and decision_id == "ECLI:DE:BVerfG:2015:rs20150127.1bvr047110"
    row = conn.execute("SELECT numbering, chamber FROM decisions WHERE id=?", (decision_id,)).fetchone()
    assert row["numbering"] == "rn" and row["chamber"] == "Erster Senat"
    ids = {r["id"] for r in conn.execute("SELECT decision_id || '#' || anchor AS id FROM paragraphs WHERE decision_id=?", (decision_id,))}
    assert f"{decision_id}#rn-159" in ids and f"{decision_id}#abweichende_meinung-rn-31" in ids
