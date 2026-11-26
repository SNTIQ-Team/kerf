"""Multi-source law history: identity, Stand notes, builder, API contract.

The builder runs over the real AsylG fixture (three complete GII states and
the retrospective intervals/events of tests/fixtures/law_history) plus a
synthetic *archived capture* derived from the oldest real state, so the
merge, de-duplication, attribution and change_summary rules are exercised
end to end through the same router the API serves.
"""
import gzip
import json
import shutil
import sqlite3
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.history_identity import norm_identity
from api.law_history import LawHistoryStore, change_state, summarize
from api.law_routes import create_router
from api.retrospective_store import validate_manifest
from api.stand_notes import amending_reference, parse_notes
from tools import build_law_history, history_bundle

FIXTURE = Path(__file__).parent / "fixtures" / "law_history"
P29A = "de/bund/asylvfg_1992/para/29a"
P47 = "de/bund/asylvfg_1992/para/47"


# ------------------------------------------------------------ identity

def test_identity_ignores_whitespace_but_not_wording():
    xml = "(1) Leistungen erhalten Personen, die 1.das 15. Lebensjahr"
    html = "(1) Leistungen erhalten Personen, die\n 1. das 15.\xa0Lebensjahr"
    assert norm_identity("§ 7", "Leistungsberechtigte", xml) == \
        norm_identity("§\xa07", " Leistungsberechtigte ", html)
    assert norm_identity("§ 7", "Leistungsberechtigte", xml) != \
        norm_identity("§ 7", "Leistungsberechtigte", xml + ".")
    assert norm_identity("§ 7", "", "Wort") == \
        norm_identity("§ 7", "", "Wo­rt")


# ---------------------------------------------------------- stand notes

SGB2_STAND = (
    "Neuf: Neugefasst durch Bek. v. 13.5.2011 I 850, 2094; | Stand: zuletzt "
    "geändert durch Art. 8 G v. 22.12.2025 I Nr. 363 | Hinweis: Änderung "
    "durch Art. 1 G v. 16.4.2026 I Nr. 107 textlich nachgewiesen, "
    "dokumentarisch noch nicht abschließend bearbeitet | Hinweis: Mittelbare "
    "Änderung durch Art. 1b G v. 16.4.2026 I Nr. 107 ist berücksichtigt")


def test_stand_note_types_textlich_nachgewiesen():
    notes = parse_notes(SGB2_STAND)
    act = notes["act_stand_note"]
    assert act["scope"] == "act" and act["norm_specific"] is False
    assert act["last_amended_by_text"] == "Art. 8 G v. 22.12.2025 I Nr. 363"
    assert act["amending_act"]["gazette_lookup"]["document_id"] == \
        "bgbl-1-2025-363"
    kinds = {n["kind"]: n for n in notes["documentation_notes"]}
    pending = kinds["textually_incorporated_documentation_pending"]
    assert pending["text_incorporated"] is True
    assert pending["documentation_complete"] is False
    assert pending["amending_act"]["citation_text"] == \
        "Art. 1 G v. 16.4.2026 I Nr. 107"
    assert pending["amending_act"]["article"] == "1"
    indirect = kinds["indirect_change_incorporated"]
    assert indirect["indirect"] is True and indirect["text_incorporated"]
    assert len(notes["documentation_notes"]) == 2


def test_stand_note_not_yet_incorporated_keeps_mwv_as_secondary():
    notes = parse_notes(
        "Stand: Zuletzt geändert durch Art. 3 G v. 22.12.2025 I Nr. 364; "
        "2026 I Nr. 49 | Hinweis: Änderung durch Art. 11 G v 22.7.2026 I "
        "Nr. 222 mWv 29.7.2026 bzw. mWv 1.5.2030 bzw. mit zukünftiger "
        "Wirkung noch nicht berücksichtigt")
    assert notes["act_stand_note"]["amending_act"]["gazette_lookup"][
        "document_id"] == "bgbl-1-2025-364"
    note = notes["documentation_notes"][0]
    assert note["kind"] == "not_yet_incorporated"
    assert note["text_incorporated"] is False
    assert note["announced_effective_dates"] == ["2026-07-29", "2030-05-01"]
    assert "secondary" in note["announced_effective_dates_basis"]
    assert note["announced_future_effect_undated"] is True


def test_amending_reference_legacy_page_citation():
    ref = amending_reference("Art. 1 G v. 22.12.2016 I 3155")
    assert ref["gazette_lookup"]["document_id"] == "bgbl-1-2016-s3155"
    assert ref["gazette_lookup"]["date_in_gii_wording"] == "2016-12-22"


# -------------------------------------------------------------- builder

def _capture(state_path: Path, *, edit=None) -> dict:
    """A parsed archived capture built from a real fixture state."""
    state = json.loads(gzip.decompress(state_path.read_bytes()))
    norms = [{"enbez": n["enbez"], "titel": n.get("titel") or "",
              "text": n.get("text") or ""} for n in state["norms"]]
    if edit:
        edit(norms)
    return {"schema": "kerf-history-parsed/1", "parser": "test/1",
            "source": "wayback_gii", "act_id": "fed_asylvfg_1992",
            "sha256": "a" * 64, "captured_at": "2023-06-01T10:00:00Z",
            "original_url": "https://www.gesetze-im-internet.de/asylvfg_1992/"
                            "BJNR111260992.html",
            "archive_url": "https://web.archive.org/web/20230601100000id_/"
                           "https://www.gesetze-im-internet.de/asylvfg_1992/"
                           "BJNR111260992.html",
            "stand": {"raw": "Stand: zuletzt geändert durch Art. 1 G v. "
                             "21.12.2022 I 2817",
                      "entries": [{"type": "Stand", "text": "zuletzt geändert "
                                   "durch Art. 1 G v. 21.12.2022 I 2817"}]},
            "complete": True, "norms": norms}


@pytest.fixture
def built(tmp_path):
    data = tmp_path / "web-data"
    shutil.copytree(FIXTURE, data)
    oldest = next(p for p in (data / "federal_states").rglob("898ec*.json.gz"))

    def older_29a(norms):
        # before BGBl. 2023 I Nr. 382 (in force 2023-12-23) § 29a read
        # differently; the capture shows HTML whitespace (1. das …).
        for norm in norms:
            if norm["enbez"] == "§ 29a":
                norm["text"] = norm["text"].replace(
                    "(1)", "(1)\n ", 1) + " Alte Fassung."
            if norm["enbez"] == "§ 1":
                norm["text"] = norm["text"].replace(" ", "\n", 3)
    archive = tmp_path / "archive"
    target = archive / "parsed" / "wayback_gii" / "fed_asylvfg_1992"
    target.mkdir(parents=True)
    (target / ("a" * 64 + ".json.gz")).write_bytes(gzip.compress(
        json.dumps(_capture(oldest, edit=older_29a)).encode()))
    bundle = tmp_path / "bundle"
    manifest = history_bundle.export(archive, bundle)
    assert manifest["captures"] == 1
    assert history_bundle.verify(bundle)["files"]["corpus.sqlite"]["sha256"]
    build_law_history.main([
        "--data-dir", str(data), "--bundle", str(bundle),
        "--documents", str(tmp_path / "none"), "--as-of", "2026-09-28"])
    return data


def _client(data: Path) -> TestClient:
    loaded = validate_manifest(json.loads(
        (data / "retrospective_history.json").read_text()))
    app = FastAPI()
    app.include_router(create_router(lambda: data, lambda: loaded,
                                     lambda: "web-data.release-fixture"))
    return TestClient(app)


def test_capture_merges_by_identity_and_keeps_roles(built):
    store = LawHistoryStore(built / "law_history.sqlite")
    info = store.acts()["fed_asylvfg_1992"]
    assert info["states_by_source"] == {"gii_daily": 3, "wayback_gii": 1}
    # § 1 only differs in whitespace: one version, attested by two sources
    [v1] = store.versions("de/bund/asylvfg_1992/para/1")
    sources = {a["source"] for a in v1.record["attestations"]}
    assert sources == {"GII", "Wayback/GII"}
    assert v1.known_from == "2023-06-01"
    assert v1.record["verification"]["cross_source"] == "agree"
    # the canonical wording is the GII XML one, not the capture's
    assert "\n" not in v1.text
    versions = store.versions(P29A)
    assert [v.source_role for v in versions] == [
        "archived_capture", "official_consolidation",
        "official_consolidation"]


def test_attribution_sets_legal_start_from_commencement(built):
    store = LawHistoryStore(built / "law_history.sqlite")
    first, second, third = store.versions(P29A)
    temporal = second.record["temporal"]
    # 2023-06-01 capture → 2026-07-06 GII: BGBl. 2023 I Nr. 382 (in force
    # 2023-12-23) and 2025 I Nr. 364 (2026-02-01) both fall in the window
    assert temporal["attribution"]["status"] == \
        "multiple_amendments_between_captures"
    assert temporal["valid_from"] == "2026-02-01"
    assert first.record["temporal"]["valid_until"] == "2026-02-01"
    kinds = {f["kind"] for f in store.findings(norm_id=P29A)}
    assert "intermediate_version_not_captured" in kinds
    # the verified GII interval keeps its own start
    assert third.record["temporal"]["valid_from"] == "2026-07-10"
    assert third.record["temporal"]["valid_from_status"] == \
        "official_verified"


def test_log_at_diff_blame_through_law_history(built):
    client = _client(built)
    log = client.get(f"/norms/{P29A}/log").json()
    assert log["status"] == "ok" and log["total"] == 3
    newest = log["evidence"][0]
    assert newest["temporal"]["valid_from"] == "2026-07-10"
    assert newest["source_role"] == "official_consolidation"
    oldest = log["evidence"][-1]
    assert oldest["source_role"] == "archived_capture"
    assert oldest["source"]["archive_url"].startswith(
        "https://web.archive.org/web/20230601100000id_/")
    assert oldest["temporal"]["known_at"] == "2023-06-01"
    assert oldest["temporal"]["valid_from"] is None
    assert log["searched"][0]["source"] == "law_history"
    assert log["change_summary"]["schema"] == "kerf-change-summary/1"
    assert log["history"]["versions_by_role"] == {
        "archived_capture": 1, "official_consolidation": 2}
    # the verified interval of a later state still answers an earlier date
    at = client.get(f"/norms/{P47}/at?date=2026-09-28").json()
    assert at["status"] == "ok"
    assert at["resolution"]["resolution"] == "verified_interval"
    # exact version by id
    version = client.get(
        f"/norms/{P29A}/at?version={quote(oldest['version_id'])}").json()
    assert version["status"] == "ok"
    assert version["resolution"]["resolution"] == "exact_version"
    assert version["evidence"][0]["text"].endswith("Alte Fassung.")
    both = client.get(f"/norms/{P29A}/at?version=x&date=2026-01-01")
    assert both.status_code == 422
    diff = client.get(f"/norms/{P29A}/diff?from="
                      f"{quote(oldest['version_id'])}&to=head").json()
    assert diff["evidence"][0]["source_role"] == "archived_capture"
    assert diff["total"] >= 1
    blame = client.get(f"/norms/{P29A}/blame").json()
    assert blame["status"] == "ok" and blame["lines"]


def test_log_reports_latest_state_presence_from_law_history(built):
    # law_history chains carry no per-state norm rows; the presence flag must
    # come from the builder's norms.present_in_latest, not default to false.
    store = LawHistoryStore(built / "law_history.sqlite")
    client = _client(built)
    assert store.norm(P29A)["present_in_latest"] == 1
    log = client.get(f"/norms/{P29A}/log").json()
    assert log["norm"]["present_in_latest_state"] is True
    with sqlite3.connect(built / "law_history.sqlite") as db:
        db.execute("UPDATE norms SET present_in_latest=0 WHERE norm_id=?",
                   (P29A,))
    log = _client(built).get(f"/norms/{P29A}/log").json()
    assert log["norm"]["present_in_latest_state"] is False


def test_change_records_and_summary_contract(built):
    client = _client(built)
    body = client.get(f"/norms/{P29A}/changes").json()
    assert body["status"] == "ok"
    documents = [row["amending_act"]["document_id"] for row in
                 body["evidence"]]
    # the fixture's current knowledge holds two events for § 29a
    assert documents == ["bgbl-1-2025-364", "bgbl-1-2023-382"]  # newest 1st
    row = body["evidence"][0]
    assert row["dates"]["in_force"]["date"] == "2026-02-01"
    assert row["dates"]["in_force"]["status"] == \
        "official_commencement_clause"
    # no promulgation document metadata in the fixture: adopted stays unknown
    assert row["dates"]["adopted"]["date"] is None
    assert row["dates"]["adopted"]["status"] == "unknown"
    assert row["state"] == "in_force" and row["pending"] is False
    summary = body["change_summary"]
    assert summary["scope"] == "norm" and summary["subject"] == P29A
    assert summary["last_change"]["amending_act"]["document_id"] == \
        "bgbl-1-2025-364"
    assert summary["last_change"]["text_effect"]["after_version"] == 1
    assert {"versions", "events", "events_with_in_force",
            "events_date_unknown"} <= set(summary["counts"])
    assert any(u["field"] == "adopted" for u in summary["unknown_dates"])
    page = client.get(f"/norms/{P29A}/changes?limit=1&offset=1").json()
    assert len(page["evidence"]) == 1 and page["offset"] == 1
    assert page["total"] == body["total"]
    none = client.get(f"/norms/{P29A}/changes?limit=0").json()
    assert none["evidence"] == [] and none["change_summary"]
    bad = client.get(f"/norms/{P29A}/changes?state=someday")
    assert bad.status_code == 422
    unknown = client.get(f"/norms/{P29A}/changes?when=now")
    assert unknown.status_code == 422


def test_act_changes_group_by_amending_article(built):
    client = _client(built)
    body = client.get("/acts/fed_asylvfg_1992/changes").json()
    assert body["status"] == "ok"
    group = next(g for g in body["evidence"] if
                 g["amending_act"]["document_id"] == "bgbl-1-2025-364")
    labels = {n["norm_label"] for n in group["norms"]}
    assert {"§ 29a", "§ 47"} <= labels
    assert body["change_summary"]["scope"] == "act"
    assert body["change_summary"]["counts"]["act_states"] == 4
    missing = client.get("/acts/fed_nope/changes")
    assert missing.json()["status"] == "not_in_corpus"
    filtered = client.get("/acts/fed_asylvfg_1992/changes?norm=§ 29a"
                          ).json()
    assert all(n["norm_label"] == "§ 29a" for g in filtered["evidence"]
               for n in g["norms"])


def test_summary_rules_never_guess_dates():
    changes = [
        {"change_id": "a", "dates": {"in_force": {"date": "2024-01-01",
                                                  "status": "official"},
                                     "promulgated": {"date": "2023-12-01"}}},
        {"change_id": "b", "dates": {"in_force": {"date": None,
                                                  "status": "unknown"},
                                     "promulgated": {"date": "2025-03-01"}},
         "text_effect": {"observed_from": "2025-04-01"}},
        {"change_id": "c", "dates": {"in_force": {"date": "2027-01-01",
                                                  "status": "official"},
                                     "promulgated": {"date": "2026-05-01"}}},
    ]
    summary = summarize(changes, scope="norm", subject="x",
                        as_of="2026-09-28")
    assert summary["last_change"]["change_id"] == "a"
    assert summary["last_incorporated_undated"]["change_id"] == "b"
    assert summary["next_pending_change"]["change_id"] == "c"
    assert change_state(changes[1], "2026-09-28") == \
        "incorporated_date_unknown"
    assert {u["change_id"] for u in summary["unknown_dates"]
            if u["field"] == "in_force"} == {"b"}


def test_builder_without_bundle_and_bavarian_chain(tmp_path):
    data = tmp_path / "web-data"
    shutil.copytree(FIXTURE, data)
    build_law_history.main(["--data-dir", str(data), "--documents",
                            str(tmp_path / "none")])
    db = sqlite3.connect(data / "law_history.sqlite")
    acts = {row[0] for row in db.execute("SELECT act_id FROM acts")}
    assert {"fed_asylvfg_1992", "fed_sgb_14", "by_agsg"} <= acts
    store = LawHistoryStore(data / "law_history.sqlite")
    assert store.acts()["by_agsg"]["event_history_status"] == "not_built"
    assert store.versions("de/by/agsg/art/80")


def test_law_git_commits_are_dated_by_in_force(built, tmp_path):
    import subprocess
    from tools import law_git_export
    repo = tmp_path / "law-git"
    assert law_git_export.main(["--data", str(built), "--repo", str(repo),
                                "--acts", "fed_asylvfg_1992"]) == 0
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--reverse", "--date=short",
         "--format=%ad%x09%s%x09%(trailers:key=Kerf-Author-Date-Basis,"
         "valueonly,separator=)"], capture_output=True, text=True,
        check=True).stdout.strip().splitlines()
    first = log[0].split("\t")
    assert first[0] == "2023-06-01" and first[2] == "observed_at"
    assert "legal start unresolved" in first[1]
    legal = [line.split("\t") for line in log if "\tin_force" in line]
    assert any(row[0] == "2026-02-01" and "BGBl. 2025 I Nr. 364" in row[1]
               for row in legal)
    body = subprocess.run(
        ["git", "-C", str(repo), "log", "--format=%B", "--grep",
         "BGBl. 2025 I Nr. 364"], capture_output=True, text=True,
        check=True).stdout
    assert "In force: 2026-02-01" in body
    assert "Adopted (Ausfertigung): unknown" in body
    text = (repo / "de/bund/asylvfg_1992/para/29a.md").read_text()
    assert "source_role:" in text and "in_force:" in text


def test_gii_notes_link_to_the_norms_change():
    change = {"change_id": "c1", "norm_label": "§ 7b",
              "amending_act": {"document_id": "bgbl-1-2026-107",
                               "article": "1", "citation": "Art. 1 …"},
              "dates": {"in_force": {"date": "2026-07-01",
                                     "status": "official"},
                        "promulgated": {"date": "2026-04-22"}}}
    notes = parse_notes(SGB2_STAND)
    summary = summarize([change], scope="norm", subject="n", as_of=
                        "2026-09-28", stand_notes=notes,
                        notes_observed_at="2026-09-28")
    linked = {n["kind"]: n for n in summary["documentation_notes"]}
    pending = linked["textually_incorporated_documentation_pending"]
    assert pending["applies_to_subject"] is True
    assert pending["change_id"] == "c1"
    assert linked["indirect_change_incorporated"]["applies_to_subject"] is \
        False
    doc = summary["last_change"]["gii_documentation"]
    assert doc == {"kind": "textually_incorporated_documentation_pending",
                   "text_incorporated": True,
                   "documentation_complete": False,
                   "text": pending["text"]}


def test_act_versions_list_consolidations_with_sources(built):
    client = _client(built)
    body = client.get("/acts/fed_asylvfg_1992/versions").json()
    assert body["status"] == "ok" and body["total"] == 4
    oldest = body["evidence"][-1]
    assert oldest["source"] == "wayback_gii"
    assert oldest["source_role"] == "archived_capture"
    assert oldest["known_from"] == "2023-06-01"
    assert oldest["act_stand_note"]["last_amended_by_text"] == \
        "Art. 1 G v. 21.12.2022 I 2817"
    only = client.get("/acts/fed_asylvfg_1992/versions?source=gii_daily"
                      ).json()
    assert {row["source"] for row in only["evidence"]} == {"gii_daily"}


def test_an_old_version_with_unknown_end_never_answers_today():
    from api.norm_history import NormVersion, StateVersion, select_version

    def version(key, start, observed):
        state = StateVersion(act_id="a", key=key, valid_from=start,
                             valid_from_status="official_commencement_"
                             "clause" if start else "unresolved",
                             first_observed_at=observed,
                             last_observed_at=observed)
        return NormVersion(norm_id="n", label="§ 1", title="", text=key,
                           content_sha256=key, first_state=state,
                           last_state=state, states=[state])
    old = version("old", "2006-07-01", "2006-09-05")
    middle = version("middle", None, "2010-01-01")
    current = version("current", "2024-06-01", "2024-06-01")
    old.first_state.superseded_by_observation = "2010-01-01"
    middle.first_state.superseded_by_observation = "2024-06-01"

    class Chain:
        states = [old.first_state, middle.first_state, current.first_state]
    status, picked, _res = select_version([old, middle, current],
                                          "2026-09-28", Chain())
    assert picked is current
    _status, picked, _res = select_version([old, middle, current],
                                           "2007-01-01", Chain())
    assert picked is old


def test_bundle_captures_json_stays_valid_and_old_cut_bundles_still_build():
    captures = [{"kind": "norm_html", "url": "https://example.test/" + "x" * 400,
                 "timestamp": f"2010{i:010d}"} for i in range(120)]
    text = history_bundle._captures_json(captures)
    assert len(text) <= 20000
    kept = json.loads(text)
    assert kept and kept == captures[:len(kept)]
    # A bundle exported before the fix cut the JSON string mid-value.
    cut = json.dumps(captures)[:20000]
    with pytest.raises(ValueError):
        json.loads(cut)
    assert build_law_history._capture_kinds(cut) == {"norm_html"}
    assert build_law_history._capture_kinds(None) == set()
    assert build_law_history._capture_kinds('[{"kind": "xml_zip"}]') == {
        "xml_zip"}
