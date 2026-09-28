"""Incremental, prioritised, rate-limited synchronisation of one source."""
from __future__ import annotations

import contextlib
import fcntl
import os
import sqlite3
import sys
import time
import traceback
from pathlib import Path

from .config import data_home
from .errors import UpstreamFailure
from .http import PoliteHttp
from .sources.base import Adapter
from .store import (finish_run, record_failure, register_source, relink, select_frontier,
                    set_source_status, start_run, store_decision, upsert_items, utcnow, write_capture)

# Failures that mean "the source is refusing us": stop instead of hammering.
BLOCKING = {"challenge", "login", "robots_disallowed", "rate_limited"}
MAX_CONSECUTIVE_BLOCKING = 3


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


def _result(counts: dict, status: str, run_id: int, http: PoliteHttp, requests_before: int) -> dict:
    counts["requests"] = http.requests_made - requests_before
    return counts | {"status": status, "run_id": run_id}


class SourceBusy(RuntimeError):
    """Another process is already syncing this source (its per-host pacing would be bypassed)."""


@contextlib.contextmanager
def source_lock(source_id: str, home=None):
    """Exclusive per-source lock: two processes on one host would double the request rate."""
    directory = Path(home or data_home()) / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    with open(directory / f"{source_id}.lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SourceBusy(f"source {source_id!r} is already being synced by another process") from exc
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def sync_source(adapter: Adapter, conn: sqlite3.Connection, http: PoliteHttp, *, since: str | None = None,
                limit: int | None = None, courts: list[str] | None = None, retry_failed: bool = False,
                home=None) -> dict:
    with source_lock(adapter.source_id, home):
        return _sync_source(adapter, conn, http, since=since, limit=limit, courts=courts,
                            retry_failed=retry_failed, home=home)


def _sync_source(adapter: Adapter, conn: sqlite3.Connection, http: PoliteHttp, *, since: str | None,
                 limit: int | None, courts: list[str] | None, retry_failed: bool, home) -> dict:
    register_source(conn, adapter.source_id, adapter.label, adapter.base_url, adapter.land, adapter.adapter_name)
    run_id = start_run(conn, adapter.source_id, since, limit)
    conn.execute("UPDATE sources SET status='partial', status_reason='first sync in progress' "
                 "WHERE source_id=? AND status='never_synced'", (adapter.source_id,))
    counts = {"listed": 0, "attempted": 0, "stored": 0, "unchanged": 0, "failed": 0, "requests": 0}
    requests_before = http.requests_made
    status, notes = "ok", None
    try:
        try:
            listing = adapter.list_items(http, conn, since=since, limit=limit, courts=courts)
        except UpstreamFailure as failure:
            record_failure(conn, run_id, adapter.source_id, None, failure)
            blocked = failure.kind in BLOCKING
            set_source_status(conn, adapter.source_id, "blocked" if blocked else "unavailable",
                              f"listing failed: {failure.kind}: {failure.detail}"[:500])
            status, notes = ("blocked" if blocked else "failed"), f"listing: {failure}"
            log(f"{adapter.source_id}: listing failed: {failure}")
            return _result(counts, status, run_id, http, requests_before)
        if not listing.complete:
            # Incremental adapters treat only runs with a complete listing as a baseline.
            notes = "listing incomplete"
        elif limit is None and since is None:
            # The whole upstream archive was listed at least once: from now on "no pending
            # items" really means the local corpus covers the source.
            conn.execute("UPDATE sources SET listing_complete=1 WHERE source_id=?", (adapter.source_id,))
        counts["listed"] = upsert_items(conn, adapter.source_id, listing.items)
        if listing.upstream_total is not None:
            conn.execute("UPDATE sources SET upstream_listed=? WHERE source_id=?",
                         (listing.upstream_total, adapter.source_id))
        frontier = select_frontier(conn, adapter.source_id, since=since, limit=limit, courts=courts,
                                   retry_failed=retry_failed)
        log(f"{adapter.source_id}: listed {counts['listed']} (upstream {listing.upstream_total}), "
            f"fetching {len(frontier)}")
        consecutive_blocking = 0
        for position, item in enumerate(frontier, 1):
            counts["attempted"] += 1
            try:
                capture = adapter.fetch(http, item)
                content_hash, relpath = write_capture(capture.body, adapter.source_id, capture.extension, home)
                parsed = adapter.parse(capture, item)
                _, changed = store_decision(conn, parsed, content_hash=content_hash, capture_path=relpath,
                                            media_type=capture.media_type, observed_at=utcnow())
                counts["stored" if changed else "unchanged"] += 1
                consecutive_blocking = 0
            except UpstreamFailure as failure:
                counts["failed"] += 1
                if failure.url is None:
                    failure.url = item["url"]
                record_failure(conn, run_id, adapter.source_id, item["source_doc_id"], failure)
                log(f"{adapter.source_id}: {item['source_doc_id']}: {failure.kind}: {failure.detail[:160]}")
                consecutive_blocking = consecutive_blocking + 1 if failure.kind in BLOCKING else 0
                if consecutive_blocking >= MAX_CONSECUTIVE_BLOCKING:
                    status, notes = "blocked", f"stopped after {consecutive_blocking} consecutive {failure.kind}"
                    set_source_status(conn, adapter.source_id, "blocked", notes)
                    log(f"{adapter.source_id}: {notes}")
                    break
            except OSError as exc:
                # A local I/O error (e.g. ENOSPC) is not an upstream failure: stop the run and leave
                # the item pending instead of recording a misleading parse_error for it.
                status, notes = "failed", f"local I/O error, run stopped: {type(exc).__name__}: {exc}"[:500]
                log(f"{adapter.source_id}: {notes}")
                break
            except Exception as exc:  # parser defect: typed, recorded, never stored as a decision
                counts["failed"] += 1
                detail = f"{type(exc).__name__}: {exc} | " + traceback.format_exc(limit=3).replace("\n", " ")[-600:]
                record_failure(conn, run_id, adapter.source_id, item["source_doc_id"],
                               UpstreamFailure("parse_error", detail, url=item["url"]))
                log(f"{adapter.source_id}: {item['source_doc_id']}: parse_error: {exc}")
            if position % 50 == 0:
                log(f"{adapter.source_id}: {position}/{len(frontier)} stored={counts['stored']} "
                    f"unchanged={counts['unchanged']} failed={counts['failed']}")
        relink(conn)
        if status == "ok" and counts["failed"]:
            status = "partial"
        if status == "failed":
            return _result(counts, status, run_id, http, requests_before)
        if status != "blocked":
            set_source_status(conn, adapter.source_id, "ok" if status == "ok" else "partial",
                              None if status == "ok" else f"{counts['failed']} item failures in run {run_id}")
        return _result(counts, status, run_id, http, requests_before)
    except KeyboardInterrupt:
        status, notes = "interrupted", "interrupted by user"
        raise
    finally:
        counts["requests"] = http.requests_made - requests_before
        finish_run(conn, run_id, adapter.source_id, status, counts, notes)


def reparse_source(adapter: Adapter, conn: sqlite3.Connection, *, home=None) -> dict:
    """Re-parse stored raw captures with the current parser (no network).

    Decisions whose text and parser version are unchanged are left alone; a
    capture that no longer parses is recorded as a typed failure and the old
    record is kept.
    """
    from .sources.base import Capture
    from .store import read_capture
    counts = {"reparsed": 0, "unchanged": 0, "failed": 0}
    rows = conn.execute("""SELECT d.id, d.content_hash, d.capture_path, d.media_type, d.source_url, i.*
                           FROM decisions d JOIN source_items i
                             ON i.source_id = d.source_id AND i.source_doc_id = d.source_doc_id
                           WHERE d.source_id = ?""", (adapter.source_id,)).fetchall()
    for row in rows:
        try:
            body = read_capture(row["capture_path"], row["content_hash"], home)
            extension = row["capture_path"].rsplit(".", 2)[-2]
            capture = Capture(body, row["source_url"], row["media_type"] or "", extension)
            parsed = adapter.parse(capture, row)
            _, changed = store_decision(conn, parsed, content_hash=row["content_hash"],
                                        capture_path=row["capture_path"], media_type=row["media_type"] or "")
            counts["reparsed" if changed else "unchanged"] += 1
        except UpstreamFailure as failure:
            counts["failed"] += 1
            log(f"{adapter.source_id}: reparse {row['id']}: {failure.kind}: {failure.detail[:160]}")
        except Exception as exc:  # keep the old record; report the parser defect
            counts["failed"] += 1
            log(f"{adapter.source_id}: reparse {row['id']}: {type(exc).__name__}: {exc}")
    relink(conn)
    return counts
