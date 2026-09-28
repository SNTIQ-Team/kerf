"""Historical GII / NeuRIS captures: parsers, archive, fetcher, parse tool.

Fixtures (tests/fixtures/history/) are real bytes:

* ``gii-sgb_2-20060905034624-juris-table.html.gz`` - Wayback capture of the
  2006 table layout (excerpt: header, Kapitel 1/2, §§ 1, 2, 6a, 7),
* ``gii-sgb_2-20061207204750-jnnorm-2006.html.gz`` - first ``jnnorm`` layout
  with unofficial sentence numbers (excerpt: §§ 1, 2, 7),
* ``gii-asylblg-20121227221957-jnnorm-2008.html.gz`` and
  ``gii-asylblg-20121227222021-xml.zip`` - unmodified same-day Wayback
  captures of the full AsylbLG page and the official XML package,
* ``gii-sgb_2-20260710123039-jnnorm-2022.html.gz`` - 2026 Wayback capture
  (excerpt incl. the XML ``<TOC>`` Inhaltsübersicht),
* ``gii-bgb-live20260928-…`` / ``gii-zpo-live20260928-…`` - excerpts of the
  live GII pages (footnote references, footnotes, "(XXXX)" ranges, titles
  with ``<br>``),
* ``neuris-aufenthg-2026-07-29-2026-08-10-excerpt.zip`` - NeuRIS expression
  trimmed to §§ 1, 23, 62d plus its Anlage,
* ``gii-bgb-20120729…``, ``gii-zpo-20121014…``, ``gii-bgb-20160423…``,
  ``gii-bgb-20220101…`` - Wayback excerpts with GII placeholder designators
  and an Ident-class table of contents, ``gii-zpo-20061018053022-…`` - a
  legacy page with a designator-less heading,
* ``gii-asylblg-2007…/2008…-norm-page__N.html.gz`` - unmodified Wayback
  captures of single-norm pages,
* ``expected_gii_xml_norms.json`` - XML-derived texts
  (``fetch_gii.parse_law``): Lexgraph daily states for the 2026 fixtures,
  archived xml.zip captures of the same weeks for the older ones.
"""
from __future__ import annotations

import gzip
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tools"))

import fetch_history_wayback as wb  # noqa: E402
import gii_html_parser as gp  # noqa: E402
import neuris_ldml_parser as nl  # noqa: E402
import parse_history_captures as phc  # noqa: E402
from fetch_gii import parse_law  # noqa: E402
from history_archive import HistoryArchive, sha256_hex, wayback_digest  # noqa: E402

from api.history_identity import norm_identity  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "history"
EXPECTED = json.loads((FIX / "expected_gii_xml_norms.json").read_text("utf-8"))


def html(name: str) -> bytes:
    return gzip.decompress((FIX / name).read_bytes())


def ids(norms) -> list[str]:
    return [norm_identity(n["enbez"], n["titel"], n["text"]) for n in norms]


def by_enbez(record) -> dict:
    return {n["enbez"]: n for n in record["norms"]}


# ------------------------------------------------------------------ layouts

def test_juris_table_layout_2006():
    rec = gp.parse_gii_html(html("gii-sgb_2-20060905034624-juris-table.html.gz"))
    assert rec["layout"] == "juris-table-2006"
    assert rec["complete"] is True and rec["warnings"] == []
    assert rec["jurabk"] == "SGB 2"
    assert rec["stand"]["entries"] == [
        {"type": "Stand",
         "text": "Zuletzt geändert durch Art. 1 G v. 20.7.2006 I 1706"}]
    assert [n["enbez"] for n in rec["norms"]] == ["§ 1", "§ 2", "§ 6a", "§ 7"]
    norms = by_enbez(rec)
    assert norms["§ 7"]["titel"] == "Berechtigte"
    assert norms["§ 7"]["gliederung"] == "Kapitel 2 Anspruchsvoraussetzungen"
    assert norms["§ 1"]["gliederung"] == "Kapitel 1 Fördern und Fordern"
    # list numbers sit in their own cell followed by two nbsp: removed
    assert "die1.das 15. Lebensjahr" in norms["§ 7"]["text"]
    assert "\xa0" not in norms["§ 7"]["text"]


def test_jnnorm_2006_drops_unofficial_sentence_numbers():
    rec = gp.parse_gii_html(html("gii-sgb_2-20061207204750-jnnorm-2006.html.gz"))
    assert rec["layout"] == "jnnorm-2006" and rec["complete"]
    assert rec["stand"]["raw"] == \
        "Stand: Zuletzt geändert durch Art. 253 V v. 31.10.2006 I 2407"
    text = by_enbez(rec)["§ 7"]["text"]
    # raw HTML: "(1) <span class="Rec nichtamtlich">1</span>Leistungen ..."
    assert text.startswith("(1) Leistungen nach diesem Buch erhalten Personen,"
                           " die 1.das 15. Lebensjahr")
    assert "(erwerbsfähige Hilfebedürftige). Ausgenommen" in text


def test_identity_is_stable_across_layout_change():
    """§ 2 SGB II was not amended between Sept and Dec 2006: the table layout
    and the first jnnorm layout must give the same identity."""
    old = by_enbez(gp.parse_gii_html(
        html("gii-sgb_2-20060905034624-juris-table.html.gz")))
    new = by_enbez(gp.parse_gii_html(
        html("gii-sgb_2-20061207204750-jnnorm-2006.html.gz")))
    for label in ("§ 1", "§ 2"):
        assert ids([old[label]]) == ids([new[label]])


def test_jnnorm_2008_html_equals_same_day_xml_zip():
    page = gp.parse_gii_html(html("gii-asylblg-20121227221957-jnnorm-2008.html.gz"))
    package = (FIX / "gii-asylblg-20121227222021-xml.zip").read_bytes()
    xml = gp.parse_gii_xml_zip(package, "asylblg")
    assert page["layout"] == "jnnorm-2008" and page["complete"]
    assert xml["layout"] == "gii-xml" and xml["complete"]
    assert xml["norm_count"] == page["norm_count"] == 19
    assert ids(page["norms"]) == ids(xml["norms"])
    assert page["stand"]["raw"] == xml["stand"]["raw"]
    assert xml["builddate"] and xml["doknr"] == "BJNR107410993"
    assert page["doknr"] == "BJNR107410993"


def test_xml_zip_parse_is_fetch_gii_parse_law():
    package = (FIX / "gii-asylblg-20121227222021-xml.zip").read_bytes()
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        member = next(m for m in archive.namelist() if m.endswith(".xml"))
        act, norms = parse_law(archive.read(member), "asylblg")
    rec = gp.parse_gii_xml_zip(package, "asylblg")
    assert [(n["enbez"], n["titel"], n["text"]) for n in rec["norms"]] == \
        [(n["enbez"], n["titel"], n["text"]) for n in norms]
    assert rec["stand"]["raw"] == act["stand"]
    assert [e["type"] for e in rec["stand"]["entries"]]


@pytest.mark.parametrize("fixture", [
    "gii-sgb_2-20260710123039-jnnorm-2022.html.gz",
    "gii-bgb-live20260928-jnnorm-2022.html.gz",
    "gii-zpo-live20260928-jnnorm-2022.html.gz",
    # older pages against the archived xml.zip of the same weeks
    "gii-bgb-20120729-jnnorm-2008.html.gz",
    "gii-zpo-20121014-jnnorm-2008.html.gz",
    "gii-bgb-20160423-jnnorm-2008.html.gz",
    "gii-bgb-20220101-jnnorm-2008.html.gz",
])
def test_html_matches_xml_derived_texts(fixture):
    rec = gp.parse_gii_html(html(fixture))
    assert rec["layout"].startswith("jnnorm") and rec["complete"]
    expected = EXPECTED[fixture]["norms"]
    assert ids(rec["norms"]) == ids(expected)
    # beyond identity, the text differs at most by whitespace
    for got, ref in zip(rec["norms"], expected):
        assert (got["enbez"], got["titel"]) == (ref["enbez"], ref["titel"])
        assert got["text"].split() == ref["text"].split() or \
            "".join(got["text"].split()) == "".join(ref["text"].split())


def test_jnnorm_2022_special_cases():
    sgb2 = by_enbez(gp.parse_gii_html(
        html("gii-sgb_2-20260710123039-jnnorm-2022.html.gz")))
    # XML <TOC> (text-less in parse_law) is rendered by GII: suppressed
    assert sgb2["Inhaltsübersicht"]["text"] == ""
    assert "toc_element_text_suppressed" in sgb2["Inhaltsübersicht"]["flags"]
    bgb = by_enbez(gp.parse_gii_html(html("gii-bgb-live20260928-jnnorm-2022.html.gz")))
    assert "*)" not in bgb["§ 14"]["text"]               # FnR marker
    assert "Amtlicher Hinweis" not in bgb["§ 14"]["text"]  # footnote
    assert "(XXXX) §§ 15 bis 20" in bgb
    assert bgb["§ 611b"] == {**bgb["§ 611b"], "titel": "(weggefallen)", "text": ""}
    zpo = by_enbez(gp.parse_gii_html(html("gii-zpo-live20260928-jnnorm-2022.html.gz")))
    assert zpo["§ 799a"]["titel"] == "Schadensersatzpflicht bei der"
    assert "titel_cut_at_child_element" in zpo["§ 799a"]["flags"]


def test_placeholder_designators_follow_the_xml():
    """GII XML writes "(XXXX)" designators the HTML never shows."""
    bgb16 = by_enbez(gp.parse_gii_html(html("gii-bgb-20160423-jnnorm-2008.html.gz")))
    # no designator span, the range in the title: XML enbez "(XXXX)"
    assert bgb16["(XXXX)"]["titel"] == "§§ 2354 bis 2359"
    assert "(XXXX) §§ 114, 115" in bgb16            # comma ranges too
    zpo12 = by_enbez(gp.parse_gii_html(html("gii-zpo-20121014-jnnorm-2008.html.gz")))
    assert zpo12["(XXXX) § 802l"]["titel"] == "(zukünftig in Kraft)"
    bgb22 = by_enbez(gp.parse_gii_html(html("gii-bgb-20220101-jnnorm-2008.html.gz")))
    # blank-padded " (zukünftig in Kraft) " is generated, XML titel empty
    assert bgb22["(XXXX) §§ 707a bis 707c"]["titel"] == ""
    bgb12 = by_enbez(gp.parse_gii_html(html("gii-bgb-20120729-jnnorm-2008.html.gz")))
    assert bgb12["Inhaltsübersicht"]["text"] == ""   # Ident-class <TOC>


def test_legacy_heading_without_designator_is_skipped():
    rec = gp.parse_gii_html(html("gii-zpo-20061018053022-juris-table.html.gz"))
    assert rec["layout"] == "juris-table-2006" and rec["complete"]
    assert [n["enbez"] for n in rec["norms"]] == \
        ["§ 1", "§ 2", "(XXXX) §§ 195a bis 213a"]
    assert rec["warnings"] == [
        "heading without designator skipped: Zivilprozessordnung"]
    assert [e["type"] for e in rec["stand"]["entries"]] == ["Neuf", "Stand"]


@pytest.mark.parametrize("fixture", [
    "gii-asylblg-20070104022451-norm-page__10.html.gz",   # 2006 markup, Rec
    "gii-asylblg-20081204072916-norm-page__2.html.gz",    # 2008 markup
])
def test_single_norm_pages(fixture):
    rec = gp.parse_gii_html(html(fixture))
    assert rec["layout"] == "jnnorm-page" and len(rec["norms"]) == 1
    norm = rec["norms"][0]
    assert "single_norm_page" in norm["flags"]
    assert rec["jurabk"] == "AsylbLG"
    # both norms were unchanged until the Dec 2012 full-page capture
    full = gp.parse_gii_html(html("gii-asylblg-20121227221957-jnnorm-2008.html.gz"))
    assert ids([norm])[0] in set(ids(full["norms"]))


def test_gap_years_and_norm_page_filter():
    rows = [{"timestamp": "20060101000000", "statuscode": "200"},
            {"timestamp": "20080101000000", "statuscode": "200"},
            {"timestamp": "20070101000000", "statuscode": "302"}]
    assert wb.gap_years(rows, first=2006, last=2009) == [2007, 2009]
    assert wb.is_norm_page({"original": "http://bundesrecht.juris.de:80/asylblg/__2.html"}, "asylblg")
    assert wb.is_norm_page({"original": "https://www.gesetze-im-internet.de/gg/art_1.html"}, "gg")
    assert not wb.is_norm_page({"original": "https://www.gesetze-im-internet.de/asylblg_x/__2.html"}, "asylblg")
    assert "from=2010" in wb.cdx_url("x/", prefix_filter=".*", year=2010)


def test_truncated_or_foreign_pages_are_incomplete():
    page = html("gii-asylblg-20121227221957-jnnorm-2008.html.gz")
    full = gp.parse_gii_html(page)
    cut = gp.parse_gii_html(page[: len(page) * 2 // 3])
    assert cut["complete"] is False
    assert any("truncated" in w for w in cut["warnings"])
    # the norm cut mid-text is dropped; everything kept is a real version
    assert 0 < cut["norm_count"] < full["norm_count"]
    assert set(ids(cut["norms"])) <= set(ids(full["norms"]))
    assert cut["truncated_after"] == cut["norms"][-1]["enbez"]
    assert any("dropped" in w for w in cut["warnings"])
    other = gp.parse_gii_html(b"<html><body><p>Wartungsarbeiten</p></body></html>")
    assert other["complete"] is False and other["norms"] == []
    assert "unrecognised page layout" in other["warnings"]


# ------------------------------------------------------------------- neuris

def test_neuris_expression_parse():
    fixture = "neuris-aufenthg-2026-07-29-2026-08-10-excerpt.zip"
    rec = nl.parse_neuris_zip((FIX / fixture).read_bytes())
    assert rec["complete"] and rec["jurabk"] == "AufenthG 2004"
    ident = rec["source_identifiers"]
    assert ident["expression_eli"] == "eli/bund/bgbl-1/2004/s1950/2026-07-29/1/deu"
    assert ident["expression_date_aenderung"] == "2026-07-29"
    assert ident["manifestation_date_generierung"] == "2026-08-10"
    assert ident["date_basis"] == "eli_frbr_identifiers_not_legal_effect"
    assert [e["type"] for e in rec["stand"]["entries"]][:3] == \
        ["Neuf", "Stand", "Hinweis"]
    got = dict(zip([n["enbez"] for n in rec["norms"]], ids(rec["norms"])))
    expected = EXPECTED[fixture]["norms"]
    matched = [n["enbez"] for n in expected
               if got.get(n["enbez"]) == ids([n])[0]]
    # AufenthG's GII table of contents is an XML <TOC> (text-less); NeuRIS
    # carries it as preamble TOC text - the one documented residual.
    assert matched == ["§ 1", "§ 23", "§ 62d", "Anlage"]
    norms = by_enbez(rec)
    assert norms["§ 23"]["titel"] == "Aufenthaltsgewährung durch die obersten Landesbehörden;"
    assert norms["§ 1"]["text"].startswith("(1) Das Gesetz dient")
    assert norms["Anlage"]["titel"] == "(zu § 20a Absatz 3 Nummer 2, § 20b)"


# ---------------------------------------------------------------- archive

def test_archive_raw_store_is_content_addressed_and_verified(tmp_path):
    archive = HistoryArchive(tmp_path)
    sha, path = archive.put_raw("wayback_gii", b"<html>x</html>", "html")
    assert sha == sha256_hex(b"<html>x</html>")
    assert path.name == f"{sha}.html.gz" and path.parent.name == sha[:2]
    assert archive.put_raw("wayback_gii", b"<html>x</html>", "html")[1] == path
    assert archive.read_raw("wayback_gii", sha) == b"<html>x</html>"
    path.write_bytes(gzip.compress(b"tampered", mtime=0))
    with pytest.raises(ValueError):
        archive.read_raw("wayback_gii", sha)


# ---------------------------------------------------------------- fetcher

class FakeResponse:
    def __init__(self, status, body=b"", url="https://web.archive.org/x"):
        self.status_code = status
        self._body = body
        self.url = url

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


def client_with(responses, **kw):
    clock = FakeClock()
    session = FakeSession(responses)
    client = wb.PoliteClient(session=session, sleep=clock.sleep,
                             clock=clock.monotonic, **kw)
    return client, session, clock


def test_polite_client_throttles_and_backs_off():
    client, session, clock = client_with(
        [FakeResponse(200, b"a"), FakeResponse(503), FakeResponse(200, b"b")],
        delay=0.5)                        # clamped to the 2 s minimum
    assert client.get("u1")[0] == 200
    assert client.get("u2")[:2] == (200, b"b")
    assert client.delay == 2.0
    # second request waited the full delay, the retry 30 s more
    assert clock.sleeps[0] == pytest.approx(2.0)
    assert clock.sleeps[1] == pytest.approx(32.0)
    assert client.consecutive_failures == 0


def test_polite_client_stops_after_repeated_failures_and_budget():
    client, _, _ = client_with([FakeResponse(429)] * 5,
                               max_consecutive_failures=3)
    with pytest.raises(wb.StopRun):
        client.get("u")
    client, _, _ = client_with([FakeResponse(200, b"x")] * 3, max_requests=1)
    client.get("u")
    with pytest.raises(wb.StopRun):
        client.get("u")


def target(ts, digest, original="http://www.gesetze-im-internet.de:80/asylblg/BJNR107410993.html"):
    return wb.Target("fed_asylblg", "asylblg", "act_html", ts, original,
                     digest, 100, "text/html")


def test_capture_dedupes_digests_and_resumes(tmp_path):
    archive = HistoryArchive(tmp_path)
    page = html("gii-asylblg-20121227221957-jnnorm-2008.html.gz")
    digest = wayback_digest(page)
    targets = [target("20121227221957", digest),
               target("20130101000000", digest),          # same payload later
               target("20130202000000", "OTHER"),
               target("20130303000000", "BROKEN")]
    client, session, _ = client_with(
        [FakeResponse(200, page), FakeResponse(200, page + b"<!-- x -->"),
         FakeResponse(404)])
    stats = wb.capture(client, archive, targets)
    rows = archive.read_ledger("wayback_gii")
    assert [r["status"] for r in rows] == ["ok", "skipped", "ok", "error"]
    assert rows[0]["digest_verified"] is True
    assert rows[1]["reason"] == "duplicate_digest"
    assert rows[1]["sha256"] == rows[0]["sha256"]
    assert rows[0]["captured_at"] == "2012-12-27T22:19:57Z"
    assert rows[0]["archive_url"] == ("https://web.archive.org/web/"
                                      "20121227221957id_/" + targets[0].original)
    assert rows[0]["original_url"] == \
        "https://www.gesetze-im-internet.de/asylblg/BJNR107410993.html"
    assert len(session.calls) == 3
    assert stats["fed_asylblg"]["failed"] == 1
    # resume: nothing is fetched again except the failed capture
    client, session, _ = client_with([FakeResponse(200, page)])
    wb.capture(client, archive, targets)
    assert len(session.calls) == 1
    last = archive.read_ledger("wayback_gii")[-1]
    assert last["status"] == "ok" and last["sha256"] == rows[0]["sha256"]
    assert len(list(archive.iter_raw("wayback_gii"))) == 2


def test_alt_pages_stay_in_the_act_folder():
    row = {"original": "https://www.gesetze-im-internet.de/sgb_10_kap3/BJNR014509982.html"}
    assert not wb.is_alt_page(row, "sgb_10", "BJNR114690980")
    row = {"original": "http://www.gesetze-im-internet.de/sgb_10/BJNR114690980.html"}
    assert not wb.is_alt_page(row, "sgb_10", "BJNR114690980")
    row = {"original": "http://bundesrecht.juris.de:80/sgb_10/BJNR999990980.html"}
    assert wb.is_alt_page(row, "sgb_10", "BJNR114690980")


def test_parse_cdx_rows():
    body = json.dumps([["timestamp", "original", "statuscode"],
                       ["20240101000000", "https://x/y", "200"]]).encode()
    assert wb.parse_cdx(body) == [{"timestamp": "20240101000000",
                                   "original": "https://x/y",
                                   "statuscode": "200"}]
    assert wb.parse_cdx(b"") == []


# ------------------------------------------------------------- parse tool

def test_parse_tool_marks_single_norm_pages_partial(tmp_path):
    archive = HistoryArchive(tmp_path)
    data = html("gii-asylblg-20081204072916-norm-page__2.html.gz")
    sha, _ = archive.put_raw("wayback_gii", data, "html")
    url = "https://bundesrecht.juris.de/asylblg/__2.html"
    archive.append_ledger("wayback_gii", {
        "act_id": "fed_asylblg", "status": "ok", "sha256": sha,
        "kind": "norm_html", "capture_timestamp": "20081204072916",
        "captured_at": "2008-12-04T07:29:16Z", "original_url": url,
        "archive_url": f"https://web.archive.org/web/20081204072916id_/{url}"})
    phc.run(archive, ["wayback_gii"], None, log=lambda *_: None)
    record = next(archive.iter_parsed("wayback_gii"))
    assert record["complete"] is False and record["norm_count"] == 1
    assert "single-norm page: partial act by design" in record["warnings"]
    assert record["captures"][0]["kind"] == "norm_html"


def test_parse_tool_is_idempotent(tmp_path):
    archive = HistoryArchive(tmp_path)
    page = html("gii-asylblg-20121227221957-jnnorm-2008.html.gz")
    package = (FIX / "gii-asylblg-20121227222021-xml.zip").read_bytes()
    for data, ext, ts, url in [
            (page, "html", "20121227221957", "https://www.gesetze-im-internet.de/asylblg/BJNR107410993.html"),
            (package, "zip", "20121227222021", "https://www.gesetze-im-internet.de/asylblg/xml.zip")]:
        sha, _ = archive.put_raw("wayback_gii", data, ext)
        archive.append_ledger("wayback_gii", {
            "schema": "lexgraph-history-capture/1", "source": "wayback_gii",
            "act_id": "fed_asylblg", "status": "ok", "sha256": sha,
            "capture_timestamp": ts, "captured_at": wb.captured_at(ts),
            "original_url": url,
            "archive_url": f"https://web.archive.org/web/{ts}id_/{url}"})
    stats = phc.run(archive, ["wayback_gii"], None, log=lambda *_: None)
    assert stats["parsed"] == 2 and stats["complete"] == 2
    again = phc.run(archive, ["wayback_gii"], None, log=lambda *_: None)
    assert again["unchanged"] == 2 and again["parsed"] == 0
    records = {r["media_ext"]: r for r in archive.iter_parsed("wayback_gii")}
    assert records["html"]["schema"] == "lexgraph-history-parsed/1"
    assert records["html"]["parser"] == gp.PARSER_VERSION
    assert records["zip"]["parser"] == gp.XML_PARSER_VERSION
    assert records["html"]["captured_at"] == "2012-12-27T22:19:57Z"
    assert records["html"]["date_basis"] == "capture_observation_not_legal_date"
    assert [n["identity"] for n in records["html"]["norms"]] == \
        [n["identity"] for n in records["zip"]["norms"]]
