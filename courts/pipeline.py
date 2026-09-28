"""Concurrent fetching, one writer (operator decision 2026-09-28: faster backfill, no collisions).

::

    source runner (1 thread per source)      fetchers (N threads per source)          writer (the calling thread)
    list -> upsert -> dedupe ------------->  claim (via writer) -> slot(host) ->     write_capture, store_decision,
    claims batches, finishes the run         fetch (token bucket) -> parse    ----->  after_store, failures, claims,
                                             bounded queue (backpressure)             WAL checkpoint + space guard

* The writer thread owns the database connection; every other thread reaches the database only through
  :meth:`Writer.call` (a command executed by the writer) or :meth:`Writer.submit` (a fetched document).
  Adapters' ``list_items`` get a :class:`WriterProxy` in place of a connection.
* Claims (``source_items.state='claimed'``, ``claimed_by``, ``lease_until``) are made by one UPDATE … RETURNING
  in the writer, so an item is never fetched twice concurrently; at start-up every claim is returned to
  ``pending`` (the writer lock proves that no other writer holds one), and claims not fetched when a run stops
  are returned too.
* Per host: :mod:`courts.hostgov` (shared slots + token bucket, AIMD).
* Court sites defer to RII before fetching (:func:`dedupe_before_fetch`); the store-time identity checks stay.
* First SIGTERM (``sync.STOP_AFTER_ITEM``): no new claims, documents in flight are finished and stored,
  unfetched claims are released.  The space/WAL guard (``courts.writer.Maintenance``) stops every source.
"""
from __future__ import annotations

import os
import queue
import socket
import sqlite3
import threading
import time
import traceback
from concurrent.futures import Future
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import UpstreamFailure
from .hostgov import Concurrency, Governors, _p95
from .sources.base import Adapter
from .store import (claim_items, finish_run, record_failure, register_source, relink, release_claims,
                    set_source_status, start_run, store_decision, upsert_items, utcnow, write_capture)

RESULT_QUEUE = 64          # fetched documents waiting for the writer (backpressure beyond it)
LEASE_SECONDS = 1800
PROGRESS_EVERY = 50        # items per source between sync_runs updates / log lines
METRICS_EVERY = 300.0      # seconds between writer/host metric lines
RELIST_AFTER_HOURS = 24.0  # a complete listing younger than this is reused (the frontier is already known)
# Court sites whose documents RII also publishes (with Randnummern): items RII already lists are not fetched.
DEFERS_TO = {"bverwg": ("rii", "bverwg"), "bsg": ("rii", "bsg"), "bag": ("rii", "bag"), "bverfg": ("rii", "bverfg")}
ECLI_COURT = {"bverwg": "BVerwG"}


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def concurrency_of(adapter: Adapter) -> Concurrency:
    value = getattr(adapter, "concurrency", None)
    return value if isinstance(value, Concurrency) else Concurrency(1, 2)


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower() or url


# --- the writer ---------------------------------------------------------------------------------------

@dataclass
class Job:
    run: "SourceRun"
    item: dict
    capture: object | None = None
    parsed: object | None = None
    error: BaseException | None = None


class Writer:
    def __init__(self, conn: sqlite3.Connection, maintenance, log, home=None):
        self.conn, self.maintenance, self.log, self.home = conn, maintenance, log, home
        self.commands: queue.Queue = queue.Queue()
        self.results: queue.Queue = queue.Queue(maxsize=RESULT_QUEUE)
        self.wake = threading.Event()
        self.thread = threading.current_thread()
        self.stop = threading.Event()          # space/WAL guard or a local I/O error: every source stops
        self.stop_reason: str | None = None
        self.jobs = 0
        self.busy = 0.0
        self.commit_times: list[float] = []
        self.started = time.monotonic()

    def call(self, fn, *args, **kwargs):
        """Run ``fn(conn, *args, **kwargs)`` in the writer thread and return its result."""
        if threading.current_thread() is self.thread:
            return fn(self.conn, *args, **kwargs)
        future: Future = Future()
        self.commands.put((fn, args, kwargs, future))
        self.wake.set()
        return future.result()

    def submit(self, job: Job) -> None:
        job.run.outstanding_add(1)
        self.results.put(job)   # blocks while RESULT_QUEUE documents wait: fetchers slow down, nothing is lost
        self.wake.set()

    def _commands(self) -> None:
        while True:
            try:
                fn, args, kwargs, future = self.commands.get_nowait()
            except queue.Empty:
                return
            try:
                future.set_result(fn(self.conn, *args, **kwargs))
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                future.set_exception(exc)

    def run(self, finished) -> None:
        """Serve commands and documents until ``finished()`` (polled at least once a second; it also starts
        queued sources) and nothing is waiting.  Commands and documents wake the writer at once."""
        last_metrics = last_poll = time.monotonic()
        while True:
            self.wake.clear()
            self._commands()
            if time.monotonic() - last_poll >= 1.0:
                last_poll = time.monotonic()
                finished()
            try:
                job = self.results.get_nowait()
            except queue.Empty:
                if self.commands.empty() and finished() and self.commands.empty() and self.results.empty():
                    return
                self._serve_inbox()
                self.wake.wait(0.2)
                continue
            started = time.monotonic()
            try:
                self._store(job)
            finally:
                job.run.outstanding_add(-1)
            elapsed = time.monotonic() - started
            self.busy += elapsed
            self.jobs += 1
            self.commit_times.append(elapsed)
            if len(self.commit_times) > 4000:
                del self.commit_times[:2000]
            problems = self.maintenance.tick()
            if problems and not self.stop.is_set():
                self.halt("space/WAL guard stopped the run: " + "; ".join(problems))
            if time.monotonic() - last_metrics >= METRICS_EVERY:
                last_metrics = time.monotonic()
                self.log_metrics()

    def _serve_inbox(self) -> None:
        """User-browser captures (courts.capture) go before the next document; tick() also serves them."""
        serve = getattr(self.maintenance, "serve_inbox", None)
        if serve is None or self.stop.is_set():
            return
        try:
            serve()
        except OSError as exc:
            self.halt(f"local I/O error in the capture inbox, run stopped: {type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - the inbox never stops the backfill
            self.log(f"writer: capture inbox: {type(exc).__name__}: {exc}")

    def halt(self, reason: str) -> None:
        self.stop_reason = reason[:500]
        self.stop.set()
        self.log(f"writer: {self.stop_reason}")

    def metrics(self) -> dict:
        wall = max(1e-9, time.monotonic() - self.started)
        times = self.commit_times[-2000:]
        return {"jobs": self.jobs, "busy_fraction": round(self.busy / wall, 3),
                "commit_ms_p50": round(1000 * sorted(times)[len(times) // 2], 1) if times else None,
                "commit_ms_p95": round(1000 * _p95(times), 1) if times else None,
                "queue_depth": self.results.qsize(), "items_per_hour": round(self.jobs / wall * 3600)}

    def log_metrics(self) -> None:
        lag = SourceRun.governors.lag_monitor.max_lag(METRICS_EVERY) if SourceRun.governors is not None \
            and SourceRun.governors.lag_monitor else None
        self.log(f"writer: {self.metrics()} process_lag_max_5min={lag if lag is None else round(lag, 2)}s")
        for run in list(SourceRun.active):
            self.log(f"source {run.adapter.source_id}: {run.progress()}")
        if SourceRun.governors is not None:
            for host, snap in SourceRun.governors.snapshot().items():
                self.log(f"host {host}: {snap}")

    # one fetched document -------------------------------------------------------------------------
    def _store(self, job: Job) -> None:
        from .sync import after_store
        conn, run, item = self.conn, job.run, job.item
        if self.stop.is_set():
            # The space/WAL guard or a local I/O error stopped the run: nothing more is written; the fetched
            # item goes back to pending (it is fetched again next time).
            release_claims(conn, keys=[(run.adapter.source_id, item["source_doc_id"])])
            return
        run.counts["attempted"] += 1
        content_hash = None
        try:
            if job.capture is not None:
                content_hash, relpath = write_capture(job.capture.body, run.adapter.source_id, job.capture.extension,
                                                      self.home, conn=conn)
            if job.error is not None:
                raise job.error
            decision_id, changed = store_decision(conn, job.parsed, content_hash=content_hash, capture_path=relpath,
                                                  media_type=job.capture.media_type, observed_at=utcnow())
            if changed:
                after_store(conn, decision_id)
            run.counts["stored" if changed else "unchanged"] += 1
            getattr(run.adapter, "on_stored", lambda _item: None)(item)
        except UpstreamFailure as failure:
            if failure.kind == "duplicate_identity":
                run.counts["duplicates"] = run.counts.get("duplicates", 0) + 1
                conn.execute("""UPDATE source_items SET state='skipped', attempts=attempts+1, last_attempt=?,
                                failure_kind='duplicate_identity', failure_detail=?, content_hash=?, claimed_by=NULL,
                                lease_until=NULL WHERE source_id=? AND source_doc_id=?""",
                             (utcnow(), failure.detail[:500], content_hash, run.adapter.source_id,
                              item["source_doc_id"]))
                getattr(run.adapter, "on_stored", lambda _item: None)(item)
                return
            run.counts["failed"] += 1
            if failure.url is None:
                failure.url = item["url"]
            record_failure(conn, run.run_id, run.adapter.source_id, item["source_doc_id"], failure)
            if content_hash:
                conn.execute("UPDATE source_items SET content_hash=? WHERE source_id=? AND source_doc_id=?",
                             (content_hash, run.adapter.source_id, item["source_doc_id"]))
            self.log(f"{run.adapter.source_id}: {item['source_doc_id']}: {failure.kind}: {failure.detail[:160]}")
        except OSError as exc:
            # ENOSPC and friends: not an upstream failure; the item goes back to pending, everything stops.
            release_claims(conn, keys=[(run.adapter.source_id, item["source_doc_id"])])
            self.halt(f"local I/O error, run stopped: {type(exc).__name__}: {exc}")
        except Exception as exc:  # parser defect: typed, recorded, never stored as a decision
            run.counts["failed"] += 1
            detail = f"{type(exc).__name__}: {exc} | " + "".join(
                traceback.format_exception(exc, limit=3)).replace("\n", " ")[-600:]
            record_failure(conn, run.run_id, run.adapter.source_id, item["source_doc_id"],
                           UpstreamFailure("parse_error", detail, url=item["url"]))
            if content_hash:
                conn.execute("UPDATE source_items SET content_hash=? WHERE source_id=? AND source_doc_id=?",
                             (content_hash, run.adapter.source_id, item["source_doc_id"]))
            self.log(f"{run.adapter.source_id}: {item['source_doc_id']}: parse_error: {exc}")
        finally:
            done = run.counts["attempted"]
            if done % PROGRESS_EVERY == 0:
                conn.execute("UPDATE sync_runs SET attempted=?, stored=?, unchanged=?, failed=?, requests=? WHERE id=?",
                             (done, run.counts["stored"], run.counts["unchanged"], run.counts["failed"],
                              run.requests(), run.run_id))
                self.log(f"{run.adapter.source_id}: {done}/{run.frontier} stored={run.counts['stored']} "
                         f"unchanged={run.counts['unchanged']} failed={run.counts['failed']}")


class WriterProxy:
    """What ``Adapter.list_items`` gets instead of a connection: every statement runs in the writer."""

    def __init__(self, writer: Writer):
        self.writer = writer

    def execute(self, sql: str, params=()):
        return self.writer.call(lambda conn: _Rows(conn.execute(sql, params)))

    def executemany(self, sql: str, rows):
        rows = list(rows)
        return self.writer.call(lambda conn: _Rows(conn.executemany(sql, rows)))

    def commit(self) -> None:
        self.writer.call(lambda conn: conn.commit())


class _Rows(list):
    def __init__(self, cursor):
        super().__init__(cursor.fetchall() if cursor.description else [])
        self.rowcount = cursor.rowcount

    def fetchone(self):
        return self[0] if self else None

    def fetchall(self):
        return list(self)


# --- one source -------------------------------------------------------------------------------------

@dataclass
class SourceSpec:
    adapter: Adapter
    http: object
    courts: list[str] | None = None


@dataclass
class SourceRun:
    spec: SourceSpec
    writer: Writer
    since: str | None
    limit: int | None
    retry_failed: bool
    relist_after: float
    worker: str
    run_id: int | None = None
    counts: dict = field(default_factory=lambda: {"listed": 0, "attempted": 0, "stored": 0, "unchanged": 0,
                                                  "failed": 0, "requests": 0})
    status: str = "ok"
    notes: str | None = None
    policy_note: str | None = None
    frontier: int = 0
    claimed: int = 0
    exhausted: bool = False
    consecutive_blocking: int = 0
    blocked: threading.Event = field(default_factory=threading.Event)
    buffer: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)            # claims buffer (fetchers)
    out_lock: threading.Lock = field(default_factory=threading.Lock)        # outstanding (writer + fetchers)
    outstanding: int = 0
    requests_before: int = 0
    started: float = field(default_factory=time.monotonic)
    result: dict | None = None
    phase: str = "starting"   # starting | listing | fetching | finishing

    active = set()           # runs in progress (metrics); class attributes, not fields
    governors = None

    def __hash__(self):
        return id(self)

    @property
    def adapter(self) -> Adapter:
        return self.spec.adapter

    def requests(self) -> int:
        return getattr(self.spec.http, "requests_made", 0) - self.requests_before

    def outstanding_add(self, n: int) -> None:
        # Never self.lock: a fetcher holds it while it waits for the writer to claim items.
        with self.out_lock:
            self.outstanding += n

    def progress(self) -> dict:
        hours = max(1e-9, (time.monotonic() - self.started) / 3600)
        done = self.counts["attempted"]
        return {"attempted": done, "stored": self.counts["stored"], "failed": self.counts["failed"],
                "frontier": self.frontier, "items_per_hour": round(done / hours), "status": self.status}

    def stopping(self) -> bool:
        from .sync import STOP_AFTER_ITEM
        return STOP_AFTER_ITEM.is_set() or self.writer.stop.is_set() or self.blocked.is_set()

    def next_item(self, batch: int):
        """The next claimed item for a fetcher, or None when the source is done (or stopping)."""
        with self.lock:
            while True:
                if self.stopping():
                    return None
                if self.buffer:
                    return self.buffer.pop(0)
                if self.exhausted or (self.limit is not None and self.claimed >= self.limit):
                    return None
                want = batch if self.limit is None else min(batch, self.limit - self.claimed)
                rows = self.writer.call(claim_items, self.adapter.source_id, self.worker, want,
                                        lease_seconds=LEASE_SECONDS, courts=self.spec.courts, since=self.since,
                                        retry_failed=self.retry_failed)
                self.claimed += len(rows)
                if len(rows) < want:
                    self.exhausted = True
                self.buffer.extend(dict(r) for r in rows)
                if not rows:
                    return None

    def fetched(self, error: BaseException | None) -> None:
        """Consecutive blocking fetch failures stop the source at once (not only when the writer gets to them):
        3 challenges / 429s / 503s / robots refusals in a row, whatever fetcher saw them."""
        from .sync import MAX_CONSECUTIVE_BLOCKING, is_blocking
        with self.lock:
            if isinstance(error, UpstreamFailure) and is_blocking(error):
                self.consecutive_blocking += 1
                if self.consecutive_blocking >= MAX_CONSECUTIVE_BLOCKING and not self.blocked.is_set():
                    self.status = "blocked"
                    self.notes = f"stopped after {self.consecutive_blocking} consecutive {error.kind}"
                    self.blocked.set()
            elif error is None or (isinstance(error, UpstreamFailure) and error.kind != "network"):
                self.consecutive_blocking = 0

    def give_back(self, items: list[dict]) -> None:
        if items:
            self.writer.call(release_claims, keys=[(self.adapter.source_id, i["source_doc_id"]) for i in items])


def _fresh_listing(conn: sqlite3.Connection, source_id: str, hours: float) -> dict | None:
    if hours <= 0:
        return None
    row = conn.execute("""SELECT r.id, r.started_at, r.listed FROM sync_runs r JOIN sources s USING(source_id)
                          WHERE r.source_id = ? AND r.listed > 0 AND s.listing_complete = 1
                            AND (r.notes IS NULL OR r.notes NOT LIKE 'listing incomplete%')
                            AND r.started_at >= strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now', ?)
                          ORDER BY r.id DESC LIMIT 1""", (source_id, f"-{int(hours * 3600)} seconds")).fetchone()
    return dict(row) if row else None


def dedupe_before_fetch(conn: sqlite3.Connection, source_id: str) -> int:
    """Court-site items that RII (with Randnummern) or the corpus already has are skipped before any request:
    same court, date and normalised Az, or the same ECLI (bverwg.de document ids are ECLI ordinals)."""
    from .casenumbers import case_key, split_case_numbers
    if source_id not in DEFERS_TO:
        return 0
    preferred, court = DEFERS_TO[source_id]
    covered: dict[tuple[str, str], str] = {}
    for decision_id, day, key in conn.execute(
            "SELECT d.id, d.decision_date, c.case_key FROM decisions d JOIN case_numbers c ON c.decision_id = d.id "
            "WHERE d.court_slug = ? AND d.source_id <> ?", (court, source_id)):
        covered.setdefault((day, key), decision_id)
    for doc, day, raw in conn.execute(
            "SELECT source_doc_id, listed_date, listed_case_numbers FROM source_items WHERE source_id = ? "
            "AND listed_court = ? AND state <> 'failed'", (preferred, court)):
        for number in split_case_numbers(raw or ""):
            covered.setdefault((day, case_key(number)), f"{preferred}:{doc}")
    eclis = {e for (e,) in conn.execute("SELECT ecli FROM decisions WHERE court_slug = ? AND ecli IS NOT NULL",
                                        (court,))}
    skipped = []
    for doc, day, raw in conn.execute("SELECT source_doc_id, listed_date, listed_case_numbers FROM source_items "
                                      "WHERE source_id = ? AND state = 'pending'", (source_id,)).fetchall():
        hit = next((covered[(day, case_key(n))] for n in split_case_numbers(raw or "") if (day, case_key(n)) in covered),
                   None)
        if hit is None and source_id in ECLI_COURT and day:
            ecli = f"ECLI:DE:{ECLI_COURT[source_id]}:{day[:4]}:{doc}"
            hit = ecli if ecli in eclis else None
        if hit:
            skipped.append((f"covered by {hit} (checked before fetching; RII preferred)", source_id, doc))
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany("UPDATE source_items SET state='skipped', failure_kind='duplicate_identity', failure_detail=? "
                         "WHERE source_id=? AND source_doc_id=? AND state='pending'", skipped)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return len(skipped)


def run_source(run: SourceRun, governors: Governors, log) -> None:
    from .sync import is_blocking
    writer, adapter, http = run.writer, run.adapter, run.spec.http
    source_id = adapter.source_id
    SourceRun.active.add(run)
    try:
        writer.call(register_source, source_id, adapter.label, adapter.base_url, adapter.land, adapter.adapter_name,
                    adapter.operator_policy())
        run.run_id = writer.call(start_run, source_id, run.since, run.limit)
        run.requests_before = getattr(http, "requests_made", 0)
        if adapter.robots_policy != "respect":
            run.policy_note = f"robots: {adapter.robots_policy} ({adapter.policy_decided_at}, operator decision)"
        writer.call(lambda c: c.execute("UPDATE sources SET status='partial', status_reason='first sync in progress' "
                                        "WHERE source_id=? AND status='never_synced'", (source_id,)))
        reuse = None if (run.since or run.limit) else writer.call(_fresh_listing, source_id, run.relist_after)
        if reuse:
            log(f"{source_id}: listing of run {reuse['id']} ({reuse['started_at']}, {reuse['listed']} items) reused")
            run.notes = f"listing reused from run {reuse['id']}"
        else:
            try:
                run.phase = "listing"
                listing = adapter.list_items(http, WriterProxy(writer), since=run.since, limit=run.limit,
                                             courts=run.spec.courts)
            except UpstreamFailure as failure:
                writer.call(record_failure, run.run_id, source_id, None, failure)
                blocked = is_blocking(failure)
                writer.call(set_source_status, source_id, "blocked" if blocked else "unavailable",
                            f"listing failed: {failure.kind}: {failure.detail}"[:500])
                run.status, run.notes = ("blocked" if blocked else "failed"), f"listing: {failure}"
                log(f"{source_id}: listing failed: {failure}")
                return
            if not listing.complete:
                run.notes = "listing incomplete"
            elif run.limit is None and run.since is None:
                writer.call(lambda c: c.execute("UPDATE sources SET listing_complete=1 WHERE source_id=?", (source_id,)))
            run.counts["listed"] = writer.call(upsert_items, source_id, listing.items)
            if listing.upstream_total is not None:
                writer.call(lambda c: c.execute("UPDATE sources SET upstream_listed=? WHERE source_id=?",
                                                (listing.upstream_total, source_id)))
        writer.call(lambda c: c.execute("""UPDATE sources SET status='partial', status_reason=? WHERE source_id=?
                                           AND status IN ('blocked','unavailable','never_synced','partial')""",
                                        (f"sync in progress (run {run.run_id})", source_id)))
        deduped = writer.call(dedupe_before_fetch, source_id)
        if deduped:
            run.counts["deduped_before_fetch"] = deduped
            log(f"{source_id}: {deduped} items covered by RII or the corpus, not fetched")
        run.frontier = writer.call(lambda c: c.execute(
            "SELECT count(*) FROM source_items WHERE source_id=? AND state IN (?, ?)",
            (source_id, "pending", "failed" if run.retry_failed else "pending")).fetchone()[0])
        if run.limit is not None:
            run.frontier = min(run.frontier, run.limit)
        log(f"{source_id}: listed {run.counts['listed']}, fetching {run.frontier}")
        run.phase = "fetching"
        concurrency = concurrency_of(adapter)
        slots = governors.get(host_of(adapter.base_url))
        fetchers = [threading.Thread(target=_fetcher, args=(run, slots, concurrency.maximum),
                                     name=f"fetch-{source_id}-{n}", daemon=True) for n in range(concurrency.maximum)]
        for thread in fetchers:
            thread.start()
        for thread in fetchers:
            thread.join()
        with run.lock:
            left, run.buffer = run.buffer, []
        run.give_back(left)
        while run.outstanding:       # the writer still stores this source's documents
            time.sleep(0.02)
        if run.blocked.is_set():
            writer.call(set_source_status, source_id, "blocked", run.notes)
            log(f"{source_id}: {run.notes}")
        writer.call(relink)
        if run.status == "ok":
            if writer.stop.is_set():
                run.status, run.notes = "failed", writer.stop_reason
            else:
                from .sync import STOP_AFTER_ITEM
                if STOP_AFTER_ITEM.is_set():
                    run.status, run.notes = "interrupted", "stopped after the current items (SIGTERM)"
                    log(f"{source_id}: {run.notes}")
        if run.status == "ok" and run.counts["failed"]:
            run.status = "partial"
        if run.status in ("ok", "partial"):
            writer.call(set_source_status, source_id, "ok" if run.status == "ok" else "partial",
                        None if run.status == "ok" else f"{run.counts['failed']} item failures in run {run.run_id}")
    except BaseException as exc:  # noqa: BLE001 - the run is recorded, the other sources go on
        run.status, run.notes = "failed", f"{type(exc).__name__}: {exc}"[:500]
        log(f"{source_id}: run failed: {run.notes}")
    finally:
        run.counts["requests"] = run.requests()
        if run.run_id is not None:
            try:
                writer.call(finish_run, run.run_id, source_id, run.status, run.counts,
                            "; ".join(n for n in (run.notes, run.policy_note) if n) or None)
            except Exception as exc:  # noqa: BLE001
                log(f"{source_id}: finish_run failed: {exc}")
        run.result = run.counts | {"status": run.status, "run_id": run.run_id}
        SourceRun.active.discard(run)


def _fetcher(run: SourceRun, slots, batch: int) -> None:
    adapter = run.adapter
    while True:
        item = run.next_item(max(4, 2 * batch))
        if item is None:
            return
        job = Job(run, item)
        with slots.slot():          # documents in flight for the host (adaptive)
            if run.stopping():
                run.give_back([item])
                return
            try:
                job.capture = adapter.fetch(run.spec.http, item)
            except BaseException as exc:  # noqa: BLE001 - typed and recorded by the writer
                job.error = exc
            run.fetched(job.error)
        if job.error is None:
            try:
                job.parsed = adapter.parse(job.capture, item)
            except BaseException as exc:  # noqa: BLE001
                job.error = exc
        run.writer.submit(job)


# --- the whole run -----------------------------------------------------------------------------------

def run_pipeline(conn: sqlite3.Connection, specs: list[SourceSpec], *, since=None, limit=None, retry_failed=False,
                 maintenance=None, home=None, relist_after: float = RELIST_AFTER_HOURS, max_sources: int | None = None,
                 queued: list[SourceSpec] | None = None, governors: Governors | None = None, log=None) -> dict:
    """Sync ``specs`` concurrently (``queued`` ones start as earlier sources finish, at most ``max_sources``
    at a time).  Returns {source_id: result}.  Must be called by the writer (the thread holding ``conn``)."""
    from .sync import log as default_log
    from .writer import Maintenance, checkpoint
    log = log or default_log
    # Checkpoint + space guard every 200 documents or 60 s (the btrfs probes are subprocesses).
    maintenance = maintenance or Maintenance(conn, home, every_items=200, every_seconds=60)
    if getattr(maintenance, "log", "absent") is None:
        maintenance.log = log
    governors = governors or Governors(log=log, monitor_lag=True)
    for spec in [*specs, *(queued or [])]:
        governors.declare(host_of(spec.adapter.base_url), concurrency_of(spec.adapter), spec.adapter.min_interval)
        http = spec.http
        if getattr(http, "governors", "absent") is None:
            http.governors = governors
    SourceRun.governors = governors
    writer = Writer(conn, maintenance, log, home)
    released = release_claims(conn)
    if released:
        log(f"writer: {released} claims of an earlier writer returned to pending")
    worker = worker_id()
    waiting = list(queued or [])
    limit_sources = max_sources or len(specs) + len(waiting)
    runs: dict[str, SourceRun] = {}
    threads: list[threading.Thread] = []

    def start(spec: SourceSpec) -> None:
        run = SourceRun(spec, writer, since, limit, retry_failed, relist_after, f"{worker}/{spec.adapter.source_id}")
        runs[spec.adapter.source_id] = run
        thread = threading.Thread(target=run_source, args=(run, governors, log),
                                  name=f"source-{spec.adapter.source_id}", daemon=True)
        thread.start()
        threads.append(thread)

    for spec in specs[:limit_sources]:
        start(spec)
    waiting = specs[limit_sources:] + waiting

    def finished() -> bool:
        from .sync import STOP_AFTER_ITEM
        alive = [t for t in threads if t.is_alive()]
        while waiting and len(alive) < limit_sources and not STOP_AFTER_ITEM.is_set() and not writer.stop.is_set():
            start(waiting.pop(0))
            alive = [t for t in threads if t.is_alive()]
        if alive and (STOP_AFTER_ITEM.is_set() or writer.stop.is_set()):
            # A listing cannot be cut short and has nothing in flight: such runs are recorded as interrupted
            # (the finally below) and their threads end with the process.
            listing = {r.adapter.source_id for r in runs.values() if r.phase in ("starting", "listing")}
            return all(t.name.removeprefix("source-") in listing for t in alive)
        return not alive

    try:
        writer.run(finished)
    finally:
        # A second SIGTERM (KeyboardInterrupt) lands here with fetchers still running: their claims go back
        # and the unfinished runs are recorded as interrupted.
        _release_worker(conn, worker)
        for run in runs.values():
            if run.result is None and run.run_id is not None:
                note = "listing abandoned (SIGTERM)" if run.phase in ("starting", "listing") else \
                    "interrupted at once (second SIGTERM)"
                try:
                    finish_run(conn, run.run_id, run.adapter.source_id, "interrupted", run.counts, note)
                except Exception:  # noqa: BLE001
                    pass
        checkpoint(conn)
        writer.log_metrics()
    results = {source_id: run.result or {"status": "failed", "detail": "run did not finish"}
               for source_id, run in runs.items()}
    for spec in waiting:
        results[spec.adapter.source_id] = {"status": "not_started",
                                           "detail": "stopped before its turn (SIGTERM or space/WAL guard)"}
    results["_writer"] = writer.metrics() | {"hosts": governors.snapshot()}
    return results


def _release_worker(conn: sqlite3.Connection, worker: str) -> None:
    conn.execute("UPDATE source_items SET state='pending', claimed_by=NULL, lease_until=NULL "
                 "WHERE state='claimed' AND claimed_by LIKE ?", (worker + "/%",))
    conn.commit()
