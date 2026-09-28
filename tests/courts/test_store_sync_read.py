"""Store, incremental sync engine and read API (evidence envelope) — offline."""
import gzip
import json

import pytest

from courts.config import db_path
from courts.errors import UpstreamFailure
from courts.read import citing_decisions, coverage, fts_query, get_decision, get_paragraph, open_db, search
from courts.sources.base import Adapter, Capture, Listing
from courts.sources.nrw import parse_document
from courts.sources.rii import RiiAdapter
from courts.store import connect, init_db, read_capture, register_source, set_source_status
from courts.sync import sync_source

DOC = "jb-WBRE201600193"
NRW = "L_9_SO_202_26_B_ER_und_L_9_SO_203_26_B_Beschluss_20260813"
ECLI = "ECLI:DE:BVerwG:2016:160316B1B19.16.0"


class FakeHttp:
    requests_made = 0


class FixtureAdapter(Adapter):
    """Serves real captured fixtures; failures are injected per item."""
    source_id, label, base_url, land, adapter_name = "rii", "fixture RII", "https://x", "de", "fixture"

    def __init__(self, fixture, items, failures=None, listing_failure=None):
        self.fixture, self.items, self.failures, self.listing_failure = fixture, items, failures or {}, listing_failure

    def list_items(self, http, conn, *, since, limit, courts):
        if self.listing_failure:
            raise self.listing_failure
        return Listing(items=self.items, upstream_total=len(self.items))

    def fetch(self, http, item):
        failure = self.failures.get(item["source_doc_id"])
        if failure:
            raise failure
        return Capture(self.fixture(f"rii/{DOC}.zip"), item["url"], "application/zip", "zip")

    def parse(self, capture, item):
        return RiiAdapter().parse(capture, item)


def item(doc=DOC, modified="2025-06-23T21:55:54.378Z", date="2016-03-16"):
    return {"source_doc_id": doc, "url": f"https://x/{doc}.zip", "listed_date": date, "listed_court": "bverwg",
            "listed_case_numbers": "1 B 19/16", "listed_modified": modified, "priority": 0}


@pytest.fixture
def conn():
    c = connect()
    init_db(c)
    return c


def test_sync_stores_decision_capture_and_is_incremental(conn, fixture, isolated_corpus):
    adapter = FixtureAdapter(fixture, [item()])
    first = sync_source(adapter, conn, FakeHttp())
    assert (first["status"], first["stored"]) == ("ok", 1)
    row = conn.execute("SELECT * FROM decisions").fetchone()
    assert row["id"] == ECLI and row["numbering"] == "rn" and row["parser_version"].startswith("courts-parser/")
    assert json.loads(row["case_numbers"]) == ["1 B 19/16", "1 PKH 55/16"]
    raw = read_capture(conn, row["content_hash"], row["capture_path"], isolated_corpus)
    assert raw == fixture(f"rii/{DOC}.zip")
    assert row["capture_path"].startswith("packs/rii/rii-0001.pack@")
    assert not (isolated_corpus / "raw").exists()  # no per-capture files any more
    # Unchanged listing: nothing pending, nothing fetched.
    second = sync_source(adapter, conn, FakeHttp())
    assert second["attempted"] == 0
    # The source marks the item modified: it is re-queued and re-fetched, but the text is unchanged.
    adapter.items = [item(modified="2026-09-28T00:00:00Z")]
    third = sync_source(adapter, conn, FakeHttp())
    assert (third["attempted"], third["unchanged"], third["stored"]) == (1, 1, 0)


def test_challenge_is_recorded_as_typed_failure_never_as_decision(conn, fixture):
    items = [item(f"jb-X{i}") for i in range(5)]
    failures = {i["source_doc_id"]: UpstreamFailure("challenge", "title='Security Check'", url=i["url"])
                for i in items}
    result = sync_source(FixtureAdapter(fixture, items, failures), conn, FakeHttp())
    assert result["status"] == "blocked" and result["attempted"] == 3  # stops after 3 consecutive
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
    kinds = {r[0] for r in conn.execute("SELECT kind FROM failures")}
    assert kinds == {"challenge"}
    assert conn.execute("SELECT status FROM sources WHERE source_id='rii'").fetchone()[0] == "blocked"


def test_listing_blocked_by_robots_marks_source_unavailable_for_search(conn, fixture):
    failure = UpstreamFailure("robots_disallowed", "path disallowed by robots.txt", url="https://x/toc.xml")
    result = sync_source(FixtureAdapter(fixture, [], listing_failure=failure), conn, FakeHttp())
    assert result["status"] == "blocked"
    envelope = search(open_db(), "Mitteilung")
    assert envelope["status"] == "partial" and envelope["total"] is None
    assert envelope["searched"] == [{"source": "rii", "status": "blocked", "count": None}]
    assert envelope["failed_components"][0]["status"] == "blocked"


def seed(conn, fixture):
    sync_source(FixtureAdapter(fixture, [item()]), conn, FakeHttp())
    from courts.store import store_decision, write_capture
    register_source(conn, "nrw", "NRWE", "https://nrwe.justiz.nrw.de", "nw", "nrwe-html")
    body = fixture(f"nrw/{NRW}.html")
    content_hash, rel = write_capture(body, "nrw", "html", conn=conn)
    parsed = parse_document(body.decode("utf-8"), f"https://nrwe.justiz.nrw.de/sgs/lsg_nrw/j2026/{NRW}.html", NRW)
    store_decision(conn, parsed, content_hash=content_hash, capture_path=rel, media_type="text/html")
    set_source_status(conn, "nrw", "ok", None)
    # Both fixture sources count as fully listed (the RII fixture sync listed everything).
    conn.execute("UPDATE sources SET listing_complete=1 WHERE source_id='nrw'")


def test_search_complete_scope_counts_and_evidence(conn, fixture):
    seed(conn, fixture)
    env = search(open_db(), "Untätigkeitsklage Mitteilung")
    assert env["status"] == "ok" and env["total"] == 1
    ev = env["evidence"][0]
    assert ev["id"] == f"{ECLI}#rn-10" and ev["pinpoint"] == "Rn. 10"
    assert ev["source"]["content_hash"] and ev["source"]["parser_version"]
    assert ev["decision"]["case_numbers"] == ["1 B 19/16", "1 PKH 55/16"]
    assert {s["source"]: s["count"] for s in env["searched"]} == {"nrw": 0, "rii": 1}


def test_search_zero_over_complete_scope_is_ok_zero(conn, fixture):
    seed(conn, fixture)
    env = search(open_db(), "Quantenchromodynamik")
    assert env["status"] == "ok" and env["total"] == 0


def test_search_zero_with_pending_backfill_is_not_a_silent_zero(conn, fixture):
    seed(conn, fixture)
    conn.execute("INSERT INTO source_items(source_id, source_doc_id, url, state) VALUES('nrw','pending-1','u','pending')")
    env = search(open_db(), "Quantenchromodynamik")
    assert env["status"] == "not_found_in_searched_scope" and env["total"] == 0
    assert any("backfill incomplete" in w for w in env["warnings"])


def test_search_with_unavailable_source_returns_null_total(conn, fixture):
    seed(conn, fixture)
    register_source(conn, "by", "Bayern", "https://www.gesetze-bayern.de", "by", "x")
    set_source_status(conn, "by", "blocked", "challenge")
    env = search(open_db(), "Mitteilung")
    assert env["status"] == "partial" and env["total"] is None and env["evidence"]
    assert {"source": "by", "status": "blocked", "count": None} in env["searched"]
    # Restricting the scope to an available source gives a definite total again.
    scoped = search(open_db(), "Mitteilung", source="rii")
    assert scoped["status"] == "ok" and scoped["total"] >= 1


def test_search_filters_norm_prefix_court_and_case_number(conn, fixture):
    seed(conn, fixture)
    db = open_db()
    by_norm = search(db, None, norm="de/bund/vwgo/para/75")
    assert {e["id"] for e in by_norm["evidence"]} == {f"{ECLI}#rn-10"}
    assert search(db, None, norm="de/bund/vwgo/para/7")["total"] == 0  # prefix is segment-exact
    assert search(db, None, case_number="L 9 SO 203/26 B")["evidence"][0]["decision"]["court_id"] == "lsg-nw"
    assert search(db, "Beschwerde", court="lsg-nw")["total"] >= 1
    assert search(db, "x", limit=0)["status"] == "invalid_argument"
    assert search(db, None, relation="applies")["status"] == "invalid_argument"


def test_fts_query_is_literal():
    assert fts_query('Klage OR "§ 75" NEAR(x)') == '"75" AND "Klage" AND "OR" AND "NEAR" AND "x"'


def test_get_decision_paragraph_and_citing(conn, fixture):
    seed(conn, fixture)
    db = open_db()
    d = get_decision(db, "1 PKH 55/16")["decision"]
    assert d["id"] == ECLI and d["case_numbers_raw"] == "1 B 19/16, 1 B 19/16, 1 PKH 55/16"
    assert [s["name"] for s in d["sections"]] == ["titel", "gruende"]
    p = get_paragraph(db, f"{ECLI}#rn-10")
    assert p["status"] == "ok" and len(p["evidence"][0]["blocks"]) == 3
    assert any(r["target_id"] == "de/bund/vwgo/para/75" for r in p["evidence"][0]["relations"])
    cites = citing_decisions(db, "de/bund/asylg/para/24")
    assert cites["total"] >= 5 and all(r["evidence"]["legal_effect_verified"] is False for r in cites["relations"])
    assert {r["relation"] for r in cites["relations"]} <= {"mentions", "quotes", "procedural_reference"}
    assert get_decision(db, "does-not-exist")["status"] == "not_in_corpus"


def test_paragraph_integrity_check(conn, fixture):
    seed(conn, fixture)
    conn.execute("UPDATE paragraphs SET text = text || ' manipulated' WHERE id = ?", (f"{ECLI}#rn-1",))
    assert get_paragraph(open_db(), f"{ECLI}#rn-1")["status"] == "integrity_check_failed"


def test_procedural_reference_from_editorial_vorinstanz_is_relinked(conn, fixture):
    seed(conn, fixture)
    rows = conn.execute("SELECT relation, target_id, origin FROM relations WHERE paragraph_id IS NULL "
                        "AND target_kind='decision'").fetchall()
    assert ("procedural_reference", "court:vgh-bw/2015-12-01/A_11_S_490-15", "editorial_metadata") in \
        [tuple(r) for r in rows]


def test_coverage_report(conn, fixture):
    seed(conn, fixture)
    report = coverage(open_db())
    rows = {s["source_id"]: s for s in report["sources"]}
    assert rows["rii"]["decisions"] == 1 and rows["rii"]["date_from"] == "2016-03-16"
    assert report["schema_version"] == 2 and report["paragraphs"] > 0
    assert rows["rii"]["robots_policy"] == "respect" and rows["rii"]["policy_decided_at"] is None


def test_reader_refuses_other_schema_versions(conn):
    conn.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError):
        open_db()
    assert db_path().exists()


@pytest.mark.parametrize("ecli,valid", [
    ("ECLI:DE:BVerwG:2016:160316B1B19.16.0", True),
    ("ECLI:DE:LSGNRW:2026:0813.L9SO202.26B.ER.UN.00", True),
    ("ECLI:DE:SGCOTTB:2023:0525.00", False),   # published by a portal without the Az part: not unique
    ("ECLI:DE:BVerwG:2016", False),
])
def test_malformed_ecli_is_not_used_as_decision_id(ecli, valid):
    from courts.model import Block, ParsedDecision, Section
    from courts.store import decision_identity
    d = ParsedDecision(source_id="x", source_doc_id="1", source_url="https://x", court_name="Sozialgericht Cottbus",
                       decision_date="2023-05-25", case_numbers_raw="S 1 AS 1/23", case_numbers=["S 1 AS 1/23"],
                       decision_type="Urteil", sections=[Section("gruende", "court", "t", [Block("x")])],
                       ecli=ecli, court_id="sg-cottbus")
    identity = decision_identity(d)
    assert (identity == ecli) is valid
    if not valid:
        assert identity == "court:sg-cottbus/2023-05-25/S_1_AS_1-23"
        assert d.metadata["ecli_published_invalid"] == ecli


def test_reparse_is_offline_and_only_rewrites_on_parser_change(conn, fixture, monkeypatch):
    import courts.store as store_module
    from courts.sync import reparse_source
    seed(conn, fixture)
    adapter = FixtureAdapter(fixture, [item()])
    adapter.fetch = lambda *a, **k: pytest.fail("reparse must not fetch")
    assert reparse_source(adapter, conn) == {"reparsed": 0, "unchanged": 1, "failed": 0}
    monkeypatch.setattr(store_module, "PARSER_VERSION", "courts-parser/9.9.9")
    assert reparse_source(adapter, conn) == {"reparsed": 1, "unchanged": 0, "failed": 0}
    row = conn.execute("SELECT parser_version, first_seen, observed_at FROM decisions WHERE id=?", (ECLI,)).fetchone()
    assert row["parser_version"] == "courts-parser/9.9.9"


def test_second_concurrent_sync_of_the_same_source_is_refused(isolated_corpus):
    from courts.sync import SourceBusy, source_lock
    with source_lock("nrw", isolated_corpus):
        with pytest.raises(SourceBusy):
            with source_lock("nrw", isolated_corpus):
                pass
        with source_lock("rii", isolated_corpus):  # other sources are independent
            pass


def test_search_scoped_to_several_available_sources_has_a_total(conn, fixture):
    seed(conn, fixture)
    register_source(conn, "by", "Bayern", "https://www.gesetze-bayern.de", "by", "x")
    set_source_status(conn, "by", "blocked", "challenge")
    db = open_db()
    assert search(db, "Beschwerde")["total"] is None
    scoped = search(db, "Beschwerde", source=["rii", "nrw"])
    assert scoped["status"] == "ok" and scoped["total"] >= 2
    assert [s["source"] for s in scoped["searched"]] == ["nrw", "rii"]
    assert search(db, "Beschwerde", source=["nope"])["status"] == "not_in_corpus"


def test_bounded_backfill_is_never_reported_as_complete(conn, fixture):
    seed(conn, fixture)
    conn.execute("UPDATE sources SET listing_complete=0 WHERE source_id='nrw'")  # e.g. synced with --limit
    env = search(open_db(), "Beschwerde", source="nrw")
    assert env["status"] == "partial" and env["total"] >= 1
    assert any("backfill incomplete" in w for w in env["warnings"])
    assert search(open_db(), "Quantenchromodynamik", source="nrw")["status"] == "not_found_in_searched_scope"


def test_listing_complete_is_set_only_by_an_unbounded_full_listing(conn, fixture):
    sync_source(FixtureAdapter(fixture, [item()]), conn, FakeHttp(), limit=1)
    assert conn.execute("SELECT listing_complete FROM sources WHERE source_id='rii'").fetchone()[0] == 0
    sync_source(FixtureAdapter(fixture, [item()]), conn, FakeHttp())
    assert conn.execute("SELECT listing_complete FROM sources WHERE source_id='rii'").fetchone()[0] == 1


def test_local_disk_full_stops_the_run_and_leaves_items_pending(conn, fixture, monkeypatch):
    import errno
    import courts.sync as sync_module

    def no_space(*a, **k):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(sync_module, "write_capture", no_space)
    result = sync_source(FixtureAdapter(fixture, [item("jb-A"), item("jb-B")]), conn, FakeHttp())
    assert result["status"] == "failed" and result["failed"] == 0 and result["attempted"] == 1
    states = {r[0] for r in conn.execute("SELECT state FROM source_items")}
    assert states == {"pending"}
    assert conn.execute("SELECT count(*) FROM failures").fetchone()[0] == 0
    run = conn.execute("SELECT status, notes FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["status"] == "failed" and "No space left" in run["notes"]


def test_blocks_are_offsets_into_text_and_contexts_are_derived(conn, fixture):
    seed(conn, fixture)
    raw = conn.execute("SELECT blocks, text FROM paragraphs WHERE id=?", (f"{ECLI}#rn-10",)).fetchone()
    blocks = json.loads(raw["blocks"])
    assert all(set(b) == {"start", "end", "locator", "kind"} for b in blocks)      # no duplicated text
    assert "\n\n".join(raw["text"][b["start"]:b["end"]] for b in blocks) == raw["text"]
    p = get_paragraph(open_db(), f"{ECLI}#rn-10")["evidence"][0]
    assert p["blocks"][2]["text"].startswith("rechtfertigt diese ebenfalls nicht")
    stored = conn.execute("SELECT quoted_context FROM relations WHERE paragraph_id=? AND target_id=?",
                          (f"{ECLI}#rn-10", "de/bund/vwgo/para/75")).fetchone()[0]
    assert stored == ""                                                            # derived at read time
    rel = next(r for r in p["relations"] if r["target_id"] == "de/bund/vwgo/para/75")
    assert "Untätigkeitsklage" in rel["evidence"]["quoted_context"]


def test_same_decision_from_second_source_is_skipped_not_failed(conn, fixture):
    sync_source(FixtureAdapter(fixture, [item()]), conn, FakeHttp())

    class CourtSite(FixtureAdapter):
        source_id = "bverwg"

        def parse(self, capture, it):
            parsed = RiiAdapter().parse(capture, dict(it, source_doc_id=DOC))
            parsed.source_id, parsed.source_doc_id = "bverwg", it["source_doc_id"]
            return parsed
    result = sync_source(CourtSite(fixture, [item("160316B1B19.16.0")]), conn, FakeHttp())
    assert result["failed"] == 0 and result["duplicates"] == 1 and result["status"] == "ok"
    row = conn.execute("SELECT state, failure_kind FROM source_items WHERE source_id='bverwg'").fetchone()
    assert tuple(row) == ("skipped", "duplicate_identity")


def test_failed_parse_keeps_the_capture_hash_for_offline_reexamination(conn, fixture):
    class Broken(FixtureAdapter):
        def parse(self, capture, it):
            raise UpstreamFailure("empty_document", "no decision text blocks")
    result = sync_source(Broken(fixture, [item()]), conn, FakeHttp())
    assert result["failed"] == 1
    row = conn.execute("SELECT state, content_hash FROM source_items").fetchone()
    assert row["state"] == "failed"
    assert row["content_hash"] == conn.execute("SELECT sha256 FROM captures").fetchone()[0]


def test_refused_second_sync_does_not_clobber_the_holders_pid(isolated_corpus):
    import os
    from courts.sync import SourceBusy, source_lock
    with source_lock("rii", isolated_corpus):
        with pytest.raises(SourceBusy):
            with source_lock("rii", isolated_corpus):
                pass
        assert (isolated_corpus / "locks" / "rii.lock").read_text() == str(os.getpid())
