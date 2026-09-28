import copy

import pytest

from pipeline import legacy_bgbl_history
from pipeline.legacy_bgbl_history import load_reviewed_candidates
from api.retrospective_store import amending_act, list_changes
from tools.retrospective_history import RetrospectiveHistoryError, _public_event


def test_reviewed_sgb12_command_keeps_publication_and_effect_separate():
    candidate, = load_reviewed_candidates()
    event = _public_event(candidate)
    assert event["document_id"] == "bgbl-1-2022-s2328"
    assert event["published_at"] == "2022-12-20"
    assert event["effective_at"] == event["date"] == "2023-01-01"
    assert event["date_basis"] == "official_bgbl_reviewed_command_commencement"
    assert event["affected_norms"] == ["§ 11", "§ 12"]
    assert event["commands"][0]["ref"]["paras"] == ["11", "12"]
    assert event["commencement_evidence"][0]["article"] == "13"
    assert event["commencement_evidence"][0]["source_page"] == 2351
    assert event["text_status"] == "event_only"
    assert event["historical_text_reconstructed"] is False
    assert [item["source"] for item in event["evidence"]] == ["BGBl"]


@pytest.mark.parametrize("field,value", [
    ("official_html_url", "https://example.com/fake"),
    ("effective_at", "2022-12-20"),
    ("amending_article", "6"),
    ("affected_norms", ["§ 13"]),
    ("pdf_sha256", "0" * 64),
    ("text_sha256", "1" * 64),
])
def test_legacy_source_exception_is_limited_to_reviewed_command(field, value):
    candidate = copy.deepcopy(load_reviewed_candidates()[0])
    candidate[field] = value
    with pytest.raises(RetrospectiveHistoryError, match="legacy BGBl"):
        _public_event(candidate)


def test_joint_command_is_in_each_norm_history_and_document_details():
    event = _public_event(load_reviewed_candidates()[0])
    event["knowledge_from"] = "2026-09-21T14:01:51Z"
    event["knowledge_to"] = None
    manifest = {
        "schema_version": 1, "built_at": "2026-09-21T14:01:51Z",
        "acts": {"fed_sgb_12": {
            "jurabk": "SGB 12", "events": [event],
        }},
    }
    for norm in ["§ 11", "§ 12"]:
        changes = list_changes(manifest, act="fed_sgb_12", norm=norm)
        assert changes["matched"] == 1
        assert changes["events"][0]["effective_at"] == "2023-01-01"
    assert list_changes(manifest, act="fed_sgb_12", norm="§ 110")["matched"] == 0
    details = amending_act(manifest, "bgbl-1-2022-s2328")
    assert details["affected_acts"][0]["affected_norms"] == ["§ 11", "§ 12"]


def test_reviewed_command_cannot_be_replaced_with_unreviewed_text():
    candidate = copy.deepcopy(load_reviewed_candidates()[0])
    candidate["commands"][0]["raw"] += " unreviewed"
    with pytest.raises(RetrospectiveHistoryError, match="legacy BGBl"):
        _public_event(candidate)


def test_legacy_supplement_does_not_change_existing_modern_event_projection():
    candidate = {
        "id": "modern", "act_id": "fed_example", "source": "gii+dip+recht.bund.de",
        "publication_date": "2024-01-01", "effective_at": "2024-02-01",
        "pdf_sha256": "a" * 64, "candidate_only": True,
        "historical_text_reconstructed": False,
        "official_html_url": "https://www.recht.bund.de/example",
        "official_pdf_url": "https://www.recht.bund.de/example.pdf",
    }
    before = _public_event(candidate)
    candidate["commencement_evidence"] = [{"raw": "existing modern inventory evidence"}]
    assert _public_event(candidate) == before


@pytest.mark.parametrize("folder,digest,suffix", [
    ("objects", legacy_bgbl_history.REVIEWED_PDF_SHA256, ".pdf"),
    ("texts", legacy_bgbl_history.REVIEWED_TEXT_SHA256, ".txt"),
])
def test_corrupt_cached_source_blocks_offline_build(tmp_path, monkeypatch, folder, digest, suffix):
    monkeypatch.setattr(legacy_bgbl_history, "ROOT", tmp_path)
    source = tmp_path / "data" / "bgbl_documents" / folder / (digest + suffix)
    source.parent.mkdir(parents=True)
    source.write_bytes(b"corrupt source")
    with pytest.raises(ValueError, match="cached source digest mismatch"):
        load_reviewed_candidates()
