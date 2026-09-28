"""Cross-representation verification: real NRWE HTML vs PDF, typed findings, stored decision untouched."""
import json

from courts.sources.nrw import parse_document
from courts.store import finalise
from courts.verify import compare, parse_nrwe_pdf, parsed_representation, stored_representation, verify_sample

N = "L_9_SO_202_26_B_ER_und_L_9_SO_203_26_B_Beschluss_20260813"


def representations(fixture):
    html = fixture(f"nrw/{N}.html").decode("utf-8")
    a = parsed_representation("nrwe-html", finalise(parse_document(html, "https://nrwe.justiz.nrw.de/x.html", N)))
    b = parsed_representation("nrwe-pdf", parse_nrwe_pdf(fixture(f"nrw/{N}.pdf"), "https://x.pdf", N))
    return a, b


def test_nrwe_html_and_pdf_agree(fixture):
    a, b = representations(fixture)
    assert len(a.rn_texts) == len(b.rn_texts) == 17
    assert compare(a, b) == []


def test_disagreements_are_typed_findings(fixture):
    a, b = representations(fixture)
    b.rn_texts["9"] = b.rn_texts["9"] + " " + b.rn_texts.pop("10")            # boundary shifted
    b.rn_texts["3"] = "völlig anderer Text"
    b.case_numbers = ["L 9 SO 202/26 B ER"]
    kinds = {(f["kind"], f["locator"]) for f in compare(a, b)}
    assert ("rn_count_mismatch", None) in kinds and ("case_numbers_mismatch", None) in kinds
    assert ("rn_boundary_mismatch", "rn-9") in kinds and ("text_mismatch", "rn-3") in kinds


def test_verify_sample_uses_packed_duplicate_capture_and_never_changes_the_decision(fixture):
    from courts.store import connect, init_db
    from courts.sync import sync_source
    from tests.courts.test_store_sync_read import ECLI, FakeHttp, FixtureAdapter, item, DOC
    from courts.sources.rii import RiiAdapter
    conn = connect()
    init_db(conn)
    sync_source(FixtureAdapter(fixture, [item()]), conn, FakeHttp())

    class CourtSite(FixtureAdapter):
        source_id = "bverwg"

        def parse(self, capture, it):
            parsed = RiiAdapter().parse(capture, dict(it, source_doc_id=DOC))
            parsed.source_id, parsed.source_doc_id = "bverwg", it["source_doc_id"]
            return parsed
    sync_source(CourtSite(fixture, [item("160316B1B19.16.0")]), conn, FakeHttp())
    before = conn.execute("SELECT text_hash FROM decisions WHERE id=?", (ECLI,)).fetchone()[0]
    import courts.sources as sources
    real = sources.get_adapter
    sources.get_adapter = lambda sid: CourtSite(fixture, []) if sid == "bverwg" else real(sid)
    try:
        result = verify_sample(conn, sample=5, http=None)
    finally:
        sources.get_adapter = real
    assert result["pairs"] == 1 and result["agreeing"] == 1 and result["findings"] == 0
    check = conn.execute("SELECT representation_a, representation_b, outcome FROM verification_checks").fetchone()
    assert tuple(check) == ("stored:rii", "capture:bverwg", "agree")
    assert conn.execute("SELECT text_hash FROM decisions WHERE id=?", (ECLI,)).fetchone()[0] == before
