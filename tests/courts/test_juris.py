"""juris Bürgerservice adapter (courts/sources/juris.py), offline.

Fixtures in fixtures/juris/ are real captures from the Hessian LaReDa portal
(exact response bytes; documents gzip-compressed): he-doc-*.json.gz are
``recherche3/document`` responses, he-search-*.json ``recherche3/search``
responses, he-error-security_notAuthenticated.json the API's session error.
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from courts import http as http_module
from courts.errors import UpstreamFailure
from courts.http import PoliteHttp
from courts.paragraphs import assemble, locate_quote
from courts.sources import juris
from courts.sources.base import Capture
from courts.store import connect, init_db
from courts.sync import sync_source

FIX = Path(__file__).parent / "fixtures" / "juris"
HE = juris.adapters()["he"].portal


def doc_bytes(doc_id: str) -> bytes:
    return gzip.decompress((FIX / f"he-doc-{doc_id}.json.gz").read_bytes())


def doc(doc_id: str):
    return juris.parse_document(json.loads(doc_bytes(doc_id)), doc_id, HE)


def listing(name: str) -> list[dict]:
    items, _ = juris.parse_result_list(json.loads((FIX / f"he-search-{name}.json").read_bytes()), HE)
    return items


# --- offline HTTP ----------------------------------------------------------------

class FakeTransport(BaseAdapter):
    """requests transport answering from a handler; records every request."""

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.calls: list[tuple[str, str, dict | None]] = []

    def send(self, request, **kwargs):
        body = json.loads(request.body) if request.body else None
        self.calls.append((request.method, request.url, body))
        status, headers, content = self.handler(request.method, request.url, body)
        response = requests.Response()
        response.status_code = status
        response.headers = CaseInsensitiveDict(headers)
        response._content = content
        response.url = request.url
        response.request = request
        response.reason = "fake"
        return response

    def close(self):
        pass


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(http_module.time, "sleep", lambda seconds: None)


def make_http(handler) -> tuple[PoliteHttp, FakeTransport]:
    transport = FakeTransport(handler)
    session = requests.Session()
    session.mount("https://", transport)
    session.mount("http://", transport)
    return PoliteHttp(min_interval=1.0, retries=0, session=session), transport


JSON = {"content-type": "application/json"}


def portal_handler(api):
    """robots.txt missing, portal page sets the session, init returns a token, api(op, body) the rest."""
    def handler(method, url, body):
        if url.endswith("/robots.txt"):
            return 404, {"content-type": "text/html"}, b"<html><title>404</title></html>"
        if "/jportal/portal/page/" in url:
            return 302, {"location": "https://x/bshe/", "set-cookie": "JSESSIONID=abc; Path=/"}, b""
        op = url.rsplit("/", 1)[-1]
        if op == "init":
            return 200, JSON, json.dumps({"csrfToken": "tok"}).encode()
        return api(op, body)
    return handler


def ops(transport: FakeTransport) -> list[str]:
    return [url.rsplit("/", 1)[-1] if "/recherche3/" in url else
            ("robots" if url.endswith("robots.txt") else "portal") for _, url, _ in transport.calls]


# --- configuration -----------------------------------------------------------------

def test_portal_table_has_all_ten_juris_laender():
    table = {p.source_id: (p.land, p.host, p.code) for p in juris.PORTALS}
    assert table == {
        "bw": ("bw", "www.landesrecht-bw.de", "bsbw"),
        "be": ("be", "gesetze.berlin.de", "bsbe"),
        "hh": ("hh", "www.landesrecht-hamburg.de", "bsha"),
        "he": ("he", "www.lareda.hessenrecht.hessen.de", "bshe"),
        "mv": ("mv", "www.landesrecht-mv.de", "bsmv"),
        "rp": ("rp", "www.landesrecht.rlp.de", "bsrp"),
        "sl": ("sl", "recht.saarland.de", "bssl"),
        "st": ("st", "www.landesrecht.sachsen-anhalt.de", "bsst"),
        "sh": ("sh", "www.gesetze-rechtsprechung.sh.juris.de", "bssh"),
        "th": ("th", "landesrecht.thueringen.de", "bsth"),
    }
    registered = juris.adapters()
    assert sorted(registered) == sorted(table)
    for source_id, adapter in registered.items():
        assert isinstance(adapter, juris.JurisPortalAdapter)
        assert adapter.source_id == source_id and adapter.land == table[source_id][0]
        assert adapter.base_url == f"https://{table[source_id][1]}"
    assert HE.permalink("LARE260001060") == "https://www.lareda.hessenrecht.hessen.de/perma?d=LARE260001060"
    assert HE.api == "https://www.lareda.hessenrecht.hessen.de/jportal/wsrest/recherche3/"
    assert HE.portal_page == "https://www.lareda.hessenrecht.hessen.de/jportal/portal/page/bshe"


# --- listing ------------------------------------------------------------------------

def test_result_list_parsing():
    items = listing("datum")
    assert [i["source_doc_id"] for i in items] == [
        "LARE260001139", "LARE260001126", "LARE260001074", "LARE260001092", "LARE260001095"]
    first, last = items[0], items[-1]
    assert first["url"] == "https://www.lareda.hessenrecht.hessen.de/perma?d=LARE260001139"
    assert first["listed_date"] == "2026-09-24"
    assert first["listed_case_numbers"] == "33026 C 122/25"
    assert first["listed_court"] == "ag-frankfurt" and first["priority"] == 1
    assert first["extra"] == {"docPart": "L", "gericht": "AG Frankfurt 33026 . Einzelrichter",
                              "doktyp": "Urteil", "branch": None}
    assert last["listed_court"] == "vg-wiesbaden" and last["priority"] == 0   # administrative court
    sozial = listing("sozial")
    assert [(i["listed_court"], i["listed_case_numbers"]) for i in sozial] == [
        ("lsg-he", "L 4 SO 147/26 B ER"), ("sg-frankfurt", "S 14 KR 409/26 ER"), ("sg-fulda", "S 4 SF 25/25 K")]
    assert {i["priority"] for i in sozial} == {0}


def test_result_list_rejects_non_listing_json():
    with pytest.raises(UpstreamFailure) as info:
        juris.parse_result_list({"foo": 1}, HE)
    assert info.value.kind == "unexpected_representation"


def test_list_items_respects_since_and_limit_and_lists_priority_branches_first(tmp_path):
    sozial = (FIX / "he-search-sozial.json").read_bytes()
    verwaltung = (FIX / "he-search-verwaltung-last.json").read_bytes()

    def api(op, body):
        branch = next(s["value"] for s in body["searches"] if s["id"] == "Gerichtsbarkeit")
        return 200, JSON, sozial if branch == "Sozialgerichtsbarkeit" else verwaltung

    http, transport = make_http(portal_handler(api))
    conn = connect(tmp_path / "c.sqlite")
    init_db(conn)
    result = juris.adapters()["he"].list_items(http, conn, since="2026-01-01", limit=2, courts=None)
    assert ops(transport) == ["robots", "portal", "init", "search", "search"]
    searches = [body for _, url, body in transport.calls if url.endswith("/search")]
    assert [s["searches"] for s in searches] == [
        [{"id": "Gerichtsbarkeit", "value": "Sozialgerichtsbarkeit"}, {"id": "Datum", "value": "ab 01.01.2026"}],
        [{"id": "Gerichtsbarkeit", "value": "Verwaltungsgerichtsbarkeit"}, {"id": "Datum", "value": "ab 01.01.2026"}]]
    for s in searches:
        assert s["searchTasks"]["RESULT_LIST"] == {"start": 1, "size": 2, "sort": "date", "addToHistory": False,
                                                   "addCategory": True}
        assert s["clientID"] == "bshe" and s["r3ID"] == searches[0]["r3ID"]   # constant per session
    assert len(result.items) == 5 and {i["priority"] for i in result.items} == {0}
    assert result.complete is False and result.upstream_total is None
    # Session headers: portal id and the CSRF token from init.
    init_body = transport.calls[2][2]
    assert init_body == {"clientID": "bshe", "clientVersion": "bshe - V08_35_00", "r3ID": searches[0]["r3ID"]}


def test_list_items_falls_through_to_all_courts_when_priority_branches_are_short(tmp_path):
    def one(name, index):
        data = json.loads((FIX / f"he-search-{name}.json").read_bytes())
        data["resultList"], data["hits"] = [data["resultList"][index]], 1
        return json.dumps(data).encode()

    everything = (FIX / "he-search-datum.json").read_bytes()

    def api(op, body):
        branch = next((s["value"] for s in body["searches"] if s["id"] == "Gerichtsbarkeit"), None)
        if branch == "Sozialgerichtsbarkeit":
            return 200, JSON, one("sozial", 0)
        if branch == "Verwaltungsgerichtsbarkeit":
            return 200, JSON, one("datum", 4)
        return 200, JSON, everything

    http, transport = make_http(portal_handler(api))
    conn = connect(tmp_path / "c.sqlite")
    init_db(conn)
    conn.execute("INSERT INTO source_items(source_id, source_doc_id, url, state) VALUES('he','LARE260001139','u',"
                 "'stored')")
    result = juris.adapters()["he"].list_items(http, conn, since=None, limit=4, courts=None)
    searches = [body for _, url, body in transport.calls if url.endswith("/search")]
    assert [s["searches"] for s in searches][2] == []            # all courts, newest first
    assert searches[2]["searchTasks"]["RESULT_LIST"]["size"] == 2  # only the 2 still needed
    ids = [i["source_doc_id"] for i in result.items]
    assert ids == ["LARE260001013", "LARE260001095", "LARE260001139", "LARE260001126", "LARE260001074",
                   "LARE260001092"]
    assert result.upstream_total == 96



def test_court_filter_skips_priority_branches_that_cannot_match(tmp_path):
    sozial = (FIX / "he-search-sozial.json").read_bytes()
    http, transport = make_http(portal_handler(lambda op, body: (200, JSON, sozial)))
    conn = connect(tmp_path / "c.sqlite")
    init_db(conn)
    result = juris.adapters()["he"].list_items(http, conn, since=None, limit=1, courts=["lsg"])
    searches = [body["searches"] for _, url, body in transport.calls if url.endswith("/search")]
    assert searches == [[{"id": "Gerichtsbarkeit", "value": "Sozialgerichtsbarkeit"}]]   # no Verwaltung paging
    assert [i["listed_court"] for i in result.items] == ["lsg-he", "sg-frankfurt", "sg-fulda"]


def test_after_a_full_listing_only_recent_additions_are_listed(tmp_path):
    """Erstabgabe (date added) catches new documents whatever their decision date."""
    http, transport = make_http(portal_handler(lambda op, body: (200, JSON, b'{"hits":0,"resultList":[]}')))
    conn = connect(tmp_path / "c.sqlite")
    init_db(conn)
    conn.execute("INSERT INTO sync_runs(source_id, started_at, status) VALUES('he','2026-09-20T03:00:00+00:00','ok')")
    conn.execute("INSERT INTO sync_runs(source_id, started_at, status, limit_n) VALUES('he','2026-09-27T03:00:00+00:00',"
                 "'ok',5)")   # a bounded run is not a full listing
    result = juris.adapters()["he"].list_items(http, conn, since=None, limit=None, courts=None)
    searches = [body["searches"] for _, url, body in transport.calls if url.endswith("/search")]
    assert searches == [
        [{"id": "Gerichtsbarkeit", "value": "Sozialgerichtsbarkeit"}, {"id": "Erstabgabe", "value": "ab 13.09.2026"}],
        [{"id": "Gerichtsbarkeit", "value": "Verwaltungsgerichtsbarkeit"},
         {"id": "Erstabgabe", "value": "ab 13.09.2026"}],
        [{"id": "Erstabgabe", "value": "ab 13.09.2026"}]]
    assert result.items == [] and result.complete is True

# --- documents -------------------------------------------------------------------------

def test_document_metadata():
    p = doc("LARE260001060")
    assert p.source_id == "he" and p.source_doc_id == "LARE260001060"
    assert p.source_url == "https://www.lareda.hessenrecht.hessen.de/perma?d=LARE260001060"
    assert (p.court_name, p.chamber) == ("Hessischer Verwaltungsgerichtshof", "8. Senat")
    assert (p.court_id, p.court_type, p.jurisdiction, p.land) == ("vgh-he", "vgh", "verwaltung", "he")
    assert p.decision_date == "2026-08-04"
    assert p.case_numbers_raw == "8 A 1759/24" and p.case_numbers == ["8 A 1759/24"]
    assert p.ecli == "ECLI:DE:VGHHE:2026:0804.8A1759.24.00"
    assert p.decision_type == "Urteil"
    assert p.title == "Bestellung von Aufsichtsräten in einer kommunalen Beteiligungsgesellschaft"
    assert p.metadata["norms"] == ["§ 101 AktG", "§ 52 GmbHG", "§ 122 Abs 1 S 1 Nr 3 HGO", "§ 125 Abs 1 S 1 HGO",
                                   "§ 125 Abs 2 S 1 HGO"]
    assert p.metadata["norms_truncated"] is True          # portal shows "... mehr"
    assert p.procedural == [{
        "direction": "previous", "court": "VG Gießen", "date": "2024-08-26", "case_number": "8 K 678/23.GI",
        "decision_type": "Urteil", "raw": "vorgehend VG Gießen, 26. August 2024, 8 K 678/23.GI, Urteil",
        "field": "juris:verfahrensgang", "origin": "editorial_metadata"}]


def test_sections_and_authorship():
    p = doc("LARE260001060")
    assert [(s.name, s.authorship) for s in p.sections] == [
        ("titel", "editorial"), ("leitsatz", "court"), ("tenor", "court"), ("tatbestand", "court"),
        ("entscheidungsgruende", "court")]
    by_name = {s.name: s for s in p.sections}
    assert "Leitsatz" in by_name["leitsatz"].authorship_basis and "Orientierungssatz" in \
        by_name["leitsatz"].authorship_basis
    assert by_name["leitsatz"].blocks[0].text.startswith("1. Die Stimmvergabe zur Neubestellung")
    assert "\n2. Statthafte Klageart" in by_name["leitsatz"].blocks[0].text
    assert by_name["tenor"].blocks[0].text.startswith("Auf die Berufung der Klägerin")
    # The Verfahrensgang box and the permalink box are apparatus, not decision text.
    text = "\n".join(b.text for s in p.sections for b in s.blocks)
    assert "vorgehend VG Gießen" not in text and "perma?d=" not in text


def test_orientierungssatz_is_editorial():
    name, authorship, basis = juris.SECTION_LABELS["orientierungssatz"]
    assert (name, authorship) == ("orientierungssatz", "editorial")
    assert juris.SECTION_LABELS["leitsatz"][:2] == ("leitsatz", "court")
    assert juris.SECTION_LABELS["gründe"][:2] == ("gruende", "court")


def test_randnummern_and_quote_location_including_unnumbered_continuation():
    p = doc("LARE260001060")
    paragraphs, numbering = assemble("D", p.sections)
    assert numbering == "rn"
    rns = [q.rn for q in paragraphs if q.rn]
    assert rns == [str(n) for n in range(1, 68)]
    rn10 = next(q for q in paragraphs if q.id == "D#rn-10")
    assert len(rn10.blocks) == 3                                   # <dt/> blocks continue Rn 10
    assert rn10.blocks[0].locator.endswith("#rd_10") and "#rd_" not in rn10.blocks[1].locator
    # A quote from an unnumbered <dt/> continuation block resolves to the preceding Rn.
    assert locate_quote(paragraphs, "hilfsweise festzustellen, dass die Beschlüsse des Beklagten in der Sitzung "
                                    "vom 24. August 2022 unter den Tagesordnungspunkten 9 bis 13") == ["D#rn-10"]
    assert locate_quote(paragraphs, "Die Revision wird nicht zugelassen, da keiner der in § 132 Abs. 2 VwGO "
                                    "genannten Zulassungsgründe vorliegt") == ["D#rn-67"]
    assert locate_quote(paragraphs, "Die Klägerin begehrt die Wiederholung […] Wahl zur Neubestellung") == ["D#rn-1"]
    # Tenor and headnotes have positional ids, never Randnummern.
    assert [q.id for q in paragraphs if q.section == "tenor"] == [f"D#tenor-{k}" for k in range(1, 7)]
    assert [q.id for q in paragraphs if q.section == "leitsatz"] == ["D#leitsatz-1"]


def test_document_without_randnummern_is_numbering_none():
    p = doc("LARE260001095")
    assert (p.court_id, p.decision_date, p.case_numbers) == ("vg-wiesbaden", "2026-09-09", ["7 K 593/25.WI.A"])
    assert p.ecli == "ECLI:DE:VGWIESB:2026:0909.7K593.25.WI.A.00"
    paragraphs, numbering = assemble("D", p.sections)
    assert numbering == "none"
    assert not any(b.label for s in p.sections for b in s.blocks)
    assert locate_quote(paragraphs, "Das Urteil ist unanfechtbar (§ 78 Abs. 1 AsylG).") == [paragraphs[-1].id]
    assert paragraphs[-1].id.startswith("D#p-")


def test_verfahrensgang_self_reference_is_not_a_lower_instance():
    p = doc("LARE260001123")
    assert p.case_numbers == ["5 L 4227/26.F"] and p.decision_date == "2026-09-03"
    assert p.procedural == []
    assert p.metadata["verfahrensgang_self_references"] == [
        "vorgehend VG Frankfurt am Main, 3. September 2026, 5 L 4227/26.F, Beschluss"]
    paragraphs, numbering = assemble("D", p.sections)
    assert numbering == "rn" and [s.name for s in p.sections] == ["titel", "leitsatz", "tenor", "gruende"]


def test_parse_validates_identity_against_listing():
    adapter = juris.adapters()["he"]
    item = listing("datum")[4]                       # the real listing entry of LARE260001095
    capture = Capture(body=doc_bytes("LARE260001095"), url=item["url"], media_type="application/json",
                      extension="json")
    parsed = adapter.parse(capture, item)
    assert parsed.metadata["capture"]["docId"] == "LARE260001095"
    for field, value in (("listed_date", "2026-09-10"), ("listed_case_numbers", "7 K 594/25.WI.A"),
                         ("listed_court", "vg-kassel")):
        with pytest.raises(UpstreamFailure) as info:
            adapter.parse(capture, dict(item, **{field: value}))
        assert info.value.kind == "identity_mismatch"
    with pytest.raises(UpstreamFailure) as info:            # a different document than the one requested
        adapter.parse(capture, dict(item, source_doc_id="LARE260001094"))
    assert info.value.kind == "identity_mismatch"


def test_error_json_is_a_typed_failure_never_a_decision():
    adapter = juris.adapters()["he"]
    error = (FIX / "he-error-security_notAuthenticated.json").read_bytes()
    item = listing("datum")[4]
    with pytest.raises(UpstreamFailure) as info:
        adapter.parse(Capture(body=error, url=item["url"], media_type="application/json", extension="json"), item)
    assert info.value.kind == "login"


# --- session / transport ---------------------------------------------------------------

def test_security_not_authenticated_reopens_session_once_then_fails():
    error = (FIX / "he-error-security_notAuthenticated.json").read_bytes()
    http, transport = make_http(portal_handler(lambda op, body: (500, JSON, error)))
    adapter = juris.adapters()["he"]
    with pytest.raises(UpstreamFailure) as info:
        adapter.search(http, [], start=1, size=1)
    assert info.value.kind == "login" and info.value.status == 500
    assert "security_notAuthenticated" in info.value.detail
    assert ops(transport) == ["robots", "portal", "init", "search", "portal", "init", "search"]


def test_security_not_authenticated_recovers_with_fresh_session():
    error = (FIX / "he-error-security_notAuthenticated.json").read_bytes()
    answers = [(500, JSON, error), (200, JSON, (FIX / "he-search-sozial.json").read_bytes())]
    http, transport = make_http(portal_handler(lambda op, body: answers.pop(0)))
    adapter = juris.adapters()["he"]
    _, data = adapter.search(http, [], start=1, size=3)
    assert data["hits"] == 7766
    first, second = [b for _, url, b in transport.calls if url.endswith("/search")]
    assert first["r3ID"] and second["r3ID"]
    assert ops(transport) == ["robots", "portal", "init", "search", "portal", "init", "search"]


def test_msgid_json_with_http_200_is_typed():
    body = json.dumps({"msgId": "search_invalid", "msgParams": {}, "httpStatus": 200}).encode()
    http, _ = make_http(portal_handler(lambda op, b: (200, JSON, body)))
    with pytest.raises(UpstreamFailure) as info:
        juris.adapters()["he"].search(http, [], start=1, size=1)
    assert info.value.kind == "error_page"


def test_fetch_returns_exact_document_bytes():
    raw = doc_bytes("LARE260001123")
    http, transport = make_http(portal_handler(lambda op, body: (200, JSON, raw)))
    item = {"source_doc_id": "LARE260001123", "extra": json.dumps({"docPart": "L"})}
    capture = juris.adapters()["he"].fetch(http, item)
    assert capture.body == raw and capture.media_type == "application/json" and capture.extension == "json"
    assert capture.url == "https://www.lareda.hessenrecht.hessen.de/perma?d=LARE260001123"
    body = transport.calls[-1][2]
    assert {k: body[k] for k in ("docId", "format", "docPart", "keyword", "sourceParams")} == {
        "docId": "LARE260001123", "format": "xsl", "docPart": "L", "keyword": None,
        "sourceParams": {"source": "TL", "category": "Rechtsprechung"}}
    assert ops(transport) == ["robots", "portal", "init", "document"]


# --- robots.txt ---------------------------------------------------------------------------

DISALLOW_ALL = b"User-agent: Googlebot\nAllow: /\n\nUser-agent: *\nDisallow: /\n"


def robots_disallow(method, url, body):
    if url.endswith("/robots.txt"):
        return 200, {"content-type": "text/plain"}, DISALLOW_ALL
    raise AssertionError(f"fetched {url} although robots.txt disallows it")


def test_hessen_robots_redirect_to_app_shell_means_no_robots_file():
    """Real LaReDa behaviour: /robots.txt -> 302 portal page -> 302 /bshe/ (HTML app shell)."""
    host = "https://www.lareda.hessenrecht.hessen.de"

    def handler(method, url, body):
        if url == host + "/robots.txt":
            return 302, {"location": host + "/jportal/portal/page/bshe"}, b""
        if url == host + "/jportal/portal/page/bshe":
            return 302, {"location": host + "/bshe/"}, b""
        if url == host + "/bshe/":
            return 200, {"content-type": "text/html"}, b"<!doctype html><title>Buergerservice</title>"
        op = url.rsplit("/", 1)[-1]
        if op == "init":
            return 200, JSON, b'{"csrfToken":"tok"}'
        return 200, JSON, (FIX / "he-search-sozial.json").read_bytes()

    http, transport = make_http(handler)
    _, data = juris.adapters()["he"].search(http, [], start=1, size=3)
    assert data["hits"] == 7766
    urls = [url for _, url, _ in transport.calls]
    assert urls[:3] == [host + "/robots.txt", host + "/jportal/portal/page/bshe", host + "/bshe/"]
    # The session is opened on the portal page without following its redirect to the JS app shell.
    assert urls[3:] == [host + "/jportal/portal/page/bshe", HE.api + "init", HE.api + "search"]


@pytest.mark.parametrize("source_id", ["bw", "th"])
def test_robots_disallow_stops_listing_after_robots_txt(source_id):
    http, transport = make_http(robots_disallow)
    with pytest.raises(UpstreamFailure) as info:
        juris.adapters()[source_id].list_items(http, None, since=None, limit=1, courts=None)
    assert info.value.kind == "robots_disallowed"
    host = juris.adapters()[source_id].portal.host
    assert transport.calls == [("GET", f"https://{host}/robots.txt", None)]


def test_sync_records_robots_disallowed_and_blocks_source(tmp_path):
    http, transport = make_http(robots_disallow)
    conn = connect(tmp_path / "c.sqlite")
    init_db(conn)
    result = sync_source(juris.adapters()["bw"], conn, http, limit=1, home=tmp_path)
    assert result["status"] == "blocked" and result["requests"] == 1 and result["attempted"] == 0
    assert len(transport.calls) == 1
    failure = conn.execute("SELECT kind, url FROM failures WHERE source_id='bw'").fetchone()
    assert failure["kind"] == "robots_disallowed"
    assert failure["url"] == "https://www.landesrecht-bw.de/jportal/portal/page/bsbw"
    assert conn.execute("SELECT status FROM sources WHERE source_id='bw'").fetchone()[0] == "blocked"


def test_printed_endnotes_remain_separate_from_randnummern():
    text = """
        <h4 class="doc">Gründe</h4><div><dl class="RspDL"><dt>1</dt><dd><p>
        EU:C:2017:120<a class="footnotelink" data-juris-target="t" data-juris-target-tooltip="f">43</a>
        <span id="t" class="hidefootnote">Vgl. <i>Quelle</i>.</span><span id="f" class="hidefootnote">Vgl. Quelle.</span>
        und weiterer Text</p></dd></dl><dl><dt>2</dt><dd>Ende.</dd></dl></div>
        <h3 class="doc"><a name="fn">Fußnoten</a></h3>
        <div><div class="footnoteDL"><dl><dt>43) </dt><dd>Vgl. <i>Quelle</i>.</dd></dl></div></div>
    """
    result = juris.parse_body(text, "doc")
    paragraphs, numbering = assemble("doc", result["sections"])
    assert numbering == "rn"
    first, second, note = paragraphs
    assert first.rn == "1"
    assert " ".join(first.text.split()) == "EU:C:2017:120 43 und weiterer Text"
    assert (second.rn, second.text) == ("2", "Ende.")
    assert (note.id, note.rn, note.text) == ("doc#fussnoten-1", None, "43) Vgl. Quelle.")
    assert result["sections"][-1].authorship == "court"
    assert result["sections"][-1].heading == "Fußnoten"
    assert note.blocks[0].locator == "text:/div[2]/div[1]"


def test_endnote_attribution_and_hidden_text_without_matching_printed_copy():
    text = """
        <h4 class="doc">Orientierungssatz</h4><p>Erster Hinweis<a class="footnotelink"
        data-juris-target="t" data-juris-target-tooltip="f">1</a><span id="t" class="hidefootnote">Quelle A</span><span
        id="f" class="hidefootnote">Quelle</span>.</p>
        <h4 class="doc">Gründe</h4><p>Text<a class="footnotelink" data-juris-target="x"
        data-juris-target-tooltip="y">2</a><span id="x" class="hidefootnote">Andere Fassung</span>
        <span id="y" class="hidefootnote">Andere Fassung</span></p>
        <div class="footnoteDL"><dl><dt>1) </dt><dd>Quelle A</dd></dl></div>
        <div class="footnoteDL"><dl><dt>2) </dt><dd>Quelle B</dd></dl></div>
    """
    result = juris.parse_body(text, "doc")
    paragraphs, numbering = assemble("doc", result["sections"])
    assert numbering == "none"
    assert [p.id for p in paragraphs] == ["doc#orientierungssatz-1", "doc#p-1",
                                          "doc#redaktionelle_fussnoten-1", "doc#fussnoten_unklar-1"]
    assert [s.authorship for s in result["sections"]] == ["editorial", "court", "editorial", "unknown"]
    assert "Andere Fassung" in paragraphs[1].text
    assert [p.text for p in paragraphs[-2:]] == ["1) Quelle A", "2) Quelle B"]
