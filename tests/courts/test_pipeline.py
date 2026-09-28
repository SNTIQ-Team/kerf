"""The concurrent pipeline: adaptive per-host concurrency (AIMD) with a shared token bucket, atomic claims
with leases, dedupe before fetching, one writer, graceful stop."""
import threading
import time

import pytest

from courts.hostgov import Concurrency, Governors, HostGovernor
from courts.sources.base import Adapter, Capture, Listing


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def governor(clock, logs, start=4, maximum=8):
    return HostGovernor("nrwe.justiz.nrw.de", start=start, maximum=maximum, min_interval=1.0, log=logs.append,
                        clock=clock, sleep=clock.sleep)


def test_additive_increase_needs_two_quiet_minutes_and_low_latency():
    clock, logs = Clock(), []
    g = governor(clock, logs)
    for _ in range(20):
        g.answered(0.30)                       # baseline p95 = 0.30 s
    assert g.baseline == pytest.approx(0.30) and g.limit == 4
    clock.now += 60
    for _ in range(10):
        g.answered(0.35)
    assert g.limit == 4                        # not yet 2 minutes since the last change
    clock.now += 61
    g.answered(0.35)
    assert g.limit == 5 and "4 -> 5" in logs[-1]
    clock.now += 121
    for _ in range(9):
        g.answered(0.70)                       # p95 >= 2x baseline: no growth
    g.answered(0.70)
    assert g.limit == 5
    g.failed()                                 # an error in the window blocks growth too
    clock.now += 121
    for _ in range(10):
        g.answered(0.3)
    assert g.limit == 5


def test_throttle_halves_honours_retry_after_and_cools_down():
    clock, logs = Clock(), []
    g = governor(clock, logs, start=8)
    for _ in range(20):
        g.answered(0.3)
    g.throttled("HTTP 429", retry_after=30)
    assert g.limit == 4
    t0 = clock.now
    g.take_token()                             # every thread waits out Retry-After
    assert clock.now - t0 >= 30
    clock.now += 125                           # 2 minutes later, but inside the 5-minute cooldown
    for _ in range(12):
        g.answered(0.3)
    assert g.limit == 4
    clock.now += 200                           # cooldown over
    for _ in range(10):
        g.answered(0.3)
    assert g.limit == 5


def test_three_blocks_in_ten_minutes_drop_the_host_to_one_and_alert():
    clock, logs = Clock(), []
    g = governor(clock, logs, start=8)
    g.throttled("challenge page (HTTP 403)")
    clock.now += 60
    g.throttled("HTTP 503", retry_after=5)
    clock.now += 60
    g.throttled("connection error ConnectionError")
    assert g.limit == 1 and any(line.startswith("ALERT nrwe.justiz.nrw.de") for line in logs)
    clock.now += 1000                          # still inside the 30-minute hold: no growth past 1
    for _ in range(30):
        g.answered(0.1)
    assert g.limit == 1


def test_latency_spike_halves():
    clock, logs = Clock(), []
    g = governor(clock, logs, start=6)
    for _ in range(20):
        g.answered(0.3)
    for _ in range(20):
        g.answered(2.0)                        # p95 > 3x baseline and > baseline + 1 s
    assert g.limit == 3 and "latency spike" in logs[-1]


def test_token_bucket_rate_is_slots_per_min_interval():
    clock, logs = Clock(), []
    g = HostGovernor("h", start=2, maximum=2, min_interval=1.0, log=logs.append, clock=clock, sleep=clock.sleep)
    t0 = clock.now
    for _ in range(12):
        g.take_token()
    # 2 tokens of burst, then 2 per second: 10 more tokens take ~5 s
    assert 4.5 <= clock.now - t0 <= 5.5


def test_slots_limit_documents_in_flight():
    g = HostGovernor("h", start=2, maximum=2, min_interval=1.0)
    active, peak, lock = [0], [0], threading.Lock()

    def work():
        with g.slot():
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.02)
            with lock:
                active[0] -= 1
    threads = [threading.Thread(target=work) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 2


# --- claims, pipeline -------------------------------------------------------------------------

class SlowAdapter(Adapter):
    """20 items; each fetch takes a moment; counts fetches per item and the peak in flight."""
    source_id, label, base_url, land, adapter_name = "rii", "slow", "https://slow.example", "de", "slow"
    concurrency = Concurrency(3, 4)

    def __init__(self, fixture, n=20, stop_after=None):
        self.fixture, self.n, self.stop_after = fixture, n, stop_after
        self.fetches, self.active, self.peak = {}, 0, 0
        self.lock = threading.Lock()

    def list_items(self, http, conn, *, since, limit, courts):
        return Listing(items=[{"source_doc_id": f"d{i:02d}", "url": f"https://slow.example/d{i:02d}",
                               "listed_date": "2016-03-16", "listed_court": "bverwg", "priority": 0}
                              for i in range(self.n)], upstream_total=self.n)

    def fetch(self, http, item):
        import courts.sync as sync_module
        with self.lock:
            self.fetches[item["source_doc_id"]] = self.fetches.get(item["source_doc_id"], 0) + 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.stop_after and sum(self.fetches.values()) >= self.stop_after:
                sync_module.STOP_AFTER_ITEM.set()
        time.sleep(0.03)
        with self.lock:
            self.active -= 1
        return Capture(self.fixture("rii/jb-WBRE201600193.zip"), item["url"], "application/zip", "zip")

    def parse(self, capture, item):
        from courts.sources.rii import RiiAdapter
        parsed = RiiAdapter().parse(capture, dict(item, source_doc_id="jb-WBRE201600193",
                                                  listed_case_numbers=None))
        parsed.source_doc_id = item["source_doc_id"]
        return parsed


class FakeHttp:
    requests_made = 0


@pytest.fixture
def conn():
    from courts.store import connect, init_db
    c = connect()
    init_db(c)
    return c


def test_every_item_is_fetched_exactly_once_with_bounded_concurrency(conn, fixture):
    from courts.pipeline import SourceSpec, run_pipeline
    adapter = SlowAdapter(fixture)
    results = run_pipeline(conn, [SourceSpec(adapter, FakeHttp())], relist_after=0)
    assert sorted(adapter.fetches) == [f"d{i:02d}" for i in range(20)]
    assert set(adapter.fetches.values()) == {1}                       # no item twice
    assert adapter.peak <= 4                                          # the host's maximum
    run = results["rii"]
    assert run["attempted"] == 20 and run["stored"] == 1 and run["duplicates"] == 19   # one ECLI
    states = dict(conn.execute("SELECT state, count(*) FROM source_items GROUP BY state").fetchall())
    assert states == {"stored": 1, "skipped": 19}
    assert conn.execute("SELECT count(*) FROM source_items WHERE claimed_by IS NOT NULL").fetchone()[0] == 0


def test_claims_are_atomic_across_threads(conn):
    from courts.store import claim_items, upsert_items
    upsert_items(conn, "rii", [{"source_doc_id": f"d{i:03d}", "url": "u", "listed_date": "2020-01-01"}
                               for i in range(200)])
    from courts.pipeline import Writer
    from courts.writer import Maintenance
    writer = Writer(conn, Maintenance(conn), lambda m: None)
    got, lock = [], threading.Lock()

    def claimer(n):
        for _ in range(10):
            rows = writer.call(claim_items, "rii", f"w{n}", 3)
            with lock:
                got.extend(r["source_doc_id"] for r in rows)
    threads = [threading.Thread(target=claimer, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    done = threading.Event()
    watcher = threading.Thread(target=lambda: ([t.join() for t in threads], done.set()))
    watcher.start()
    writer.run(done.is_set)
    assert len(got) == 180 and len(set(got)) == 180
    assert conn.execute("SELECT count(*) FROM source_items WHERE state='claimed'").fetchone()[0] == 180


def test_stale_claims_return_to_pending_at_start_and_sigterm_releases_unfetched(conn, fixture):
    import courts.sync as sync_module
    from courts.pipeline import SourceSpec, run_pipeline
    from courts.store import upsert_items
    upsert_items(conn, "rii", [{"source_doc_id": "stale", "url": "u", "listed_date": "2016-03-16"}])
    conn.execute("UPDATE source_items SET state='claimed', claimed_by='deadhost:1/rii', "
                 "lease_until='2026-01-01T00:00:00+00:00'")
    adapter = SlowAdapter(fixture, n=20, stop_after=5)
    try:
        results = run_pipeline(conn, [SourceSpec(adapter, FakeHttp())], relist_after=0)
    finally:
        sync_module.STOP_AFTER_ITEM.clear()
    assert results["rii"]["status"] == "interrupted"
    fetched = set(adapter.fetches)
    assert 5 <= len(fetched) <= 5 + 4                                  # the ones in flight are finished
    states = dict(conn.execute("SELECT state, count(*) FROM source_items GROUP BY state").fetchall())
    assert states.get("claimed", 0) == 0                               # nothing left claimed
    assert states["pending"] == 21 - len(fetched)                      # the stale claim is pending again
    assert "stale" not in fetched or adapter.fetches["stale"] == 1


def test_court_site_items_that_rii_lists_are_not_fetched(conn):
    from courts.pipeline import dedupe_before_fetch
    from courts.store import upsert_items
    upsert_items(conn, "rii", [{"source_doc_id": "jb-1", "url": "u", "listed_date": "2016-03-16",
                                "listed_court": "bverwg", "listed_case_numbers": "1 B 19/16"}])
    upsert_items(conn, "bverwg", [
        {"source_doc_id": "160316B1B19.16.0", "url": "u", "listed_date": "2016-03-16", "listed_case_numbers": "1 B 19.16"},
        {"source_doc_id": "010105U2C1.04.0", "url": "u", "listed_date": "2005-01-01", "listed_case_numbers": "2 C 1.04"}])
    assert dedupe_before_fetch(conn, "bverwg") == 1
    rows = dict(conn.execute("SELECT source_doc_id, state FROM source_items WHERE source_id='bverwg'").fetchall())
    assert rows == {"160316B1B19.16.0": "skipped", "010105U2C1.04.0": "pending"}   # 2005: not in RII
    assert dedupe_before_fetch(conn, "nrw") == 0                       # portals do not defer


def test_governors_share_one_bucket_per_host_and_the_stricter_policy_wins():
    g = Governors()
    g.declare("gesetze.berlin.de", Concurrency(2, 4), 1.1)
    g.declare("gesetze.berlin.de", Concurrency(1, 2), 2.0)
    host = g.get("gesetze.berlin.de")
    assert (host.limit, host.maximum, host.min_interval) == (1, 2, 2.0)
    assert g.get("GESETZE.berlin.de") is host


def test_existing_source_items_get_the_claim_columns(tmp_path):
    """The live v4 file had source_items without claims: init_db rebuilds the table, rows kept."""
    import sqlite3
    from courts.store import connect, init_db
    db = tmp_path / "c.sqlite"
    conn = connect(db)
    init_db(conn)
    conn.executescript("""
        DROP VIEW source_coverage; DROP INDEX source_items_claim; DROP TABLE source_items;
        CREATE TABLE source_items(source_id TEXT NOT NULL, source_doc_id TEXT NOT NULL, url TEXT NOT NULL,
          listed_date TEXT, listed_court TEXT, listed_case_numbers TEXT, listed_modified TEXT,
          priority INTEGER NOT NULL DEFAULT 1,
          state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','stored','failed','skipped')),
          fetched_modified TEXT, content_hash TEXT, decision_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          last_attempt TEXT, failure_kind TEXT, failure_detail TEXT, extra TEXT NOT NULL DEFAULT '{}',
          PRIMARY KEY(source_id, source_doc_id));
        CREATE INDEX source_items_frontier ON source_items(source_id, state, priority, listed_date);
        INSERT INTO source_items(source_id, source_doc_id, url, state) VALUES('nrw','a','u','pending'),
                                                                            ('nrw','b','u','stored');""")
    init_db(conn)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(source_items)")}
    assert {"claimed_by", "lease_until"} <= columns
    assert dict(conn.execute("SELECT source_doc_id, state FROM source_items").fetchall()) == {"a": "pending",
                                                                                             "b": "stored"}
    conn.execute("UPDATE source_items SET state='claimed' WHERE source_doc_id='a'")   # the CHECK admits it
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "source_items_claim" in names and "source_items_frontier" not in names


def test_a_small_baseline_does_not_turn_noise_into_spikes():
    clock, logs = Clock(), []
    g = governor(clock, logs, start=4)
    for _ in range(20):
        g.answered(0.2)
    for _ in range(20):
        g.answered(0.9)                        # 4.5x the baseline, but only 0.7 s above it
    assert g.limit == 4


def test_first_sigterm_does_not_wait_for_a_listing(conn, fixture):
    import courts.sync as sync_module
    from courts.pipeline import SourceSpec, run_pipeline
    release = threading.Event()

    class Lister(SlowAdapter):
        source_id = "nrw"

        def list_items(self, http, conn, *, since, limit, courts):
            release.wait(30)            # a long listing
            return super().list_items(http, conn, since=since, limit=limit, courts=courts)
    fast = SlowAdapter(fixture, n=6, stop_after=3)
    started = time.monotonic()
    try:
        results = run_pipeline(conn, [SourceSpec(fast, FakeHttp()), SourceSpec(Lister(fixture), FakeHttp())],
                               relist_after=0)
    finally:
        sync_module.STOP_AFTER_ITEM.clear()
        release.set()
    assert time.monotonic() - started < 10
    assert results["rii"]["status"] == "interrupted"
    row = conn.execute("SELECT status, notes FROM sync_runs WHERE source_id='nrw'").fetchone()
    assert tuple(row) == ("interrupted", "listing abandoned (SIGTERM)")
