import gzip
import hashlib
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.native_court_search import router
from tools.build_native_court_search import build, public_originals


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.sqlite"
    with sqlite3.connect(ledger) as db:
        db.execute("CREATE TABLE decisions (id TEXT,ecli TEXT,source_id TEXT,source_doc_id TEXT,court_name TEXT,"
                   "decision_date TEXT,case_numbers_raw TEXT,source_url TEXT,content_hash TEXT,paragraph_count INTEGER)")
        db.execute("CREATE TABLE paragraphs (decision_id TEXT,ordinal INTEGER,text TEXT)")
        source = "https://www.rechtsprechung-im-internet.de/"
        for i, (origin, url, original) in enumerate([
                ("rii", source, "a"), ("rii", source, "b"), ("rii", source, "wrong"),
                ("he", "https://www.lareda.hessenrecht.hessen.de/", "d"),
                ("rii", "https://example.org/", "e")]):
            doc = f"jb-TEST{i}"
            db.execute("INSERT INTO decisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (f"id{i}", f"ECLI:DE:TEST:{i}", origin, doc, "Gericht", f"2020-01-0{1+i}",
                        "I ZR", url, original, 1))
            db.execute("INSERT INTO paragraphs VALUES(?,?,?)",
                       (f"id{i}", 1, "Kindeswohl Äußerung" + (" Geheimnis" if i >= 2 else "")))
    release = tmp_path / "index"
    build(ledger, release, {f"jb-TEST{i}": {h} for i, h in enumerate(["a", "b", "c", "d", "e"])}, "pin")
    monkeypatch.setenv("KERF_NATIVE_COURT_INDEX", str(release))
    key = tmp_path / "operator.key"
    key.write_text("z" * 40)
    monkeypatch.setenv("KERF_NATIVE_COURT_TOKEN_FILE", str(key))
    app = FastAPI()
    app.include_router(router)
    return TestClient(app), release


def test_scope_original_gate_and_literal_search(fixture):
    client, release = fixture
    q = "/courts/decisions/search"
    response = client.get(q, params={"q": "kindeswohl äußerung", "limit": 1})
    data = response.json()
    assert data["indexed_decisions"] == 2
    assert data["items"][0]["document"] == "jb-TEST1"
    assert "text" not in data["items"][0]
    next_data = client.get(q, params={"q": "kindeswohl äußerung", "after": data["next"]}).json()
    assert [v["document"] for v in next_data["items"]] == ["jb-TEST0"] and not next_data["next"]
    assert client.get(q, params={"q": "Geheimnis"}).json()["items"] == []
    assert client.get(q, params={"q": 'Kindeswohl OR "Geheimnis"*'}).json()["items"] == []
    assert client.get(q, params={"q": "§§"}).status_code == 422
    assert client.get(q, params={"q": "Kindeswohl", "after": "0" * 20 + ".1"}).status_code == 409
    for path in [release / "public.sqlite", release / "operator.sqlite"]:
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT text FROM terms WHERE terms MATCH 'kindeswohl' LIMIT 1").fetchone()[0] is None


def test_operator_index_requires_credential_and_never_permits_shared_cache(fixture):
    client, _ = fixture
    endpoint = "/courts/decisions/search/operator"
    for auth in ["", "Basic test", "Bearer invalid"]:
        result = client.get(endpoint, params={"q": "Geheimnis"}, headers={"Authorization": auth})
        assert result.status_code == 401
        assert "no-store" in result.headers["cache-control"]
    allowed = client.get(endpoint, params={"q": "Geheimnis"}, headers={"Authorization": "Bearer " + "z" * 40})
    assert allowed.json()["indexed_decisions"] == 5 and len(allowed.json()["items"]) == 3
    assert "no-store" in allowed.headers["cache-control"] and "authorization" in allowed.headers["vary"].lower()
    assert all(v["document_path"].startswith("/kerf/courts/decisions/document?identifier=")
               for v in allowed.json()["items"])


def test_lookup_is_bound_to_verified_public_manifest(tmp_path):
    lookup = tmp_path / "lookup" / "00.json.gz"
    lookup.parent.mkdir()
    raw = gzip.compress(json.dumps({"jb-T": {"zip_sha256": "a", "xml_sha256": "b"}}).encode())
    lookup.write_bytes(raw)
    manifest = json.dumps({"format": "kerf-rii-publications/1", "files": {
        "lookup/00.json.gz": {"sha256": hashlib.sha256(raw).hexdigest(), "size": len(raw)}}}).encode()
    (tmp_path / "manifest.json").write_bytes(manifest)
    h = hashlib.sha256(manifest).hexdigest()
    assert public_originals(tmp_path, h) == {"jb-T": {"a", "b"}}
    lookup.write_bytes(raw[:-1])
    with pytest.raises(ValueError, match="Corrupt public lookup"):
        public_originals(tmp_path, h)
