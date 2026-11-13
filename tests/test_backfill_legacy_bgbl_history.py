from __future__ import annotations

import hashlib
import io
import json
import gzip
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import backfill_legacy_bgbl_history as legacy


def procedure(*, page: str = "926", year: str = "2006") -> dict:
    return {
        "id": "1234", "wahlperiode": 16, "beratungsstand": "Verkündet",
        "titel": "Gesetz zur Änderung des Aufenthaltsgesetzes",
        "deskriptor": [{"typ": "Rechtsmaterialien", "name": "Aufenthaltsgesetz"}],
        "inkrafttreten": [{"datum": "2006-05-01"}],
        "verkuendung": [{
            "jahrgang": year, "heftnummer": "17", "seite": page,
            "verkuendungsdatum": f"{year}-04-24",
            "ausfertigungsdatum": f"{year}-04-20",
            "verkuendungsblatt_kuerzel": "BGBl I",
            "fundstelle": f"BGBl I {year}, {page}",
            "pdf_url": "https://www.bgbl.de/xaver/bgbl/start.xav?"
            "startbk=Bundesanzeiger_BGBl&start=//*%5b@attr_id=%27"
            f"bgbl1{year[-2:]}s{int(page):04d}.pdf%27%5d",
        }],
    }


def act() -> dict:
    return {"slug": "aufenthg_2004", "jurabk": "AufenthG 2004",
            "long_title": "Gesetz über den Aufenthalt, die Erwerbstätigkeit "
                          "und die Integration von Ausländern im Bundesgebiet"}


def selection() -> dict:
    return legacy.select_legacy_documents([procedure()], [act()],
                                          "2006-01-01", "2022-12-31")[0]


def pdf_bytes(*, page: str = "926", heading: str = "Aufenthaltsgesetzes") -> bytes:
    writer = PdfWriter()
    pdfpage = writer.add_blank_page(595, 842)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica"),
                             NameObject("/Encoding"): NameObject("/WinAnsiEncoding")})
    pdfpage[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    lines = [
        "Bundesgesetzblatt Jahrgang 2006 Teil I Nr. 17,",
        "ausgegeben zu Bonn am 24. April 2006", page,
        "Gesetz zur Änderung des Aufenthaltsgesetzes", "Vom 20. April 2006",
        "Artikel 1", f"Änderung des {heading}",
        "Das Aufenthaltsgesetz wird wie folgt geändert:",
        "1. § 1 wird aufgehoben.", "Artikel 2", "Inkrafttreten",
        "Dieses Gesetz tritt am 1. Mai 2006 in Kraft.",
    ]
    stream = DecodedStreamObject()
    stream.set_data(("BT /F1 11 Tf 14 TL 50 790 Td\n" +
                     "\n".join(f"({line}) Tj T*" for line in lines) + "\nET")
                    .encode("cp1252"))
    pdfpage[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class Response:
    def __init__(self, body: bytes, *, length: int | None = None):
        self.body = body
        self.headers = {} if length is None else {"Content-Length": str(length)}
        self.status_code = 200
        self.closed = False
        self.read = 0

    def raise_for_status(self):
        pass

    def iter_content(self, chunk_size):
        for pos in range(0, len(self.body), 8):
            chunk = self.body[pos:pos + 8]
            self.read += len(chunk)
            yield chunk

    def close(self):
        self.closed = True


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}

    def get(self, url, **kwargs):
        if not self.responses:
            pytest.fail("unexpected network request during cached resume")
        return self.responses.pop(0)


def test_pre2023_resolution_uses_publication_page_not_issue():
    selected = selection()
    assert selected["document_id"] == "bgbl-1-2006-s926"
    assert selected["source_filename"] == "bgbl106s0926.pdf"
    assert selected["page"] == "926"
    assert selected["issue"] == "17"
    assert selected["publication_date"] == "2006-04-24"
    assert "text.xav?" in selected["official_pdf_url"]


@pytest.mark.parametrize("url", [
    "https://www.bgbl.de.example.org/xaver/bgbl/start.xav?start=bgbl106s0926.pdf",
    "https://www.bgbl.de/xaver/bgbl/start.xav?start=bgbl107s0926.pdf",
    "https://www.bgbl.de/xaver/bgbl/start.xav?start=bgbl106s0927.pdf",
    "https://user@www.bgbl.de/xaver/bgbl/start.xav?start=bgbl106s0926.pdf",
])
def test_selection_rejects_untrusted_or_mismatched_publication_url(url):
    row = procedure()
    row["verkuendung"][0]["pdf_url"] = url
    assert legacy.select_legacy_documents([row], [act()],
                                           "2006-01-01", "2022-12-31") == []


def test_distinct_laws_in_same_issue_remain_distinct_documents():
    second = procedure(page="930")
    second["id"] = "1235"
    selected = legacy.select_legacy_documents([procedure(), second], [act()],
                                             "2006-01-01", "2022-12-31")
    assert {row["document_id"] for row in selected} == {
        "bgbl-1-2006-s926", "bgbl-1-2006-s930"}


def test_conflicting_publication_is_quarantined_without_blocking_valid_documents():
    first = procedure()
    conflicting = procedure()
    conflicting["id"] = "1235"
    conflicting["verkuendung"][0]["ausfertigungsdatum"] = "2006-04-21"
    valid = procedure(page="930")
    valid["id"] = "1236"
    for rows in ([first, conflicting, valid, first],
                 [valid, conflicting, first, first]):
        conflicts = {}
        selected = legacy.select_legacy_documents(
            rows, [act()], "2006-01-01", "2022-12-31", conflicts=conflicts)
        assert [row["document_id"] for row in selected] == ["bgbl-1-2006-s930"]
        assert set(conflicts) == {"bgbl-1-2006-s926"}
        variants = conflicts["bgbl-1-2006-s926"]["variants"]
        assert {row["execution_date"] for row in variants} == {"2006-04-20", "2006-04-21"}
        assert {p for row in variants for p in row["procedure_ids"]} == {"1234", "1235"}
        assert all(row["official_pdf_url"].startswith("https://www.bgbl.de/") for row in variants)


def test_unreported_conflict_still_fails_closed():
    conflicting = procedure()
    conflicting["verkuendung"][0]["ausfertigungsdatum"] = "2006-04-21"
    with pytest.raises(legacy.SourceIntegrityError, match="conflicting legacy"):
        legacy.select_legacy_documents([procedure(), conflicting], [act()],
                                       "2006-01-01", "2022-12-31")


def test_publish_reports_conflict_and_retains_but_does_not_project_cached_document(tmp_path, monkeypatch):
    archive = legacy.LegacyArchive(tmp_path / "archive", max_bytes=1000000, min_free_bytes=0)
    document = legacy.capture_legacy_bytes(selection(), pdf_bytes(), archive)
    state = legacy.load_checkpoint(archive)
    state["documents"][document["document_id"]] = document
    legacy.save_checkpoint(archive, state)
    monkeypatch.setattr(legacy, "SNAPSHOT_ROOT", tmp_path / "snapshots")
    conflicts = {document["document_id"]: {"reason": "conflicting legacy publication identity", "variants": []}}
    summary = legacy._publish(
        archive, state, start="2006-01-01", end="2022-12-31", selected=[], procedures=[],
        http=legacy.BoundedHttp(max_requests=1, max_bytes=1000, delay=0),
        stats={}, stopped=None, selection_conflicts=conflicts)
    assert summary["selected_documents_conflicted"] == 1
    assert summary["selection_conflicts"] == conflicts
    assert summary["quarantined_captured_documents"] == 1
    assert summary["amendment_candidates"] == 0
    assert summary["verified_documents"] == 0
    assert document["document_id"] in legacy.load_checkpoint(archive)["documents"]
    assert (archive.root / document["pdf_object"]).is_file()
    # the patched root is honoured: the empty test snapshot never lands in
    # the real data/snapshots tree (it once became the build's latest input)
    [written] = (tmp_path / "snapshots").iterdir()
    assert (written / "summary.json").is_file()


def _snapshot(root: Path, day: str, dip_procedures: int | None) -> Path:
    path = root / day
    path.mkdir(parents=True)
    (path / "candidates.jsonl").write_text("")
    if dip_procedures is not None:
        (path / "summary.json").write_text(json.dumps(
            {"schema_version": 1, "dip_procedures": dip_procedures}))
    return path


def test_latest_legacy_snapshot_skips_empty_or_test_sized_runs(tmp_path):
    real = _snapshot(tmp_path, "2026-09-27", 2380)
    _snapshot(tmp_path, "2026-09-28", 0)          # leaked test publish
    _snapshot(tmp_path, "2026-09-29", 3)          # below the sane floor
    _snapshot(tmp_path, "2026-09-30", None)       # no summary at all
    (tmp_path / ".writing-x").mkdir()
    assert legacy.latest_legacy_snapshot(tmp_path) == real
    assert not legacy.usable_legacy_snapshot(tmp_path / "2026-09-28")
    newer = _snapshot(tmp_path, "2026-10-01", 5521)
    assert legacy.latest_legacy_snapshot(tmp_path) == newer
    # a signed workstation attestation is admitted on its own evidence
    attested = _snapshot(tmp_path, "2026-10-02", None)
    (attested / "attestation.json").write_text("{}")
    assert legacy.latest_legacy_snapshot(tmp_path) == attested
    assert legacy.latest_legacy_snapshot(tmp_path / "missing") is None


def test_web_build_reads_legacy_candidates_only_from_a_usable_snapshot(
        tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import build_web_data

    real = _snapshot(tmp_path, "2026-09-27", 2380)
    (real / "candidates.jsonl").write_text('{"id": "kept"}\n')
    _snapshot(tmp_path, "2026-09-28", 0)
    monkeypatch.setattr(legacy, "SNAPSHOT_ROOT", tmp_path)
    assert build_web_data.load_legacy_candidates() == [{"id": "kept"}]


def test_request_budget_and_stream_limit_stop_before_excess_is_buffered():
    response = Response(b"x" * 80)
    http = legacy.BoundedHttp(max_requests=1, max_bytes=20, delay=0,
                              session=Session([response]))
    with pytest.raises(legacy.BudgetExceeded):
        http.get_bytes("https://www.bgbl.de/", max_bytes=50)
    assert response.closed
    assert response.read <= 28
    assert http.downloaded_bytes <= 20
    with pytest.raises(legacy.BudgetExceeded):
        http.get_bytes("https://www.bgbl.de/")


def test_content_length_rejection_never_reads_response_body():
    response = Response(b"x" * 80, length=80)
    http = legacy.BoundedHttp(max_requests=5, max_bytes=20, delay=0,
                              session=Session([response]))
    with pytest.raises(legacy.BudgetExceeded):
        http.get_bytes("https://www.bgbl.de/")
    assert response.closed and response.read == 0


def test_archive_budget_is_cumulative_across_restarts_and_deduplicates(tmp_path):
    archive = legacy.LegacyArchive(tmp_path, max_bytes=200, min_free_bytes=0)
    first = archive.put("objects", b"x" * 100, ".pdf")
    assert archive.put("objects", b"x" * 100, ".pdf") == first
    reopened = legacy.LegacyArchive(tmp_path, max_bytes=200, min_free_bytes=0)
    with pytest.raises(legacy.BudgetExceeded):
        reopened.put("objects", b"y" * 101, ".pdf")
    assert reopened.size_bytes() == 100


def test_archive_rejects_existing_symlink_and_corrupt_object(tmp_path):
    archive = legacy.LegacyArchive(tmp_path, max_bytes=1000, min_free_bytes=0)
    relative = archive.put("objects", b"pdf", ".pdf")
    (tmp_path / relative).write_bytes(b"bad")
    with pytest.raises(legacy.SourceIntegrityError):
        archive.put("objects", b"pdf", ".pdf")
    (tmp_path / relative).unlink()
    (tmp_path / relative).symlink_to(tmp_path / "outside")
    with pytest.raises(legacy.SourceIntegrityError):
        archive.put("objects", b"pdf", ".pdf")


def test_legacy_capture_verifies_identity_and_emits_only_event_evidence(tmp_path):
    archive = legacy.LegacyArchive(tmp_path, max_bytes=1000000, min_free_bytes=0)
    document = legacy.capture_legacy_bytes(selection(), pdf_bytes(), archive,
                                          captured_at="2026-09-21T10:00:00+00:00")
    candidates = legacy.build_legacy_inventory([document], archive)
    assert len(candidates) == 1
    row = candidates[0]
    assert row["act_id"] == "fed_aufenthg_2004"
    assert row["publication_date"] == "2006-04-24"
    assert row["effective_at"] is None
    assert row["effective_date_status"] == "unresolved_legacy_commencement_review"
    assert row["historical_text_reconstructed"] is False
    assert row["candidate_only"] is True
    assert row["affected_norms"] == ["§ 1"]
    assert row["source"] == "gii+dip+bgbl.de"
    assert row["integrity_basis"] == "official_dip_url+publication_header+sha256"
    legacy.validate_legacy_candidate(row, archive=archive)
    tampered = dict(row, publication_date="2006-04-25")
    with pytest.raises(legacy.SourceIntegrityError):
        legacy.validate_legacy_candidate(tampered, archive=archive)
    (tmp_path / document["pdf_object"]).write_bytes(b"bad")
    with pytest.raises(legacy.SourceIntegrityError):
        legacy.validate_legacy_candidate(row, archive=archive)


def test_wrong_pdf_page_or_non_pdf_never_creates_source_objects(tmp_path):
    archive = legacy.LegacyArchive(tmp_path, max_bytes=1000000, min_free_bytes=0)
    for payload in (pdf_bytes(page="999"), b"<html>Login</html>"):
        with pytest.raises(legacy.SourceIntegrityError):
            legacy.capture_legacy_bytes(selection(), payload, archive)
    assert archive.size_bytes() == 0


def test_checkpoint_resume_keeps_completed_document_and_skips_download(tmp_path):
    archive = legacy.LegacyArchive(tmp_path, max_bytes=1000000, min_free_bytes=0)
    state = legacy.load_checkpoint(archive)
    document = legacy.capture_legacy_bytes(selection(), pdf_bytes(), archive)
    state["documents"][document["document_id"]] = document
    legacy.save_checkpoint(archive, state)
    before = archive.size_bytes()
    resumed = legacy.load_checkpoint(archive)
    http = legacy.BoundedHttp(max_requests=1, max_bytes=20, delay=0,
                              session=Session([]))
    documents, stats = legacy.capture_selected([selection()], resumed, archive, http,
                                              limit=1)
    assert len(documents) == 1 and stats["reused"] == 1
    assert stats["captured"] == 0 and http.requests == 0
    assert documents[0]["sha256"] == hashlib.sha256(pdf_bytes()).hexdigest()
    assert archive.size_bytes() == before


def test_discovery_resume_continues_saved_cursor_and_keeps_first_page(tmp_path):
    archive = legacy.LegacyArchive(tmp_path, max_bytes=1000000, min_free_bytes=0)
    state = legacy.load_checkpoint(archive)
    first = json.dumps({"documents": [procedure()], "cursor": "next", "numFound": 2}).encode()
    http = legacy.BoundedHttp(max_requests=1, max_bytes=100000, delay=0,
                              session=Session([Response(first)]))
    with pytest.raises(legacy.BudgetExceeded):
        legacy.discover_procedures(http, "public-key", archive, state,
                                   start="2006-01-01", end="2022-12-31",
                                   wahlperioden=[16])
    resumed = legacy.load_checkpoint(archive)
    assert resumed["discovery"]["2006-01-01:2022-12-31:16"]["cursor"] == "next"
    second = procedure(page="930")
    second["id"] = "1235"
    http = legacy.BoundedHttp(max_requests=1, max_bytes=100000, delay=0,
        session=Session([Response(json.dumps({"documents": [second], "cursor": "next"}).encode())]))
    rows = legacy.discover_procedures(http, "public-key", archive, resumed,
        start="2006-01-01", end="2022-12-31", wahlperioden=[16])
    assert {row["id"] for row in rows} == {"1234", "1235"}
    assert resumed["discovery"]["2006-01-01:2022-12-31:16"]["complete"] is True


def test_real_legacy_typography_allows_split_word_teil_in_publication_header():
    text = ("334 Bundesgesetzblatt Jahrgang 2006 T eil I Nr. 8, "
            "ausgegeben zu Bonn am 22. Februar 2006")
    candidate = {"year": "2006", "issue": "8", "page": "334",
                 "publication_date": "2006-02-22"}
    legacy._verify_publication_header(candidate, text)


def test_crypto_dependency_failure_aborts_without_source_failure(tmp_path, monkeypatch):
    archive = legacy.LegacyArchive(tmp_path, max_bytes=1000000, min_free_bytes=0)
    state = legacy.load_checkpoint(archive)

    def missing_crypto(*args, **kwargs):
        raise legacy.DependencyError("cryptography is unavailable")

    monkeypatch.setattr(legacy, "capture_legacy_document", missing_crypto)
    with pytest.raises(legacy.DependencyError):
        legacy.capture_selected([selection()], state, archive, None, limit=1)
    assert state["failures"] == {} and state["documents"] == {}


def test_resumed_document_rejoins_expanded_corpus_without_redownloading_pdf(tmp_path):
    archive = legacy.LegacyArchive(tmp_path, max_bytes=1000000, min_free_bytes=0)
    initial = selection()
    initial["retrospective_procedures"][0]["matched_acts"] = []
    document = legacy.capture_legacy_bytes(initial, pdf_bytes(), archive)
    state = legacy.load_checkpoint(archive)
    state["documents"][document["document_id"]] = document
    legacy.save_checkpoint(archive, state)
    http = legacy.BoundedHttp(max_requests=1, max_bytes=20, delay=0,
                              session=Session([]))
    documents, stats = legacy.capture_selected([selection()], state, archive, http,
                                              limit=1)
    assert len(legacy.build_legacy_inventory(documents, archive)) == 1
    assert documents[0]["sha256"] == document["sha256"]
    assert stats["reused"] == 1 and http.requests == 0
    assert len(list((tmp_path / "objects").glob("*.pdf"))) == 1
    assert len(list((tmp_path / "texts").glob("*.txt"))) == 1


def test_wrapped_legacy_heading_does_not_match_law_named_inside_other_law_title():
    text = ("Artikel 177\nÄnderung des\nEinführungsgesetzes zum Strafgesetzbuch\n"
            "(450-16)\nDas Einführungsgesetz zum Strafgesetzbuch vom\n"
            "2. März 1974 wird wie folgt geändert:\n1. Artikel 315c wird aufgehoben.")
    sections = legacy.legacy_article_sections(text)
    assert sections[0]["heading"] == "Änderung des Einführungsgesetzes zum Strafgesetzbuch"
    assert legacy.legacy_exact_target(sections[0], {"aliases": ["strafgesetzbuch"]}) is False
    assert legacy.legacy_exact_target(sections[0], {
        "aliases": ["einführungsgesetz zum strafgesetzbuch"]}) is True


def test_legacy_heading_rejects_regulation_mentioning_a_target_law():
    text = ("Artikel 14\nÄnderung der\nVerordnung zur Bezeichnung\n"
            "der als Einkommen geltenden sonstigen\nEinnahmen nach § 21 Abs. 3 Nr. 4\n"
            "des Bundesausbildungsförderungsgesetzes\n(2212-2-14)\n"
            "§ 1 Nr. 1 Buchstabe b der Verordnung wird aufgehoben.")
    section = legacy.legacy_article_sections(text)[0]
    assert legacy.legacy_exact_target(section, {
        "aliases": ["bundesausbildungsförderungsgesetz"]}) is False


def test_legacy_heading_joins_sgb_name_and_preserves_raw_article_bytes():
    text = ("Artikel 1\nÄnderung des\nDritten Buches Sozialgesetzbuch\n"
            "(860-3)\nDas Dritte Buch Sozialgesetzbuch wird wie folgt geändert:\n"
            "1. § 1 wird aufgehoben.")
    section = legacy.legacy_article_sections(text)[0]
    assert legacy.legacy_exact_target(section, {
        "aliases": ["drittes buch sozialgesetzbuch", "sozialgesetzbuch iii"]}) is True
    assert section["text"] == text


def test_real_legacy_heading_split_inside_word_preserves_exact_letter_sequence():
    section = {"heading": "Änderung des Zweiten Buches Soz ialgesetzbuch"}
    assert legacy.legacy_exact_target(section, {
        "aliases": ["zweites buch sozialgesetzbuch"]}) is True
    assert legacy.legacy_exact_target(section, {
        "aliases": ["zwölftes buch sozialgesetzbuch"]}) is False


def test_oversize_single_response_does_not_exhaust_entire_run_budget():
    too_large = Response(b"x" * 80, length=80)
    http = legacy.BoundedHttp(max_requests=3, max_bytes=200, delay=0,
                              session=Session([too_large, Response(b"small")]))
    with pytest.raises(legacy.ResponseTooLarge):
        http.get_bytes("https://www.bgbl.de/", max_bytes=40)
    assert too_large.read == 0 and too_large.closed
    assert http.get_bytes("https://www.bgbl.de/") == b"small"


def test_oversize_stream_without_length_skips_only_that_response():
    http = legacy.BoundedHttp(max_requests=3, max_bytes=200, delay=0,
                              session=Session([Response(b"x" * 80), Response(b"small")]))
    with pytest.raises(legacy.ResponseTooLarge):
        http.get_bytes("https://www.bgbl.de/", max_bytes=40)
    assert http.get_bytes("https://www.bgbl.de/") == b"small"


def test_oversized_document_is_recorded_and_next_document_captured(tmp_path, monkeypatch):
    archive = legacy.LegacyArchive(tmp_path, max_bytes=1000000, min_free_bytes=0)
    state = legacy.load_checkpoint(archive)
    oversized = {**selection(), "document_id": "oversized"}

    def capture(candidate, archive, http):
        if candidate["document_id"] == "oversized":
            raise legacy.ResponseTooLarge("32 MiB exceeds 16 MiB response cap")
        return legacy.capture_legacy_bytes(candidate, pdf_bytes(), archive)

    monkeypatch.setattr(legacy, "capture_legacy_document", capture)
    documents, stats = legacy.capture_selected([oversized, selection()], state, archive,
                                              None, limit=2)
    assert stats["failed"] == 1 and stats["captured"] == 1
    assert len(documents) == 1
    assert "ResponseTooLarge" in state["failures"]["oversized"]["reason"]


def test_resumed_selection_is_deterministic_across_python_hash_seeds():
    script = ("import json,sys; sys.path.insert(0,'pipeline'); "
              "import backfill_legacy_bgbl_history as h; "
              f"print(json.dumps(h.select_legacy_documents([{procedure()!r}],"
              f"[{act()!r}], '2006-01-01','2022-12-31'),sort_keys=True))")
    outputs = [subprocess.check_output([sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "PYTHONHASHSEED": seed}) for seed in ("1", "2", "3")]
    assert outputs[0] == outputs[1] == outputs[2]


def test_offline_discovery_accounting_retains_deduplicated_checkpoint_rows(tmp_path):
    archive = legacy.LegacyArchive(tmp_path, max_bytes=1000000, min_free_bytes=0)
    state = legacy.load_checkpoint(archive)
    payload = gzip.compress(json.dumps({"documents": [procedure()]}).encode(), mtime=0)
    relative = archive.put("dip", payload, ".json.gz")
    page = {"object": relative, "sha256": hashlib.sha256(payload).hexdigest()}
    for scope in ("2006-01-01:2006-12-31:16", "2006-01-01:2022-12-31:16"):
        state["discovery"][scope] = {"pages": [page], "complete": True}
    rows = legacy.cached_procedures(archive, state, start="2006-01-01",
                                   end="2022-12-31", wahlperioden=[16])
    assert [row["id"] for row in rows] == ["1234"]
    assert len(legacy.select_legacy_documents(rows, [act()],
                                             "2006-01-01", "2022-12-31")) == 1
    assert legacy.cached_procedures(archive, state, start="1976-01-01",
                                    end="1990-12-31", wahlperioden=[8]) == []
