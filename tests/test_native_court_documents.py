from contextlib import closing
from hashlib import sha256
from io import BytesIO
from urllib.request import Request
import gzip
import json
import sqlite3

from fastapi.testclient import TestClient
import pytest

from api import main, native_court_documents as native

ID = "example local/id 1"
REV = "a" * 40
ACCESS = {"Authorization": "Bearer " + "1" * 40}
PATH = "/courts/decisions/document"
SHARD = "packs/demo-0000.pack"


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    token = tmp_path / "operator"
    token.write_text(ACCESS["Authorization"].split()[1])
    address = tmp_path / "locs.sqlite"
    monkeypatch.setenv("KERF_NATIVE_COURT_TOKEN_FILE", str(token))
    monkeypatch.setenv("KERF_NATIVE_DOCUMENT_REVISION", REV)
    monkeypatch.setenv("KERF_NATIVE_DOCUMENT_INDEX", str(address))
    monkeypatch.setenv("KERF_NATIVE_DOCUMENT_REPO", "example/archive")
    monkeypatch.setenv("KERF_NATIVE_DOCUMENT_RELEASE", "releases/test")
    data = {"status": "ok", "decision": {"id": ID, "paragraphs": [{"rn": None, "text": "words"}]}}
    raw = json.dumps(data).encode()
    packed = gzip.compress(raw)
    with closing(sqlite3.connect(address)) as db:
        db.executescript("""
            CREATE TABLE locations(id TEXT PRIMARY KEY, shard TEXT, start INTEGER, length INTEGER, raw_length INTEGER, sha256 TEXT);
            CREATE TABLE shards(path TEXT PRIMARY KEY,size INTEGER,sha256 TEXT);
        """)
        db.execute("INSERT INTO locations VALUES (?,?,?,?,?,?)",
                   (ID, SHARD, 8, len(packed), len(raw), sha256(packed).hexdigest()))
        db.execute("INSERT INTO shards VALUES (?,?,?)", (SHARD, len(packed) + 30, "unused"))
        db.commit()
    def read(url, start, length, total):
        assert url == f"https://huggingface.co/datasets/example/archive/resolve/{REV}/releases/test/{SHARD}"
        assert (start, length, total) == (8, len(packed), len(packed) + 30)
        return packed
    monkeypatch.setattr(native, "read_range", read)
    return TestClient(main.app), address, data


def private(response, status):
    assert response.status_code == status
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Authorization"


def test_private_document_and_unavailable_ids(corpus):
    client, _, record = corpus
    response = client.get(PATH, params={"identifier": ID}, headers=ACCESS)
    private(response, 200)
    assert response.json() == record and response.headers["x-kerf-court-revision"] == REV
    private(client.get(PATH, params={"identifier": "absent"}, headers=ACCESS), 404)
    unauthorized = client.get(PATH, params={"identifier": ID})
    private(unauthorized, 401)
    assert unauthorized.headers["www-authenticate"] == "Bearer"


def test_validation_and_snapshot_errors_are_private(corpus):
    client, _, _ = corpus
    for params in ({}, {"identifier": ""}, {"identifier": "x" * 1025}, {"identifier": ID, "unknown": "1"},
                   [("identifier", ID), ("identifier", ID)]):
        private(client.get(PATH, params=params, headers=ACCESS), 422)
    private(client.get(PATH, params={"identifier": ID}, headers={**ACCESS, "If-Kerf-Snapshot": "other"}), 409)


@pytest.mark.parametrize("column,value", [
    ("l.shard", "../any"), ("s.size", 9), ("l.start", -1), ("l.length", native.MAX_FRAME + 1),
    ("l.raw_length", native.MAX_FRAME + 1), ("l.sha256", "wrong"),
])
def test_broken_locations_fail_closed_before_remote_fetch(corpus, monkeypatch, column, value):
    client, address, _ = corpus
    table, key = column.split(".")
    with closing(sqlite3.connect(address)) as db:
        db.execute(f"UPDATE {'locations' if table == 'l' else 'shards'} SET {key}=?", (value,))
        db.commit()
    monkeypatch.setattr(native, "read_range", lambda *_: pytest.fail("invalid entry made an HTTP request"))
    private(client.get(PATH, params={"identifier": ID}, headers=ACCESS), 503)


@pytest.mark.parametrize("kind,expected", [
    ("held", 200), ("wrong-held", 503), ("wrong-id", 503), ("absent-paragraphs", 503),
    ("gzip-tail", 503), ("raw-length", 503), ("wrong-hash", 503),
])
def test_record_integrity_and_identity_holds(corpus, monkeypatch, kind, expected):
    client, address, record = corpus
    if kind in {"held", "wrong-held"}:
        record = {"status": "integrity_check_failed", "decision_id": ID if kind == "held" else "other",
                  "source_check": {"text": "never publish"}, "decision": {"text": "never publish"}}
    if kind == "wrong-id":
        record["decision"]["id"] = "other"
    if kind == "absent-paragraphs":
        del record["decision"]["paragraphs"]
    raw = json.dumps(record).encode()
    packed = gzip.compress(raw) + (b"tail" if kind == "gzip-tail" else b"")
    digest = sha256(packed).hexdigest() if kind != "wrong-hash" else "f" * 64
    with closing(sqlite3.connect(address)) as db:
        db.execute("UPDATE locations SET length=?, raw_length=?, sha256=?", (len(packed), len(raw) + (kind == "raw-length"), digest))
        db.execute("UPDATE shards SET size=?", (len(packed) + 30,))
        db.commit()
    monkeypatch.setattr(native, "read_range", lambda *_: packed)
    response = client.get(PATH, params={"identifier": ID}, headers=ACCESS)
    private(response, expected)
    if kind == "held":
        assert response.json() == {"status": "integrity_check_failed", "decision_id": ID, "reason": "source_identity_mismatch"}


def test_credentials_stay_on_resolver_origin():
    request = Request("https://huggingface.co/datasets/example/archive", headers={"Range": "bytes=1-4"})
    request.add_unredirected_header("Authorization", "Bearer example")
    redirects = native.HubRedirects()
    for url, credential in [("https://huggingface.co/api/resolve-cache/file", "Bearer example"),
                            ("https://cdn-lfs.hf.co/file", None)]:
        redirected = redirects.redirect_request(request, None, 302, "", {}, url)
        assert redirected.get_header("Authorization") == credential
        assert redirected.get_header("Range") == "bytes=1-4"
    for url in ("http://huggingface.co/file", "https://outside.example/file", "https://user@huggingface.co/file"):
        with pytest.raises(ValueError):
            redirects.redirect_request(request, None, 302, "", {}, url)


@pytest.mark.parametrize("status,content_range,valid", [
    (206, "bytes 3-5/8", True), (200, "bytes 3-5/8", False), (206, "bytes 3-5/9", False), (206, "bytes 3-6/8", False),
])
def test_range_checks_status_both_offsets_and_total(tmp_path, monkeypatch, status, content_range, valid):
    token = tmp_path / "hf"
    token.write_text("hf_" + "t" * 30)
    monkeypatch.setenv("KERF_NATIVE_DOCUMENT_TOKEN_FILE", str(token))
    response = BytesIO(b"yes")
    response.status = status
    response.headers = {"Content-Range": content_range}
    class Opener:
        def open(self, req, timeout):
            assert req.get_header("Range") == "bytes=3-5" and req.get_header("Authorization") == "Bearer " + token.read_text()
            return response
    monkeypatch.setattr(native, "build_opener", lambda *_: Opener())
    if valid:
        assert native.read_range("https://huggingface.co/file", 3, 3, 8) == b"yes"
    else:
        with pytest.raises(ValueError):
            native.read_range("https://huggingface.co/file", 3, 3, 8)
