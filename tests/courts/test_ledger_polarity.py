"""Corrections ledger (append-only, overlay after re-parse, conformance fixture) and the assessment stage."""
import json
import sqlite3

import pytest

import courts.ledger as ledger
from courts.ledger import CorrectionError, record
from courts.read import get_decision, open_db
from tests.courts.test_store_sync_read import ECLI, FakeHttp, FixtureAdapter, item


@pytest.fixture
def conn(fixture, tmp_path, monkeypatch):
    from courts.store import connect, init_db
    from courts.sync import sync_source
    monkeypatch.setattr(ledger, "CASES", tmp_path / "cases")
    monkeypatch.setattr(ledger, "FIXTURES", tmp_path / "fixtures")
    monkeypatch.setattr(ledger, "ROOT", tmp_path)
    c = connect()
    init_db(c)
    sync_source(FixtureAdapter(fixture, [item()]), c, FakeHttp())
    return c


def test_assessment_stage_runs_after_store_and_keeps_parser_output_pure(conn):
    rows = conn.execute("SELECT assessment, target_id, rule_id, derived_by, legal_effect_verified, paragraph_id "
                        "FROM relation_assessments").fetchall()
    keys = {(r["assessment"], r["target_id"]) for r in rows}
    assert ("applies", "de/bund/asylvfg_1992/para/24/abs/4") in keys
    # Cost clauses ("beruht auf § 154 Abs. 2 VwGO") are procedural_reference and never assessed.
    assert not any(r["target_id"] == "de/bund/vwgo/para/154/abs/2" for r in rows)
    assert conn.execute("SELECT relation FROM relations WHERE target_id='de/bund/vwgo/para/154/abs/2'"
                        ).fetchone()[0] == "procedural_reference"
    assert ("interprets", "de/bund/vwgo/para/132/abs/2/nr/1") in keys            # "im Sinne des … , wenn"
    assert all(r["derived_by"] == "rules" and r["legal_effect_verified"] == 0 for r in rows)
    assert not any(r["target_id"] == "de/bund/vwgo/para/75" for r in rows)      # Rn 10: mentions only
    assert {r[0] for r in conn.execute("SELECT DISTINCT relation FROM relations")} <= \
        {"mentions", "quotes", "procedural_reference"}


def test_ledger_is_append_only(conn):
    record(conn, target=ECLI, kind="decision_field", field="decision_type", new="Beschluss (berichtigt)",
           evidence="Berichtigungsbeschluss vom …", author="test", write_fixture=False)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE corrections SET new='x'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM corrections")


def test_decision_field_correction_survives_reparse_and_writes_open_fixture(conn, tmp_path):
    from courts.sources.rii import RiiAdapter
    from courts.sync import reparse_source
    result = record(conn, target=ECLI, kind="decision_field", field="decision_date", new="2016-03-17",
                    evidence="Verkündungsvermerk (test)", author="user")
    assert result["old"] == "2016-03-16"
    assert get_decision(open_db(), ECLI)["decision"]["decision_date"] == "2016-03-17"
    import courts.store as store_module
    store_module.PARSER_VERSION = store_module.PARSER_VERSION + "-x"             # force a real re-parse
    try:
        reparse_source(RiiAdapter(), conn)
    finally:
        store_module.PARSER_VERSION = store_module.PARSER_VERSION.removesuffix("-x")
    assert get_decision(open_db(), ECLI)["decision"]["decision_date"] == "2016-03-17"
    case = json.loads((tmp_path / result["fixture"]).read_text())
    assert (case["kind"], case["status"], case["expected"]) == ("decision_metadata", "open",
                                                                 {"decision_date": "2016-03-17"})
    assert (tmp_path / case["input"]["fixture"]).exists()                       # capture copied out of the pack


def test_rejecting_a_rule_proposal(conn, tmp_path):
    target = f"{ECLI}#rn-7"
    record(conn, target=target, kind="relation_assessment", target_id="de/bund/asylvfg_1992/para/24/abs/4",
           new={"assessment": "applies", "status": "rejected"}, evidence="test rejection", author="user")
    status = conn.execute("SELECT status FROM relation_assessments WHERE target_id=? AND assessment='applies'",
                          ("de/bund/asylvfg_1992/para/24/abs/4",)).fetchone()[0]
    assert status == "rejected"
    case = json.loads(next((tmp_path / "cases").glob("*.json")).read_text())
    assert case["expected"] == {"assessment_keys": {"$excludes": ["applies|de/bund/asylvfg_1992/para/24/abs/4"]}}


def test_confirmation_is_user_derived_and_verified(conn):
    record(conn, target=f"{ECLI}#rn-10", kind="relation_assessment", target_id="de/bund/vwgo/para/75",
           new={"assessment": "holds_inapplicable", "status": "confirmed"}, evidence="Rn 10: no Untätigkeitsklage",
           author="user", write_fixture=False)
    row = conn.execute("SELECT derived_by, legal_effect_verified, status FROM relation_assessments "
                       "WHERE target_id='de/bund/vwgo/para/75'").fetchone()
    assert tuple(row) == ("user", 1, "confirmed")


def test_invalid_corrections_are_refused(conn):
    with pytest.raises(CorrectionError):
        record(conn, target=ECLI, kind="decision_field", field="text_hash", new="x", evidence="e", author="a")
    with pytest.raises(CorrectionError):
        record(conn, target=ECLI, kind="decision_field", field="decision_date", new="16.03.2016", evidence="e",
               author="a")
    with pytest.raises(CorrectionError):
        record(conn, target="ECLI:none", kind="case_numbers", new=["x"], evidence="e", author="a")
    with pytest.raises(CorrectionError):
        record(conn, target=ECLI, kind="case_numbers", new=["1 B 19/16"], evidence=" ", author="a")

