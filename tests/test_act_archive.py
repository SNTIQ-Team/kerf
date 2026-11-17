from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from api.act_archive import (
    InvalidArchiveDateError,
    UnknownNormError,
    build_archive_index,
    head_date_for,
    markdown_filename,
    render_markdown_snapshot,
)


def _act(*, juris: str = "DE", norms: list[dict] | None = None,
         versions: list[dict] | None = None) -> dict:
    return {
        "id": "fed_testg" if juris == "DE" else "by_testg",
        "jurabk": "TestG",
        "juris": juris,
        "title": "Testgesetz",
        "build": "20200101",  # source fetch marker, not deployment HEAD
        "norms": norms or [
            {"enbez": "§ 1", "titel": "Erster Teil", "text": "Text eins"},
            {"enbez": "§ 24", "titel": "Schutz", "text": "Neuer Text"},
        ],
        "versions": versions or [],
    }


def test_numbered_article_keeps_its_own_paragraph_in_export() -> None:
    act = _act(norms=[
        {"glied": "Art 224", "enbez": "§ 1", "text": "Abstammung"},
        {"glied": "Art 229", "enbez": "§ 1", "text": "Fällige Zahlungen"},
    ])
    with pytest.raises(UnknownNormError, match=r"Art\. 224 § 1, Art\. 229 § 1"):
        render_markdown_snapshot(act, norm="§ 1")
    body = render_markdown_snapshot(act, norm="Art 229 § 1")["markdown"]
    assert 'scope: "Art. 229 § 1"' in body
    assert "## Art. 229 § 1" in body
    assert "Fällige Zahlungen" in body and "Abstammung" not in body
    assert "## Art. 224 § 1" in render_markdown_snapshot(act)["markdown"]


def test_head_markdown_is_exact_and_supports_full_act_or_one_norm() -> None:
    act = _act()
    assert head_date_for(act, "2026-07-15T12:34:56+00:00") == "2026-07-15"
    assert head_date_for(act, "2026-07-14T23:08:20+00:00") == "2026-07-15"

    full = render_markdown_snapshot(act, fallback_head="2026-07-15")
    assert full["exact"] is True
    assert full["partial"] is False
    assert full["complete"] is True
    assert full["source_exact"] is True
    assert full["verified_reconstruction"] is False
    assert full["archive_status"] == "exact"
    assert full["resolved_at"] == "2026-07-15"
    assert "## § 1 — Erster Teil" in full["markdown"]
    assert "## § 24 — Schutz" in full["markdown"]

    one = render_markdown_snapshot(
        act, requested_at="2026-07-15", norm="§24",
        fallback_head="2026-07-15")
    assert one["norm"] == "§ 24"
    assert "Neuer Text" in one["markdown"]
    assert "Text eins" not in one["markdown"]
    assert markdown_filename(one) == "fed_testg-24-2026-07-15.md"


def test_plain_norm_chemistry_and_angle_brackets_remain_literal_markdown():
    text = ("[1-(1,3-Benzodioxol-5-yl)propan-2-yl](methyl)azan; "
            "<chronisch-persistierende Hepatitis>; "
            "[Hinweis](22a); *; &amp;; C:\\Pfad.")
    act = _act(norms=[{"enbez": "§ 1", "titel": "Formel", "text": text}])
    result = render_markdown_snapshot(act, fallback_head="2026-09-27")
    body = result["markdown"].split("## § 1 — Formel\n\n", 1)[1].strip()
    assert "norm_body_format: escaped_plain_text\n" in result["markdown"]
    assert body == (r"\[1-(1,3-Benzodioxol-5-yl)propan-2-yl\](methyl)azan; "
                    r"\<chronisch-persistierende Hepatitis\>; "
                    r"\[Hinweis\](22a); \*; \&amp;; C:\\Pfad.")
    assert act["norms"][0]["text"] == text
    assert result["source_exact"] is True


def test_plain_norm_lines_do_not_become_headings_lists_or_thematic_breaks():
    text = "# Angabe\n- Minus\n+ Plus\n1. Erster\n2) Zweiter\n---\n===\n> Wort"
    act = _act(norms=[{"enbez": "§ 1", "titel": "Text", "text": text}])
    result = render_markdown_snapshot(act, fallback_head="2026-09-27")
    body = result["markdown"].split("## § 1 — Text\n\n", 1)[1].strip()
    assert body == ("\\# Angabe\n\\- Minus\n\\+ Plus\n1\\. Erster\n"
                    "2\\) Zweiter\n\\---\n\\===\n\\> Wort")


def test_effective_date_drives_reverse_state_not_publication_date() -> None:
    act = _act(juris="DE-BY", norms=[
        {"enbez": "Art. 44", "titel": "Befugnis", "text": "Neue Fassung"},
    ], versions=[{
        "date": "2024-07-23",
        "text": "Art. 44 geändert (GVBl. S. 247)",
        "changes": [{
            "para": "Art. 44",
            "effective_date": "2024-08-01",
            "old": "Alte Fassung",
            "new": "Neue Fassung",
        }],
    }])
    archive = build_archive_index(act, fallback_head="2026-07-15")
    dates = {entry["date"] for entry in archive["entries"]}
    assert {"2024-07-23", "2024-08-01", "2026-07-15"} <= dates

    before = render_markdown_snapshot(
        act, requested_at="2024-07-31", norm="Art.44",
        fallback_head="2026-07-15")
    assert "Alte Fassung" in before["markdown"]
    assert "Neue Fassung" not in before["markdown"]
    assert before["partial"] is True
    assert before["complete"] is False
    assert before["source_exact"] is False
    assert before["verified_reconstruction"] is False
    assert before["archive_status"] == "partial"
    assert any(gap["reason"] == "reconstructed_not_source_snapshot"
               for gap in before["gaps"])

    effective = render_markdown_snapshot(
        act, requested_at="2024-08-01", norm="44",
        fallback_head="2026-07-15")
    assert "Neue Fassung" in effective["markdown"]


def test_wayback_state_anchor_is_used_when_later_head_no_longer_matches() -> None:
    act = _act(juris="DE-BY", norms=[
        {"enbez": "Art. 44", "titel": "Befugnis", "text": "Fassung 2026"},
    ], versions=[{
        "date": "2025-12-23",
        "text": "Spätere Änderung ohne Synopse",
    }, {
        "date": "2024-07-23",
        "text": "Art. 44 geändert (GVBl. S. 247)",
        "changes": [{
            "para": "Art. 44",
            "effective_date": "2024-08-01",
            "old_valid": "2023-05-07",
            "new_valid": "2024-08-01",
            "old": "Archivierte alte Fassung",
            "new": "Archivierte neue Fassung",
            "source": "wayback",
        }],
    }])

    before = render_markdown_snapshot(
        act, requested_at="2024-07-31", norm="Art. 44",
        fallback_head="2026-07-15")
    effective = render_markdown_snapshot(
        act, requested_at="2024-08-01", norm="Art. 44",
        fallback_head="2026-07-15")

    assert "Archivierte alte Fassung" in before["markdown"]
    assert "Archivierte neue Fassung" in effective["markdown"]
    assert "Fassung 2026" not in before["markdown"]
    assert before["partial"] is True
    assert effective["partial"] is True
    assert any(gap["reason"] == "missing_old_new"
               for gap in before["gaps"])


def test_empty_side_is_not_treated_as_a_whole_norm_lifecycle() -> None:
    act = _act(versions=[{
        "date": "2025-01-01",
        "text": "Absatz eingefügt",
        "changes": [{"para": "24", "old": "", "new": "Neuer Text"}],
    }])
    snapshot = render_markdown_snapshot(
        act, requested_at="2024-12-31", norm="24",
        fallback_head="2026-07-15")
    # The known current text stays visible, explicitly marked partial; the API
    # does not delete § 24 based on a possibly paragraph-level empty side.
    assert "Neuer Text" in snapshot["markdown"]
    assert snapshot["partial"] is True
    assert any(gap["reason"] == "empty_change_side"
               for gap in snapshot["gaps"])


def test_truncated_and_metadata_only_transitions_are_explicit_gaps() -> None:
    act = _act(versions=[
        {"date": "2025-03-01", "text": "Metadata only"},
        {"date": "2025-02-01", "text": "Captured synopse", "changes": [{
            "para": "24", "old": "A" * 1200, "new": "B" * 1200,
        }]},
    ])
    archive = build_archive_index(act, fallback_head="2026-07-15")
    reasons = {gap["reason"] for gap in archive["gaps"]}
    assert {"metadata_only_versions", "truncated_synopse"} <= reasons
    snapshot = render_markdown_snapshot(
        act, requested_at="2025-01-01", fallback_head="2026-07-15")
    reasons = {gap["reason"] for gap in snapshot["gaps"]}
    assert {"missing_old_new", "truncated_synopse"} <= reasons


def test_historical_bare_gg_designator_uses_predominant_article_kind() -> None:
    gg = {
        "id": "fed_gg",
        "jurabk": "GG",
        "juris": "DE",
        "title": "Grundgesetz",
        "norms": [
            {"enbez": "Art. 1", "titel": "Menschenwürde", "text": "Eins"},
            {"enbez": "Art. 2", "titel": "Freiheit", "text": "Zwei"},
        ],
        "versions": [{
            "date": "2008-08-01", "text": "Art. 75 aufgehoben",
            "changes": [{"para": "75", "old": "Historischer Text", "new": ""}],
        }],
    }
    archive = build_archive_index(gg, fallback_head="2026-07-15")
    labels = {row["enbez"] for row in archive["norms"]}
    assert "Art. 75" in labels
    assert "§ 75" not in labels

    snapshot = render_markdown_snapshot(
        gg, requested_at="2008-07-31", norm="Art.75",
        fallback_head="2026-07-15")
    assert snapshot["norm"] == "Art. 75"
    assert snapshot["partial"] is True
    assert "historical designator" in snapshot["markdown"]


def test_ordinary_federal_bare_historical_designator_becomes_section() -> None:
    act = _act(versions=[{
        "date": "2010-01-01", "text": "§ 99 aufgehoben",
        "changes": [{"para": "99", "old": "Alt", "new": ""}],
    }])
    archive = build_archive_index(act, fallback_head="2026-07-15")
    assert "§ 99" in {row["enbez"] for row in archive["norms"]}


def test_invalid_future_date_and_unknown_norm_are_rejected() -> None:
    act = _act()
    with pytest.raises(InvalidArchiveDateError):
        render_markdown_snapshot(
            act, requested_at="2026-07-16", fallback_head="2026-07-15")
    with pytest.raises(InvalidArchiveDateError):
        render_markdown_snapshot(
            act, requested_at="not-a-date", fallback_head="2026-07-15")
    with pytest.raises(UnknownNormError):
        render_markdown_snapshot(
            act, norm="§ 404", fallback_head="2026-07-15")


def test_complete_official_observation_is_exact_but_not_effective_date() -> None:
    digest = "a" * 64
    act = _act(norms=[
        {"enbez": "§ 24", "titel": "Schutz", "text": "HEAD"},
    ])
    act["official_states"] = [{
        "observed_at": "2026-07-13",
        "state_sha256": digest,
        "norm_count": 1,
        "builddate": "20260712010101",
        "source_url": "https://www.gesetze-im-internet.de/testg/",
        "date_basis": "retrieval_observation_not_effective_date",
        "verification": "exact",
    }]
    state = {
        **act["official_states"][0],
        "source": "GII",
        "norms": [{"enbez": "§ 24", "titel": "Schutz",
                   "text": "Beobachteter amtlicher Text"}],
    }

    archive = build_archive_index(act, fallback_head="2026-07-15")
    observed = next(row for row in archive["entries"]
                    if row["date"] == "2026-07-13")
    assert observed["exact"] is True
    assert observed["partial"] is False
    assert observed["date_basis"] == \
        "retrieval_observation_not_effective_date"
    assert observed["state_digest"] == digest

    rendered = render_markdown_snapshot(
        act, requested_at="2026-07-13", norm="§ 24",
        fallback_head="2026-07-15", observed_state=state)
    assert rendered["exact"] is True
    assert rendered["partial"] is False
    assert rendered["complete"] is True
    assert rendered["source_exact"] is True
    assert rendered["verified_reconstruction"] is False
    assert rendered["archive_status"] == "exact"
    assert rendered["partial"] is False
    assert rendered["state_sha256"] == digest
    assert "Beobachteter amtlicher Text" in rendered["markdown"]
    assert "not an inferred legal effective date" in rendered["markdown"]
    assert "date_basis: \"retrieval_observation_not_effective_date\"" in \
        rendered["markdown"]


@pytest.mark.parametrize("state_kind", ["observed", "retrospective"])
def test_exact_historical_norm_query_cannot_resolve_from_unrelated_legacy_history(state_kind):
    act = _act(versions=[{
        "date": "2026-06-01", "changes": [{
            "para": "§ 9", "old": "Later old text", "new": "Later new text",
        }],
    }])
    state = {"observed_at": "2025-01-01", "state_sha256": "d" * 64,
             "norms": [{"enbez": "§ 1", "text": "Only historical norm"}]}
    options = {"observed_state": state} if state_kind == "observed" else {
        "retrospective_state": state,
        "retrospective_interval": {"requested_at": "2025-01-01",
            "state_sha256": "d" * 64, "text_status": "official_exact",
            "date_status": "official_verified", "gaps": []},
    }
    with pytest.raises(UnknownNormError):
        render_markdown_snapshot(act, requested_at="2025-01-01", norm="§ 9",
                                 fallback_head="2026-07-16", **options)


@pytest.mark.parametrize("state_kind", ["observed", "retrospective"])
def test_exact_historical_export_uses_the_selected_state_title(state_kind):
    act = _act()
    state = {"observed_at": "2025-01-01", "state_sha256": "e" * 64,
             "title": "Historischer Gesetzestitel",
             "norms": [{"enbez": "§ 1", "text": "Historical body"}]}
    options = {"observed_state": state} if state_kind == "observed" else {
        "retrospective_state": state,
        "retrospective_interval": {"requested_at": "2025-01-01",
            "state_sha256": "e" * 64, "text_status": "official_exact",
            "date_status": "official_verified", "gaps": []},
    }
    result = render_markdown_snapshot(act, requested_at="2025-01-01",
                                     fallback_head="2026-07-16", **options)
    assert "# Historischer Gesetzestitel\n" in result["markdown"]
    assert "# Testgesetz\n" not in result["markdown"]
    assert result["title"] == "Historischer Gesetzestitel"


def test_reviewed_legal_transition_exposes_provenance_without_claiming_exact() -> None:
    act = _act(norms=[
        {"enbez": "§ 29a", "titel": "Bericht", "text": "Neue Fassung"},
    ], versions=[{
        "date": "2026-07-10",
        "published_at": "2026-07-09",
        "effective_at": "2026-07-10",
        "observed_at": "2026-07-13",
        "date_basis": "official_bgbl_command_and_commencement_clause",
        "verification": "official_final_text_and_complete_state_pair",
        "legal_effect_verified": True,
        "review_id": "fed-review:test",
        "procedure_id": "327966",
        "source_url": "https://www.recht.bund.de/example.pdf",
        "text": "Amtliche BGBl-Änderung · Inkrafttreten geprüft",
        "changes": [{
            "para": "§ 29a", "effective_date": "2026-07-10",
            "old": "Alte Fassung", "new": "Neue Fassung",
            "source": "official_bgbl_review",
        }],
    }])

    archive = build_archive_index(act, fallback_head="2026-07-15")
    entry = next(row for row in archive["entries"]
                 if row["date"] == "2026-07-10")
    assert entry["legal_effect_verified"] is True
    assert entry["published_at"] == "2026-07-09"
    assert entry["observed_at"] == "2026-07-13"
    assert entry["exact"] is False

    rendered = render_markdown_snapshot(
        act, requested_at="2026-07-10", norm="§ 29a",
        fallback_head="2026-07-15")
    assert rendered["legal_effect_verified"] is True
    assert rendered["effective_at"] == "2026-07-10"
    assert rendered["published_at"] == "2026-07-09"
    assert rendered["partial"] is True
    assert "legal_effect_verified: true" in rendered["markdown"]
    assert "final BGBl command" in rendered["markdown"]


def test_retrospective_state_renders_two_date_axes_and_exact_body() -> None:
    act = _act(norms=[
        {"enbez": "§ 24", "titel": "Schutz", "text": "HEAD"},
    ])
    digest = "b" * 64
    interval = {
        "requested_at": "2024-06-01",
        "as_of": "2026-07-16T12:00:00Z",
        "effective_from": "2024-01-01",
        "effective_to": "2025-01-01",
        "knowledge_from": "2026-07-16T10:00:00Z",
        "knowledge_to": None,
        "published_at": "2023-12-20",
        "observed_at": "2025-01-02",
        "verified_through_observed_at": "2025-01-02",
        "state_sha256": digest,
        "text_status": "official_exact",
        "date_status": "official_verified",
        "date_basis": "official_bgbl_commencement_clause",
        "verification": "official_state_pair_and_final_bgbl",
        "retroactive": False,
        "gaps": [],
        "evidence": [{"source": "BGBl", "url": "https://example.test/bgbl"}],
    }
    state = {
        "act_id": "fed_testg", "jurabk": "TestG",
        "norms": [{"enbez": "§ 24", "titel": "Schutz",
                   "text": "Historisch exakt"}],
    }

    rendered = render_markdown_snapshot(
        act, requested_at="2024-06-01", norm="§ 24",
        fallback_head="2026-07-16", retrospective_state=state,
        retrospective_interval=interval)

    assert rendered["exact"] is True
    assert rendered["as_of"] == "2026-07-16T12:00:00Z"
    assert rendered["effective_from"] == "2024-01-01"
    assert rendered["knowledge_from"] == "2026-07-16T10:00:00Z"
    assert rendered["source_url"] == "https://example.test/bgbl"
    assert "Historisch exakt" in rendered["markdown"]
    assert "as_of: \"2026-07-16T12:00:00Z\"" in rendered["markdown"]
    assert "effective_from: \"2024-01-01\"" in rendered["markdown"]
    assert "knowledge_from: \"2026-07-16T10:00:00Z\"" in rendered["markdown"]
    assert "legal-validity interval" in rendered["markdown"]


def test_verified_reconstruction_is_complete_without_becoming_source_exact(
        ) -> None:
    act = _act(norms=[
        {"enbez": "§ 24", "titel": "Schutz", "text": "HEAD"},
    ])
    interval = {
        "requested_at": "2026-05-01",
        "as_of": "2026-07-16T12:00:00Z",
        "effective_from": "2026-04-29",
        "effective_to": "2026-06-12",
        "knowledge_from": "2026-07-16T12:00:00Z",
        "knowledge_to": None,
        "published_at": "2026-04-28",
        "observed_at": "2026-06-13",
        "verified_through_observed_at": "2026-06-13",
        "state_sha256": "c" * 64,
        "text_status": "derived_verified",
        "date_status": "official_verified",
        "date_basis": "official_bgbl_commencement_clause",
        "verification": "reviewed_inverse_then_canonical_forward_replay",
        "body_complete": True,
        "source_exact": False,
        "reverse_replay_verified": True,
        "gaps": [],
        "evidence": [{"source": "BGBl",
                      "url": "https://example.test/bgbl"}],
    }
    state = {
        "act_id": "fed_testg", "jurabk": "TestG",
        "norms": [{"enbez": "§ 24", "titel": "Schutz",
                   "text": "Replay-verifizierter Text"}],
    }

    rendered = render_markdown_snapshot(
        act, requested_at="2026-05-01", norm="§ 24",
        fallback_head="2026-07-16", retrospective_state=state,
        retrospective_interval=interval)

    assert rendered["exact"] is False
    assert rendered["partial"] is False
    assert rendered["complete"] is True
    assert rendered["source_exact"] is False
    assert rendered["verified_reconstruction"] is True
    assert rendered["archive_status"] == "verified_reconstruction"
    assert "archive_status: verified_reconstruction" in rendered["markdown"]
    assert "complete: true" in rendered["markdown"]
    assert "source_exact: false" in rendered["markdown"]
    assert "verified_reconstruction: true" in rendered["markdown"]
    assert "not an exact historical source capture" in rendered["markdown"]


def test_bavarian_archived_capture_keeps_provider_and_archive_provenance():
    act=_act(juris='DE-BY')
    state={'title':'Bayerisches Testgesetz','observed_at':'2016-01-29','state_sha256':'d'*64,'norms':[{'enbez':'Art. 1','text':'Archivtext'}], 'source':'BAYERN.RECHT','source_store':'bavarian_states','source_url':'https://www.gesetze-bayern.de/Content/Document/BayTest/true','archive_url':'https://web.archive.org/web/20160129120000id_/https://www.gesetze-bayern.de/Content/Document/BayTest/true','archive_timestamp':'20160129120000','date_basis':'archive_capture_not_effective_date'}
    result=render_markdown_snapshot(act,requested_at='2016-01-29',observed_state=state,fallback_head='2026-09-27')
    assert 'official GII' not in result['markdown']
    assert 'BAYERN.RECHT' in result['markdown']
    assert 'archive_url:' in result['markdown']
    assert 'archive_timestamp: "20160129120000"' in result['markdown']
    assert result['archive_url']==state['archive_url']
    assert result['date_basis']=='archive_capture_not_effective_date'
