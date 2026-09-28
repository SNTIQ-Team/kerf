"""Secondary copies: Wayback (archived primary pages), Open Legal Data (aggregator), prefetch staging, and the
precedence of primary sources over them."""
import gzip
import json
import sqlite3
import time
from pathlib import Path

import pytest

from courts.errors import UpstreamFailure
from courts.model import Block, ParsedDecision, Section
from courts.sources import get_adapter
from courts.sources.base import Capture
from courts.store import SECONDARY_SOURCES, connect, finalise, init_db, source_kind, store_decision

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).with_name("fixtures")
DECISIONS = ROOT / "conformance" / "fixtures" / "decisions"


def gz(path: Path) -> bytes:
    return gzip.decompress(path.read_bytes())


def wayback_item(route: str, doc: str, ts: str = "20250817161656", listed_date=None, az=None) -> dict:
    original = (f"https://www.gesetze-bayern.de/Content/Document/{doc}" if route == "by"
                else f"https://www.bverwg.de/de/{doc}")
    return {"source_doc_id": f"{route}:{doc}", "url": f"https://web.archive.org/web/{ts}id_/{original}",
            "listed_date": listed_date, "listed_court": "bverwg" if route == "bverwg" else None,
            "listed_case_numbers": az,
            "extra": {"route": route, "doc": doc, "original": original, "captures": [[ts, original]]}}


def test_adapters_declare_the_source_kind_the_store_ranks():
    for source_id, kind in SECONDARY_SOURCES.items():
        assert get_adapter(source_id).source_kind == kind
    assert get_adapter("by").source_kind == "primary" and source_kind("by") == "primary"


# --- Wayback ----------------------------------------------------------------------------------------------

def test_wayback_by_page_parses_as_an_archived_copy():
    doc = "Y-300-Z-BECKRS-B-2023-N-10142"
    item = wayback_item("by", doc)
    body = gz(DECISIONS / "bayern-recht-B-2023-N-10142.html.gz")
    d = finalise(get_adapter("wayback").parse(Capture(body, item["url"], "text/html", "html"), item))
    assert (d.source_id, d.source_doc_id, d.court_id, d.decision_date) == ("wayback", f"by:{doc}", "vgh-by", "2023-01-18")
    assert d.source_url == f"https://www.gesetze-bayern.de/Content/Document/{doc}"
    assert d.metadata["source_kind"] == "archive" and d.metadata["archive"]["timestamp"] == "20250817161656"


def test_wayback_bverwg_page_and_old_layout():
    doc = "010223B4BN1.23.0"
    item = wayback_item("bverwg", doc, "20230605075242", "2023-02-01", "4 BN 1.23")
    body = gz(FIX / "wayback" / "bverwg-010223B4BN1.23.0-20230605075242.html.gz")
    d = finalise(get_adapter("wayback").parse(Capture(body, item["url"], "text/html", "html"), item))
    assert d.ecli == "ECLI:DE:BVerwG:2023:010223B4BN1.23.0" and d.source_id == "wayback"
    assert [s.name for s in d.sections] == ["tenor", "gruende"]
    # The layout before 2017 (entscheidung.php) has its own reader: full text with Randnummern.
    old = gz(FIX / "wayback" / "bverwg-010316B1B30.16.0-20160810121139-old-layout.html.gz")
    from courts.paragraphs import assemble
    from courts.sources.wayback import usable
    assert usable("bverwg", "010316B1B30.16.0", old) is None
    assert usable("bverwg", "010223B4BN1.23.0", old) == "not a bverwg.de decision page of this document"
    item = wayback_item("bverwg", "010316B1B30.16.0", "20160810121139", "2016-03-01", "1 B 30.16")
    d = finalise(get_adapter("wayback").parse(Capture(old, item["url"], "text/html", "html"), item))
    paras, numbering = assemble("D", d.sections)
    assert (d.ecli, d.decision_type, numbering) == ("ECLI:DE:BVerwG:2016:010316B1B30.16.0", "Beschluss", "rn")
    assert [p.rn for p in paras if p.rn] == [str(n) for n in range(1, 14)]
    assert [(s.name, len(s.blocks)) for s in d.sections] == [("tenor", 3), ("gruende", 15)]
    assert [p["court"] for p in d.procedural] == ["VG Bremen", "OVG Bremen"]


def test_wayback_bverwg_2015_layout_without_data_ecli():
    item = wayback_item("bverwg", "010415B1C7.15.0", "20150511111941", "2015-04-01", "1 C 7.15")
    body = gz(FIX / "wayback" / "bverwg-010415B1C7.15.0-20150511111941-2015-layout.html.gz")
    d = finalise(get_adapter("wayback").parse(Capture(body, item["url"], "text/html", "html"), item))
    assert d.ecli == "ECLI:DE:BVerwG:2015:010415B1C7.15.0" and [(s.name, len(s.blocks)) for s in d.sections] == [
        ("tenor", 5), ("gruende", 7)]


def test_wayback_bverwg_reasons_after_the_tenor_are_gruende():
    item = wayback_item("bverwg", "010206B7B80.05.0", "20240622222938", "2006-02-01", "7 B 80.05")
    body = gz(FIX / "wayback" / "bverwg-010206B7B80.05.0-20240622222938.html.gz")
    d = finalise(get_adapter("wayback").parse(Capture(body, item["url"], "text/html", "html"), item))
    assert [(s.name, len(s.blocks)) for s in d.sections] == [("tenor", 3), ("gruende", 1)]


def test_wayback_listing_from_cdx_rows():
    from courts.sources.wayback import by_items, bverwg_items
    rows = [["https://www.gesetze-bayern.de/Content/Document/Y-300-Z-BECKRS-B-2020-N-1?hl=true", "20240101000000", "9000"],
            ["https://www.gesetze-bayern.de/Content/Document/Y-300-Z-BECKRS-B-2020-N-1", "20250101000000", "3500"],
            ["https://www.gesetze-bayern.de/Content/Document/Y-300-Z-BECKRS-B-2020-N-1", "20230101000000", "12000"],
            ["https://www.gesetze-bayern.de/Content/Document/Y-300-Z-BECKRS-B-2020-N-2", "20230101000000", "3000"]]
    items = by_items(rows)
    assert [i["source_doc_id"] for i in items] == ["by:Y-300-Z-BECKRS-B-2020-N-1"]   # N-2 only a challenge-sized capture
    assert [c[0] for c in items[0]["extra"]["captures"]] == ["20240101000000", "20230101000000"]
    rows = [["https://www.bverwg.de/de/010206B7B80.05.0", "20240622222938", "5948"],
            ["http://www.bverwg.de/entscheidungen/entscheidung.php?ent=010206B7B80.05.0&add_az=7+B+80.05", "20160101000000", "20000"],
            ["https://www.bverwg.de/de/010223B4BN1.23.0", "20230605075242", "32548"]]
    items = bverwg_items(rows, {"010206B7B80.05.0": {"listed_date": "2006-02-01", "listed_case_numbers": "7 B 80.05"}})
    assert [i["source_doc_id"] for i in items] == ["bverwg:010206B7B80.05.0"]
    assert [c[0] for c in items[0]["extra"]["captures"]] == ["20240622222938", "20160101000000"]


class FakeHttp:
    def __init__(self, pages):
        self.pages, self.urls = pages, []

    def get(self, url, **kwargs):
        from courts.http import Response
        self.urls.append(url)
        body = self.pages[len(self.urls) - 1]
        return Response(url=url, status=200, body=body, headers={"content-type": "text/html"}, elapsed=0.1)


def test_wayback_fetch_skips_an_archived_challenge_and_uses_staging(fixture):
    from courts.prefetch import stage, staged_capture
    doc = "Y-300-Z-BECKRS-B-2023-N-10142"
    item = wayback_item("by", doc)
    item["extra"]["captures"] = [["20260101000000", item["extra"]["original"]], ["20250817161656", item["extra"]["original"]]]
    good = gz(DECISIONS / "bayern-recht-B-2023-N-10142.html.gz")
    http = FakeHttp([fixture("by/gesetze_bayern_security_check_503.html"), gzip.compress(good)])
    adapter = get_adapter("wayback")
    capture = adapter.fetch(http, item)
    assert "20250817161656" in capture.url and capture.body == good and len(http.urls) == 2
    stage("wayback", item, capture)
    again = adapter.fetch(FakeHttp([]), item)          # staged: no request at all
    assert again.body == good and staged_capture("wayback", item) is not None
    adapter.on_stored(item)
    assert staged_capture("wayback", item) is None
    with pytest.raises(UpstreamFailure) as failure:
        adapter.fetch(FakeHttp([fixture("by/gesetze_bayern_security_check_503.html")] * 2), item)
    assert failure.value.kind == "unexpected_representation" and "archived challenge page" in failure.value.detail


def test_prefetch_stages_without_touching_the_database(tmp_path):
    from courts.prefetch import prefetch, staged_capture
    from courts.sources.base import Listing

    class Source:
        source_id, prefetch_ok = "wayback", True

        def list_items(self, http, conn, **kw):
            return Listing(items=[{"source_doc_id": f"by:{n}", "url": f"u{n}"} for n in range(3)])

        def fetch(self, http, item):
            if item["source_doc_id"] == "by:2":
                raise UpstreamFailure("unexpected_representation", "no usable capture")
            return Capture(b"<html>" + item["source_doc_id"].encode(), item["url"], "text/html", "html")
    conn = sqlite3.connect(":memory:")
    result = prefetch(Source(), None, readonly_conn=conn, workers=2)
    assert (result["staged"], result["failed"], result["status"]) == (2, 1, "ok")
    assert staged_capture("wayback", {"source_doc_id": "by:1"}).body == b"<html>by:1"
    assert prefetch(Source(), None, readonly_conn=conn)["already_staged"] == 2
    with pytest.raises(ValueError):
        prefetch(type("NoPrefetch", (), {"source_id": "x"})(), None, readonly_conn=conn)


# --- precedence -------------------------------------------------------------------------------------------

def _decision(source_id: str, doc: str, text: str = "Die Klage wird abgewiesen.") -> ParsedDecision:
    return ParsedDecision(source_id=source_id, source_doc_id=doc, source_url=f"https://example.invalid/{doc}",
                          court_name="Bayerischer Verwaltungsgerichtshof", decision_date="2023-01-18",
                          case_numbers_raw="16b D 22.2183", case_numbers=["16b D 22.2183"], decision_type="Urteil",
                          sections=[Section("gruende", "court", "fixture", [Block(text, "1")])])


def _db():
    conn = connect()
    init_db(conn)
    conn.row_factory = sqlite3.Row
    return conn


def _item(conn, source_id, doc):
    conn.execute("INSERT INTO source_items(source_id, source_doc_id, url) VALUES(?,?,?)", (source_id, doc, "u"))
    conn.commit()


def test_a_primary_copy_supersedes_a_secondary_one_and_not_the_other_way_round():
    conn = _db()
    for source_id, doc in (("old", "1"), ("wayback", "by:Y-1"), ("by", "Y-1")):
        _item(conn, source_id, doc)
    first, _ = store_decision(conn, _decision("old", "1"), content_hash="a", capture_path="-", media_type="application/json")
    second, _ = store_decision(conn, _decision("wayback", "by:Y-1"), content_hash="b", capture_path="-", media_type="text/html")
    assert first == second
    assert conn.execute("SELECT source_id FROM decisions WHERE id=?", (first,)).fetchone()[0] == "wayback"
    old = conn.execute("SELECT state, failure_detail FROM source_items WHERE source_id='old'").fetchone()
    assert old["state"] == "skipped" and "primary source takes precedence" in old["failure_detail"]
    store_decision(conn, _decision("by", "Y-1"), content_hash="c", capture_path="-", media_type="text/html")
    assert conn.execute("SELECT source_id FROM decisions WHERE id=?", (first,)).fetchone()[0] == "by"
    with pytest.raises(UpstreamFailure) as failure:     # a secondary copy never replaces the primary one
        store_decision(conn, _decision("old", "1", "anderer Text."), content_hash="d", capture_path="-",
                       media_type="application/json")
    assert failure.value.kind == "duplicate_identity"
    kinds = dict(conn.execute("SELECT source_id, source_kind FROM sources").fetchall())
    assert kinds == {} or kinds.get("old") in (None, "aggregator")


# --- Open Legal Data --------------------------------------------------------------------------------------

def rows() -> list[dict]:
    return [json.loads(gz(p)) for p in sorted((FIX / "old").glob("*.json.gz"))]


def test_old_rows_of_every_origin_parse():
    from courts.sources.old import content_origin, parse_row
    from courts.paragraphs import assemble
    from courts.store import decision_identity
    out = {}
    for row in rows():
        d = finalise(parse_row(row, shard="train-00000-of-00054.parquet"))
        paras, numbering = assemble(decision_identity(d), d.sections)
        out[content_origin(row["content"])] = out.get(content_origin(row["content"]), []) + [
            (d.court_id, numbering, sum(1 for p in paras if p.rn))]
        assert d.metadata["source_kind"] == "aggregator" and d.metadata["aggregator"]["old_id"] == row["id"]
    assert out["bayern_recht"] == [("oblg-by", "rn", 31)]
    assert out["voris"] == [("ovg-ni", "none", 0)]
    assert sorted(out["juris"]) == [("lg-stuttgart", "rn", 17), ("ovg-sl", "rn", 24)]


def test_old_dump_shards_are_filtered_staged_and_read_back(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from courts.sources.old import OpenLegalDataAdapter, shard_rows, stage_rows
    shard = tmp_path / "train-00000-of-00054.parquet"
    pq.write_table(pa.Table.from_pylist(rows()), shard)
    ni = next(r for r in rows() if r["court"]["state"] == 11)
    keys = {("lg-stuttgart", "2026-08-17", "7O138/26")}              # already in the corpus
    eclis = {(ni["ecli"] or "").upper()}                            # already in the corpus by ECLI
    out = tmp_path / "rows-train-00000-of-00054.zst"
    items, skipped = stage_rows(shard_rows(shard), out, {"by", "ni", "bw", "sl"}, keys, eclis, shard.name)
    assert sorted(i["listed_court"] for i in items) == ["oblg-by", "ovg-sl"]
    assert skipped == {"court_date_az": 1, "ecli": 1}
    import courts.sources.old as old
    monkeypatch.setattr(old, "staging", lambda home=None: tmp_path)
    adapter = OpenLegalDataAdapter()
    for item in items:
        capture = adapter.fetch(None, item)
        d = adapter.parse(capture, item)
        assert d.source_doc_id == item["source_doc_id"] and d.metadata["aggregator"]["shard"] == shard.name


def test_old_without_access_is_a_login_failure(monkeypatch):
    from courts.sources.old import OpenLegalDataAdapter
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("HUGGING_FACE_HUB_TOKEN", raising=False)
    with pytest.raises(UpstreamFailure) as failure:
        OpenLegalDataAdapter().list_items(None, None, since=None, limit=None, courts=None)
    assert failure.value.kind == "login"


# --- ingest-capture --source bverwg ------------------------------------------------------------------------

def test_bverwg_capture_urls_and_ingest():
    from courts.capture import ingest_capture
    from courts.sources.bverwg import capture_canonical_url
    doc = "010223B4BN1.23.0"
    for url in (f"https://www.bverwg.de/de/{doc}", f"https://www.bverwg.de/{doc}",
                f"https://www.bverwg.de/entscheidungen/entscheidung.php?ent={doc}&add_az=4+BN+1.23"):
        assert capture_canonical_url(url) == (doc, f"https://www.bverwg.de/de/{doc}")
    for url in ("https://www.bverwg.de/de/pm/2023/10", "https://www.bverwg.de/suche?q=x", "https://example.org/de/" + doc):
        with pytest.raises(ValueError):
            capture_canonical_url(url)
    body = gz(FIX / "wayback" / "bverwg-010223B4BN1.23.0-20230605075242.html.gz")
    result = ingest_capture("bverwg", f"https://www.bverwg.de/de/{doc}", body, captured_at="2026-09-28T10:00:00Z")
    assert result["decision_id"] == "ECLI:DE:BVerwG:2023:010223B4BN1.23.0" and result["stored"] is True
    conn = _db()
    meta = json.loads(conn.execute("SELECT metadata FROM decisions WHERE id=?", (result["decision_id"],)).fetchone()[0])
    assert meta["capture_origin"] == "user_browser"
    from courts.sync import reparse_source
    assert reparse_source(get_adapter("bverwg"), conn, force=True)["failed"] == 0
    meta = json.loads(conn.execute("SELECT metadata FROM decisions WHERE id=?", (result["decision_id"],)).fetchone()[0])
    assert meta["capture_origin"] == "user_browser"
