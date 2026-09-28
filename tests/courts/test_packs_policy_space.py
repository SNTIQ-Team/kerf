"""Packed captures, legacy migration, v1->v2 upgrade, operator robots policy, backoff, space guard."""
import gzip
import hashlib
import json
import sqlite3

import pytest

from courts import http as http_module
from courts.cli import http_for, main
from courts.errors import UpstreamFailure
from courts.http import PoliteHttp, retry_after_seconds
from courts.migrate import migrate_legacy_captures, verify
from courts.packs import CaptureIntegrityError, PackStore, scan_pack
from courts.read import coverage, open_db
from courts.sources import registry
from courts.sources.base import OPERATOR_DECISION_DATE, Adapter, Capture, Listing
from courts.space import SpaceReport, evaluate, parse_btrfs
from courts.store import connect, init_db, read_capture

DOC = "jb-WBRE201600193"


@pytest.fixture
def conn():
    c = connect()
    init_db(c)
    return c


# --- packs ---------------------------------------------------------------------------------

def test_pack_roundtrip_dedup_and_rotation(conn, isolated_corpus, fixture):
    store = PackStore(isolated_corpus, conn, max_bytes=6000)
    body = fixture(f"rii/{DOC}.zip")
    first = store.put(body, "rii", "zip")
    assert store.put(body, "rii", "zip") == first                     # content-addressed: stored once
    assert store.get(first.sha256) == body and first.codec == "zstd"
    others = [store.put(body + bytes([i]) * 3000, "rii", "zip") for i in range(3)]
    packs = sorted(p.name for p in (isolated_corpus / "packs" / "rii").iterdir())
    assert packs[0] == "rii-0001.pack" and len(packs) >= 2            # rotated at max_bytes
    assert all(store.get(r.sha256) for r in others)
    scanned = [d for p in sorted((isolated_corpus / "packs" / "rii").iterdir()) for _, d, *_ in scan_pack(p)]
    assert sorted(scanned) == sorted({first.sha256, *(r.sha256 for r in others)})


def test_pack_corruption_is_detected(conn, isolated_corpus, fixture):
    store = PackStore(isolated_corpus, conn)
    ref = store.put(fixture(f"rii/{DOC}.zip"), "rii", "zip")
    path = isolated_corpus / ref.pack
    data = bytearray(path.read_bytes())
    data[ref.offset + 20] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(Exception) as err:
        store.get(ref.sha256)
    assert isinstance(err.value, (CaptureIntegrityError, Exception))
    assert verify(conn, isolated_corpus)["verify_error_count"] >= 1


def test_torn_tail_is_ignored_by_scan(conn, isolated_corpus, fixture):
    store = PackStore(isolated_corpus, conn)
    ref = store.put(fixture(f"rii/{DOC}.zip"), "rii", "zip")
    path = isolated_corpus / ref.pack
    with open(path, "ab") as handle:
        handle.write(b"SCAP1\n" + b"\x00" * 10)                       # crash mid-header
    assert [d for _, d, *_ in scan_pack(path)] == [ref.sha256]
    assert store.get(ref.sha256)


# --- migration from legacy raw/*.gz ----------------------------------------------------------

def _legacy(home, source, body, ext):
    digest = hashlib.sha256(body).hexdigest()
    path = home / "raw" / source / digest[:2] / f"{digest}.{ext}.gz"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(body, mtime=0))
    return digest, path.relative_to(home).as_posix()


def test_migration_packs_repoints_verifies_and_deletes(conn, isolated_corpus, fixture):
    from courts.sync import sync_source
    from tests.courts.test_store_sync_read import FakeHttp, FixtureAdapter, item
    sync_source(FixtureAdapter(fixture, [item()]), conn, FakeHttp())
    # Simulate a v1 corpus: capture as a legacy file, index row absent, decision pointing at raw/.
    body = fixture(f"rii/{DOC}.zip")
    digest, rel = _legacy(isolated_corpus, "rii", body, "zip")
    orphan_digest, _ = _legacy(isolated_corpus, "nrw", b"<html>captured but never parsed</html>", "html")
    conn.execute("DELETE FROM captures")
    conn.execute("UPDATE decisions SET capture_path=?", (rel,))
    for pack in (isolated_corpus / "packs").rglob("*.pack"):
        pack.unlink()
    assert read_capture(conn, digest, rel, isolated_corpus) == body   # legacy fallback still works
    report = migrate_legacy_captures(conn, isolated_corpus, delete_legacy=True)
    assert (report["legacy_files"], report["packed"], report["verify_error_count"]) == (2, 2, 0)
    assert report["decisions_repointed"] == 1 and report["deleted"] == 2
    assert not (isolated_corpus / "raw").exists()
    path = conn.execute("SELECT capture_path FROM decisions").fetchone()[0]
    assert path.startswith("packs/rii/rii-0001.pack@")
    assert read_capture(conn, digest, path, isolated_corpus) == body
    assert PackStore(isolated_corpus, conn).get(orphan_digest).startswith(b"<html>")
    again = migrate_legacy_captures(conn, isolated_corpus, delete_legacy=True)   # idempotent
    assert again["legacy_files"] == 0 and again["verify_error_count"] == 0


def test_migration_keeps_legacy_files_when_verification_fails(conn, isolated_corpus):
    digest, rel = _legacy(isolated_corpus, "nrw", b"<html>ok</html>", "html")
    bad = isolated_corpus / "raw" / "nrw" / "ab" / ("ab" + "0" * 62 + ".html.gz")
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(gzip.compress(b"content that does not match its name"))
    report = migrate_legacy_captures(conn, isolated_corpus, delete_legacy=True)
    assert report["bad_legacy"] and report["deleted"] == 0 and bad.exists()
    assert (isolated_corpus / rel).exists()


def test_v1_database_is_upgraded_in_place(isolated_corpus):
    db = isolated_corpus / "courts.sqlite"
    isolated_corpus.mkdir(parents=True, exist_ok=True)
    old = sqlite3.connect(db)
    old.executescript("""CREATE TABLE sources(source_id TEXT PRIMARY KEY, label TEXT NOT NULL, base_url TEXT NOT NULL,
                           land TEXT, adapter TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'never_synced',
                           status_reason TEXT, upstream_listed INTEGER, last_sync_started TEXT,
                           last_sync_finished TEXT, last_sync_status TEXT, last_sync_run INTEGER);
                         INSERT INTO sources(source_id,label,base_url,adapter) VALUES('nrw','NRWE','https://x','n');
                         PRAGMA user_version=1;""")
    old.close()
    conn = connect(db)
    init_db(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    row = conn.execute("SELECT robots_policy, listing_complete FROM sources WHERE source_id='nrw'").fetchone()
    assert tuple(row) == ("respect", 0)
    assert conn.execute("SELECT count(*) FROM captures").fetchone()[0] == 0


# --- operator robots policy -----------------------------------------------------------------

IGNORED = {"rii", "bw", "be", "hh", "mv", "rp", "sl", "st", "sh", "th", "he", "sn", "bverwg"}


def test_operator_decision_sources_are_flagged():
    adapters = registry()
    for source_id in IGNORED:
        a = adapters[source_id]
        assert a.robots_policy == "ignore_by_operator_decision" and a.policy_decided_at == OPERATOR_DECISION_DATE
        assert "operator decision" in a.policy_basis and "never bypassed" in a.policy_basis
        assert a.concurrency.maximum <= {"rii": 6}.get(source_id, 4 if a.adapter_name == "juris-jportal" else 2)
        http = http_for(a)
        assert http.respect_robots is False and http.min_interval >= 1.0
    for source_id in ("nrw", "bsg", "bag", "bverfg", "bb", "ni", "hb", "by"):
        assert adapters[source_id].robots_policy == "respect" and http_for(adapters[source_id]).respect_robots
    for source_id in ("bverwg", "bsg", "bverfg", "hb", "by", "sn"):
        assert 2.0 <= adapters[source_id].min_interval <= 5.0   # small court sites


class FakeResponse:
    def __init__(self, status, body=b"ok", headers=None, url="https://example.org/"):
        self.status_code, self.content, self.url = status, body, url
        self.headers = headers or {"Content-Type": "text/plain"}


class Session:
    def __init__(self, pages):
        self.headers, self.pages, self.calls = {}, pages, []

    def get(self, url, timeout=None, allow_redirects=True):          # robots.txt
        self.calls.append(url)
        return FakeResponse(200, b"User-agent: *\nDisallow: /\n", url=url)

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append(url)
        responses = self.pages[url]
        status, body, hdrs = responses.pop(0) if len(responses) > 1 else responses[0]
        return FakeResponse(status, body, hdrs, url=url)


@pytest.fixture
def sleeps(monkeypatch):
    waits = []
    monkeypatch.setattr(http_module.time, "sleep", lambda s: waits.append(s))
    return waits


def test_ignore_policy_skips_robots_but_keeps_challenge_detection(sleeps, fixture):
    url = "https://www.rechtsprechung-im-internet.de/rii-toc.xml"
    session = Session({url: [(200, b"<items/>", {"Content-Type": "application/xml"})]})
    http = PoliteHttp(session=session, respect_robots=False)
    assert http.get(url).body == b"<items/>"
    assert session.calls == [url]                                     # robots.txt not consulted
    challenge = fixture("by/gesetze_bayern_security_check_503.html")
    session.pages[url] = [(503, challenge, {"Content-Type": "text/html"})]
    with pytest.raises(UpstreamFailure) as err:
        http.get(url)
    assert err.value.kind == "challenge"                              # never bypassed


class PolicyAdapter(Adapter):
    source_id, label, base_url, land, adapter_name = "rii", "RII test", "https://x", "de", "t"
    robots_policy, policy_decided_at, policy_basis = "ignore_by_operator_decision", OPERATOR_DECISION_DATE, "b"

    def __init__(self, failures):
        self.failures = failures

    def list_items(self, http, conn, *, since, limit, courts):
        return Listing(items=[{"source_doc_id": f"d{i}", "url": f"https://x/{i}", "priority": 0}
                              for i in range(6)])

    def fetch(self, http, item):
        raise self.failures.pop(0)

    def parse(self, capture, item):
        raise AssertionError


def test_policy_recorded_and_repeated_503_stops_the_source(conn):
    from courts.sync import sync_source

    class Http:
        requests_made = 0
    failures = [UpstreamFailure("http_error", "HTTP 503 (retry-after 120, waited 120s)", url="u", status=503)
                for _ in range(6)]
    result = sync_source(PolicyAdapter(failures), conn, Http())
    # Concurrent fetchers: documents already in flight when the third block arrives are still recorded,
    # but no further item is claimed.
    assert result["status"] == "blocked" and 3 <= result["attempted"] <= 6
    src = conn.execute("SELECT * FROM sources WHERE source_id='rii'").fetchone()
    assert (src["robots_policy"], src["policy_decided_at"], src["status"]) == \
        ("ignore_by_operator_decision", OPERATOR_DECISION_DATE, "blocked")
    run = conn.execute("SELECT notes FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    assert "operator decision" in run
    rows = {s["source_id"]: s for s in coverage(open_db())["sources"]}
    assert rows["rii"]["policy_note"].startswith("robots.txt/TDM reservation not applied: operator decision")


# --- 429/503 backoff -------------------------------------------------------------------------

def test_retry_after_seconds_and_http_date():
    assert retry_after_seconds("7") == 7
    assert retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0   # in the past
    assert retry_after_seconds("") is None and retry_after_seconds("soon") is None


def test_503_retry_after_is_honoured_then_succeeds_and_host_slows_down(sleeps):
    url = "https://www.lareda.hessenrecht.hessen.de/x"
    session = Session({url: [(503, b"busy", {"Retry-After": "7"}), (200, b"ok", {})]})
    http = PoliteHttp(session=session, respect_robots=False)
    assert http.get(url).body == b"ok"
    assert 7 in sleeps
    assert http.crawl_delay(url) >= 2.2                               # host interval doubled


def test_429_without_retry_after_backs_off_exponentially_and_fails_typed(sleeps):
    url = "https://nrwesuche.justiz.nrw.de/index.php"
    session = Session({url: [(429, b"slow down", {})]})
    http = PoliteHttp(session=session, respect_robots=False, retries=2)
    with pytest.raises(UpstreamFailure) as err:
        http.get(url)
    assert err.value.kind == "rate_limited" and err.value.status == 429
    assert [s for s in sleeps if s >= 30] == [30, 60]


# --- space guard ---------------------------------------------------------------------------

USAGE_FULL = """Overall:
    Device size:\t\t     2044111147008
    Device allocated:\t\t     2044110045184
    Device unallocated:\t\t           1101824
    Free (estimated):\t\t      126647689216\t(min: 126647689216)
    Data ratio:\t\t              1.00
    Metadata ratio:\t\t              2.00
"""
DF = """Data, single: total=2007535714304, used=1880888025088
System, DUP: total=33554432, used=278528
Metadata, DUP: total=18253611008, used=13414072320
GlobalReserve, single: total=536870912, used=0
"""
GIB = 1024 ** 3


def test_space_guard_refuses_bulk_writes_without_unallocated_space():
    report = evaluate(parse_btrfs(USAGE_FULL, DF, SpaceReport("/home", "btrfs")), starting=True)
    assert report.unallocated == 1101824
    assert any("Device unallocated" in p for p in report.problems)
    assert round(report.metadata_used_fraction, 3) == 0.735


def test_space_guard_allows_after_balance_and_watches_metadata():
    usage = USAGE_FULL.replace("1101824", str(20 * GIB))
    ok = evaluate(parse_btrfs(usage, DF, SpaceReport("/home", "btrfs")), starting=True)
    assert ok.problems == []
    full_meta = DF.replace("used=13414072320", "used=18000000000")
    # While running, 5 GiB unallocated is enough; metadata that cannot grow any more is not.
    usage_5 = USAGE_FULL.replace("1101824", str(5 * GIB))
    assert evaluate(parse_btrfs(usage_5, DF, SpaceReport("/home", "btrfs")), starting=False).problems == []
    usage_tight = USAGE_FULL.replace("1101824", str(GIB // 2))
    running = evaluate(parse_btrfs(usage_tight, full_meta, SpaceReport("/home", "btrfs")), starting=False)
    assert running.metadata_effective_fraction > 0.9
    assert any("metadata" in p for p in running.problems)


def test_cli_sync_waits_for_space(monkeypatch, capsys):
    import courts.cli as cli
    monkeypatch.setattr(cli, "check_space", lambda *a, **k: evaluate(
        parse_btrfs(USAGE_FULL, DF, SpaceReport("/home", "btrfs")), starting=True))
    assert main(["sync", "--source", "rii", "--limit", "1"]) == 4
    out = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert out["status"] == "waiting_for_space" and out["unallocated_bytes"] == 1101824


def test_compact_keeps_content_and_refuses_while_a_sync_runs(conn, fixture, isolated_corpus, capsys):
    from courts.sync import source_lock, sync_source
    from tests.courts.test_store_sync_read import FakeHttp, FixtureAdapter, item
    sync_source(FixtureAdapter(fixture, [item()]), conn, FakeHttp())
    conn.close()
    with source_lock("rii", isolated_corpus):
        assert main(["compact"]) == 3
    assert main(["compact"]) == 0
    db = open_db()
    assert db.execute("SELECT count(*) FROM paragraphs").fetchone()[0] > 10
    assert db.execute("SELECT count(*) FROM paragraphs_fts WHERE paragraphs_fts MATCH 'Untätigkeitsklage'").fetchone()[0] == 1
