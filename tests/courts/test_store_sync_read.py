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


def test_ecli_case_variants_retain_the_published_identity(conn, fixture):
    seed(conn, fixture)
    for ref in (ECLI.upper(), ECLI.lower()):
        result = get_decision(conn, ref, with_paragraphs=False, with_relations=False)
        assert result["status"] == "ok"
        assert result["decision"]["ecli"] == ECLI
        assert "identity_match" not in result["decision"]
        assert search(conn, "Untätigkeitsklage Mitteilung", ecli=ref)["total"] == 1
        passage = get_paragraph(conn, f"{ref}#rn-10")
        assert passage["status"] == "ok"
        assert passage["evidence"][0]["id"] == f"{ECLI}#rn-10"


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
    cites = citing_decisions(db, "de/bund/asylvfg_1992/para/24")
    assert cites["total"] >= 5 and all(r["evidence"]["legal_effect_verified"] is False for r in cites["relations"])
    assert {r["relation"] for r in cites["relations"]} <= {"mentions", "quotes", "procedural_reference"}
    assert get_decision(db, "does-not-exist")["status"] == "not_in_corpus"


def test_paragraph_integrity_check(conn, fixture):
    seed(conn, fixture)
    conn.execute("UPDATE paragraphs SET text = text || ' manipulated' WHERE decision_id = ? AND anchor = 'rn-1'", (ECLI,))
    assert get_paragraph(open_db(), f"{ECLI}#rn-1")["status"] == "integrity_check_failed"


def test_procedural_reference_from_editorial_vorinstanz_is_relinked(conn, fixture):
    seed(conn, fixture)
    rows = conn.execute("SELECT relation, target_id, origin FROM relations WHERE paragraph_ordinal IS NULL "
                        "AND target_kind='decision'").fetchall()
    assert ("procedural_reference", "court:vgh-bw/2015-12-01/A_11_S_490-15", "editorial_metadata") in \
        [tuple(r) for r in rows]


def test_coverage_report(conn, fixture):
    seed(conn, fixture)
    report = coverage(open_db())
    rows = {s["source_id"]: s for s in report["sources"]}
    assert rows["rii"]["decisions"] == 1 and rows["rii"]["date_from"] == "2016-03-16"
    assert report["schema_version"] == 4 and report["paragraphs"] > 0
    assert rows["rii"]["robots_policy"] == "respect" and rows["rii"]["policy_decided_at"] is None


def test_reader_refuses_other_schema_versions(conn):
    conn.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError):
        open_db()
    assert db_path().exists()


@pytest.mark.parametrize("ecli,date,valid,reason", [
    ("ECLI:DE:BVerwG:2016:160316B1B19.16.0", "2016-03-16", True, None),
    ("ECLI:DE:LSGNRW:2026:0813.L9SO202.26B.ER.UN.00", "2026-08-13", True, None),
    ("ECLI:DE:SGCOTTB:2023:0525.00", "2023-05-25", False, "ecli_published_invalid"),  # no Az part: not unique
    ("ECLI:DE:BVerwG:2016", "2016-03-16", False, "ecli_published_invalid"),
    # RII: a Tatbestandsberichtigungsbeschluss (31.08.2023) published with the judgment's ECLI (15.02.2023).
    ("ECLI:DE:BSG:2023:150223UB11AL4221R0", "2023-08-31", False, "ecli_published_other_document"),
    ("ECLI:DE:BSG:2023:150223UB11AL4221R0", "2023-02-15", True, None),
    ("ECLI:DE:BVerfG:2012:ls20120718.1bvl001010", "2012-07-18", True, None),
    # Länder ECLIs are often a few days off the portal date for the same document: kept.
    ("ECLI:DE:OVGNRW:2026:0916.4B21.26.00", "2026-09-17", True, None),
    ("ECLI:DE:BSG:2021:301121BB14AS4120R0", "2021-11-11", False, "ecli_published_other_document"),
])
def test_malformed_ecli_is_not_used_as_decision_id(ecli, date, valid, reason):
    from courts.model import Block, ParsedDecision, Section
    from courts.store import decision_identity, finalise
    d = ParsedDecision(source_id="x", source_doc_id="1", source_url="https://x", court_name="Sozialgericht Cottbus",
                       decision_date=date, case_numbers_raw="S 1 AS 1/23", case_numbers=["S 1 AS 1/23"],
                       decision_type="Urteil", sections=[Section("gruende", "court", "t", [Block("x")])],
                       ecli=ecli, court_id="sg-cottbus")
    identity = decision_identity(finalise(d))
    assert (identity == ecli) is valid
    if not valid:
        assert identity == f"court:sg-cottbus/{date}/S_1_AS_1-23"
        published = d.metadata[reason]
        assert published == ecli or published["ecli"] == ecli


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
    import courts.pipeline as sync_module   # the writer stores captures

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
    from courts.paragraphs import decode_blocks
    raw = conn.execute("SELECT blocks, text, ordinal FROM paragraphs WHERE decision_id=? AND anchor='rn-10'",
                       (ECLI,)).fetchone()
    compact = json.loads(raw["blocks"])                                           # v4: [[length, locator(, kind)]]
    assert all(isinstance(b[0], int) and isinstance(b[1], str) and len(b) in (2, 3) for b in compact)
    blocks = decode_blocks(raw["blocks"])
    assert all(set(b) == {"start", "end", "locator", "kind"} for b in blocks)      # no duplicated text
    assert "\n\n".join(raw["text"][b["start"]:b["end"]] for b in blocks) == raw["text"]
    p = get_paragraph(open_db(), f"{ECLI}#rn-10")["evidence"][0]
    assert p["blocks"][2]["text"].startswith("rechtfertigt diese ebenfalls nicht")
    stored = conn.execute("SELECT quoted_context FROM relations WHERE decision_id=? AND paragraph_ordinal=? "
                          "AND target_id=?", (ECLI, raw["ordinal"], "de/bund/vwgo/para/75")).fetchone()[0]
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
    # bverwg.de defers to RII before fetching: same court, date and Az are already listed (and stored).
    assert result["failed"] == 0 and result["attempted"] == 0 and result["deduped_before_fetch"] == 1
    row = conn.execute("SELECT state, failure_kind, failure_detail FROM source_items WHERE source_id='bverwg'").fetchone()
    assert tuple(row)[:2] == ("skipped", "duplicate_identity") and "checked before fetching" in row[2]

    class Portal(CourtSite):   # a source that does not defer: the store-time identity check catches it
        source_id = "bb"

        def parse(self, capture, it):
            parsed = super().parse(capture, it)
            parsed.source_id = "bb"
            return parsed
    result = sync_source(Portal(fixture, [item("bb-1")]), conn, FakeHttp())
    assert result["failed"] == 0 and result["duplicates"] == 1 and result["status"] == "ok"
    row = conn.execute("SELECT state, failure_kind FROM source_items WHERE source_id='bb'").fetchone()
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


def test_parallel_init_db_does_not_race(isolated_corpus):
    import threading
    from courts.store import connect, init_db
    errors = []

    def worker():
        try:
            init_db(connect())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_explicit_rn_representation_supersedes_unnumbered_duplicate():
    """bsg.bund.de prints no Randnummern, RII does: whichever arrives second, the Rn copy is stored."""
    from courts.model import Block, ParsedDecision, Section
    from courts.store import connect, init_db, store_decision
    from courts.errors import UpstreamFailure
    conn = connect()
    init_db(conn)
    ecli = "ECLI:DE:BSG:2024:120324UB1KR1223R0"

    def decision(source, doc, labels):
        return ParsedDecision(source_id=source, source_doc_id=doc, source_url=f"https://x/{doc}",
                              court_name="Bundessozialgericht", decision_date="2024-03-12",
                              case_numbers_raw="B 1 KR 12/23 R", case_numbers=["B 1 KR 12/23 R"],
                              decision_type="Urteil", ecli=ecli,
                              sections=[Section("gruende", "court", "t",
                                                [Block(t, label) for t, label in zip(("Erster Satz.", "Zweiter Satz."),
                                                                                     labels)])])
    for source in ("bsg", "rii"):
        conn.execute("INSERT OR IGNORE INTO source_items(source_id, source_doc_id, url) VALUES(?,?,?)",
                     (source, "d-" + source, "https://x"))
    store_decision(conn, decision("bsg", "d-bsg", (None, None)), content_hash="h1", capture_path="-",
                   media_type="text/html")
    _, changed = store_decision(conn, decision("rii", "d-rii", ("1", "2")), content_hash="h2", capture_path="-",
                                media_type="application/zip")
    assert changed
    row = conn.execute("SELECT source_id, numbering FROM decisions WHERE id=?", (ecli,)).fetchone()
    assert tuple(row) == ("rii", "rn")
    item = conn.execute("SELECT state, failure_detail, content_hash FROM source_items WHERE source_id='bsg'").fetchone()
    assert item["state"] == "skipped" and "superseded by rii:d-rii" in item["failure_detail"] and item["content_hash"] == "h1"
    # The unnumbered copy arriving later stays a duplicate.
    with pytest.raises(UpstreamFailure) as exc:
        store_decision(conn, decision("bsg", "d-bsg", (None, None)), content_hash="h1", capture_path="-",
                       media_type="text/html")
    assert exc.value.kind == "duplicate_identity"


def test_reexamine_skipped_stores_the_second_document_of_a_shared_federal_ecli():
    """RII published a Beschluss (31.08.2023) with the judgment's ECLI (15.02.2023): the judgment arrived
    second and was skipped as a duplicate.  Under the ECLI date rule both are kept."""
    from courts.model import Block, ParsedDecision, Section
    from courts.sources.base import Adapter
    from courts.store import connect, init_db, store_decision, write_capture
    from courts.sync import reexamine_skipped
    conn = connect()
    init_db(conn)
    ecli = "ECLI:DE:BSG:2023:150223UB11AL4221R0"

    def decision(doc, date, dtype):
        return ParsedDecision(source_id="rii", source_doc_id=doc, source_url="https://x", court_name="Bundessozialgericht",
                              decision_date=date, case_numbers_raw="B 11 AL 42/21 R", case_numbers=["B 11 AL 42/21 R"],
                              decision_type=dtype, ecli=ecli,
                              sections=[Section("gruende", "court", "t", [Block(f"{dtype} vom {date}.", "1")])])

    class Stub(Adapter):
        source_id = "rii"

        def parse(self, capture, item):
            return decision(item["source_doc_id"], "2023-02-15", "Urteil")
    content_hash, ref = write_capture(b"<judgment/>", "rii", "xml", conn=conn)
    conn.execute("INSERT INTO source_items(source_id, source_doc_id, url, state, content_hash, failure_kind, "
                 "failure_detail) VALUES('rii','judgment','https://x','skipped',?,'duplicate_identity',?)",
                 (content_hash, f"{ecli} already stored from rii:beschluss"))
    conn.execute("INSERT INTO source_items(source_id, source_doc_id, url) VALUES('rii','beschluss','https://x')")
    # Stored under the old rule: the Beschluss holds the ECLI as its id.
    import courts.store as store_module
    original = store_module.ecli_date_conflict
    store_module.ecli_date_conflict = lambda *a: None
    try:
        store_decision(conn, decision("beschluss", "2023-08-31", "Beschluss"), content_hash="b", capture_path="-",
                       media_type="application/xml")
    finally:
        store_module.ecli_date_conflict = original
    # Re-parse under the new rule frees the ECLI; re-examining the skipped item stores the judgment under it.
    store_decision(conn, decision("beschluss", "2023-08-31", "Beschluss"), content_hash="b", capture_path="-",
                   media_type="application/xml")
    counts = reexamine_skipped(Stub(), conn)
    assert counts["skipped_stored"] == 1
    rows = {r["id"]: r["decision_date"] for r in conn.execute("SELECT id, decision_date FROM decisions")}
    assert rows[ecli] == "2023-02-15"
    assert "court:bsg/2023-08-31/B_11_AL_42-21_R" in rows


def test_first_sigterm_finishes_the_current_item_then_stops(conn, fixture):
    import courts.sync as sync_module

    class StopDuringFirst(FixtureAdapter):
        def fetch(self, http, it):
            sync_module.STOP_AFTER_ITEM.set()   # SIGTERM arrives while the first item is being fetched
            return super().fetch(http, it)
    items = [item(), item(doc="second", date="2016-03-17")]
    try:
        result = sync_module.sync_source(StopDuringFirst(fixture, items), conn, FakeHttp())
    finally:
        sync_module.STOP_AFTER_ITEM.clear()
    assert result["status"] == "interrupted"
    states = sorted(r[0] for r in conn.execute("SELECT state FROM source_items"))
    assert result["attempted"] == 1 and states.count("pending") == 1   # one item done, the other untouched
    assert conn.execute("SELECT status, notes FROM sync_runs ORDER BY id DESC").fetchone()[0] == "interrupted"


def _bsg(source, doc, ecli, labels, date="2025-06-18", dtype="Urteil"):
    from courts.model import Block, ParsedDecision, Section
    return ParsedDecision(source_id=source, source_doc_id=doc, source_url=f"https://x/{doc}",
                          court_name="Bundessozialgericht", decision_date=date, case_numbers_raw="B 6 KA 4/24 R",
                          case_numbers=["B 6 KA 4/24 R"], decision_type=dtype, ecli=ecli,
                          sections=[Section("gruende", "court", "t", [Block(t, lab) for t, lab in
                                                                      zip(("Erster Satz.", "Zweiter Satz."), labels)])])


def test_rn_copy_without_valid_ecli_supersedes_its_unnumbered_twin_and_keeps_the_ecli():
    """RII publishes the Urteil of 18.06.2025 with the ECLI of a 2024 Beschluss (dropped as the other
    document's); bsg.bund.de has the right ECLI but no Rn.  One decision results: RII's text with Rn under the
    validated ECLI."""
    from courts.store import connect, init_db, store_decision
    conn = connect()
    init_db(conn)
    for source in ("bsg", "rii"):
        conn.execute("INSERT INTO source_items(source_id, source_doc_id, url) VALUES(?,?,?)", (source, "d-" + source, "x"))
    store_decision(conn, _bsg("bsg", "d-bsg", "ECLI:DE:BSG:2025:180625UB6KA424R0", (None, None)), content_hash="h1",
                   capture_path="-", media_type="text/html")
    decision_id, _ = store_decision(conn, _bsg("rii", "d-rii", "ECLI:DE:BSG:2024:200624BB6KA424R0", ("1", "2")),
                                    content_hash="h2", capture_path="-", media_type="application/zip")
    assert decision_id == "ECLI:DE:BSG:2025:180625UB6KA424R0"
    rows = conn.execute("SELECT id, source_id, numbering, metadata FROM decisions").fetchall()
    assert [(r["id"], r["source_id"], r["numbering"]) for r in rows] == [(decision_id, "rii", "rn")]
    meta = json.loads(rows[0]["metadata"])
    assert meta["ecli_from_source"] == "bsg:d-bsg"
    assert meta["ecli_published_other_document"]["ecli"] == "ECLI:DE:BSG:2024:200624BB6KA424R0"


def test_ecli_spelling_variants_of_one_decision_are_one_decision():
    from courts.errors import UpstreamFailure
    from courts.store import connect, init_db, store_decision
    conn = connect()
    init_db(conn)
    store_decision(conn, _bsg("rii", "a", "ECLI:DE:BSG:2025:180625UB6KA424R0", ("1", "2")), content_hash="h1",
                   capture_path="-", media_type="x")
    with pytest.raises(UpstreamFailure) as exc:
        store_decision(conn, _bsg("bsg", "b", "ECLI:DE:BSG:2025:180625UB6KA0424R0", ("1", "2")), content_hash="h2",
                       capture_path="-", media_type="x")
    assert exc.value.kind == "duplicate_identity"
    # Another document of the same day and docket (a Beschluss) is its own decision.
    store_decision(conn, _bsg("bsg", "c", None, ("1", "2"), dtype="Beschluss"), content_hash="h3", capture_path="-",
                   media_type="x")
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 2


def test_reexamine_twins_keeps_the_first_of_two_numbered_copies(monkeypatch):
    import courts.store as store_module
    from courts.store import connect, init_db, store_decision
    from courts.sync import reexamine_twins
    conn = connect()
    init_db(conn)
    for source, doc in (("bb", "x"), ("be", "y")):
        conn.execute("INSERT INTO source_items(source_id, source_doc_id, url) VALUES(?,?,?)", (source, doc, "u"))
    with monkeypatch.context() as m:   # stored under the old rules: no twin detection
        m.setattr(store_module, "same_decision_elsewhere", lambda conn, parsed: None)
        store_decision(conn, _bsg("bb", "x", "ECLI:DE:BSG:2025:180625UB6KA424R0", ("1", "2")), content_hash="h1",
                       capture_path="-", media_type="x", observed_at="2026-01-01T00:00:00+00:00")
        store_decision(conn, _bsg("be", "y", "ECLI:DE:BSG:2025:180625UB6KA0424R0", ("1", "2")), content_hash="h2",
                       capture_path="-", media_type="x", observed_at="2026-02-01T00:00:00+00:00")
    assert reexamine_twins(conn) == {"twins_merged": 0, "twins_skipped": 1}
    assert [r[0] for r in conn.execute("SELECT source_id FROM decisions")] == ["bb"]
    item = conn.execute("SELECT state, failure_detail FROM source_items WHERE source_id='be'").fetchone()
    assert item["state"] == "skipped" and item["failure_detail"].startswith("ECLI:DE:BSG:2025:180625UB6KA424R0 already")


def test_two_documents_of_one_portal_with_one_ecli_are_both_kept():
    """ThürVerfGH 112/20, 06.04.2022: two Beschlüsse (Verfassungsbeschwerde; Befangenheit) with one ECLI."""
    from courts.model import Block, ParsedDecision, Section
    from courts.store import connect, init_db, store_decision
    from courts.read import resolve_decision
    conn = connect()
    init_db(conn)
    ecli = "ECLI:DE:VERFGHT:2022:0406.112.20.00"

    def doc(doc_id, text):
        return ParsedDecision(source_id="th", source_doc_id=doc_id, source_url="https://x", court_name="VerfGH Weimar",
                              decision_date="2022-04-06", case_numbers_raw="112/20", case_numbers=["112/20"],
                              decision_type="Beschluss", ecli=ecli, court_id="verfgh-th",
                              sections=[Section("gruende", "court", "t", [Block(text, "1")])])
    first, _ = store_decision(conn, doc("NJRE001499442", "Die Verfassungsbeschwerde ist unzulässig."),
                              content_hash="a", capture_path="-", media_type="x")
    second, _ = store_decision(conn, doc("NJRE001499463", "Das Ablehnungsgesuch ist unbegründet."),
                               content_hash="b", capture_path="-", media_type="x")
    assert first == ecli and second == "court:verfgh-th/2022-04-06/112-20"
    assert conn.execute("SELECT count(*) FROM decisions WHERE ecli=?", (ecli,)).fetchone()[0] == 2
    status, ids = resolve_decision(conn, ecli)
    assert status == "ambiguous_identity" and set(ids) == {first, second}
    assert resolve_decision(conn, ecli.lower()) == (status, ids)
    qualified = get_paragraph(conn, ecli.lower() + "#rn-1")
    assert qualified["status"] == "ambiguous_identity"
    assert set(qualified["candidates"]) == {first, second}
    # Re-storing either keeps its id; an identical re-listing stays a duplicate.
    assert store_decision(conn, doc("NJRE001499463", "Das Ablehnungsgesuch ist unbegründet."), content_hash="b",
                          capture_path="-", media_type="x")[0] == second


def test_a_vorinstanz_without_court_is_kept_without_target():
    """OVG Sachsen: a Vorinstanz line without a recognisable court failed the whole decision (TypeError)."""
    from courts.model import Block, ParsedDecision, Section
    from courts.store import connect, init_db, store_decision
    conn = connect()
    init_db(conn)
    parsed = ParsedDecision(source_id="sn", source_doc_id="ovg:1", source_url="https://x",
                            court_name="Sächsisches Oberverwaltungsgericht", decision_date="2022-01-12",
                            case_numbers_raw="3 E 80/21", case_numbers=["3 E 80/21"], decision_type="Beschluss",
                            sections=[Section("gruende", "court", "t", [Block("Die Beschwerde ist unbegründet.", "1")])],
                            procedural=[{"direction": "previous", "court": None, "date": "2021-11-02",
                                         "case_number": "6 L 612/21", "raw": "6 L 612/21 vom 2. November 2021"}])
    decision_id, _ = store_decision(conn, parsed, content_hash="h", capture_path="-", media_type="x")
    row = conn.execute("SELECT relation, target_id, target_raw FROM relations WHERE decision_id=?", (decision_id,)).fetchone()
    assert tuple(row) == ("procedural_reference", None, "6 L 612/21 vom 2. November 2021")


def _doc(source, doc_id, texts, *, date="2017-01-18", az="7 Ca 3580/16", ecli=None, labels=None, dtype="Beschluss",
         court="arbg-frankfurt"):
    from courts.model import Block, ParsedDecision, Section
    labels = labels or [str(i) for i in range(1, len(texts) + 1)]
    return ParsedDecision(source_id=source, source_doc_id=doc_id, source_url=f"https://x/{doc_id}",
                          court_name="ArbG Frankfurt", decision_date=date, case_numbers_raw=az, case_numbers=[az],
                          decision_type=dtype, ecli=ecli, court_id=court,
                          sections=[Section("gruende", "court", "t", [Block(t, lab) for t, lab in zip(texts, labels)])])


REASONS = ["Die Klage ist zulässig.", "Sie ist jedoch unbegründet.", "Die Kosten trägt der Kläger.",
           "Der Streitwert wird auf 5.000 EUR festgesetzt."]


def test_a_portal_publishing_one_decision_twice_keeps_one_copy():
    """NRW lists LSG NRW L 10 KR 606/23 under two URL spellings; juris portals re-issue document numbers."""
    from courts.store import connect, init_db, store_decision
    conn = connect()
    init_db(conn)
    store_decision(conn, _doc("he", "LARE1", REASONS), content_hash="a", capture_path="-", media_type="x")
    with pytest.raises(UpstreamFailure) as exc:
        store_decision(conn, _doc("he", "LARE2", [t.upper().replace(".", " .") for t in REASONS]),
                       content_hash="b", capture_path="-", media_type="x")
    assert exc.value.kind == "duplicate_identity" and "republication: 100%" in exc.value.detail
    # A different text of the same day and docket is no republication.
    from courts.store import republication_of
    other = _doc("he", "LARE3", ["Der Antrag auf Prozesskostenhilfe wird abgelehnt.", "Gründe folgen."])
    assert republication_of(conn, other, [b.text for b in other.sections[0].blocks]) is None


def test_a_republication_with_randnummern_supersedes_the_unnumbered_copy():
    from courts.store import connect, init_db, store_decision
    conn = connect()
    init_db(conn)
    for doc in ("LARE1", "LARE2"):
        conn.execute("INSERT INTO source_items(source_id, source_doc_id, url) VALUES('he',?,'u')", (doc,))
    store_decision(conn, _doc("he", "LARE1", REASONS, labels=[None] * 4), content_hash="a", capture_path="-",
                   media_type="x")
    decision_id, changed = store_decision(conn, _doc("he", "LARE2", REASONS), content_hash="b", capture_path="-",
                                          media_type="x")
    assert changed
    assert [tuple(r) for r in conn.execute("SELECT source_doc_id, numbering FROM decisions")] == [("LARE2", "rn")]
    assert conn.execute("SELECT state FROM source_items WHERE source_doc_id='LARE1'").fetchone()[0] == "skipped"


ARBG_ECLI = "ECLI:DE:ARBGFFM:2017:0118.7CA3580.16.00"


def test_a_shared_ecli_belongs_to_the_document_whose_date_it_encodes():
    """hessenrecht gave the ECLI of the Beschluss of 18.01.2017 also to the Urteil of 08.11.2017 and the
    Beschluss of 31.08.2016 (same docket): whichever arrives first, the 18.01. document owns it."""
    from courts.read import resolve_decision
    from courts.store import connect, init_db, store_decision
    conn = connect()
    init_db(conn)
    wrong, _ = store_decision(conn, _doc("he", "LARE909", ["Das Urteil."], date="2017-11-08", dtype="Urteil",
                                         ecli=ARBG_ECLI), content_hash="a", capture_path="-", media_type="x")
    assert wrong == ARBG_ECLI  # nothing to compare with yet
    right, _ = store_decision(conn, _doc("he", "LARE914", ["Der Beschluss."], ecli=ARBG_ECLI), content_hash="b",
                              capture_path="-", media_type="x")
    other, _ = store_decision(conn, _doc("he", "LARE903", ["Ein früherer Beschluss."], date="2016-08-31",
                                         ecli=ARBG_ECLI), content_hash="c", capture_path="-", media_type="x")
    assert right == ARBG_ECLI and other == "court:arbg-frankfurt/2016-08-31/7_Ca_3580-16"
    rows = {r["source_doc_id"]: r for r in conn.execute("SELECT * FROM decisions")}
    assert rows["LARE909"]["id"] == "court:arbg-frankfurt/2017-11-08/7_Ca_3580-16" and rows["LARE909"]["ecli"] is None
    assert json.loads(rows["LARE909"]["metadata"])["ecli_published_other_document"]["ecli"] == ARBG_ECLI
    assert rows["LARE903"]["ecli"] is None
    assert conn.execute("SELECT count(*) FROM paragraphs WHERE decision_id=?", (rows["LARE909"]["id"],)).fetchone()[0] == 1
    assert resolve_decision(conn, ARBG_ECLI) == ("ok", [ARBG_ECLI])


def test_reviewed_targets_follow_the_document_that_releases_its_ecli(conn):
    from courts.store import store_decision

    reviewed = _doc("he", "LARE909", ["Das Urteil."], date="2017-11-08", dtype="Urteil", ecli=ARBG_ECLI)
    old, _ = store_decision(conn, reviewed, content_hash="a", capture_path="-", media_type="x")
    citing, _ = store_decision(conn, _doc("he", "citation", ["Zitiert das Urteil."], az="9 Ca 11/18"),
                               content_hash="b", capture_path="-", media_type="x")
    assert old == ARBG_ECLI
    conn.executemany("""
        INSERT INTO relations(decision_id,relation,target_kind,target_id,target_decision_id,target_raw,
                              quoted_context,origin,derived_by,legal_effect_verified,confidence)
        VALUES(?,'mentions','decision',?,?,'Urteil vom 08.11.2017','Urteil','decision_text',?,?,1)
    """, [(citing, old, old, "user", 1), (citing, old, old, "model", 0)])

    owner = _doc("he", "LARE914", ["Der Beschluss."], ecli=ARBG_ECLI)
    new, _ = store_decision(conn, owner, content_hash="c", capture_path="-", media_type="x")
    assert new == old
    targets = conn.execute("""
        SELECT r.derived_by, r.legal_effect_verified, r.target_id, d.source_doc_id
        FROM relations r JOIN decisions d ON d.id=r.target_decision_id
        WHERE r.decision_id=? ORDER BY r.derived_by
    """, (citing,)).fetchall()
    assert [tuple(row) for row in targets] == [("model", 0, old, "LARE909"), ("user", 1, old, "LARE909")]


def test_dedupe_repairs_duplicates_stored_under_the_old_rules(monkeypatch):
    import courts.store as store_module
    from courts.dedupe import dedupe
    from courts.read import resolve_decision
    from courts.store import connect, init_db, store_decision
    conn = connect()
    init_db(conn)
    for doc in ("LARE1", "LARE2", "LARE909", "LARE914"):
        conn.execute("INSERT INTO source_items(source_id, source_doc_id, url) VALUES('he',?,'u')", (doc,))
    with monkeypatch.context() as m:  # the old rules: no republication check, first document keeps the ECLI
        m.setattr(store_module, "republication_of", lambda *a: None)
        m.setattr(store_module, "ecli_fit", lambda *a: 0)
        shared = "ECLI:DE:ARBGFFM:2020:0324.14CA453.19.00"  # both copies carry it: the second got a synthetic id
        store_decision(conn, _doc("he", "LARE1", REASONS, date="2020-03-24", az="14 Ca 453/19", ecli=shared),
                       content_hash="a", capture_path="-", media_type="x")
        store_decision(conn, _doc("he", "LARE2", REASONS[:3] + ["Der Streitwert wird auf 5.000 EUR festgesetzt!!"],
                                  date="2020-03-24", az="14 Ca 453/19", ecli=shared), content_hash="b",
                       capture_path="-", media_type="x")
        store_decision(conn, _doc("he", "LARE909", ["Das Urteil."], date="2017-11-08", dtype="Urteil", ecli=ARBG_ECLI),
                       content_hash="c", capture_path="-", media_type="x")
        store_decision(conn, _doc("he", "LARE914", ["Der Beschluss."], ecli=ARBG_ECLI), content_hash="d",
                       capture_path="-", media_type="x")
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 4
    dry = dedupe(conn)
    assert dry["summary"]["republications"] == {"would merge": 1}
    # A dry run does not merge, so the two copies still share their ECLI (and fit it alike).
    assert dry["summary"]["shared_eclis"] == {"would settle": 1,
                                              "kept ambiguous: several documents fit the ordinal alike": 1}
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 4
    done = dedupe(conn, apply=True)
    assert done["summary"]["republications"] == {"merged": 1} and done["summary"]["shared_eclis"] == {"settled": 1}
    assert [r[0] for r in conn.execute("SELECT source_doc_id FROM decisions ORDER BY 1")] == ["LARE1", "LARE909", "LARE914"]
    assert conn.execute("SELECT state, failure_kind FROM source_items WHERE source_doc_id='LARE2'").fetchone()[:] == \
        ("skipped", "duplicate_identity")
    assert resolve_decision(conn, ARBG_ECLI) == ("ok", [ARBG_ECLI])
    assert conn.execute("SELECT source_doc_id FROM decisions WHERE id=?", (ARBG_ECLI,)).fetchone()[0] == "LARE914"
    assert dedupe(conn)["summary"] == {"republications": {}, "shared_eclis": {}}


def test_relink_rechecks_identity_when_a_second_document_arrives(conn):
    from courts.model import Block, Section
    from courts.store import relink, store_decision

    ecli = "ECLI:DE:BSG:2025:180625UB6KA424R0"
    first = _bsg("rii", "first", ecli, ("1", "2"))
    first_id, _ = store_decision(conn, first, content_hash="a", capture_path="-", media_type="x")
    d = conn.execute("SELECT * FROM decisions WHERE id=?", (first_id,)).fetchone()
    case_key = conn.execute("SELECT case_key FROM case_numbers WHERE decision_id=?", (first_id,)).fetchone()[0]

    def cite(target, court, day, docket, author="parser", linked=None):
        return conn.execute("""
            INSERT INTO relations(decision_id,relation,target_kind,target_id,target_court_id,
              target_date,target_case_key,target_decision_id,target_raw,quoted_context,origin,
              derived_by,legal_effect_verified,confidence,detail)
            VALUES(?,'mentions','decision',?,?,?,?,?,'citation','context','decision_text',?,0,1,'{}')
        """, (first_id,target,court,day,docket,linked,author)).lastrowid

    dated = cite("court:bsg/test", d["court_slug"], d["decision_date"], case_key)
    undated = cite("court:bsg/test", d["court_id"], None, case_key)
    printed = cite(ecli.lower(), None, None, None)
    conflicting = cite("ECLI:DE:BSG:2025:UNKNOWN", d["court_slug"], d["decision_date"], case_key)
    reviewed = cite(ecli, None, None, None, author="user", linked=first_id)

    def links():
        return dict(conn.execute("SELECT id,target_decision_id FROM relations"))

    assert relink(conn) == 3
    assert links() == {dated:first_id, undated:first_id, printed:first_id, conflicting:None, reviewed:first_id}
    assert relink(conn) == 0

    second = _bsg("rii", "second", ecli, ("1", "2"), dtype="Beschluss")
    second.sections = [Section("gruende", "court", "t", [Block("Anderer Beschluss.", "1")])]
    second_id, _ = store_decision(conn, second, content_hash="b", capture_path="-", media_type="x")
    assert second_id != first_id
    assert relink(conn) == 3
    assert links() == {dated:None, undated:None, printed:None, conflicting:None, reviewed:first_id}
    assert relink(conn) == 0
