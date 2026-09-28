"""One writer, checkpoints and the per-batch space/WAL guard (courts.writer)."""
import os
import sqlite3
import threading
import time

import pytest

from courts.writer import GuardReport, Maintenance, SharedConnection, WriterBusy, guard, wal_size, writer_lock


def test_a_second_writer_refuses_to_start(tmp_path):
    with writer_lock("first", tmp_path):
        with pytest.raises(WriterBusy) as exc:
            with writer_lock("second", tmp_path):
                pass
        assert "first" in str(exc.value)
    with writer_lock("third", tmp_path):   # released again
        pass


def _shared(tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite", timeout=10, check_same_thread=False)
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t(x INTEGER)")
    return SharedConnection(conn)


def test_a_transaction_of_one_thread_never_takes_statements_of_another(tmp_path):
    conn = _shared(tmp_path)
    started = threading.Event()

    def rolled_back():
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO t VALUES(1)")
        started.set()
        time.sleep(0.3)
        conn.execute("ROLLBACK")

    def autocommit():
        started.wait()
        conn.execute("INSERT INTO t VALUES(2)")  # must wait for the ROLLBACK, not join the transaction
    threads = [threading.Thread(target=rolled_back), threading.Thread(target=autocommit)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert [r[0] for r in conn.execute("SELECT x FROM t")] == [2]


def test_guard_checkpoints_the_wal_to_zero(tmp_path, monkeypatch):
    monkeypatch.setenv("SNTIQ_COURTS_SPACE_GUARD", "off")
    conn = _shared(tmp_path)
    conn.executemany("INSERT INTO t VALUES(?)", [(i,) for i in range(20000)])
    assert wal_size(tmp_path / "db.sqlite") > 0
    report = guard(conn, tmp_path)
    assert report.checkpoint[0] == 0 and report.wal_bytes == 0


def test_guard_stops_on_low_free_space_and_unallocated(tmp_path, monkeypatch):
    monkeypatch.delenv("SNTIQ_COURTS_SPACE_GUARD", raising=False)
    conn = _shared(tmp_path)

    class Stat:
        f_bavail, f_frsize = 10 * 1000 ** 3 // 4096, 4096   # 10 GB free
    monkeypatch.setattr(os, "statvfs", lambda path: Stat)
    import courts.space as space

    def fake_measure(path):
        report = space.SpaceReport(str(path), "btrfs", unallocated=1024 ** 2, metadata_used=95 * 1024 ** 3,
                                   metadata_total=100 * 1024 ** 3)
        return report
    monkeypatch.setattr(space, "measure", fake_measure)
    report = guard(conn, tmp_path)
    assert any("free space" in p for p in report.problems)
    assert any("unallocated" in p for p in report.problems)
    assert any("metadata" in p for p in report.problems)


def test_maintenance_is_rate_limited_and_sticky(tmp_path, monkeypatch):
    import courts.writer as writer
    calls = []

    def fake_guard(conn, home=None, **kw):
        calls.append(1)
        return GuardReport(problems=["WAL 3.00 GiB after a blocked checkpoint (> 2 GiB)"] if len(calls) > 1 else [])
    monkeypatch.setattr(writer, "guard", fake_guard)
    m = Maintenance(object(), tmp_path, every_items=3, every_seconds=3600)
    assert [m.tick() for _ in range(3)] == [[], [], []] and len(calls) == 1
    assert m.tick() == [] and m.tick() == [] and m.tick() != []      # second guard run reports the WAL
    assert m.tick() == m.stopped and len(calls) == 2                  # stays stopped without re-measuring


def test_sync_refuses_more_than_six_sources():
    from courts.cli import main
    assert main(["sync", "--source", "be,bw,he,hh,mv,rp,sh"]) == 2


def test_two_sources_as_threads_of_one_writer(fixture):
    """The shape of `courts sync --source a,b`: threads sharing one connection and one Maintenance."""
    from courts.store import connect, init_db
    from courts.sync import sync_source
    from tests.courts.test_store_sync_read import FakeHttp, FixtureAdapter, item
    conn = SharedConnection(connect(threads=True))
    init_db(conn)
    maintenance = Maintenance(conn, every_items=1)

    class Other(FixtureAdapter):
        source_id = "bverwg"

        def parse(self, capture, it):
            parsed = super().parse(capture, it)
            parsed.source_id = "bverwg"
            return parsed
    results = {}

    def run(adapter):
        results[adapter.source_id] = sync_source(adapter, conn, FakeHttp(), maintenance=maintenance)
    threads = [threading.Thread(target=run, args=(a,)) for a in (FixtureAdapter(fixture, [item()]),
                                                                 Other(fixture, [item()]))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert {r["status"] for r in results.values()} <= {"ok", "partial"}
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1          # same ECLI: one decision
    assert conn.execute("SELECT count(*) FROM source_items WHERE state='skipped'").fetchone()[0] == 1
    assert maintenance.last_report is not None                      # the guard ran during the threads
    from courts.writer import checkpoint
    assert checkpoint(conn)[0] == 0                                  # and the WAL truncates once they are done
