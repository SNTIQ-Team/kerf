import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from pipeline import backfill_legacy_bgbl_history as legacy
from tools.retrospective_history import RetrospectiveHistoryError, _public_event
from test_backfill_legacy_bgbl_history import pdf_bytes, selection


def candidate(tmp_path, monkeypatch):
    archive = legacy.LegacyArchive(tmp_path, max_bytes=1_000_000, min_free_bytes=0)
    document = legacy.capture_legacy_bytes(selection(), pdf_bytes(), archive,
                                          captured_at="2026-09-21T10:00:00+00:00")
    row, = legacy.build_legacy_inventory([document], archive)
    monkeypatch.setattr(legacy, "LegacyArchive", lambda: archive)
    return row, archive, document


def test_verified_legacy_inventory_is_visible_without_claiming_full_text(tmp_path, monkeypatch):
    row, _, _ = candidate(tmp_path, monkeypatch)
    event = _public_event(row)
    assert event["published_at"] == "2006-04-24"
    assert event["effective_at"] is None
    assert event["text_status"] == "event_only"
    assert event["historical_text_reconstructed"] is False
    assert event["affected_norms"] == ["§ 1"]
    assert event["official_pdf_url"].startswith("https://www.bgbl.de/")
    for field in ("integrity_basis", "publisher_checksum_available", "fundstelle",
                  "publication_page", "publication_issue"):
        assert event[field] == row[field]
    assert event["publisher_checksum_available"] is False


def test_legacy_projection_rechecks_source_objects_before_publishing(tmp_path, monkeypatch):
    row, archive, document = candidate(tmp_path, monkeypatch)
    (archive.root / document["pdf_object"]).write_bytes(b"corrupted")
    with pytest.raises(RetrospectiveHistoryError):
        _public_event(row)


@pytest.mark.parametrize("field,value", [
    ("effective_at", "2006-01-01"),
    ("official_pdf_url", "https://www.bgbl.de.evil.test/fake.pdf"),
    ("act_id", "fed_sgb_12"),
])
def test_legacy_source_does_not_bypass_provenance_validation(tmp_path, monkeypatch, field, value):
    row, _, _ = candidate(tmp_path, monkeypatch)
    with pytest.raises(RetrospectiveHistoryError):
        _public_event({**row, field: value})
