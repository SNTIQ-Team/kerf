"""Niedersachsen adapter (NI-VORIS) on real captured pages, offline."""
import pytest

from courts import http as http_module
from courts.errors import UpstreamFailure
from courts.http import PoliteHttp
from courts.paragraphs import assemble, locate_quote
from courts.sources import get_adapter
from courts.sources.base import Capture
from courts.sources.ni import BASE, ROOT, NiedersachsenAdapter, document_item, parse_browse, parse_document
from courts.store import connect, init_db, store_decision

DOC_A = "a3f46473-b012-40ed-8829-6653ef5d48d7"   # LSG Nds-Bremen, Beschluss 02.09.2026, L 8 SO 77/26 B ER
DOC_B = "150f3d64-691d-4e16-927d-0fddbbfea4af"   # LSG Nds-Bremen, Urteil 14.09.2026, L 2 R 135/25
CLOUDFLARE = (b"<!DOCTYPE html><html lang='en-US'><head><title>Just a moment...</title>"
              b"<meta http-equiv='refresh' content='360'></head><body><div class='main-wrapper'>"
              b"<noscript>Enable JavaScript and cookies to continue</noscript>"
              b"<script>window._cf_chl_opt={cvId: '3'};</script></div></body></html>")

# Real browse-tree pages, keyed by the last node id of their path.
TREE = {
    ROOT: "browse-rechtsprechung",
    "a68cb0a2-d16f-4327-a595-c30c8ac03ad0": "browse-sozialgerichte",
    "0b23ff44-090b-40fa-9dd3-eacb77857fdd": "browse-instanzgerichte-sozr",
    "78363ac2e4083d8486a550b55080e6ad": "browse-landessozialgerichte",
    "15349ab63b903ea18f7c47c42175d29a": "browse-lsg-niedersachsen",
    "85aca39fa2cd383587b6e2344b81dfca": "browse-lsg-nihb",
    "5c8ea1031c4c3fd5a3383bbe7a955176": "browse-lsg-nihb-2026",
    "22ddc3da9f2337889187b810808c663f": "browse-lsg-nihb-2026-09",
}
EMPTY_TREE_PAGE = b'<html><body><div class="wk-tree item-list"><ul></ul></div></body></html>'


class TreeHttp:
    """Serves the captured tree pages; unknown nodes are empty tree pages."""

    def __init__(self, fixture):
        self.fixture, self.paths, self.requests_made = fixture, [], 0

    def get(self, url, **kw):
        path = url.removeprefix(BASE)
        self.paths.append(path)
        self.requests_made += 1
        key = ROOT if path == ROOT else path.replace("%23", "%21").rsplit("%21", 1)[-1]
        name = TREE.get(key)
        body = self.fixture(f"ni/{name}.html.gz") if name else EMPTY_TREE_PAGE
        return http_module.Response(url=url, status=200, body=body,
                                    headers={"content-type": "text/html; charset=utf-8"}, elapsed=0.0)


def item_for(doc, **over):
    item = {"source_doc_id": doc, "url": f"{BASE}/browse/document/{doc}", "listed_date": None,
            "listed_court": None, "listed_case_numbers": None, "listed_modified": None, "priority": 0, "extra": {}}
    item.update(over)
    return item


def parse_fixture(fixture, doc, **over):
    item = item_for(doc, **over)
    headers = {"tdm-reservation": "1", "tdm-policy": "https://voris.wolterskluwer-online.de/nutzungsvorbehalt"}
    return NiedersachsenAdapter().parse(Capture(fixture(f"ni/doc-{doc}.html.gz"), item["url"], "text/html", "html",
                                                headers), item)


def test_registered():
    assert isinstance(get_adapter("ni"), NiedersachsenAdapter)


def test_browse_pages(fixture):
    nodes, docs = parse_browse(fixture("ni/browse-lsg-nihb-2026-09.html.gz").decode())
    assert nodes == [] and len(docs) == 4
    item = document_item(*docs[0], 0, ["Sozialgerichte"])
    assert item["source_doc_id"] == DOC_B and item["url"] == f"{BASE}/browse/document/{DOC_B}"
    assert (item["listed_date"], item["listed_court"], item["listed_case_numbers"], item["priority"]) == (
        "2026-09-14", "lsg-nihb", "L 2 R 135/25", 0)
    assert document_item(*docs[2], 0, [])["listed_case_numbers"] == "L 8 SO 77/26 B ER"
    nodes, docs = parse_browse(fixture("ni/browse-lsg-nihb-2026.html.gz").decode())
    assert [label for _, label in nodes][:3] == ["September", "August", "Juli"] and docs == []


def test_walk_limit_newest_court_first(fixture):
    fake = TreeHttp(fixture)
    listing = NiedersachsenAdapter().list_items(fake, None, since=None, limit=3, courts=None)
    # social branch first; of the two LSG, the one with the newest year (LSG Nds-Bremen 2026, not
    # LSG Niedersachsen 2023) is walked first, newest year and month first; stop once 3 items are listed.
    assert [TREE.get(p if p == ROOT else p.replace("%23", "%21").rsplit("%21", 1)[-1]) for p in fake.paths] == [
        "browse-rechtsprechung", "browse-sozialgerichte", "browse-instanzgerichte-sozr",
        "browse-landessozialgerichte", "browse-lsg-niedersachsen", "browse-lsg-nihb", "browse-lsg-nihb-2026",
        "browse-lsg-nihb-2026-09"]
    assert [i["listed_case_numbers"] for i in listing.items] == [
        "L 2 R 135/25", "L 2 EG 1/26", "L 8 SO 77/26 B ER", "L 6 U 188/23"]
    assert listing.complete is False and listing.upstream_total is None
    assert listing.items[0]["extra"]["tree"] == ["Sozialgerichte", "Instanzgerichte SozR", "Landessozialgerichte",
                                                 "LSG Niedersachsen-Bremen", "2026", "September"]


def test_walk_since_and_court_filter(fixture):
    fake = TreeHttp(fixture)
    listing = NiedersachsenAdapter().list_items(fake, None, since="2026-09-02", limit=None, courts=["lsg-nihb"])
    assert [i["listed_date"] for i in listing.items] == ["2026-09-14", "2026-09-02", "2026-09-02"]
    walked = [p for p in fake.paths if "15349ab63b903ea18f7c47c42175d29a" in p]
    assert len(walked) <= 1                        # LSG Niedersachsen (other court) is never descended into
    assert not any(p.endswith("1404e583a8a83fe4a4cbc1542f6b6c23") for p in fake.paths)   # August: before since


def test_lsg_beschluss(fixture):
    parsed = parse_fixture(fixture, DOC_A, listed_date="2026-09-02", listed_court="lsg-nihb",
                           listed_case_numbers="L 8 SO 77/26 B ER")
    assert parsed.court_name == "Landessozialgericht Niedersachsen-Bremen"
    assert (parsed.court_id, parsed.jurisdiction, parsed.land) == ("lsg-nihb", "sozial", "nihb")
    assert parsed.decision_date == "2026-09-02" and parsed.decision_type == "Beschluss"
    assert parsed.case_numbers_raw == "L 8 SO 77/26 B ER" and parsed.case_numbers == ["L 8 SO 77/26 B ER"]
    assert parsed.ecli == "ECLI:DE:LSGNIHB:2026:0902.8SO77.26.00"
    assert [(s.name, s.authorship) for s in parsed.sections] == [
        ("leitsatz", "court"), ("tenor", "court"), ("gruende", "court")]
    assert parsed.sections[0].heading == "Amtlicher Leitsatz"
    assert parsed.sections[0].blocks[0].text.startswith("1. Ein Eilantrag iSd § 86b Abs 2 SGG kann auf eine")
    assert parsed.procedural == [{
        "direction": "previous", "court": "SG Hildesheim", "date": "2026-03-25", "case_number": "S 63 SO 4003/26 ER",
        "decision_type": None, "raw": "SG Hildesheim - 25.03.2026 - AZ: S 63 SO 4003/26 ER",
        "field": "voris:Verfahrensgang", "origin": "editorial_metadata"}]
    assert "SGB X § 34" in parsed.metadata["norms"] and "Heizungsanlage" in parsed.metadata["keywords"]
    assert parsed.metadata["reference"] == "WKRS 2026, 21678"
    assert parsed.metadata["rights"]["tdm_reservation"] == "1"
    assert all(b.label is None for s in parsed.sections for b in s.blocks)

    paragraphs, numbering = assemble("D", parsed.sections)
    assert numbering == "none"
    body = [p for p in paragraphs if p.section == "gruende"]
    assert [p.id for p in body] == [f"D#p-{n}" for n in range(1, 30)]
    assert body[0].text == "I." and body[0].blocks[0].kind == "heading"
    assert body[1].text == "Im Streit ist vorläufige Hilfe für die Beschaffung einer Heizungsanlage."
    assert locate_quote(paragraphs, "Im Streit ist vorläufige Hilfe für die Beschaffung einer Heizungsanlage") == [
        "D#p-2"]
    assert locate_quote(paragraphs, "Der 1957 geborene Antragsteller, seine 1962 geborene Ehefrau […] leben im "
                                    "Kreisgebiet des Antragsgegners") == ["D#p-3"]
    assert body[0].blocks[0].locator.startswith("#idd")          # stable source element id


def test_lsg_urteil_antrag_blocks_are_paragraphs(fixture):
    parsed = parse_fixture(fixture, DOC_B, listed_date="2026-09-14", listed_court="lsg-nihb",
                           listed_case_numbers="L 2 R 135/25")
    assert [s.name for s in parsed.sections] == ["leitsatz", "tenor", "tatbestand", "entscheidungsgruende"]
    tatbestand = next(s for s in parsed.sections if s.name == "tatbestand")
    assert len(tatbestand.blocks) == 34                            # 32 <p> + 2 nested div.antrag paragraphs
    paragraphs, numbering = assemble("D", parsed.sections)
    assert numbering == "none"
    ids = [p.id for p in paragraphs if p.section in ("tatbestand", "entscheidungsgruende")]
    assert ids == [f"D#p-{n}" for n in range(1, 69)]
    assert parsed.procedural[0]["court"] == "SG Braunschweig" and parsed.procedural[0]["date"] == "2025-02-28"


@pytest.mark.parametrize("doc,over,detail", [
    (DOC_A, {"listed_case_numbers": "L 8 SO 78/26 B ER"}, "listed Az"),
    (DOC_A, {"listed_date": "2026-09-03"}, "listed date"),
    (DOC_A, {"listed_court": "sg-hildesheim"}, "listed court"),
])
def test_identity_is_validated(fixture, doc, over, detail):
    with pytest.raises(UpstreamFailure) as err:
        parse_fixture(fixture, doc, **over)
    assert err.value.kind == "identity_mismatch" and detail in err.value.detail


def test_document_served_for_another_uuid_is_rejected(fixture):
    item = item_for(DOC_B)
    with pytest.raises(UpstreamFailure) as err:
        NiedersachsenAdapter().parse(Capture(fixture(f"ni/doc-{DOC_A}.html.gz"), item["url"], "text/html", "html"),
                                     item)
    assert err.value.kind == "identity_mismatch"


def test_stored_decision(fixture, tmp_path):
    conn = connect(tmp_path / "c.sqlite")
    init_db(conn)
    parsed = parse_fixture(fixture, DOC_A, listed_date="2026-09-02", listed_court="lsg-nihb",
                           listed_case_numbers="L 8 SO 77/26 B ER")
    decision_id, _ = store_decision(conn, parsed, content_hash="x", capture_path="raw/x", media_type="text/html")
    row = conn.execute("SELECT numbering, court_id FROM decisions WHERE id=?", (decision_id,)).fetchone()
    assert (row["numbering"], row["court_id"]) == ("none", "lsg-nihb")
    proc = conn.execute("SELECT count(*) FROM relations WHERE decision_id=? AND relation='procedural_reference'",
                        (decision_id,)).fetchone()[0]
    assert proc == 1


class FakeResponse:
    def __init__(self, status, body, headers, url):
        self.status_code, self.content, self.headers, self.url = status, body, headers, url


class ChallengeSession:
    headers: dict = {}

    def request(self, method, url, **kw):
        return FakeResponse(403, CLOUDFLARE, {"Content-Type": "text/html; charset=UTF-8", "Server": "cloudflare",
                                              "cf-mitigated": "challenge"}, url)


def test_cloudflare_challenge_is_a_typed_failure():
    http = PoliteHttp(respect_robots=False, session=ChallengeSession(), min_interval=1.0)
    with pytest.raises(UpstreamFailure) as err:
        NiedersachsenAdapter().fetch(http, item_for(DOC_A))
    assert err.value.kind == "challenge"
    with pytest.raises(UpstreamFailure) as err:
        NiedersachsenAdapter().list_items(http, None, since=None, limit=1, courts=None)
    assert err.value.kind == "challenge"
    with pytest.raises(UpstreamFailure) as err:
        parse_document(CLOUDFLARE.decode(), "https://x", DOC_A)
    assert err.value.kind == "challenge"
