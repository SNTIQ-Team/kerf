"""Sachsen adapter (Sächs. OVG Entscheidungssammlung + ESAMOSplus) on real captures, offline.

Fixtures in tests/courts/fixtures/sn were captured on 2026-09-28. The ESAMOS HTML
pages contain synthetic form state (see conformance/fixtures/court-html-provenance.json).
  ovg-list-2026.html.gz        POST searchlist.phtml datum=2026 (197 hits, one page)
  ovg-list-empty.html          POST searchlist.phtml datum=01.01.1900-31.12.1990 (no hits)
  ovg-doc-8012.html            document.phtml?id=8012 (2 B 264/26)
  ovg-26B264.B01.pdf           its Volltext PDF (documents/26B264.B01.pdf)
  ovg-doc-missing.html         document.phtml?id=99999999 (empty record shell)
  ovg-doc-8020.html, ovg-26A487.B01.pdf   3 A 487/26.A (no Leitsatz; right-aligned Rn " 9" next to "10";
                               taken byte-exact from the live sync capture)
  esamos-suchen.html.gz        GET suchen.aspx (search form, ten newest decisions)
  esamos-treffer-2026-p1.html.gz   search 01.01.2026-28.09.2026, result page 1 of 13
  esamos-treffer-az-4U374-26.html.gz  search Aktenzeichen 4 U 374/26 (two documents)
  esamos-4U374-26_02.pdf       PDF delivered for the row button '4U374-26_02...'
  esamos-treffer-1900-1999-p1.html.gz, esamos-1Ss421.99.pdf   search 01.01.1900-31.12.1999 page 1 and the
                               1999 PDF of 1 Ss 421/99 (older layout, no tenor marker)
"""
import shutil
import zipfile
from datetime import date
from io import BytesIO

import pytest
from bs4 import BeautifulSoup

from courts.cli import http_for
from courts.errors import UpstreamFailure
from courts.http import Response
from courts.paragraphs import assemble, locate_quote
from courts.sources import get_adapter
from courts.sources.base import OPERATOR_DECISION_BASIS, OPERATOR_DECISION_DATE, Capture
from courts.sources.sn import (ES_RESULTS, ES_SEARCH, OVG_BASE, OVG_SEARCH, SachsenAdapter, align_margin_numbers,
                               decode, ovg_capture, parse_esamos_results, parse_ovg_list, parse_ovg_page,
                               parse_rows)
from courts.store import connect, init_db, store_decision

needs_pdftotext = pytest.mark.skipif(shutil.which("pdftotext") is None, reason="poppler pdftotext not installed")
DOC_URL = "https://www.justiz.sachsen.de/ovgentschweb/document.phtml?id=8012"
PDF_URL = OVG_BASE + "documents/26B264.B01.pdf"


def html(fixture, name):
    return decode(fixture(f"sn/{name}"))


@pytest.fixture
def ovg_item(fixture):
    _, items = parse_ovg_list(html(fixture, "ovg-list-2026.html.gz"))
    return next(i for i in items if i["source_doc_id"] == "ovg:8012")


@pytest.fixture
def ovg_capture_body(fixture):
    return ovg_capture(fixture("sn/ovg-doc-8012.html"), DOC_URL, fixture("sn/ovg-26B264.B01.pdf"), PDF_URL,
                       "documents/26B264.B01.pdf")


@pytest.fixture
def es_item(fixture):
    rows, *_ = parse_esamos_results(html(fixture, "esamos-treffer-az-4U374-26.html.gz"))
    item = next(r for r in rows if r["source_doc_id"] == "esamos:4U374-26_02")
    item.pop("_row")
    return item


def parse_ovg(capture_body, item):
    return SachsenAdapter().parse(Capture(capture_body, DOC_URL, "application/zip", "zip"), item)


def parse_es(fixture, item):
    return SachsenAdapter().parse(Capture(fixture("sn/esamos-4U374-26_02.pdf"), item["url"], "application/pdf",
                                          "pdf"), item)


# --- registration and access policy ---------------------------------------------------------------

def test_registered_with_operator_policy():
    adapter = get_adapter("sn")
    assert isinstance(adapter, SachsenAdapter) and (adapter.source_id, adapter.land) == ("sn", "sn")
    assert adapter.robots_policy == "ignore_by_operator_decision"
    assert adapter.policy_decided_at == OPERATOR_DECISION_DATE
    assert adapter.policy_basis.startswith(OPERATOR_DECISION_BASIS) and "Crawl-delay: 3600" in adapter.policy_basis
    assert adapter.min_interval == 3.0
    assert adapter.operator_policy()["robots_policy"] == "ignore_by_operator_decision"
    http = http_for(adapter)
    assert http.respect_robots is False and http.min_interval == 3.0


# --- Sächs. OVG ------------------------------------------------------------------------------------

def test_ovg_list_parse(fixture, ovg_item):
    hits, items = parse_ovg_list(html(fixture, "ovg-list-2026.html.gz"))
    assert hits == len(items) == 197
    dates = [i["listed_date"] for i in items]
    assert dates == sorted(dates, reverse=True) and dates[0] == "2026-09-21" and dates[-1] == "2026-01-05"
    assert {i["listed_court"] for i in items} == {"ovg-sn"} and {i["priority"] for i in items} == {0}
    assert ovg_item["url"] == DOC_URL and ovg_item["listed_case_numbers"] == "2 B 264/26"
    assert ovg_item["extra"]["decision_type"] == "Beschluss"
    assert ovg_item["extra"]["schlagworte"] == ["Lehramt", "Vorbereitungsdienst", "Verfassungstreue"]
    assert ovg_item["extra"]["vorschriften"][:2] == ["GG Art. 12", "SächsVerf Art. 28"]
    asylum = next(i for i in items if i["source_doc_id"] == "ovg:8020")
    assert asylum["listed_case_numbers"] == "3 A 487/26.A" and asylum["extra"]["leitsatz_excerpt"] is None
    assert parse_ovg_list(html(fixture, "ovg-list-empty.html")) == (0, [])


def test_ovg_document_page(fixture):
    page = parse_ovg_page(html(fixture, "ovg-doc-8012.html"), DOC_URL)
    assert (page["court"], page["decision_type"], page["az"], page["date"]) == (
        "Sächsisches Oberverwaltungsgericht Bautzen", "Beschluss", "2 B 264/26", "2026-09-21")
    assert page["headnote_label"] == "Leitsatz" and len(page["headnote"]) == 1
    assert page["norms"] == ["GG Art. 12", "SächsVerf Art. 28", "SächsVerf Art. 29", "LAPO II § 3 Abs. 1",
                             "LAPO II § 4"]
    assert page["fulltext"] == "documents/26B264.B01.pdf"


def test_ovg_missing_record_is_typed(fixture):
    with pytest.raises(UpstreamFailure) as err:
        parse_ovg_page(html(fixture, "ovg-doc-missing.html"), OVG_BASE + "document.phtml?id=99999999")
    assert err.value.kind == "empty_document"


@needs_pdftotext
def test_ovg_decision(ovg_capture_body, ovg_item):
    parsed = parse_ovg(ovg_capture_body, ovg_item)
    assert (parsed.court_name, parsed.court_id, parsed.jurisdiction, parsed.land) == (
        "Sächsisches Oberverwaltungsgericht", "ovg-sn", "verwaltung", "sn")
    assert parsed.decision_date == "2026-09-21" and parsed.decision_type == "Beschluss"
    assert parsed.case_numbers_raw == "2 B 264/26" and parsed.case_numbers == ["2 B 264/26"]
    assert parsed.ecli is None and parsed.chamber == "2. Senat"
    assert parsed.procedural == [{"direction": "previous", "court": "VG Dresden", "date": None,
                                  "case_number": "11 L 612/26", "decision_type": None,
                                  "raw": "11 L 612/26 VG Dresden", "field": "pdf:header",
                                  "origin": "decision_text"}]
    assert [(s.name, s.authorship) for s in parsed.sections] == [
        ("leitsatz", "court"), ("tenor", "court"), ("gruende", "court")]
    assert "Entscheidungssammlung" in parsed.sections[0].authorship_basis
    assert parsed.metadata["identity"] == {"pdf_az": "2 B 264/26", "rubrum_date_am": "2026-09-21"}
    assert parsed.metadata["norms"][0] == "GG Art. 12" and parsed.metadata["pdf"]["pages"] == 19
    labels = [b.label for s in parsed.sections for b in s.blocks if b.label]
    assert labels == [str(n) for n in range(1, 29)]          # explicit margin numbers only, in sequence

    paragraphs, numbering = assemble("D", parsed.sections)
    assert numbering == "rn"
    assert [p.id for p in paragraphs][:6] == ["D#leitsatz-1", "D#tenor-1", "D#tenor-2", "D#tenor-3", "D#rn-1",
                                              "D#rn-2"]
    # Rn 2 runs from page 2 onto page 3; the page number "3" is neither text nor a paragraph
    assert locate_quote(paragraphs, "dass sie für die Ziele des IfS aktiv eintreten und diese ziel- und "
                                    "zweckgerichtet unterstütze") == ["D#rn-2"]
    assert locate_quote(paragraphs, "Zur Verfassungstreue von Bewerbern für den Vorbereitungsdienst") == [
        "D#leitsatz-1"]
    assert locate_quote(paragraphs, "Die Kostenentscheidung beruht auf § 154 Abs. 2 VwGO.") == ["D#rn-26"]


def test_right_aligned_margin_numbers():
    # as pdftotext -layout prints a page holding Rn 8-10: one text column (5), numbers right-aligned
    page = (" 8   Text acht\n     weiter\n 9   Text neun\n10   Text zehn\n"
            "    3. Absatz bleibt eingerückt\n  4    anderer Textanfang")
    text, moved = align_margin_numbers(page + "\f 9   ohne zweistellige Nummer auf der Seite")
    first, second = text.split("\f")
    lines = first.split("\n")
    assert moved == 2 and lines[0] == "8    Text acht" and lines[2] == "9    Text neun"
    assert lines[4] == "    3. Absatz bleibt eingerückt" and lines[5] == "  4    anderer Textanfang"
    assert second == " 9   ohne zweistellige Nummer auf der Seite"     # no column-0 number on that page


@needs_pdftotext
def test_ovg_rn_sequence_survives_right_aligned_numbers(fixture):
    _, items = parse_ovg_list(html(fixture, "ovg-list-2026.html.gz"))
    item = next(i for i in items if i["source_doc_id"] == "ovg:8020")
    body = ovg_capture(fixture("sn/ovg-doc-8020.html"), item["url"], fixture("sn/ovg-26A487.B01.pdf"),
                       OVG_BASE + "documents/26A487.B01.pdf", "documents/26A487.B01.pdf")
    parsed = SachsenAdapter().parse(Capture(body, item["url"], "application/zip", "zip"), item)
    assert parsed.case_numbers_raw == "3 A 487/26.A" and parsed.case_numbers == ["3 A 487/26.A"]
    assert [s.name for s in parsed.sections] == ["tenor", "gruende"]           # no Leitsatz published
    assert parsed.procedural[0]["court"] == "VG Leipzig" and parsed.procedural[0]["case_number"] == "3 K 2629/25.A"
    assert parsed.metadata["pdf"]["margin_numbers_realigned"] == 1
    labels = [b.label for s in parsed.sections for b in s.blocks if b.label]
    assert labels == [str(n) for n in range(1, 30)]
    paragraphs, numbering = assemble("D", parsed.sections)
    assert numbering == "rn"
    assert locate_quote(paragraphs, "Das Vorbringen des Klägers zeigt keine grundsätzliche Bedeutung der "
                                    "Rechtssache") == ["D#rn-9"]
    assert locate_quote(paragraphs, "Grundsätzliche Bedeutung hat eine Rechtssache nur dann") == ["D#rn-10"]


def test_ovg_capture_is_deterministic_zip_of_exact_bytes(fixture, ovg_capture_body):
    again = ovg_capture(fixture("sn/ovg-doc-8012.html"), DOC_URL, fixture("sn/ovg-26B264.B01.pdf"), PDF_URL,
                        "documents/26B264.B01.pdf")
    assert again == ovg_capture_body
    with zipfile.ZipFile(BytesIO(ovg_capture_body)) as archive:
        assert archive.namelist() == ["manifest.json", "document.html", "documents/26B264.B01.pdf"]
        assert archive.read("documents/26B264.B01.pdf") == fixture("sn/ovg-26B264.B01.pdf")
        assert archive.read("document.html") == fixture("sn/ovg-doc-8012.html")


@needs_pdftotext
@pytest.mark.parametrize("over,detail", [
    ({"listed_case_numbers": "2 B 265/26"}, "listed Az"),
    ({"listed_date": "2026-09-20"}, "listed date"),
    ({"listed_court": "ovg-bb"}, "listed court"),
])
def test_ovg_identity_is_validated(ovg_capture_body, ovg_item, over, detail):
    with pytest.raises(UpstreamFailure) as err:
        parse_ovg(ovg_capture_body, ovg_item | over)
    assert err.value.kind == "identity_mismatch" and detail in err.value.detail


@needs_pdftotext
def test_stored_ovg_decision(ovg_capture_body, ovg_item, tmp_path):
    conn = connect(tmp_path / "c.sqlite")
    init_db(conn)
    decision_id, _ = store_decision(conn, parse_ovg(ovg_capture_body, ovg_item), content_hash="x",
                                    capture_path="raw/x", media_type="application/zip")
    row = conn.execute("SELECT numbering, paragraph_count, court_id FROM decisions WHERE id=?",
                       (decision_id,)).fetchone()
    assert (row["numbering"], row["paragraph_count"], row["court_id"]) == ("rn", 32, "ovg-sn")


# --- fake HTTP ---------------------------------------------------------------------------------------

class FakeHttp:
    """Serves fixtures; records (method, url, data)."""

    def __init__(self, routes):
        self.routes, self.calls, self.requests_made = routes, [], 0

    def _serve(self, method, url, data):
        self.calls.append((method, url, dict(data or {})))
        self.requests_made += 1
        body, headers = self.routes(method, url, data or {})
        return Response(url=url, status=200, body=body, headers=headers, elapsed=0.0)

    def get(self, url, **kw):
        return self._serve("GET", url, None)

    def post(self, url, data=None, **kw):
        return self._serve("POST", url, data)


HTML = {"content-type": "text/html; charset=utf-8"}


def test_list_items_limit_needs_one_ovg_request(fixture):
    fake = FakeHttp(lambda m, u, d: (fixture("sn/ovg-list-2026.html.gz"), HTML))
    listing = SachsenAdapter().list_items(fake, None, since=None, limit=5, courts=None)
    assert [(m, u, d["datum"]) for m, u, d in fake.calls] == [("POST", OVG_SEARCH, str(date.today().year))]
    assert len(listing.items) == 197 and listing.complete is False and listing.upstream_total is None


def test_list_items_counts_only_unstored_items(fixture, tmp_path):
    conn = connect(tmp_path / "c.sqlite")
    init_db(conn)
    _, items = parse_ovg_list(html(fixture, "ovg-list-2026.html.gz"))
    conn.executemany("INSERT INTO source_items(source_id, source_doc_id, url, state) VALUES('sn', ?, ?, 'stored')",
                     [(i["source_doc_id"], i["url"]) for i in items[:195]])
    first_year = str(date.today().year)

    def routes(method, url, data):
        name = "ovg-list-2026.html.gz" if data.get("datum") == first_year else "ovg-list-empty.html"
        return fixture(f"sn/{name}"), HTML
    fake = FakeHttp(routes)
    listing = SachsenAdapter().list_items(fake, conn, since=None, limit=5, courts=["ovg"])
    # only 2 of the 197 rows are new: the listing pages on through all older year windows
    windows = [d["datum"] for _, _, d in fake.calls]
    assert windows[0] == first_year and windows[1] == str(date.today().year - 1)
    assert windows[-1] == "01.01.1900-31.12.1990" and len(windows) == date.today().year - 1991 + 2
    assert len(listing.items) == 197 and listing.complete is False


def test_list_items_since_is_one_range_request(fixture):
    fake = FakeHttp(lambda m, u, d: (fixture("sn/ovg-list-2026.html.gz"), HTML))
    listing = SachsenAdapter().list_items(fake, None, since="2026-09-01", limit=None, courts=["ovg"])
    assert [d["datum"] for _, _, d in fake.calls] == [f"01.09.2026-{date.today().strftime('%d.%m.%Y')}"]
    # rows before `since` on the (fixture) page are not taken over
    assert [i["listed_case_numbers"] for i in listing.items] == ["2 B 264/26", "3 A 487/26.A"]
    assert listing.complete is False and listing.upstream_total is None


# --- ESAMOSplus ------------------------------------------------------------------------------------

def test_esamos_newest_and_result_tables(fixture):
    newest = parse_rows(BeautifulSoup(html(fixture, "esamos-suchen.html.gz"), "html.parser"), "DV16_Table")
    assert len(newest) == 10 and newest[0]["date"] == "17.08.2026" and newest[0]["doc_key"] == "4U719-26"
    assert [r["doc_key"] for r in newest if r["az"] == "4 U 374/26"] == ["4U374-26_02", "4U374-26"]
    assert newest[0]["leitsatz"].startswith("1. Um die Ursächlichkeit der Pflichtverletzung eines Rechtsanwalts")
    rows, current, last, forward = parse_esamos_results(html(fixture, "esamos-treffer-2026-p1.html.gz"))
    assert (len(rows), current, last, forward) == (6, 1, 13, "ctl21")
    first = rows[0]
    assert (first["source_doc_id"], first["listed_date"], first["listed_case_numbers"], first["listed_court"],
            first["priority"]) == ("esamos:1ORs25SRs447-25", "2026-03-27", "1 ORs 25 SRs 447/25", "olg-dresden", 1)
    assert first["url"] == "https://www.justiz.sachsen.de/esamosplus/pages/index.aspx#dokument=1ORs25SRs447-25"
    assert rows[3]["source_doc_id"] == "esamos:12U206.26"            # key taken verbatim ("12U206.26...")


def test_esamos_list_limit_uses_the_newest_table(fixture):
    fake = FakeHttp(lambda m, u, d: (fixture("sn/esamos-suchen.html.gz"), HTML))
    listing = SachsenAdapter().list_items(fake, None, since=None, limit=3, courts=["olg"])
    assert [(m, u) for m, u, _ in fake.calls] == [("GET", ES_SEARCH)]
    assert len(listing.items) == 10 and listing.complete is False
    assert {i["listed_court"] for i in listing.items} == {"olg-dresden"}


def esamos_routes(fixture, filename="4U374-26_02.pdf"):
    def routes(method, url, data):
        if method == "GET":
            return fixture("sn/esamos-suchen.html.gz"), HTML
        if url == ES_SEARCH:
            return fixture("sn/esamos-treffer-az-4U374-26.html.gz"), HTML
        assert url == ES_RESULTS and data.get("DV13_Table$ctl04$DV13_Table_Col3_C1") == "4U374-26_02..."
        return fixture("sn/esamos-4U374-26_02.pdf"), {
            "content-type": "application/pdf", "content-disposition": f'attachment; filename="{filename}"'}
    return routes


def test_esamos_fetch_posts_search_then_row_button(fixture, es_item):
    fake = FakeHttp(esamos_routes(fixture))
    capture = SachsenAdapter().fetch(fake, es_item)
    assert capture.media_type == "application/pdf" and capture.body == fixture("sn/esamos-4U374-26_02.pdf")
    assert [(m, u) for m, u, _ in fake.calls] == [("GET", ES_SEARCH), ("POST", ES_SEARCH), ("POST", ES_RESULTS)]
    search = fake.calls[1][2]
    assert (search["DV1_C33"], search["DV1_C34"], search["DV1_C35"], search["DV1_C24"]) == (
        "", "05.08.2026", "05.08.2026", "Suchen")
    assert search["__VIEWSTATE"] == "fixture-esamos-suchen-state"
    assert fake.calls[2][2]["__VIEWSTATE"] == "fixture-esamos-treffer-az-4U374-26-state"


def test_esamos_fetch_checks_the_delivered_file_name(fixture, es_item):
    fake = FakeHttp(esamos_routes(fixture, filename="4U374-26.pdf"))
    with pytest.raises(UpstreamFailure) as err:
        SachsenAdapter().fetch(fake, es_item)
    assert err.value.kind == "identity_mismatch"


@needs_pdftotext
def test_esamos_decision(fixture, es_item):
    parsed = parse_es(fixture, es_item)
    assert (parsed.court_name, parsed.court_id, parsed.jurisdiction, parsed.land) == (
        "Oberlandesgericht Dresden", "olg-dresden", "ordentlich", "sn")
    assert parsed.decision_date == "2026-08-05" and parsed.decision_type == "Beschluss"
    assert parsed.case_numbers_raw == "4 U 374/26" and parsed.case_numbers == ["4 U 374/26"]
    assert parsed.ecli is None and parsed.chamber == "4. Zivilsenat"
    assert parsed.title == "OLG Dresden, 4. Zivilsenat, Beschluss vom 5. August 2026, Az.: 4 U 374/26"
    assert [(s.name, s.authorship) for s in parsed.sections] == [
        ("titel", "editorial"), ("leitsatz", "court"), ("tenor", "court"), ("gruende", "court")]
    assert [b.text for b in parsed.sections[0].blocks] == [
        "OLG Dresden, 4. Zivilsenat, Beschluss vom 5. August 2026, Az.: 4 U 374/26",
        "Hinweisbeschluss vom 16. Juli 2026 vorausgehend."]
    assert parsed.procedural[0]["court"] == "Landgericht Dresden" and \
        parsed.procedural[0]["case_number"] == "8 O 2006/22"
    assert parsed.metadata["identity"]["citation_date"] == "2026-08-05"
    assert all(b.label is None for s in parsed.sections for b in s.blocks)

    paragraphs, numbering = assemble("D", parsed.sections)
    assert numbering == "none"
    assert [p.id for p in paragraphs][:8] == ["D#titel-1", "D#titel-2", "D#leitsatz-1", "D#tenor-1", "D#tenor-2",
                                              "D#tenor-3", "D#tenor-4", "D#p-1"]
    assert locate_quote(paragraphs, "Maßgeblich ist das objektiv vorliegende Krankheitsbild.") == ["D#p-3"]
    assert locate_quote(paragraphs, "Eine Vorerkrankung, die zu einem Leistungsausschluss in der "
                                    "Reiserücktrittskostenversicherung führt") == ["D#leitsatz-1"]


@needs_pdftotext
@pytest.mark.parametrize("over,detail", [
    ({"listed_case_numbers": "4 U 375/26"}, "listed Az"),
    ({"listed_date": "2026-07-16"}, "listed date"),
])
def test_esamos_identity_is_validated(fixture, es_item, over, detail):
    with pytest.raises(UpstreamFailure) as err:
        parse_es(fixture, es_item | over)
    assert err.value.kind == "identity_mismatch" and detail in err.value.detail


@needs_pdftotext
def test_esamos_older_layout(fixture):
    rows, current, last, _ = parse_esamos_results(html(fixture, "esamos-treffer-1900-1999-p1.html.gz"))
    assert (len(rows), current) == (6, 1) and last > 1
    item = next(r for r in rows if r["source_doc_id"] == "esamos:1Ss421.99")
    item.pop("_row")
    assert (item["listed_date"], item["listed_case_numbers"]) == ("1999-10-28", "1 Ss 421/99")
    parsed = SachsenAdapter().parse(Capture(fixture("sn/esamos-1Ss421.99.pdf"), item["url"], "application/pdf",
                                            "pdf"), item)
    assert (parsed.decision_type, parsed.chamber, parsed.case_numbers) == ("Beschluss", "1. Strafsenat",
                                                                           ["1 Ss 421/99"])
    assert parsed.title == "Beschl. vom 28.10.1999, Az. 1 Ss 421/99"
    # "Aktenzeichen: 1 Ss 421/99 5 Ns 951 Js 19175/98 LG Görlitz": own Az first, then the lower instance
    assert [(p["court"], p["case_number"], p["field"]) for p in parsed.procedural] == [
        ("LG Görlitz", "5 Ns 951 Js 19175/98", "pdf:Aktenzeichen")]
    assert [(s.name, s.authorship) for s in parsed.sections] == [
        ("titel", "editorial"), ("leitsatz", "court"), ("sonstiger_langtext", "court"), ("gruende", "court")]
    assert parsed.sections[1].blocks[0].text.startswith("Bei der Versagung der Strafaussetzung zur Bewährung")
    assert parsed.metadata["tenor_found"] is False       # no 'beschlossen:': rubrum + tenor kept, not dropped
    paragraphs, numbering = assemble("D", parsed.sections)
    assert numbering == "none"
    assert locate_quote(paragraphs, "Im Umfang der Aufhebung wird die Sache zu neuer Verhandlung und "
                                    "Entscheidung") == ["D#p-10"]


# --- typed failures for non-documents ---------------------------------------------------------------

CHALLENGE = (b"<html><head><title>Just a moment...</title></head><body><div class='cf-chl-widget'></div>"
             b"</body></html>")        # synthetic: no challenge was ever served by www.justiz.sachsen.de


def test_missing_ovg_record_on_fetch_is_typed(fixture, ovg_item):
    fake = FakeHttp(lambda m, u, d: (fixture("sn/ovg-doc-missing.html"), HTML))
    with pytest.raises(UpstreamFailure) as err:
        SachsenAdapter().fetch(fake, ovg_item)
    assert err.value.kind == "empty_document" and len(fake.calls) == 1        # no PDF request


@pytest.mark.parametrize("which", ["ovg-doc", "ovg-list", "esamos-form"])
def test_challenge_page_is_typed(ovg_item, which):
    fake = FakeHttp(lambda m, u, d: (CHALLENGE, HTML))
    with pytest.raises(UpstreamFailure) as err:
        if which == "ovg-doc":
            SachsenAdapter().fetch(fake, ovg_item)
        elif which == "ovg-list":
            SachsenAdapter().list_items(fake, None, since=None, limit=1, courts=["ovg"])
        else:
            SachsenAdapter().list_items(fake, None, since=None, limit=1, courts=["olg"])
    assert err.value.kind == "challenge"


def test_html_instead_of_ovg_capture_is_typed(ovg_item):
    with pytest.raises(UpstreamFailure) as err:
        SachsenAdapter().parse(Capture(b"<html><body>Fehler</body></html>", DOC_URL, "text/html", "html"),
                               ovg_item)
    assert err.value.kind == "unexpected_representation"


@needs_pdftotext
def test_ovg_form_uses_matched_pdf_and_keeps_the_catalogue_label(fixture, ovg_item):
    page = fixture("sn/ovg-doc-8012.html").replace(b"<BR />Beschluss<BR />", b"<BR />Urteil<BR />")
    capture = ovg_capture(page, DOC_URL, fixture("sn/ovg-26B264.B01.pdf"), PDF_URL, "documents/26B264.B01.pdf")
    parsed = parse_ovg(capture, ovg_item)
    assert parsed.decision_type == "Beschluss"
    assert parsed.metadata["decision_type_evidence"] == {
        "catalog": "Urteil", "pdf_cover": "Beschluss", "docket_on_cover": True, "date_on_front": True,
        "operative_dates": ["2026-09-21"],
    }
