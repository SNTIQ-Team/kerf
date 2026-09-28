"""Bundesarbeitsgericht (bundesarbeitsgericht.de): real captured pages, offline."""
import pytest

from courts.errors import UpstreamFailure
from courts.http import Response
from courts.paragraphs import assemble, locate_quote
from courts.sources.bag import BASE, LIST_URL, PAGE_URL, BagAdapter, parse_decision, parse_listing
from courts.sources.base import Capture
from courts.store import connect, finalise, init_db, store_decision

URL = BASE + "/entscheidung/{}/"


def capture(fixture, slug):
    return Capture(fixture(f"bag/{slug}.html.gz"), URL.format(slug), "text/html", "html",
                   {"content-type": "text/html; charset=UTF-8"})


def item(slug, date, az):
    return {"source_doc_id": slug, "url": URL.format(slug), "listed_date": date, "listed_court": "bag",
            "listed_case_numbers": az, "listed_modified": "2026-08-06", "priority": 1,
            "extra": {"released": "2026-08-06"}}


def parsed(fixture, slug, date, az):
    return finalise(BagAdapter().parse(capture(fixture, slug), item(slug, date, az)))


@pytest.fixture
def urteil(fixture):
    return parsed(fixture, "10-azr-88-25", "2026-05-20", "10 AZR 88/25")


@pytest.fixture
def beschluss(fixture):
    return parsed(fixture, "6-azb-10-26", "2026-09-14", "6 AZB 10/26")


def test_metadata_of_urteil(urteil):
    d = urteil
    assert (d.source_id, d.source_doc_id) == ("bag", "10-azr-88-25")
    assert (d.court_name, d.court_id, d.jurisdiction, d.land) == ("Bundesarbeitsgericht", "bag", "arbeit", "de")
    assert d.decision_date == "2026-05-20"
    assert d.case_numbers_raw == "10 AZR 88/25" and d.case_numbers == ["10 AZR 88/25"]
    assert d.ecli == "ECLI:DE:BAG:2026:200526.U.10AZR88.25.0"
    assert d.decision_type == "Urteil" and d.chamber == "10. Senat"
    assert d.title.startswith("Variable Vergütung - verspätete Zielvorgabe")
    assert d.metadata["norms"] == []
    assert d.metadata["pdf_url"].endswith("/2026/08/10-AZR-88-25.pdf")


def test_sections_and_authorship_of_urteil(urteil):
    by_name = {s.name: s for s in urteil.sections}
    assert [s.name for s in urteil.sections] == ["titel", "tenor", "tatbestand", "entscheidungsgruende"]
    assert by_name["titel"].authorship == "editorial"
    assert all(by_name[n].authorship == "court" for n in ("tenor", "tatbestand", "entscheidungsgruende"))
    assert by_name["tenor"].blocks[0].text.startswith("1. Die Revision des Klägers gegen das Urteil")
    assert all(b.label is None for b in by_name["tenor"].blocks)


def test_leitsatz_is_court_authored_and_beschluss_reasons_are_gruende(beschluss):
    names = [s.name for s in beschluss.sections]
    assert names == ["titel", "leitsatz", "tenor", "gruende"]
    leitsatz = next(s for s in beschluss.sections if s.name == "leitsatz")
    assert leitsatz.authorship == "court" and "Senate" in leitsatz.authorship_basis
    assert leitsatz.blocks[0].text.startswith("Auf eine noch fristwahrende Weiterleitung")
    gruende = next(s for s in beschluss.sections if s.name == "gruende")
    assert gruende.heading == "Entscheidungsgründe"
    assert beschluss.decision_type == "Beschluss"
    assert beschluss.ecli == "ECLI:DE:BAG:2026:140926.B.6AZB10.26.0"


def test_randnummern_and_unnumbered_continuation(urteil):
    paras, numbering = assemble(urteil.ecli, urteil.sections)
    assert numbering == "rn"
    rns = [p.rn for p in paras if p.rn]
    assert rns == [str(n) for n in range(1, 34)]
    e = urteil.ecli
    assert locate_quote(paras, "Die Kostenentscheidung beruht auf § 97 Abs. 1 ZPO") == [f"{e}#rn-33"]
    assert locate_quote(paras, "Der Gläubiger ist wirtschaftlich so zu stellen, wie er stehen würde, "
                               "wenn der Schuldner den Vertrag ordnungsgemäß erfüllt hätte") == [f"{e}#rn-26"]
    # The quoted contract table after Rn 3 has no number: it continues Rn 3.
    assert locate_quote(paras, "Der Baustein ‚Ziele und Leistung‘ ist ein Prozentsatz in Höhe von 20 %") == [f"{e}#rn-3"]
    rn3 = next(p for p in paras if p.rn == "3")
    assert len(rn3.blocks) == 2 and "2.\tREGLEMENT" in rn3.blocks[1].text
    # Signature table continues the last Rn; tenor paragraphs are positional.
    assert paras[-1].id == f"{e}#rn-33" and "Günther-Gräff" in paras[-1].text
    assert [p.id for p in paras if p.section == "tenor"] == [f"{e}#tenor-1", f"{e}#tenor-2"]


def test_old_decision_without_ecli_keeps_leitsatz(fixture):
    d = parsed(fixture, "6-azr-785-08", "2010-01-21", "6 AZR 785/08")
    assert d.ecli is None and d.decision_date == "2010-01-21" and d.case_numbers == ["6 AZR 785/08"]
    assert [s.name for s in d.sections] == ["titel", "leitsatz", "tenor", "tatbestand", "entscheidungsgruende"]
    paras, numbering = assemble("court:bag/2010-01-21/6_AZR_785-08", d.sections)
    assert numbering == "rn"
    hits = locate_quote(paras, "Eine Leistungsklage gegen den Insolvenzverwalter auf Zahlung der Abfindung")
    assert hits == ["court:bag/2010-01-21/6_AZR_785-08#leitsatz-1"]


def test_lower_instance_from_tenor(urteil, beschluss):
    assert urteil.procedural == [{
        "direction": "previous", "court": "Landesarbeitsgericht Hamburg", "date": "2025-03-27",
        "case_number": "1 SLa 24/24", "decision_type": "Urteil",
        "raw": "Urteil des Landesarbeitsgerichts Hamburg vom 27. März 2025 – 1 SLa 24/24 –",
        "field": "bag:tenor", "origin": "decision_text"}]
    assert beschluss.procedural[0]["court"] == "Landesarbeitsgericht Schleswig-Holstein"
    assert beschluss.procedural[0]["case_number"] == "1 Sa 28/26"


def test_listing_page(fixture):
    items, total = parse_listing(fixture("bag/listing_page1.html.gz"))
    assert total == 146 and len(items) == 50
    first = items[0]
    assert first["source_doc_id"] == "6-azb-10-26"
    assert first["url"] == URL.format("6-azb-10-26")
    assert (first["listed_date"], first["listed_case_numbers"], first["listed_court"]) == (
        "2026-09-14", "6 AZB 10/26", "bag")
    assert first["listed_modified"] == first["extra"]["released"] == "2026-09-24"
    assert first["priority"] == 1
    released = [i["extra"]["released"] for i in items]
    assert released == sorted(released, reverse=True)
    assert any(i["listed_case_numbers"] == "8 AZR 194/25 (F)" for i in items)


class PagedHttp:
    """Serves page 1 and page 146 fixtures; records requested URLs."""

    def __init__(self, fixture):
        self.fixture, self.urls, self.requests_made = fixture, [], 0

    def get(self, url, **kwargs):
        self.urls.append(url)
        name = "listing_page1" if url == LIST_URL else "listing_page146"
        return Response(url, 200, self.fixture(f"bag/{name}.html.gz"), {"content-type": "text/html"}, 0.0)


def test_list_items_limit_fetches_only_the_first_page(fixture):
    http = PagedHttp(fixture)
    conn = connect()
    init_db(conn)
    listing = BagAdapter().list_items(http, conn, since=None, limit=3, courts=None)
    assert http.urls == [LIST_URL]
    assert len(listing.items) == 50 and listing.complete is False


def test_list_items_since_stops_at_release_date(fixture):
    http = PagedHttp(fixture)
    listing = BagAdapter().list_items(http, None, since="2026-08-01", limit=None, courts=None)
    assert http.urls == [LIST_URL]
    assert listing.items and all(i["listed_date"] >= "2026-08-01" for i in listing.items)
    assert listing.complete is False


def test_list_items_limit_skips_pages_already_stored(fixture):
    http = PagedHttp(fixture)
    conn = connect()
    init_db(conn)
    items, _ = parse_listing(fixture("bag/listing_page1.html.gz"))
    conn.executemany("INSERT INTO source_items(source_id, source_doc_id, url, state, fetched_modified) "
                     "VALUES('bag', ?, ?, 'stored', ?)",
                     [(i["source_doc_id"], i["url"], i["listed_modified"]) for i in items])
    conn.commit()
    BagAdapter().list_items(http, conn, since=None, limit=3, courts=None)
    assert http.urls == [LIST_URL, PAGE_URL.format(page=2)]


def test_court_filter_excludes_bag():
    assert BagAdapter().list_items(None, None, since=None, limit=None, courts=["bsg"]).items == []


CHALLENGE = (b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
             b"<div id='cf-chl-widget'>Checking your browser</div></body></html>")


def test_challenge_body_is_a_typed_failure_not_a_decision():
    cap = Capture(CHALLENGE, URL.format("10-azr-88-25"), "text/html", "html", {"content-type": "text/html"})
    with pytest.raises(UpstreamFailure) as err:
        BagAdapter().parse(cap, item("10-azr-88-25", "2026-05-20", "10 AZR 88/25"))
    assert err.value.kind == "challenge"


def test_listing_page_served_as_decision_is_rejected(fixture):
    with pytest.raises(UpstreamFailure) as err:
        parse_decision(fixture("bag/listing_page1.html.gz"), URL.format("10-azr-88-25"))
    assert err.value.kind == "unexpected_representation"


@pytest.mark.parametrize("date,az", [("2026-05-21", "10 AZR 88/25"), ("2026-05-20", "10 AZR 89/25")])
def test_identity_mismatch(fixture, date, az):
    with pytest.raises(UpstreamFailure) as err:
        BagAdapter().parse(capture(fixture, "10-azr-88-25"), item("10-azr-88-25", date, az))
    assert err.value.kind == "identity_mismatch"


def test_store_roundtrip(urteil):
    conn = connect()
    init_db(conn)
    decision_id, changed = store_decision(conn, urteil, content_hash="x", capture_path="raw/x", media_type="text/html")
    assert changed and decision_id == urteil.ecli
    row = conn.execute("SELECT numbering, paragraph_count FROM decisions WHERE id=?", (decision_id,)).fetchone()
    assert row["numbering"] == "rn" and row["paragraph_count"] == 36
    rel = conn.execute("SELECT origin, target_id FROM relations WHERE decision_id=? AND relation='procedural_reference'"
                       " AND target_kind='decision'", (decision_id,)).fetchone()
    assert rel["origin"] == "decision_text" and rel["target_id"] == "court:lag-hh/2025-03-27/1_SLa_24-24"
    # Cost norms are procedural steps of the decision, not applied substantive law (parser 1.5.0).
    costs = {r[0] for r in conn.execute("SELECT target_id FROM relations WHERE decision_id=? AND "
                                        "relation='procedural_reference' AND target_kind='norm'", (decision_id,))}
    assert "de/bund/zpo/para/97/abs/1" in costs
