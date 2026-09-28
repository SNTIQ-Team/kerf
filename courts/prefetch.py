"""Fetch now, store later: network work for a source while another writer holds the corpus.

The corpus has one writer (courts.writer).  While a long backfill runs, a new source can still do its slow
part - listing and fetching - without touching the database: ``python -m courts prefetch --source wayback``
reads the corpus read-only (listing, dedupe), fetches every item's capture with the source's own politeness
(PoliteHttp + per-host governor) and stages the exact bytes under ``<data home>/staging/<source>/`` (one
``<key>.body`` + ``<key>.json`` per item, written atomically).  The next writer that syncs the source
(``sync --source wayback``) takes a staged capture instead of fetching it again (``staged_capture``) and
stores it like any other; the staged files are removed once stored.

Only sources that declare ``prefetch_ok = True`` (their fetch has no side effects on the corpus) may be
prefetched.  The space guard runs before and every 100 items.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from .config import data_home, db_path
from .errors import UpstreamFailure
from .sources.base import Capture


def staging_dir(source_id: str, home: Path | None = None) -> Path:
    return Path(home or data_home()) / "staging" / source_id


def _key(source_doc_id: str) -> str:
    return hashlib.sha256(source_doc_id.encode()).hexdigest()[:32]


def _write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def stage(source_id: str, item: dict, capture: Capture, home: Path | None = None) -> Path:
    directory = staging_dir(source_id, home)
    directory.mkdir(parents=True, exist_ok=True)
    key = _key(item["source_doc_id"])
    _write(directory / f"{key}.body", capture.body)
    meta = {"source_doc_id": item["source_doc_id"], "url": capture.url, "media_type": capture.media_type,
            "extension": capture.extension, "headers": capture.headers, "sha256": hashlib.sha256(capture.body).hexdigest(),
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    _write(directory / f"{key}.json", json.dumps(meta, ensure_ascii=False).encode())
    return directory / f"{key}.json"


def staged_capture(source_id: str, item, home: Path | None = None) -> Capture | None:
    """The staged capture of ``item`` (verified against its sha256), or None."""
    directory = staging_dir(source_id, home)
    key = _key(item["source_doc_id"])
    try:
        meta = json.loads((directory / f"{key}.json").read_text(encoding="utf-8"))
        body = (directory / f"{key}.body").read_bytes()
    except (OSError, ValueError):
        return None
    if meta.get("source_doc_id") != item["source_doc_id"] or hashlib.sha256(body).hexdigest() != meta.get("sha256"):
        return None
    return Capture(body=body, url=meta["url"], media_type=meta["media_type"], extension=meta["extension"],
                   headers=dict(meta.get("headers") or {}))


def discard_staged(source_id: str, source_doc_id: str, home: Path | None = None) -> None:
    directory = staging_dir(source_id, home)
    key = _key(source_doc_id)
    for suffix in (".json", ".body"):
        (directory / f"{key}{suffix}").unlink(missing_ok=True)


def _space_problems(home: Path) -> list[str]:
    from .space import check as check_space
    return check_space(home, starting=True).problems


def prefetch(adapter, http, *, home: Path | None = None, since=None, limit=None, courts=None, log=None,
             workers: int = 2, readonly_conn: sqlite3.Connection | None = None) -> dict:
    """List (read-only) and fetch every item not staged yet; returns counts."""
    home = Path(home or data_home())
    log = log or (lambda message: None)
    if not getattr(adapter, "prefetch_ok", False):
        raise ValueError(f"source {adapter.source_id!r} does not support prefetching")
    problems = _space_problems(home)
    if problems:
        return {"status": "waiting_for_space", "problems": problems}
    conn = readonly_conn or sqlite3.connect(f"file:{db_path(home)}?mode=ro", uri=True, timeout=60)
    listing = adapter.list_items(http, conn, since=since, limit=limit, courts=courts)
    items = [i for i in listing.items if staged_capture(adapter.source_id, i, home) is None]
    counts = {"listed": len(listing.items), "already_staged": len(listing.items) - len(items), "staged": 0,
              "failed": 0, "failures": {}}
    log(f"{adapter.source_id}: prefetch: {len(listing.items)} listed, {len(items)} to fetch")
    lock = threading.Lock()
    queue = list(reversed(items))
    stop = threading.Event()

    def work() -> None:
        while not stop.is_set():
            with lock:
                if not queue:
                    return
                item = queue.pop()
            try:
                capture = adapter.fetch(http, item)
                stage(adapter.source_id, item, capture, home)
                with lock:
                    counts["staged"] += 1
            except UpstreamFailure as failure:
                with lock:
                    counts["failed"] += 1
                    counts["failures"][failure.kind] = counts["failures"].get(failure.kind, 0) + 1
                if failure.kind in ("challenge", "login", "rate_limited", "robots_disallowed"):
                    log(f"{adapter.source_id}: prefetch stopped: {failure.kind}: {failure.detail[:160]}")
                    stop.set()
            done = counts["staged"] + counts["failed"]
            if done % 100 == 0:
                log(f"{adapter.source_id}: prefetch {done}/{len(items)} staged={counts['staged']} "
                    f"failed={counts['failed']}")
                problems = _space_problems(home)
                if problems:
                    counts["stopped"] = "space guard: " + "; ".join(problems)
                    stop.set()

    threads = [threading.Thread(target=work, name=f"prefetch-{n}", daemon=True) for n in range(max(1, workers))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    counts["status"] = "stopped" if stop.is_set() and queue else "ok"
    return counts
