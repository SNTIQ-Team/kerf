"""Single-page ingest of user-browser captures (courts.capture): contract, rate guard, inbox, provenance."""
import gzip
import io
import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from courts import capture
from courts.capture import CaptureRefused, ingest_capture
from courts.config import data_home, db_path

ROOT = Path(__file__).resolve().parents[2]
DECISIONS = ROOT / "conformance" / "fixtures" / "decisions"
DOC = "Y-300-Z-BECKRS-B-2023-N-10142"
URL = f"https://www.gesetze-bayern.de/Content/Document/{DOC}"
OTHER = "Y-300-Z-BECKRS-B-2023-N-10253"
OTHER_URL = f"https://www.gesetze-bayern.de/Content/Document/{OTHER}"


def page(name: str) -> bytes:
    data = (DECISIONS / f"bayern-recht-{name}").read_bytes()
    return gzip.decompress(data) if name.endswith(".gz") else data


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def db():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    return conn


def test_direct_ingest_stores_the_decision_with_provenance():
    result = ingest_capture("by", URL + "?hl=true", page("B-2023-N-10142.html.gz"),
                            captured_at="2026-09-28T19:00:00+02:00")
    assert result["status"] == "ok" and result["stored"] is True
    assert result["decision_id"] == "court:vgh-by/2023-01-18/16b_D_22.2183"
    assert result["numbering"] == "rn" and result["paragraphs"] > 36
    assert result["provenance"]["capture_origin"] == "user_browser"
    assert result["provenance"]["captured_at"] == "2026-09-28T17:00:00Z"
    assert result["provenance"]["capture_url"] == URL + "?hl=true" and result["provenance"]["canonical_url"] == URL
    conn = db()
    d = conn.execute("SELECT * FROM decisions WHERE id = ?", (result["decision_id"],)).fetchone()
    meta = json.loads(d["metadata"])
    assert meta["capture_origin"] == "user_browser" and meta["capture"]["sha256"] == result["provenance"]["sha256"]
    assert d["source_id"] == "by" and d["source_doc_id"] == DOC and d["source_url"] == URL
    item = conn.execute("SELECT * FROM source_items WHERE source_id='by' AND source_doc_id=?", (DOC,)).fetchone()
    extra = json.loads(item["extra"])
    assert item["state"] == "stored" and extra["route"] == "portal_capture" and extra["capture_origin"] == "user_browser"
    assert conn.execute("SELECT count(*) FROM captures WHERE sha256 = ?", (d["content_hash"],)).fetchone()[0] == 1
    assert capture.pending_tickets() == []


def test_reparse_keeps_the_provenance():
    from courts.sources import get_adapter
    from courts.store import connect
    from courts.sync import reparse_source
    result = ingest_capture("by", URL, page("B-2023-N-10142.html.gz"), captured_at="2026-09-28T10:00:00Z")
    conn = connect()
    counts = reparse_source(get_adapter("by"), conn, force=True)
    assert counts["failed"] == 0 and counts["reparsed"] == 1
    meta = json.loads(conn.execute("SELECT metadata FROM decisions WHERE id=?", (result["decision_id"],)).fetchone()[0])
    assert meta["capture_origin"] == "user_browser" and meta["capture"]["captured_at"] == "2026-09-28T10:00:00Z"


def test_rate_guard_interval_and_daily_limit():
    clock = Clock()
    ingest_capture("by", URL, page("B-2023-N-10142.html.gz"), clock=clock, sleep=clock.sleep)
    clock.t += 3
    with pytest.raises(CaptureRefused) as refused:
        ingest_capture("by", OTHER_URL, page("B-2023-N-10253.html.gz"), clock=clock, sleep=clock.sleep)
    assert refused.value.status == "rate_limited" and refused.value.reason == "min_interval"
    assert 6.5 < refused.value.as_dict()["retry_after_s"] <= 7.0
    clock.t += 7.5
    assert ingest_capture("by", OTHER_URL, page("B-2023-N-10253.html.gz"), clock=clock,
                          sleep=clock.sleep)["status"] == "ok"
    for n in range(98):   # 100 attempts within 24 h
        capture._ledger_append(None, {"t": clock.t + 20 + n * 11, "attempt": True, "event": "queued"})
    clock.t += 20 + 98 * 11
    with pytest.raises(CaptureRefused) as refused:
        ingest_capture("by", f"https://www.gesetze-bayern.de/Content/Document/Y-300-Z-BECKRS-B-2025-N-12316",
                       page("B-2025-N-12316.html.gz"), clock=clock, sleep=clock.sleep)
    assert refused.value.reason == "daily_limit" and refused.value.retry_after_s > 80000


def test_the_same_capture_again_returns_the_first_result_and_is_not_counted():
    clock = Clock()
    first = ingest_capture("by", URL, page("B-2023-N-10142.html.gz"), clock=clock, sleep=clock.sleep)
    clock.t += 1
    again = ingest_capture("by", URL, page("B-2023-N-10142.html.gz"), clock=clock, sleep=clock.sleep)
    assert again["decision_id"] == first["decision_id"] and "ingested before" in again["warnings"][-1]
    assert sum(1 for e in capture.ledger_entries() if e.get("attempt")) == 1


def test_challenge_page_is_rejected_and_counted(fixture):
    challenge = fixture("by/gesetze_bayern_security_check_503.html")
    with pytest.raises(CaptureRefused) as refused:
        ingest_capture("by", URL, challenge)
    assert refused.value.status == "invalid_argument" and refused.value.reason == "challenge"
    assert [e["event"] for e in capture.ledger_entries()] == ["rejected"]
    assert not db_path().exists() or db().execute("SELECT count(*) FROM decisions").fetchone()[0] == 0


def test_page_of_another_document_is_rejected():
    with pytest.raises(CaptureRefused) as refused:
        ingest_capture("by", OTHER_URL, page("B-2023-N-10142.html.gz"))
    assert refused.value.reason == "url_mismatch"


def test_the_portal_pdf_is_accepted():
    result = ingest_capture("by", f"https://www.gesetze-bayern.de/Content/Pdf/{DOC}?all=False",
                            page("B-2023-N-10142.pdf"))
    assert result["decision_id"] == "court:vgh-by/2023-01-18/16b_D_22.2183"
    assert result["provenance"]["media_type"] == "application/pdf"


@pytest.mark.parametrize("url,body,reason", [
    ("https://www.gesetze-bayern.de/Search/Hitlist", b"<html></html>", "not_a_decision_url"),
    (URL, [b"<html>", b"<html>"], "bulk_refused"),
    (URL, b"", "empty_capture"),
    (URL, b"\x89PNG....", "unsupported_media"),
    (URL, b"<html><body><div id='content'><p>Startseite</p></div></body></html>", "not_a_decision_page"),
])
def test_argument_refusals(url, body, reason):
    with pytest.raises(CaptureRefused) as refused:
        ingest_capture("by", url, body)
    assert refused.value.status == "invalid_argument" and refused.value.reason == reason


def test_unknown_source_is_refused():
    with pytest.raises(CaptureRefused) as refused:
        ingest_capture("openjur", "https://openjur.de/u/1.html", b"<html></html>")
    assert refused.value.reason == "unknown_source"


def test_busy_writer_queues_the_capture_and_the_next_call_ingests_it():
    from courts.writer import writer_lock
    body = page("B-2023-N-10142.html.gz")
    with writer_lock("compact (does not serve the inbox)"):
        with pytest.raises(CaptureRefused) as refused:
            ingest_capture("by", URL, body, wait=0.2)
    out = refused.value.as_dict()
    assert out["status"] == "busy" and out["queued"] is True and out["retry_after_s"] == 30
    assert capture.pending_tickets() == [out["ticket"]]
    result = ingest_capture("by", URL, body)       # no writer now: the call takes the lock and drains the inbox
    assert result["status"] == "ok" and result["ticket"] == out["ticket"] and capture.pending_tickets() == []
    assert sum(1 for e in capture.ledger_entries() if e.get("attempt")) == 1


def test_a_running_writer_serves_the_inbox_between_documents():
    """A writer (sync pipeline, reparse, classify) holds the lock; Maintenance.serve_inbox ingests the item."""
    from courts.store import connect, init_db
    from courts.writer import Maintenance, writer_lock
    stop, ready = threading.Event(), threading.Event()
    served = []

    def writer():
        with writer_lock("sync (test)"):
            conn = connect()
            init_db(conn)
            maintenance = Maintenance(conn, every_items=10 ** 6, every_seconds=10 ** 6)
            ready.set()
            while not stop.is_set():
                served.append(maintenance.tick())     # not due: serves the inbox only
                time.sleep(0.05)
            conn.close()
    thread = threading.Thread(target=writer)
    thread.start()
    ready.wait(5)
    try:
        result = ingest_capture("by", URL, page("B-2023-N-10142.html.gz"), wait=20)
    finally:
        stop.set()
        thread.join(5)
    assert result["status"] == "ok" and result["stored"] is True
    assert (capture.inbox_dir() / "results" / f"{result['ticket']}.json").exists()


def test_pipeline_writer_serves_the_inbox_when_idle():
    from courts.pipeline import Writer
    from courts.store import connect, init_db
    from courts.writer import Maintenance
    body = page("B-2023-N-10253.html.gz")
    p = capture.check_content(capture.check_arguments("by", OTHER_URL, body, captured_at=None, captured_at_basis=None,
                                                      now=time.time()))
    ticket = capture.enqueue(p, None, time.time())
    conn = connect()
    init_db(conn)
    writer = Writer(conn, Maintenance(conn), log=lambda m: None)
    writer._serve_inbox()
    assert capture.read_result(None, ticket)["decision_id"] == "court:olg-bamberg/2023-05-04/2_UF_244-22"


def test_duplicate_of_a_stored_decision_returns_the_existing_id():
    first = ingest_capture("by", f"https://www.gesetze-bayern.de/Content/Pdf/{DOC}", page("B-2023-N-10142.pdf"),
                           clock=Clock(), sleep=time.sleep)
    later = Clock(1_800_000_100.0)
    second = ingest_capture("by", URL + "?hl=true", page("B-2023-N-10142.html.gz"), clock=later, sleep=later.sleep)
    # One BAYERN.RECHT document, two representations: the same source item; the HTML re-stores it.
    assert second["decision_id"] == first["decision_id"]


def test_cli_contract(tmp_path, capsys):
    from courts.cli import main
    f = tmp_path / "page.html"
    f.write_bytes(page("B-2023-N-10142.html.gz"))
    assert main(["ingest-capture", "--source", "by", "--url", URL, "--file", str(f),
                 "--captured-at", "2026-09-28T12:00:00Z"]) == 0
    out, err = capsys.readouterr()
    result = json.loads(out)
    assert result["decision_id"] == "court:vgh-by/2023-01-18/16b_D_22.2183" and err == ""
    assert main(["ingest-capture", "--source", "by", "--url", OTHER_URL, "--file", str(f)]) == 5
    out, err = capsys.readouterr()
    line = json.loads(err.strip())
    assert out == "" and line["status"] == "rate_limited" and "retry_after_s" in line and len(err.splitlines()) == 1
    assert main(["ingest-capture", "--source", "by", "--url", URL, "--file", str(f), "--file", str(f)]) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "bulk_refused"
    assert main(["ingest-capture", "--source", "by", "--url", URL, "--file", str(tmp_path)]) == 2
    assert json.loads(capsys.readouterr().err)["reason"] == "bulk_refused"
    assert main(["ingest-capture", "--source", "by", "--url", URL, "--bogus"]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "invalid_argument"
    assert main(["ingest-capture", "--status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["attempts_24h"] == 1 and status["results"][0]["decision_id"] == result["decision_id"]


def test_captured_at_defaults_to_the_file_mtime(tmp_path, capsys):
    import os
    from courts.cli import main
    f = tmp_path / "page.html"
    f.write_bytes(page("B-2023-N-10142.html.gz"))
    os.utime(f, (1_790_000_000, 1_790_000_000))
    assert main(["ingest-capture", "--source", "by", "--url", URL, "--file", str(f)]) == 0
    prov = json.loads(capsys.readouterr().out)["provenance"]
    assert prov["captured_at"] == "2026-09-21T14:13:20Z" and prov["captured_at_basis"] == "file_mtime"


def test_captured_at_in_the_future_is_refused():
    with pytest.raises(CaptureRefused) as refused:
        ingest_capture("by", URL, page("B-2023-N-10142.html.gz"), captured_at="2099-01-01T00:00:00Z")
    assert refused.value.reason == "captured_at_in_future"


def test_capture_of_a_decision_stored_from_the_court_pdf_is_a_skipped_duplicate():
    from courts.model import Block, ParsedDecision, Section
    from courts.store import connect, init_db, store_decision
    conn = connect()
    init_db(conn)
    court_pdf = ParsedDecision(
        source_id="by", source_doc_id="vgh:/media/16b-d-22-2183.pdf", source_url="https://www.vgh.bayern.de/x.pdf",
        court_name="Bayerischer Verwaltungsgerichtshof", decision_date="2023-01-18", case_numbers_raw="16b D 22.2183",
        case_numbers=["16b D 22.2183"], decision_type="Urteil",
        sections=[Section("gruende", "court", "court PDF", [Block("Der Kläger begehrt …", "1")])])
    stored_id, _ = store_decision(conn, court_pdf, content_hash="x", capture_path="-", media_type="application/pdf")
    conn.close()
    result = ingest_capture("by", URL, page("B-2023-N-10142.html.gz"))
    assert result["decision_id"] == stored_id and result["stored"] is False
    assert result["stored_from"] == "by:vgh:/media/16b-d-22-2183.pdf"
    assert any(w.startswith("duplicate_identity") for w in result["warnings"])
    item = db().execute("SELECT state, failure_kind, content_hash FROM source_items WHERE source_doc_id=?",
                        (DOC,)).fetchone()
    assert item["state"] == "skipped" and item["failure_kind"] == "duplicate_identity" and item["content_hash"]
