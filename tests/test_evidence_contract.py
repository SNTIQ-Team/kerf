"""Audit findings d–g as executable contracts.

(d) paper §7.3 — an integrity failure is never an empty success;
(e) paper §7.2 — current vs historical search are explicit capabilities and
    no route silently drops a date;
(f) paper §7.4 — Markdown front matter provenance survives as structure;
(g) GII per-norm URLs come from the official TOC, e.g. gg/art_1.html.
"""
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from api import main
from api.server import server

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def client():
    return TestClient(server, root_path="/lex")


# ------------------------------------------------------------------ (d)

@pytest.fixture
def consulted(monkeypatch):
    monkeypatch.setattr(main, "_retrospective_manifest",
                        lambda **kwargs: {"fixture": True})
    monkeypatch.setattr(main, "resolve_changes",
                        lambda *args, **kwargs: {"matched": 0, "events": []})
    monkeypatch.setattr(main, "_load", lambda *args: [])


def test_paper_6_9_integrity_failure_differs_from_zero_matches(
        consulted, monkeypatch):
    """The paper's fault injection, as a regression test: successful
    consultation with no matches and an injected integrity failure must not
    produce identical dictionaries."""
    zero = main._append_temporal_search({"result_total": 0}, "x")

    def broken(*args, **kwargs):
        raise main.RetrospectiveIntegrityError("hash mismatch")

    monkeypatch.setattr(main, "resolve_changes", broken)
    failed = main._append_temporal_search({"result_total": 0}, "x")
    assert zero != failed
    assert zero["status"] == "ok" and zero["change_total"] == 0
    assert zero["result_total"] == 0
    assert failed["status"] == "partial"
    assert failed["change_total"] is None and failed["result_total"] is None
    assert failed["components"]["changes"] == {
        "status": "unavailable", "reason": "integrity_check_failed"}


def test_missing_catalogue_is_unavailable_not_zero(monkeypatch):
    def load(name):
        raise HTTPException(503, "data file missing")

    monkeypatch.setattr(main, "_load", load)
    result = main._append_gii_catalog({"result_total": 3}, "x", 10)
    assert result["catalog_total"] is None
    assert result["components"]["catalog"]["status"] == "unavailable"
    assert result["result_total"] is None
    assert result["result_total_lower_bound"] == 3


def test_changes_integrity_failure_is_a_server_side_503(monkeypatch):
    def broken(*args, **kwargs):
        raise main.RetrospectiveIntegrityError("hash mismatch")

    monkeypatch.setattr(main, "_retrospective_manifest",
                        lambda **kwargs: {"fixture": True})
    monkeypatch.setattr(main, "resolve_changes", broken)
    response = TestClient(main.app).get("/changes")
    assert response.status_code == 503


# ------------------------------------------------------------------ (e)

@pytest.mark.parametrize("url", [
    "/decisions?q=x&date=2020-01-01",
    "/citations?act=GG&valid_at=2020-01-01",
    "/acts/fed_gg/archive?at=2020-01-01",
    "/eu-index?q=x&as_of_date=2020",
    "/changes?stichtag=2020-01-01",
])
def test_no_route_silently_drops_a_date(client, url):
    response = client.get(url)
    assert response.status_code == 422
    body = response.json()
    assert body["status"] in {"unsupported_temporal_request",
                              "invalid_argument"}
    assert body["unsupported_arguments"]
    assert body["total"] is None


def test_undeclared_argument_is_rejected_everywhere(client):
    response = client.get("/feed?limit=5&jurisdiction=EU")
    assert response.status_code == 422
    assert response.json()["status"] == "invalid_argument"
    assert "limit" in response.json()["allowed_arguments"]


def test_transport_cache_keys_are_disclosed_not_silently_dropped(
        client, monkeypatch):
    monkeypatch.setattr(main, "_load", lambda name: [])
    response = client.get("/feed?limit=1&t=1727500000000")
    assert response.status_code == 200
    assert response.headers["X-Lexgraph-Ignored-Arguments"] == "t"
    # A cache key never excuses a material argument on the same request.
    assert client.get("/feed?limit=1&t=1&at=2020-01-01").status_code == 422
    # Only /digest treats d= as a cache key.
    assert client.get("/feed?limit=1&d=2026-09-28").status_code == 422


def test_search_declares_current_only_and_rejects_dates(client):
    capabilities = client.get("/capabilities").json()
    temporal = capabilities["temporal"]
    assert temporal["search"]["mode"] == "current_consolidation_only"
    assert temporal["search"]["valid_at"] == "unsupported"
    assert "norms/{id}/at" in temporal["norm_history"]["operations"]
    rejected = client.get("/search?q=Asyl&valid_at=2019-01-01").json()
    assert rejected["status"] == "unsupported_temporal_request"
    assert rejected["historical_alternative"] == "/norms/{id}/at?date="


# ------------------------------------------------------------------ (f)

def test_front_matter_is_parsed_not_stripped():
    text = ('---\nact_id: "fed_x"\nsource_exact: false\ncomplete: true\n'
            'verified_reconstruction: true\ncoverage_gaps: 2\n'
            'date_basis: "retrieval_observation_not_effective_date"\n'
            'requested_at: 2026-07-06\n---\n\n# X\n\n## § 1\n\nText.\n')
    fields, body = main.parse_front_matter(text)
    assert fields["source_exact"] is False
    assert fields["verified_reconstruction"] is True
    assert fields["coverage_gaps"] == 2
    assert fields["requested_at"] == "2026-07-06"
    assert body.startswith("\n# X")


def test_markdown_json_keeps_markdown_and_structured_provenance(
        monkeypatch, tmp_path):
    from fastapi.responses import Response
    raw = ('---\nact_id: "fed_x"\nsource_exact: false\ncomplete: true\n'
           'verified_reconstruction: true\ncoverage_gaps: 1\n---\n\n'
           '# X\n\n> Archive status: **verified_reconstruction**\n')

    def fake(**kwargs):
        return Response(raw, media_type="text/markdown", headers={
            "X-Lexgraph-Source-Exact": "false"})

    monkeypatch.setattr(main, "act_markdown", fake)
    body = TestClient(main.app).get("/acts/fed_x/markdown.json").json()
    assert body["markdown"] == raw  # nothing stripped
    assert body["provenance"]["source_exact"] is False
    assert body["quality"]["reconstruction_status"] == \
        "verified_reconstruction"
    assert any("not an exact official source" in w for w in body["warnings"])
    assert any("coverage gap" in w for w in body["warnings"])
    assert body["response_headers"]["x-lexgraph-source-exact"] == "false"


# ------------------------------------------------------------------ (g)

def test_gii_toc_resolves_articles_not_numeric_templates():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from pipeline.fetch_gii_toc import parse_toc
    from api.norm_identity import gii_norm_url

    raw = (FIXTURES / "gii_toc" / "gg_index.html").read_bytes()
    norms = parse_toc(raw)
    assert norms["art/1"] == "art_1.html"
    assert norms["art/143h"] == "art_143h.html"
    assert "para/1" not in norms  # the GG has no "§ 1": never __1.html
    toc = {"acts": {"gg": {"norms": norms, "fetched_at": "2026-09-28",
                           "sha256": "6b9b27b5"}}}
    assert gii_norm_url(toc, "gg", "art", "1")["url"] == \
        "https://www.gesetze-im-internet.de/gg/art_1.html"
    missing = gii_norm_url(toc, "gg", "para", "1")
    assert missing["url"] is None and missing["status"] == "norm_not_in_toc"
    assert gii_norm_url(None, "gg", "art", "1")["url"] is None


def test_bundled_toc_covers_the_deep_corpus():
    from api.law_routes import BUNDLED_TOC
    payload = json.loads(BUNDLED_TOC.read_text())
    assert payload["acts"]["gg"]["norms"]["art/1"] == "art_1.html"
    assert payload["acts"]["sgb_14"]["norms"]["para/37"] == "__37.html"
    assert not payload["failures"]


def test_head_markdown_front_matter_names_its_source():
    from api.act_archive import render_markdown_snapshot
    act = {"id": "fed_x", "jurabk": "X", "juris": "DE", "title": "X-Gesetz",
           "source_url": "https://www.gesetze-im-internet.de/x/",
           "build": "20260630", "stand": "Stand: zuletzt geändert durch Art. 1",
           "norms": [{"enbez": "§ 1", "titel": "", "text": "(1) Text."}],
           "versions": [], "official_states": []}
    result = render_markdown_snapshot(act, fallback_head="2026-09-28")
    front, _ = main.parse_front_matter(result["markdown"])
    assert front["source"] == "GII"
    assert front["source_url"] == "https://www.gesetze-im-internet.de/x/"
    assert front["source_build"] == "20260630"
    assert "retrieval" in front["date_basis"]
