"""Schema v3 -> v4: the in-place upgrade (init_db) and the verified copy (migrate-schema) on a real v3 file
written by the v3 code (tests/courts/fixtures/v3: BVerwG 1 B 19.16 via RII, two NRWE decisions, a
paragraph-level and a decision-level user assessment)."""
import hashlib
import json
import sqlite3

import pytest

from courts.store import connect, create_schema, init_db

FIXTURE = "v3/corpus-v3.sqlite.gz"
ECLI = "ECLI:DE:BVerwG:2016:160316B1B19.16.0"


@pytest.fixture
def v3_file(fixture, tmp_path):
    path = tmp_path / "v3.sqlite"
    path.write_bytes(fixture(FIXTURE))   # the fixture loader decompresses .gz
    return path


def test_the_fixture_is_v3(v3_file):
    conn = sqlite3.connect(v3_file)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert {r[1] for r in conn.execute("PRAGMA table_info(paragraphs)")} >= {"id", "blocks", "text_hash"}


def test_verified_copy_reproduces_every_row(v3_file, tmp_path):
    from courts.schema_v4 import build_copy, verify
    target = tmp_path / "v4.sqlite"
    counts = build_copy(v3_file, target, create_schema, log=lambda *_: None)
    report = verify(v3_file, target, sample=50, log=lambda *_: None)
    assert report["problems"] == [], report
    assert report["fts_integrity"] == "ok" and report["integrity_check"] == "ok"
    assert counts["paragraphs"] == report["row_counts"]["paragraphs"][0] > 50
    conn = sqlite3.connect(target)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    # the v3 file is untouched (the fallback)
    assert sqlite3.connect(v3_file).execute("PRAGMA user_version").fetchone()[0] == 3


def test_in_place_upgrade_and_the_v4_reader_contract(v3_file, monkeypatch):
    from courts.read import citing_decisions, get_paragraph, open_db, search
    monkeypatch.setenv("SNTIQ_COURTS_HOME", str(v3_file.parent))
    home_db = v3_file.parent / "courts.sqlite"
    v3_file.rename(home_db)
    conn = connect(home_db)
    init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    columns = {r[1] for r in conn.execute("PRAGMA table_info(paragraphs)")}
    assert "id" not in columns and {"anchor", "ordinal", "decision_id"} <= columns
    assert "paragraph_ordinal" in {r[1] for r in conn.execute("PRAGMA table_info(relations)")}
    conn.execute("INSERT INTO paragraphs_fts(paragraphs_fts, rank) VALUES('integrity-check', 1)")
    # paragraph id -> (decision_id, anchor); text_hash = 32 raw bytes of SHA-256(text)
    row = conn.execute("SELECT text, text_hash, blocks, ordinal FROM paragraphs WHERE decision_id=? AND anchor='rn-10'",
                       (ECLI,)).fetchone()
    assert row["text_hash"] == hashlib.sha256(row["text"].encode()).digest()
    assert json.loads(row["blocks"])[0][0] > 0
    reader = open_db(home_db)
    item = get_paragraph(reader, f"{ECLI}#rn-10")["evidence"][0]
    assert item["id"] == f"{ECLI}#rn-10" and len(item["blocks"]) == 3
    assert "Streitgegenstand der erhobenen Klage" in item["text"]
    assert item["version_id"] == hashlib.sha256(item["text"].encode()).hexdigest()
    hits = search(reader, q='"Streitgegenstand der erhobenen Klage"')["evidence"]
    assert [h["id"] for h in hits] == [f"{ECLI}#rn-10"]
    # user rows: the paragraph-level one keeps its paragraph, the decision-level one stays decision-level
    users = conn.execute("SELECT paragraph_ordinal, target_id FROM relation_assessments WHERE derived_by='user' "
                         "ORDER BY id").fetchall()
    assert [tuple(u) for u in users] == [(row["ordinal"], "de/bund/vwgo/para/75"),
                                         (None, "de/bund/asylvfg_1992/para/24/abs/4")]
    rels = citing_decisions(reader, "de/bund/vwgo/para/75")["relations"]
    rn10 = [r for r in rels if r["evidence"]["paragraph_id"] == f"{ECLI}#rn-10"]
    assert rn10 and any(a["derived_by"] == "user" and a["assessment"] == "holds_inapplicable"
                        for a in rn10[0]["assessments"])
    assert rn10[0]["detail"]["act_prefix"] == "de/bund/vwgo" and rn10[0]["detail"]["chain_no"] >= 1


def test_detail_round_trip_and_trimmed_storage():
    from courts.detail import expand_detail, trim_detail
    row = {"target_kind": "norm", "target_id": "de/bund/gvg/para/198/abs/4", "target_raw": "§ 198 Abs. 4 GVG",
           "target_court_id": None, "target_date": None, "target_case_key": None}
    full = {"act": "GVG", "act_prefix": "de/bund/gvg", "act_resolution": "act_index", "chain": "§ 198 Abs. 4 GVG",
            "chain_no": 1, "lexgraph_act_id": "fed_gvg", "unit_type": "para"}
    assert json.loads(trim_detail(full, row)) == {"act": "GVG"}
    assert expand_detail(trim_detail(full, row), row) == full
    other = dict(full, chain="§ 198 Abs. 2 Satz 2, Abs. 4 GVG", chain_no=2, lexgraph_act_id=None)
    assert json.loads(trim_detail(other, row)) == {"act": "GVG", "chain": other["chain"], "chain_no": 2,
                                                    "lexgraph_act_id": None}
    assert expand_detail(trim_detail(other, row), row) == other
    ecli_only = {"target_kind": "decision", "target_id": "ECLI:EU:C:2021:89", "target_raw": "ECLI:EU:C:2021:89",
                 "target_court_id": None, "target_date": None, "target_case_key": None}
    assert expand_detail(trim_detail({"ecli": "ECLI:EU:C:2021:89"}, ecli_only), ecli_only) == {"ecli": "ECLI:EU:C:2021:89"}


def test_demoted_rules_are_stored_as_mentions_with_cue(monkeypatch, tmp_path):
    from courts.model import Block, ParsedDecision, Section
    from courts.store import store_decision
    from courts.sync import after_store
    conn = connect()
    init_db(conn)
    parsed = ParsedDecision(source_id="rii", source_doc_id="x", source_url="https://x", court_name="Bundesverwaltungsgericht",
                            decision_date="2026-01-01", case_numbers_raw="1 C 1.26", case_numbers=["1 C 1.26"],
                            decision_type="Urteil", ecli="ECLI:DE:BVerwG:2026:010126U1C1.26.0",
                            sections=[Section("gruende", "court", "t", [
                                Block("Dies ergibt sich aus der systematischen Auslegung des § 13 Abs. 1 PBefG.", "1")])])
    decision_id, _ = store_decision(conn, parsed, content_hash="h", capture_path="-", media_type="x")
    after_store(conn, decision_id)
    row = conn.execute("SELECT assessment, polarity, cue, cue_suggests, rule_id, evidence_text, paragraph_ordinal "
                       "FROM relation_assessments WHERE decision_id=?", (decision_id,)).fetchone()
    assert tuple(row)[:5] == ("mentions_with_cue", "neutral", "Auslegung des ", "interprets", "norm.auslegung_des")
    assert "systematischen Auslegung" in row["evidence_text"] and row["paragraph_ordinal"] == 1


def test_migrate_schema_command_builds_verifies_and_swaps(fixture, isolated_corpus):
    from courts.cli import main
    isolated_corpus.mkdir(parents=True, exist_ok=True)
    (isolated_corpus / "courts.sqlite").write_bytes(fixture(FIXTURE))
    assert main(["migrate-schema"]) == 0
    assert sqlite3.connect(isolated_corpus / "courts.sqlite").execute("PRAGMA user_version").fetchone()[0] == 4
    assert sqlite3.connect(isolated_corpus / "courts.sqlite.v3").execute("PRAGMA user_version").fetchone()[0] == 3
    report = json.loads((isolated_corpus / "logs" / "migrate-v3-v4-report.json").read_text())
    assert report["problems"] == [] and report["fts_integrity"] == "ok"
    assert main(["migrate-schema"]) == 0          # idempotent: already v4
