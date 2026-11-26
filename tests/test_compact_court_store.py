from contextlib import closing
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import compact_court_store
from api.court_observations import PROFILE as OBSERVATIONS
from api.decision_passages import create_router
from tests.test_decision_passages import ROW, XML, zipped
from tools import compact_court_index as builder
from tools.court_observations import attach
from tools.decision_passages import build_index


def client(root):
    app = FastAPI()
    app.include_router(create_router(lambda: root))
    return TestClient(app)


@pytest.fixture
def generation(tmp_path, monkeypatch):
    web, cache = tmp_path / "original", tmp_path / "cache"
    cache.mkdir()
    raw = zipped(XML)
    (cache / "jb-TEST1.zip").write_bytes(raw)
    build_index(
        [ROW, {"id": "missing"}], cache, web, observed_at="2026-09-27T10:00:00+00:00"
    )
    projected = tmp_path / "observations.json"
    projected.write_text(json.dumps({"profile": OBSERVATIONS, "observations": []}))
    attach(projected, web)
    revision = "a" * 40
    archive = SimpleNamespace(
        rows={ROW["id"]: {"original": {"sha256": hashlib.sha256(raw).hexdigest()}}},
        read=lambda key, *, original: raw,
    )
    monkeypatch.setattr(builder, "pinned_archive", lambda rev, digest: archive)
    output = tmp_path / "compact"
    descriptor = builder.compact(web, output, revision)
    return web, output, raw, revision, descriptor


def test_local_routes_match_and_originals_are_remote(generation, monkeypatch):
    web, output, raw, revision, descriptor = generation
    local, slim = client(web), client(output)
    accesses = []

    def remote(rev, digest):
        accesses.append((rev, digest))
        return SimpleNamespace(read=lambda key, *, original: raw)

    monkeypatch.setattr(compact_court_store, "pinned_archive", remote)
    for route in (
        "/decision-passages?q=claim",
        "/decision-passages?decision_id=rii-test1",
        "/decision-observations",
        "/decisions/rii-test1/passages/entscheidungsgruende:rd_7",
        "/decisions/rii-test1/relations",
        "/decisions/unknown/source",
        "/decisions/missing/source",
    ):
        a, b = local.get(route), slim.get(route)
        assert (a.status_code, a.json()) == (b.status_code, b.json())
    assert not accesses
    with closing(sqlite3.connect(output / compact_court_store.DATABASE)) as db:
        assert db.execute("select count(source_zip) from decisions").fetchone() == (0,)
        assert db.execute(
            "select count(*) from passages where json_type(data,'$.text') is not null"
        ).fetchone() == (0,)
    a, b = (
        local.get("/decisions/rii-test1/source"),
        slim.get("/decisions/rii-test1/source"),
    )
    assert a.content == b.content == raw
    assert dict(a.headers) == dict(b.headers)
    assert accesses == [(revision, descriptor["public_database_sha256"])]


def test_remote_failure_does_not_break_local_search(generation, monkeypatch):
    _, output, _, _, _ = generation
    slim = client(output)

    def timeout(*args):
        raise TimeoutError

    monkeypatch.setattr(compact_court_store, "pinned_archive", timeout)
    a = slim.get("/decisions/rii-test1/source")
    assert a.status_code == 503 and a.json()["detail"]["status"] == "source_unavailable"
    assert slim.get("/decision-passages?q=exact").json()["total"] == 1
    monkeypatch.setattr(
        compact_court_store,
        "pinned_archive",
        lambda *args: SimpleNamespace(read=lambda *a, **k: b"different"),
    )
    assert (
        slim.get("/decisions/rii-test1/source").json()["detail"]["status"]
        == "integrity_check_failed"
    )


def test_binding_is_verified_before_using_the_compact_index(generation):
    _, output, _, _, _ = generation
    view = client(output)
    path = output / compact_court_store.DESCRIPTOR
    descriptor = json.loads(path.read_bytes())
    descriptor["revision"] = "b" * 40
    path.write_text(json.dumps(descriptor))
    assert view.get("/decision-passages?q=evidence").status_code == 503
