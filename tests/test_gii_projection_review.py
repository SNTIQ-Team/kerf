"""Repairs to an extraction preserve both observations and never add a legal event."""
from pathlib import Path
import json

from api.act_archive import build_archive_index, render_markdown_snapshot
from api.official_state_store import observation_on, single_state_observations, unordered_observation_days
from tools import official_states

REVIEW = json.loads((Path(__file__).resolve().parents[1] / "api/data/gii_projection_review.json").read_bytes())
OLD = REVIEW["preceding_projection_sha256"]
FIXED = REVIEW["corrected_projection_sha256"]


def row(digest, day):
    return {"act_id": "fed_bgbeg", "jurabk": "BGBEG", "observed_at": day, "state_sha256": digest}


def test_correction_supersedes_only_the_reviewed_pair_on_the_same_day():
    old = row(OLD, "2026-11-25")
    replay = row(OLD, "2026-11-26")
    corrected = row(FIXED, "2026-11-26")
    observations = [old, replay, corrected]
    assert single_state_observations(observations) == [corrected]
    assert single_state_observations(observations, allow_partial=True) == [old, corrected]
    assert unordered_observation_days(observations) == set()
    assert observation_on({"official_states": observations}, old["observed_at"]) == old
    assert observation_on({"official_states": observations}, corrected["observed_at"]) == corrected
    other = row("a"*64, "2026-11-26")
    assert unordered_observation_days([replay, other]) == {("fed_bgbeg", "2026-11-26")}
    assert observation_on({"official_states": [replay, corrected, other]}, corrected["observed_at"]) is None
    assert [r["state_sha256"] for r in observations] == [OLD, OLD, FIXED]


def test_materialized_history_uses_the_reviewed_observation(tmp_path):
    from tools.build_law_history import gii_states
    old = row(OLD, "2026-11-25")
    other_old = row(OLD, "2026-11-26")
    verified = row(FIXED, "2026-11-26")
    states = gii_states({"official_states": [old, other_old, verified]}, tmp_path, [])
    assert [(s.key, s.known_from, s.known_until, s.complete) for s in states] == [
        (OLD, "2026-11-25", "2026-11-25", False),
        (FIXED, "2026-11-26", "2026-11-26", True)]


def test_present_and_missing_provisions_in_incomplete_capture_have_different_evidence():
    day = "2026-11-25"
    old = row(OLD, day)
    kept = {"enbez": "§ 1", "text": "Retained section.", "glied": "Art 224"}
    recovered = {"enbez": "Art 50", "text": "Later extraction only."}
    act = {"id": "fed_bgbeg", "juris": "DE", "jurabk": "BGBEG",
           "norms": [kept, recovered], "official_states": [old]}
    def render(norm=None):
        return render_markdown_snapshot(act, requested_at=day, fallback_head="2026-11-26", norm=norm,
                                        observed_state={**old, "norms": [kept]})
    whole = render()
    assert whole["partial"] and not whole["complete"]
    assert whole["gaps"][0]["reason"] == "incomplete_source_projection"
    assert "Retained section." in whole["markdown"] and "Later extraction only." not in whole["markdown"]
    assert render("Art. 224 § 1")["exact"]
    missing = render("Art. 50")
    assert missing["partial"] and "Later extraction only." not in missing["markdown"]
    assert "**Art 50**" in missing["markdown"]
    legacy_head = {**act, "norms": [kept], "official_states": [{**old, "norm_count": 1}]}
    assert render_markdown_snapshot(legacy_head, fallback_head=day)["partial"]
    index = build_archive_index(act, fallback_head="2026-11-26")
    assert next(r for r in index["entries"] if r["date"] == day)["partial"]


def test_replayed_extraction_creates_no_amendment(tmp_path, monkeypatch):
    from tests.test_official_states import _snapshot
    prior = _snapshot(tmp_path/"old", "2026-11-25")
    repeat = _snapshot(tmp_path/"repeat", "2026-11-26")
    article = {"slug":"demog","jurabk":"DemoG","enbez":"Art 50","titel":"","text":"Direct article.",
               "doknr":"A50","gliederung":""}
    corrected = _snapshot(tmp_path/"corrected", "2026-11-26", extra_norms=[article])
    later = _snapshot(tmp_path/"later", "2026-11-27", text="new section text", extra_norms=[article])
    store = tmp_path/"store"
    m = official_states.archive_gii_states([prior, repeat, corrected, later], store)
    old = m["observations"][0]["state_sha256"]
    new = next(r["state_sha256"] for r in m["observations"] if r["observed_at"]=="2026-11-26" and r["norm_count"]==2)
    from api import official_state_store
    monkeypatch.setattr(official_state_store,"_projection_review",lambda:(frozenset({old}),old,new))
    changes = official_states.transitions(m, store)
    assert len(m["observations"]) == 4
    assert [(r["date"], [c["para"] for c in r["changes"]]) for r in changes] == [("2026-11-27", ["§ 1"])]
