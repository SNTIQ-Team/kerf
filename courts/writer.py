"""One writer for the corpus database (operator instruction 2026-09-28).

16 sync processes and 20 parallel re-parse processes on one WAL database kept the WAL from ever being
checkpointed; it grew to 86 GB and filled /home.  Rules enforced here:

* **One writer process.**  Every command that writes takes the exclusive writer lock
  (``<data home>/.writer.lock``); a second writer refuses to start.  Several sources are synced as threads
  of that one process, sharing one connection through :class:`SharedConnection`, whose transaction lock
  keeps each ``BEGIN … COMMIT`` of one thread free of statements from another.
* **Checkpoints.**  :class:`Maintenance` runs ``PRAGMA wal_checkpoint(TRUNCATE)`` every ``every_items``
  items or ``every_seconds`` seconds (retrying while readers block it); ``journal_size_limit`` caps the
  WAL file that remains.
* **A space guard per batch.**  The same call checks free data space (statvfs), btrfs unallocated space
  and metadata use, and the WAL size, and tells the caller to stop.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import data_home, db_path

GB = 1000 ** 3
GIB = 1024 ** 3
MIN_FREE_BYTES = 50 * GB               # statvfs free space for the data directory
MIN_UNALLOCATED = 5 * GIB              # btrfs "Device unallocated"
MAX_METADATA_USED = 0.90               # btrfs metadata, effective fraction
MAX_WAL_BYTES = 2 * GIB                # after a checkpoint that could not truncate
JOURNAL_SIZE_LIMIT = 256 * 1024 ** 2   # PRAGMA journal_size_limit


class WriterBusy(RuntimeError):
    pass


@contextlib.contextmanager
def writer_lock(what: str, home: Path | None = None):
    """Exclusive, non-blocking: at most one writing process per corpus."""
    home = Path(home or data_home())
    home.mkdir(parents=True, exist_ok=True)
    path = home / ".writer.lock"
    with open(path, "a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.seek(0)
            raise WriterBusy(f"another writer holds {path}: {handle.read().strip() or 'unknown'}") from None
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid {os.getpid()} {what}\n")
        handle.flush()
        try:
            yield
        finally:
            handle.seek(0)
            handle.truncate()
            fcntl.flock(handle, fcntl.LOCK_UN)


class SharedConnection:
    """A sqlite3 connection shared by the source threads of the one writer process.

    Single statements run under the lock; an explicit ``BEGIN`` takes the lock until ``COMMIT``/``ROLLBACK``,
    so a transaction never interleaves with another thread's statements.  Everything else is delegated.
    """

    def __init__(self, conn):
        self._conn = conn
        self._lock = threading.RLock()

    def __getattr__(self, name):
        return getattr(self._conn, name)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def execute(self, sql: str, params=()):
        head = sql.lstrip()[:8].upper()
        if head.startswith("BEGIN"):
            self._lock.acquire()
            try:
                return self._conn.execute(sql, params)
            except BaseException:
                self._lock.release()
                raise
        if head.startswith(("COMMIT", "ROLLBACK", "END")):
            try:
                return self._conn.execute(sql, params)
            finally:
                self._lock.release()
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows):
        with self._lock:
            return self._conn.executemany(sql, rows)

    def executescript(self, script: str):
        with self._lock:
            return self._conn.executescript(script)

    def commit(self):
        with self._lock:
            return self._conn.commit()


@dataclass
class GuardReport:
    wal_bytes: int = 0
    checkpoint: tuple | None = None
    free_bytes: int | None = None
    unallocated: int | None = None
    metadata_fraction: float | None = None
    problems: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"wal_bytes": self.wal_bytes, "checkpoint": self.checkpoint, "free_bytes": self.free_bytes,
                "unallocated_bytes": self.unallocated, "metadata_fraction": self.metadata_fraction,
                "problems": self.problems}


def checkpoint(conn, *, attempts: int = 3) -> tuple:
    """(busy, wal_frames, checkpointed_frames) of the last attempt; busy=0 means the WAL was truncated.

    Runs on a connection of its own (a statement still open on the writer connection would make the
    checkpoint fail with "table is locked"), while the writer's transaction lock is held, so no write
    transaction is in flight; readers elsewhere (MCP, verify) only delay it."""
    import sqlite3
    result = (1, -1, -1)
    path = _db_file(conn)
    if path is None:
        return result
    lock = getattr(conn, "lock", None)
    side = sqlite3.connect(path, timeout=30)
    try:
        for attempt in range(attempts):
            with (lock if lock is not None else contextlib.nullcontext()):
                try:
                    result = tuple(side.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
                except sqlite3.OperationalError:
                    result = (1, -1, -1)
            if result[0] == 0:
                return result
            time.sleep(0.2 * (attempt + 1))  # a reader holds an old snapshot; give it a moment
    finally:
        side.close()
    return result


def wal_size(path: Path | None = None) -> int:
    wal = Path(str(path or db_path()) + "-wal")
    try:
        return wal.stat().st_size
    except FileNotFoundError:
        return 0


def guard(conn, home: Path | None = None, *, measure_btrfs: bool = True) -> GuardReport:
    """Checkpoint, then check WAL size, free space, btrfs unallocated and metadata.  Problems mean: stop."""
    if os.environ.get("SNTIQ_COURTS_SPACE_GUARD") == "off":  # tests only (the checkpoint still runs)
        report = GuardReport(checkpoint=checkpoint(conn))
        report.wal_bytes = wal_size(_db_file(conn))
        return report
    home = Path(home or data_home())
    report = GuardReport(checkpoint=checkpoint(conn))
    report.wal_bytes = wal_size(_db_file(conn))
    if report.checkpoint[0] != 0 and report.wal_bytes > MAX_WAL_BYTES:
        report.problems.append(f"WAL {report.wal_bytes / GIB:.2f} GiB after a blocked checkpoint (> 2 GiB)")
    stat = os.statvfs(home)
    report.free_bytes = stat.f_bavail * stat.f_frsize
    if report.free_bytes < MIN_FREE_BYTES:
        report.problems.append(f"free space {report.free_bytes / GB:.1f} GB < 50 GB")
    if measure_btrfs:
        from .space import measure
        space = measure(home)
        if space.filesystem == "btrfs":
            report.unallocated = space.unallocated
            report.metadata_fraction = space.metadata_effective_fraction
            if space.unallocated is None or space.metadata_total is None:
                report.problems.append("could not read btrfs allocation")
            else:
                if space.unallocated < MIN_UNALLOCATED:
                    report.problems.append(f"btrfs unallocated {space.unallocated / GIB:.2f} GiB < 5 GiB")
                if report.metadata_fraction is not None and report.metadata_fraction > MAX_METADATA_USED:
                    report.problems.append(f"btrfs metadata {report.metadata_fraction:.1%} used (> 90%)")
    return report


def _db_file(conn) -> Path | None:
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row[2]) if row and row[2] else None


class Maintenance:
    """Rate-limited :func:`guard` for long loops (sync items, re-parse decisions), shared by threads."""

    def __init__(self, conn, home: Path | None = None, *, every_items: int = 25, every_seconds: float = 60.0):
        self.conn, self.home = conn, home
        self.every_items, self.every_seconds = every_items, every_seconds
        self._items = 0
        self._last = time.monotonic()
        self._lock = threading.Lock()
        self.last_report: GuardReport | None = None
        self.stopped: list[str] = []

    def tick(self, items: int = 1) -> list[str]:
        """Count work; when due, checkpoint and check.  Returns the problems (empty: carry on)."""
        with self._lock:
            if self.stopped:
                return self.stopped
            self._items += items
            if self._items < self.every_items and time.monotonic() - self._last < self.every_seconds:
                return []
            self._items, self._last = 0, time.monotonic()
            self.last_report = guard(self.conn, self.home)
            if self.last_report.problems:
                self.stopped = self.last_report.problems
            return self.last_report.problems
