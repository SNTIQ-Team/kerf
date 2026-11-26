from __future__ import annotations

import copy
import gzip
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "pipeline")]

from api import main
from official_states import store_state_object
from retrospective_history import build_public_manifest, materialize_history, RetrospectiveHistoryError
from verified_editions import add_reviewed_editions, GG_REVIEW
from verified_reconstruction import ARTIFACT_KIND, ReconstructionError

BUILT = "2026-11-26T08:00:00Z"


def _inputs(day="2026-11-26"):
    review = json.loads(GG_REVIEW.read_bytes())
    state = json.loads(gzip.decompress(
        (ROOT / "tests/fixtures/verified_editions/gg-20260506.json.gz").read_bytes()))
    obs = {**review["anchor"], "act_id": "fed_gg", "jurabk": "GG", "norm_count": len(state["norms"]),
           "observed_at": day, "source_slug": "gg", "source": "GII", "verification": "exact",
           "date_basis": "retrieval_observation_not_effective_date"}
    states = {review["anchor"]["state_sha256"]: state}
    artifact = {"schema_version": 1, "kind": ARTIFACT_KIND, "built_at": BUILT,
                "reconstructions": [], "state_objects": {}, "object_metadata": {}}
    return artifact, {"observations": [obs]}, states


def _published(tmp_path, *, missing_successor=False):
    artifact, source, raw = _inputs()
    add_reviewed_editions(artifact, source, raw)
    if missing_successor:
        artifact["reconstructions"].pop()
    head = next(iter(raw.values()))
    head_hash, head_meta = store_state_object(tmp_path / "federal_states", head)
    for key, state in artifact["state_objects"].items():
        actual, meta = store_state_object(tmp_path / "federal_states", state)
        assert actual == key
        artifact["object_metadata"][key].update(meta)
    internal = materialize_history(source["observations"], raw, [], include_norm_intervals=False)
    public = build_public_manifest(internal, {**raw, **artifact["state_objects"]}, {head_hash: head_meta}, [],
                                   built_at=BUILT, verified_reconstructions=artifact)
    (tmp_path / "acts").mkdir()
    detail = {**head, "slug": "gg", "versions": [], "source_url": "https://www.gesetze-im-internet.de/gg/"}
    (tmp_path / "acts/fed_gg.json").write_text(json.dumps(detail))
    (tmp_path / "wiki.json").write_text(json.dumps([detail]))
    (tmp_path / "summary.json").write_text(json.dumps({"built_at": BUILT}))
    (tmp_path / "retrospective_history.json").write_text(json.dumps(public))
    return public


def test_basic_law_dates_and_addresses_through_the_reader(tmp_path, monkeypatch):
    public = _published(tmp_path)
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "_CACHE", {})
    monkeypatch.setattr(main, "_NORM_STORES", {})
    monkeypatch.setattr(main, "_RETROSPECTIVE_MANIFEST", None)
    monkeypatch.setattr(main, "_RETROSPECTIVE_SOURCE", None)
    assert len(public["acts"]["fed_gg"]["intervals"]) == 9
    with TestClient(main.app) as client:
        def reading(day, article):
            return client.get("/acts/fed_gg/markdown", params={"at": day, "norm": article})
        gap = reading("2017-07-19", "Art. 93")
        assert gap.headers["x-kerf-complete"] == "false"
        assert client.get("/acts/fed_gg/markdown", params={
            "at": "2017-07-19", "norm": "Art. 93", "as_of": BUILT}).status_code == 404
        for day, court, spending, status in [
            ("2022-12-24", False, False, "derived_verified"),
            ("2024-12-27", False, False, "derived_verified"),
            ("2024-12-28", True, False, "derived_verified"),
            ("2025-03-24", True, False, "derived_verified"),
            ("2025-03-25", True, True, "official_exact"),
        ]:
            for article, clause, introduced in [
                ("Art. 93", "allen übrigen Verfassungsorganen", court),
                ("Art. 94", "Die Entscheidungen des Bundesverfassungsgerichts binden", court),
                ("Art. 109", "Verteidigungsausgaben", spending),
                ("Art. 115", "Verteidigungsausgaben", spending),
                ("Präambel", "Im Bewußtsein seiner Verantwortung", True),
            ]:
                response = reading(day, article)
                assert response.status_code == 200, (day, article, response.text[:250])
                assert response.headers["x-kerf-text-status"] == status
                assert response.headers["x-kerf-complete"] == "true"
                assert (clause in response.text) == introduced
            for slug, check, introduced in [
                ("art/93", "allen übrigen Verfassungsorganen", court),
                ("art/94", "Die Entscheidungen des Bundesverfassungsgerichts binden", court),
                ("art/109", "Verteidigungsausgaben", spending),
                ("art/115", "Verteidigungsausgaben", spending),
                ("text/praambel", "Im Bewußtsein seiner Verantwortung", True),
            ]:
                response = client.get(f"/norms/de/bund/gg/{slug}/at", params={"date": day})
                data = response.json()
                assert response.status_code == 200 and data["status"] == "ok"
                item = data["evidence"][0]
                assert (check in item["text"]) == introduced
                if slug == "text/praambel":
                    assert item["source"]["url"] == "https://www.gesetze-im-internet.de/gg/pr_ambel.html"
            by_id = client.get("/norms/de/bund/gg/art/143h/at", params={"date": day})
            assert by_id.status_code == (200 if spending else 404)
            if spending:
                assert "500 Milliarden Euro" in by_id.json()["evidence"][0]["text"]
            inserted = reading(day, "Art. 143h")
            assert inserted.status_code == (200 if spending else 404)
            if spending:
                assert "500 Milliarden Euro" in inserted.text

        for day, wording in [
                ("2020-10-07", None), ("2020-10-08", "Mindereinnahmen aus der Gewerbesteuer"),
                ("2020-12-31", "Mindereinnahmen aus der Gewerbesteuer"), ("2021-01-01", None),
                ("2025-03-24", None), ("2025-03-25", "500 Milliarden Euro")]:
            md = reading(day, "Art. 143h")
            evidence = client.get("/norms/de/bund/gg/art/143h/at", params={"date": day})
            if wording is None:
                assert md.status_code == evidence.status_code == 404
            else:
                assert md.status_code == evidence.status_code == 200
                assert wording in md.text and wording in evidence.json()["evidence"][0]["text"]
        for label, change, previous, boundary in [
                ("Art. 104b", "zusätzlich zu eigenen Mitteln", "2019-04-03", "2019-04-04"),
                ("Art. 105", "über die Grundsteuer", "2019-11-20", "2019-11-21"),
                ("Art. 104a", "drei Viertel der Ausgaben", "2020-10-07", "2020-10-08"),
                ("Art. 87a", "100 Milliarden Euro", "2022-06-30", "2022-07-01"),
                ("Art. 82", "elektronischer Form", "2022-12-23", "2022-12-24")]:
            old, new = reading(previous, label), reading(boundary, label)
            assert old.status_code == new.status_code == 200
            assert change not in old.text and change in new.text

        for label, expected in [("Art. 93", "de/bund/gg/art/93"),
                                ("Art. 143h", "de/bund/gg/art/143h"),
                                ("Präambel", "de/bund/gg/text/praambel")]:
            response = client.get("/norms/resolve", params={"act": "fed_gg", "norm": label})
            assert response.status_code == 200
            assert expected in response.text


def test_older_generation_skips_new_edition_review():
    artifact, manifest, states = _inputs("2026-11-25")
    add_reviewed_editions(artifact, manifest, states)
    assert artifact["reconstructions"] == []


def test_changed_witness_does_not_append_partial_chain(tmp_path):
    artifact, manifest, states = _inputs()
    review = json.loads(GG_REVIEW.read_bytes())
    review["editions"][1]["changes_from_previous"][0]["previous_text"] += "x"
    path = tmp_path / "bad-review.json"
    path.write_text(json.dumps(review))
    before = copy.deepcopy(artifact)
    with pytest.raises(ReconstructionError, match="state hash"):
        add_reviewed_editions(artifact, manifest, states, review_path=path)
    assert artifact == before


def test_missing_consecutive_edition_cannot_publish_an_official_anchor(tmp_path):
    with pytest.raises(RetrospectiveHistoryError, match="lacks its reviewed consecutive edition"):
        _published(tmp_path, missing_successor=True)


def test_later_observations_do_not_bridge_an_intervening_change():
    artifact, manifest, states = _inputs()
    source = manifest["observations"][0]
    manifest["observations"].extend([
        {**source, "observed_at": "2026-11-27"},
        {**source, "observed_at": "2026-11-28", "state_sha256": "a" * 64},
        {**source, "observed_at": "2026-11-29"},
    ])
    add_reviewed_editions(artifact, manifest, states)
    earlier, *_, latest = artifact["reconstructions"]
    assert earlier["observed_at"] == latest["observed_at"] == "2026-11-26"
    assert latest["anchor_verified_through"] == "2026-11-27"


def test_indexed_norm_reads_the_complete_edition_for_the_selected_date(tmp_path, monkeypatch):
    from tools import build_law_history
    _published(tmp_path)
    _, manifest, _ = _inputs()
    path = tmp_path / "acts/fed_gg.json"
    act = json.loads(path.read_bytes())
    act["official_states"] = manifest["observations"]
    path.write_text(json.dumps(act))
    assert build_law_history.main(["--data-dir", str(tmp_path), "--documents", str(tmp_path/"none")]) == 0
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "_CACHE", {})
    monkeypatch.setattr(main, "_NORM_STORES", {})
    monkeypatch.setattr(main, "_RETROSPECTIVE_MANIFEST", None)
    monkeypatch.setattr(main, "_RETROSPECTIVE_SOURCE", None)
    with TestClient(main.app) as client:
        path = "/norms/de/bund/gg/art/143h"
        for day in ("2020-10-07", "2021-01-01", "2025-03-24"):
            missing = client.get(path+"/at", params={"date": day})
            assert missing.status_code == 404 and "complete edition" in missing.text
        for day, marker in [("2020-12-31", "Gewerbesteuer"), ("2025-03-25", "Infrastruktur")]:
            present = client.get(path+"/at", params={"date": day})
            assert marker in present.json()["evidence"][0]["text"]
        change = client.get(path+"/diff", params={"from":"2020-12-31","to":"2025-03-25"})
        assert change.status_code == 200 and "Gewerbesteuer" in change.text and "Infrastruktur" in change.text
        assert client.get(path+"/blame", params={"date":"2021-01-01"}).status_code == 404
        assert client.get(path+"/log").status_code == 200
