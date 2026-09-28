"""Norm log/at/diff/blame over real captured Lexgraph states.

Fixture (tests/fixtures/law_history): the three complete official GII states
of the Asylgesetz (AsylVfG 1992) observed 2026-07-06, 2026-07-13 and
2026-07-29, byte-identical to the published content-addressed store, plus
the retrospective intervals that verify two legal starts against the final
BGBl (2026 I Nr. 199 → 2026-07-10; 2026 I Nr. 221 → 2026-10-01).
"""
import gzip
import json
import shutil
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.law_routes import create_router
from api.retrospective_store import validate_manifest

FIXTURE = Path(__file__).parent / "fixtures" / "law_history"
P29A = "de/bund/asylvfg_1992/para/29a"


def _client(data: Path, manifest=True, toc=True):
    loaded = (validate_manifest(json.loads(
        (data / "retrospective_history.json").read_text()))
        if manifest else None)
    app = FastAPI()
    app.include_router(create_router(lambda: data, lambda: loaded,
                                     lambda: "web-data.release-fixture"))
    if not toc:
        (data / "gii_toc.json").unlink()
    return TestClient(app)


@pytest.fixture
def data(tmp_path):
    target = tmp_path / "web-data"
    shutil.copytree(FIXTURE, target)
    return target


def test_log_lists_versions_with_amendment_and_commencement(data):
    body = _client(data).get(f"/norms/{P29A}/log").json()
    assert body["status"] == "ok" and body["total"] == 2
    newest, oldest = body["evidence"]
    assert newest["version_id"].startswith(f"{P29A}@2026-07-10#")
    assert newest["temporal"]["valid_from"] == "2026-07-10"
    assert newest["temporal"]["valid_from_status"] == "official_verified"
    assert newest["amendment"]["reference"] == "BGBl. 2026 I Nr. 199"
    assert newest["amendment"]["commencement_basis"] == \
        "official_bgbl_command_and_commencement_clause"
    assert newest["source"]["role"] == "official_consolidation_gii"
    # A retrieval date is never promoted to a legal start.
    assert oldest["temporal"]["valid_from"] is None
    assert oldest["temporal"]["valid_from_status"] == "unresolved"
    assert oldest["temporal"]["observed_from"] == "2026-07-06"
    assert oldest["temporal"]["valid_until_exclusive"] == "2026-07-10"
    assert body["searched"][0] == {"source": "federal_states",
                                   "status": "ok", "count": 3}


def test_at_prefers_legal_validity_over_the_newest_gii_text(data):
    client = _client(data)
    # GII already serves the version effective 2026-10-01 (state f74d8…),
    # but on 2026-09-28 the verified interval of the 2026-07-10 state applies.
    body = client.get(f"/norms/de/bund/asylvfg_1992/para/47/log").json()
    assert body["evidence"][0]["temporal"]["valid_from"] == "2026-10-01"
    at = client.get("/norms/de/bund/asylvfg_1992/para/47/at?date=2026-09-28"
                    ).json()
    assert at["status"] == "ok"
    assert at["resolution"]["resolution"] == "verified_interval"
    assert at["evidence"][0]["temporal"]["valid_until_exclusive"] == \
        "2026-10-01"
    later = client.get("/norms/de/bund/asylvfg_1992/para/47/at?date="
                       "2026-10-02").json()
    assert later["evidence"][0]["temporal"]["valid_from"] == "2026-10-01"


def test_at_is_typed_when_validity_is_not_verified(data):
    client = _client(data)
    unresolved = client.get(f"/norms/{P29A}/at?date=2026-07-08")
    assert unresolved.status_code == 200
    body = unresolved.json()
    assert body["status"] == "temporally_unresolved" and body["total"] is None
    assert body["evidence"][0]["temporal"]["valid_from"] is None
    before = client.get(f"/norms/{P29A}/at?date=2020-01-01")
    assert before.status_code == 404
    assert before.json()["status"] == "not_in_corpus"
    assert before.json()["evidence"] == []


def test_structure_travels_with_the_text(data):
    body = _client(data).get(
        "/norms/de/bund/sgb_14/para/37/at?date=2026-07-06").json()
    structure = body["evidence"][0]["structure"]
    assert structure["counts"]["absatz"] == 2
    assert structure["counts"]["nummer"] == 6
    item = _client(data).get(
        "/norms/de/bund/sgb_14/para/37/abs/2/nr/3/at?date=2026-07-06").json()
    assert item["evidence"][0]["sub_unit_text"] == (
        "die Anforderungen an die personelle Ausstattung und an die "
        "Qualifikation des Personals,")
    missing = _client(data).get(
        "/norms/de/bund/sgb_14/para/37/abs/3/at?date=2026-07-06")
    assert missing.json()["status"] == "not_found_in_searched_scope"


def test_sentence_aware_diff_with_structural_locators(data):
    body = _client(data).get(
        f"/norms/{P29A}/diff?from=2026-07-06&to=head").json()
    # The from-side has no verified legal start: the diff is labelled so.
    assert body["status"] == "temporally_unresolved"
    changed = [op for op in body["changes"] if op["op"] != "equal"]
    assert len(changed) == 1 and changed[0]["op"] == "replace"
    assert changed[0]["old"]["label"] == changed[0]["new"]["label"] == \
        "Abs. 2a Satz 1"
    words = changed[0]["words"]
    assert {"op": "delete", "text": "23. Oktober 2017"} in words
    assert {"op": "insert", "text": "12. Juni 2027,"} in words
    assert body["total"] == 1


def test_blame_attributes_sentences_to_the_introducing_amendment(data):
    body = _client(data).get(f"/norms/{P29A}/blame").json()
    lines = {row["label"]: row for row in body["lines"]}
    introduced = lines["Abs. 2a Satz 1"]
    assert introduced["status"] == "introduced_within_history"
    assert introduced["introduced_by"]["valid_from"] == "2026-07-10"
    assert introduced["introduced_by"]["amendment"]["document_id"] == \
        "bgbl-1-2026-199"
    older = lines["Abs. 1 Satz 1"]
    assert older["status"] == "predates_text_history"
    assert older["introduced_by"] is None
    assert body["summary"]["predates_text_history"] >= 1
    assert any("predate" in warning for warning in body["warnings"])


def test_strict_arguments_and_typed_failures(data):
    client = _client(data)
    ignored = client.get(f"/norms/{P29A}/at?date=2026-09-28&valid_at=x")
    assert ignored.status_code == 422
    assert ignored.json()["status"] == "unsupported_temporal_request"
    bad = client.get(f"/norms/{P29A}/at?date=28.09.2026")
    assert bad.status_code == 422 and bad.json()["status"] == \
        "invalid_argument"
    assert client.get(f"/norms/{P29A}/diff?from=2026-07-06").json()[
        "status"] == "invalid_argument"
    eu = client.get("/norms/eu/32016R0679/art/5/log")
    assert eu.status_code == 404 and eu.json()["status"] == "not_in_corpus"
    unknown = client.get("/norms/de/bund/nonexistent_act/para/1/log")
    assert unknown.json()["status"] == "not_in_corpus"


def test_integrity_failure_is_never_an_empty_history(data):
    digest = "84157c9fa877823847052ca2abca76fe8ba0b3390b51ac56a487964874605917"
    path = (data / "federal_states" / "objects" / "sha256" / digest[:2]
            / f"{digest}.json.gz")
    state = json.loads(gzip.open(path).read())
    state["norms"][0]["text"] += " manipulated"
    path.write_bytes(gzip.compress(json.dumps(state).encode()))
    response = _client(data).get(f"/norms/{P29A}/log")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "integrity_check_failed"
    assert body["total"] is None and body["evidence"] == []


def test_missing_retrospective_archive_is_partial_not_zero(data):
    body = _client(data, manifest=False).get(f"/norms/{P29A}/log").json()
    assert body["status"] == "partial" and body["total"] is None
    component = body["searched"][1]
    assert component == {"source": "retrospective_history",
                         "status": "unavailable", "count": None}
    # Without verified intervals no legal start is claimed.
    assert all(item["temporal"]["valid_from"] is None
               for item in body["evidence"])


def test_source_urls_come_from_the_captured_toc_only(data):
    client = _client(data)
    gg = client.get("/norms/resolve?q=Art. 1 GG").json()
    assert gg["evidence"][0]["canonical_id"] == "de/bund/gg/art/1"
    item = client.get(
        "/norms/de/bund/sgb_14/para/37/log").json()["evidence"][0]
    assert item["source"]["norm_url"] == \
        "https://www.gesetze-im-internet.de/sgb_14/__37.html"
    no_toc = _client(data, toc=False).get(
        "/norms/de/bund/sgb_14/para/37/log").json()["evidence"][0]
    # The bundled TOC still resolves; a norm absent from every TOC never
    # gets a URL built from its number.
    assert no_toc["source"]["norm_url_status"] in {"resolved"}


def test_bavarian_history_from_archived_official_pages(data):
    body = _client(data).get("/norms/de/by/agsg/art/80/log").json()
    assert body["status"] == "ok" and body["total"] == 2
    newest, oldest = body["evidence"]
    assert newest["temporal"]["valid_from"] == "2022-01-01"
    assert oldest["temporal"]["valid_until_exclusive"] == "2022-01-01"
    assert newest["source"]["role"] == "archived_official_page_bayern_recht"
    assert newest["amendment"]["reference"] == \
        "§ 2 G v. 23.12.2021 (GVBl. S. 671)"
    assert newest["source"]["norm_url"] is None  # never built from a number
    assert any("incomplete" in warning for warning in body["warnings"])
