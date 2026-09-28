"""Brandenburg adapter (gerichtsentscheidungen.brandenburg.de) on real captured pages, offline."""
from urllib.parse import parse_qs, urlsplit

import pytest

from courts import http as http_module
from courts.errors import UpstreamFailure
from courts.http import PoliteHttp
from courts.paragraphs import assemble, locate_quote
from courts.sources import get_adapter
from courts.sources.bb import DOC_URL, BrandenburgAdapter, parse_document, parse_result_list
from courts.sources.base import Capture
from courts.store import connect, init_db, store_decision

CLOUDFLARE = (b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>"
              b"<div id='cf-chl-widget'></div><script src='/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1'>"
              b"</script></body></html>")


def item_for(doc, **over):
    item = {"source_doc_id": doc, "url": DOC_URL.format(doc=doc), "listed_date": None, "listed_court": None,
            "listed_case_numbers": None, "listed_modified": None, "priority": 0, "extra": {}}
    item.update(over)
    return item


def parse_fixture(fixture, doc, **over):
    body = fixture(f"bb/doc-{doc}.html.gz")
    item = item_for(doc, **over)
    return BrandenburgAdapter().parse(Capture(body, item["url"], "text/html", "html", {}), item)


def test_registered():
    assert isinstance(get_adapter("bb"), BrandenburgAdapter)


def test_result_list_parse(fixture):
    pages, items = parse_result_list(fixture("bb/list-ovg-from-2026-08-01.html.gz").decode(), "OVG Berlin-Brandenburg")
    assert pages == 1 and len(items) == 11
    first = items[0]
    assert first["source_doc_id"] == "29408"
    assert first["url"] == "https://gerichtsentscheidungen.brandenburg.de/gerichtsentscheidung/29408"
    assert first["listed_date"] == "2026-08-20" and first["listed_court"] == "ovg-bebb" and first["priority"] == 0
    assert first["listed_case_numbers"] is None          # the list shows no Aktenzeichen
    assert first["extra"]["decision_type"] == "Beschluss" and first["extra"]["court_chamber"] == "OVG Der 4. Senat"
    dates = [i["listed_date"] for i in items]
    assert dates == sorted(dates, reverse=True)           # newest first
    assert any(i["source_doc_id"] == "29407" for i in items)

    pages, items = parse_result_list(fixture("bb/list-lsg-from-2026-01-01-p2.html.gz").decode(), "LSG Berlin-Potsdam")
    assert pages == 3 and len(items) == 20 and {i["listed_court"] for i in items} == {"lsg-bebb"}

    assert parse_result_list(fixture("bb/list-lsg-empty.html.gz").decode(), "LSG Berlin-Potsdam") == (0, [])


class FakeHttp:
    def __init__(self, body):
        self.body, self.urls, self.requests_made = body, [], 0

    def get(self, url, **kw):
        self.urls.append(url)
        self.requests_made += 1
        return http_module.Response(url=url, status=200, body=self.body,
                                    headers={"content-type": "text/html; charset=UTF-8"}, elapsed=0.0)


def test_list_items_respects_limit_and_since_social_courts_first(fixture):
    fake = FakeHttp(fixture("bb/list-lsg-from-2026-01-01-p2.html.gz"))
    listing = BrandenburgAdapter().list_items(fake, None, since="2026-01-01", limit=5, courts=None)
    assert len(fake.urls) == 1                            # one page is enough for 5 items
    query = parse_qs(urlsplit(fake.urls[0]).query)
    assert query == {"input_date_promulgation_from": ["01.01.2026"], "select_source": ["LSG Berlin-Potsdam"],
                     "page": ["1"]}
    assert len(listing.items) == 20 and listing.complete is False and listing.upstream_total is None


def test_list_items_court_filter(fixture):
    fake = FakeHttp(fixture("bb/list-lsg-empty.html.gz"))
    BrandenburgAdapter().list_items(fake, None, since="2026-09-01", limit=None, courts=["ovg-bebb", "vg-"])
    sources = [parse_qs(urlsplit(u).query)["select_source"][0] for u in fake.urls]
    assert sources == ["OVG Berlin-Brandenburg", "VG Cottbus", "VG Frankfurt (Oder)", "VG Potsdam"]


def test_ovg_beschluss_metadata_sections_and_paragraphs(fixture):
    parsed = parse_fixture(fixture, "29407", listed_date="2026-08-13", listed_court="ovg-bebb")
    assert parsed.court_name == "Oberverwaltungsgericht Berlin-Brandenburg"
    assert (parsed.court_id, parsed.jurisdiction, parsed.land) == ("ovg-bebb", "verwaltung", "bebb")
    assert parsed.chamber == "3. Senat"
    assert parsed.decision_date == "2026-08-13"
    assert parsed.case_numbers_raw == "OVG 3 N 89/26" and parsed.case_numbers == ["OVG 3 N 89/26"]
    assert parsed.ecli == "ECLI:DE:OVGBEBB:2026:0813.OVG3N89.26.00"
    assert parsed.decision_type == "Beschluss"
    assert [(s.name, s.authorship) for s in parsed.sections] == [
        ("titel", "editorial"), ("tenor", "court"), ("gruende", "court")]
    assert "60 Abs. 5 AufenthG" in parsed.metadata["norms"]
    assert all(b.label is None for s in parsed.sections for b in s.blocks)   # no Randnummern published

    paragraphs, numbering = assemble("D", parsed.sections)
    assert numbering == "none"
    body = [p for p in paragraphs if p.section == "gruende"]
    assert [p.id for p in body] == [f"D#p-{n}" for n in range(1, len(body) + 1)]
    assert [p.id for p in paragraphs if p.section == "tenor"] == ["D#tenor-1", "D#tenor-2"]
    assert body[0].text == "Der Antrag auf Zulassung der Berufung hat keinen Erfolg."
    assert locate_quote(paragraphs, "Der Antrag auf Zulassung der Berufung hat keinen Erfolg") == ["D#p-1"]
    assert locate_quote(paragraphs, "Die Berufung ist nicht nach § 78 Abs. 3 Nr. 1 AsylG zuzulassen") == ["D#p-2"]
    # indented quotation of the question is its own paragraph (own <p>)
    assert locate_quote(paragraphs, "ob männlichen, ledigen, jungen, gesunden syrischen Asylantragstellern") == ["D#p-5"]


def test_lsg_urteil_with_leitsatz_list(fixture):
    parsed = parse_fixture(fixture, "28876", listed_date="2026-04-22", listed_court="lsg-bebb")
    assert (parsed.court_id, parsed.chamber, parsed.case_numbers) == ("lsg-bebb", "7. Senat", ["L 7 KA 32/24"])
    assert parsed.ecli == "ECLI:DE:LSGBEBB:2026:0422.L7KA32.24.00"
    names = [(s.name, s.authorship, len(s.blocks)) for s in parsed.sections]
    assert names[:3] == [("titel", "editorial", 1), ("leitsatz", "court", 3), ("tenor", "court", 3)]
    assert [n for n, *_ in names[3:]] == ["tatbestand", "entscheidungsgruende"]
    assert parsed.sections[1].blocks[0].text.startswith("In Verfahren der Wirtschaftlichkeitsprüfung")
    paragraphs, numbering = assemble("D", parsed.sections)
    assert numbering == "none"
    assert [p.id for p in paragraphs][:5] == ["D#titel-1", "D#leitsatz-1", "D#leitsatz-2", "D#leitsatz-3",
                                              "D#tenor-1"]
    body = [p for p in paragraphs if p.section in ("tatbestand", "entscheidungsgruende")]
    assert [p.id for p in body] == [f"D#p-{n}" for n in range(1, len(body) + 1)]
    first_reason = next(p for p in body if p.section == "entscheidungsgruende")
    assert locate_quote(paragraphs, "Der Senat konnte verhandeln und entscheiden, obwohl die Beigeladenen "
                                    "im Verhandlungstermin nicht erschienen sind") == [first_reason.id]
    assert locate_quote(paragraphs, "Streitig ist die Rechtmäßigkeit eines Arzneimittelregresses […] wegen "
                                    "der Verordnung von Oxycodon") == ["D#p-1"]


def test_malformed_ecli_is_not_an_identity(fixture):
    parsed = parse_fixture(fixture, "22290", listed_date="2023-05-25", listed_court="sg-cottbus")
    assert parsed.court_id == "sg-cottbus" and parsed.land == "bb"
    assert parsed.ecli is None
    assert parsed.metadata["ecli_published_invalid"] == "ECLI:DE:SGCOTTB:2023:0525.00"
    assert parsed.case_numbers == ["S 13 U 56/21"]


@pytest.mark.parametrize("over,detail", [
    ({"listed_date": "2026-08-14"}, "listed date"),
    ({"listed_court": "vg-potsdam"}, "listed court"),
    ({"listed_case_numbers": "OVG 3 N 90/26"}, "listed Az"),
])
def test_identity_is_validated(fixture, over, detail):
    with pytest.raises(UpstreamFailure) as err:
        parse_fixture(fixture, "29407", **over)
    assert err.value.kind == "identity_mismatch" and detail in err.value.detail


def test_stored_decision_has_numbering_none(fixture, tmp_path):
    conn = connect(tmp_path / "c.sqlite")
    init_db(conn)
    parsed = parse_fixture(fixture, "29407", listed_date="2026-08-13", listed_court="ovg-bebb")
    decision_id, changed = store_decision(conn, parsed, content_hash="x", capture_path="raw/x",
                                          media_type="text/html")
    assert changed and decision_id == "ECLI:DE:OVGBEBB:2026:0813.OVG3N89.26.00"
    row = conn.execute("SELECT numbering, paragraph_count FROM decisions WHERE id=?", (decision_id,)).fetchone()
    assert row["numbering"] == "none" and row["paragraph_count"] == 27
    ids = [r["id"] for r in conn.execute("SELECT id FROM paragraphs WHERE decision_id=? ORDER BY ordinal",
                                         (decision_id,))]
    assert ids[3] == decision_id + "#p-1"


class FakeResponse:
    def __init__(self, status, body, headers, url):
        self.status_code, self.content, self.headers, self.url = status, body, headers, url


class ChallengeSession:
    headers: dict = {}

    def request(self, method, url, **kw):
        return FakeResponse(403, CLOUDFLARE, {"Content-Type": "text/html; charset=UTF-8", "Server": "cloudflare"}, url)


def test_challenge_page_is_a_typed_failure_never_a_decision():
    http = PoliteHttp(respect_robots=False, session=ChallengeSession(), min_interval=1.0)
    with pytest.raises(UpstreamFailure) as err:
        BrandenburgAdapter().fetch(http, item_for("29407"))
    assert err.value.kind == "challenge"
    with pytest.raises(UpstreamFailure) as err:        # a stored challenge body can never parse as a decision
        parse_document(CLOUDFLARE.decode(), "https://x", "29407")
    assert err.value.kind == "challenge"
