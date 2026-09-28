"""Single-page ingest of user-browser captures (operator decision 2026-09-28).

Some sources answer automated requests with a bot challenge (gesetze-bayern.de: Myra Security).  The corpus
never bypasses a challenge.  On an explicit request the MCP agent opens ONE page in the operator's Chrome;
the operator may solve the challenge there; the saved page (HTML, or the portal's PDF) is handed over::

    python -m courts ingest-capture --source by --url <canonical URL> --file <page.html|page.pdf> \
        [--captured-at ISO-8601]

or from Python: :func:`ingest_capture`.  Contract (SCHEMA.md "User-browser captures"):

* success: one JSON object on stdout with ``decision_id`` (plus ``warnings``, ``provenance``, ``ticket`` …),
  exit 0;
* refusal: one JSON line on stderr, ``status`` ∈ ``invalid_argument`` (exit 2), ``busy`` (exit 3),
  ``rate_limited`` (exit 5), with ``detail``, ``reason`` and, for ``busy``/``rate_limited``,
  ``retry_after_s``.

Rules:

* **one page per call** - one ``--url``, one ``--file`` (a regular file); lists, directories and search or
  listing URLs are refused (``reason: bulk_refused`` / ``not_a_decision_url``);
* **rate guard** - at most one ingest every ``MIN_INTERVAL`` (10 s) and ``MAX_PER_DAY`` (100) in any 24 hours,
  across sources, counted in ``<data home>/inbox/ledger.jsonl``; every attempt that reaches the content checks
  counts (a rejected challenge page too); handing in the same bytes for the same URL again is not a new
  ingest (it returns, or waits for, the first result);
* **challenge pages are rejected** (``courts.detect``; ``reason: challenge``), as are pages that are not a
  decision (``not_a_decision_page``) or name another document id than the URL (``url_mismatch``);
* **provenance** - the exact bytes go into the source's zstd pack like any capture; ``source_items.extra`` and
  ``decisions.metadata`` carry ``capture_origin: "user_browser"`` with ``captured_at`` (``--captured-at``,
  else the file's mtime), ``ingested_at``, the URL as handed in, media type and sha256;
* **one writer** - the ingest never needs the writer lock while another writer runs: the capture goes into
  the inbox (``<data home>/inbox/pending/``: the bytes plus a JSON manifest, written atomically), the running
  writer (sync pipeline, reparse, classify) ingests it with top priority between documents and writes
  ``inbox/results/<ticket>.json``; the call waits up to ``WAIT_SECONDS`` (60 s) for it.  With no writer
  running, the call takes the writer lock and ingests directly.  A writer that does not serve the inbox
  (compact, migrate-schema, verify) makes the call return ``busy`` after the wait; the item stays queued and
  the next writer (or the same call repeated) ingests it.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import data_home
from .errors import UpstreamFailure

MIN_INTERVAL = 10.0
MAX_PER_DAY = 100
DAY = 86400.0
MAX_BYTES = 25 * 1024 ** 2
WAIT_SECONDS = 60.0
POLL_SECONDS = 0.5
RESULT_RETENTION_DAYS = 30
EXIT_CODES = {"ok": 0, "invalid_argument": 2, "busy": 3, "rate_limited": 5}


class CaptureRefused(Exception):
    """A refused ingest; ``as_dict()`` is the stderr JSON line."""

    def __init__(self, status: str, detail: str, *, reason: str | None = None,
                 retry_after_s: float | None = None, **extra) -> None:
        if status not in ("invalid_argument", "busy", "rate_limited"):
            raise ValueError(f"unknown refusal status {status!r}")
        super().__init__(f"{status}: {detail}")
        self.status, self.detail, self.reason, self.retry_after_s, self.extra = \
            status, detail, reason, retry_after_s, extra

    def as_dict(self) -> dict:
        out = {"status": self.status, "detail": self.detail}
        if self.reason:
            out["reason"] = self.reason
        if self.retry_after_s is not None:
            out["retry_after_s"] = round(max(0.0, self.retry_after_s), 1)
        return out | self.extra


# --- sources that accept user-browser captures ----------------------------------------------------------

@dataclass(frozen=True)
class CaptureSource:
    source_id: str
    label: str
    canonical_url: object       # url -> (doc id, canonical url); ValueError otherwise
    parse: object               # (body, item, url) -> ParsedDecision
    page_doc_ids: object        # body -> set of document ids the page names
    route: str


CAPTURE_SOURCE_IDS = ("by", "bverwg")
LEGACY_WRITER_SOURCES = ("by",)      # writers started before the lock named its capture sources


def running_writer_sources(home: Path | None = None) -> tuple[str, ...] | None:
    """Capture sources the writer that holds the lock can store; None when no writer runs."""
    path = Path(home or data_home()) / ".writer.lock"
    try:
        handle = open(path, "r+")
    except FileNotFoundError:
        return None
    with handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            text = handle.read()
            m = re.search(r"^capture-sources (.*)$", text, re.M)
            return tuple(x for x in m.group(1).split(",") if x) if m else LEGACY_WRITER_SOURCES
        fcntl.flock(handle, fcntl.LOCK_UN)
        return None


def capture_sources() -> dict[str, CaptureSource]:
    from .sources import bverwg, by_portal
    return {"by": CaptureSource("by", "BAYERN.RECHT (gesetze-bayern.de) decision page", by_portal.canonical_url,
                                by_portal.parse_capture, by_portal.page_doc_ids, by_portal.ROUTE),
            "bverwg": CaptureSource("bverwg", "bverwg.de decision page", bverwg.capture_canonical_url,
                                    bverwg.capture_parse, bverwg.capture_page_doc_ids, "user_capture")}


# --- preparation (no database) --------------------------------------------------------------------------

@dataclass
class Prepared:
    source: CaptureSource
    url_given: str
    url: str
    doc_id: str
    body: bytes
    media_type: str
    extension: str
    sha256: str
    captured_at: str
    captured_at_basis: str
    parsed: object = None
    warnings: list[str] = field(default_factory=list)

    @property
    def ticket(self) -> str:
        return ticket_for(self.body, self.url)


def ticket_for(body: bytes, url: str) -> str:
    return hashlib.sha256(body + b"\n" + url.encode()).hexdigest()[:24]


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_captured_at(value: str | None, *, now: float) -> str:
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise CaptureRefused("invalid_argument", f"--captured-at is not ISO 8601: {value!r}",
                             reason="captured_at_invalid") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if moment.timestamp() > now + 300:
        raise CaptureRefused("invalid_argument", f"--captured-at lies in the future: {value!r}",
                             reason="captured_at_in_future")
    return utc_iso(moment)


def check_arguments(source: str, url: str, body, *, captured_at: str | None, captured_at_basis: str | None,
                    now: float) -> Prepared:
    """Argument-level checks (nothing counted yet): one page, a known source, a single-decision URL."""
    if isinstance(body, (list, tuple, set, dict)) or isinstance(url, (list, tuple, set)):
        raise CaptureRefused("invalid_argument", "one page per call: bulk ingest is refused", reason="bulk_refused")
    if not isinstance(body, (bytes, bytearray)):
        raise CaptureRefused("invalid_argument", "the capture must be the page's bytes", reason="invalid_body")
    sources = capture_sources()
    if source not in sources:
        raise CaptureRefused("invalid_argument", f"source {source!r} does not accept user-browser captures "
                             f"(accepted: {', '.join(sorted(sources))})", reason="unknown_source")
    handler = sources[source]
    try:
        doc_id, canonical = handler.canonical_url(url)
    except ValueError as exc:
        raise CaptureRefused("invalid_argument", str(exc), reason="not_a_decision_url") from None
    body = bytes(body)
    if not body.strip():
        raise CaptureRefused("invalid_argument", "the capture is empty", reason="empty_capture")
    if len(body) > MAX_BYTES:
        raise CaptureRefused("invalid_argument", f"the capture has {len(body)} bytes (> {MAX_BYTES})",
                             reason="too_large")
    from .detect import looks_like_html
    if body.startswith(b"%PDF"):
        media_type, extension = "application/pdf", "pdf"
    elif looks_like_html(body):
        media_type, extension = "text/html", "html"
    else:
        raise CaptureRefused("invalid_argument", "the capture is neither HTML nor PDF", reason="unsupported_media")
    warnings = []
    if captured_at:
        stamp = parse_captured_at(captured_at, now=now)
        basis = captured_at_basis or "given"
    else:
        stamp, basis = utc_iso(datetime.fromtimestamp(now, timezone.utc)), "ingest_time"
        warnings.append("captured_at not given: the ingest time is recorded")
    return Prepared(handler, url, canonical, doc_id, body, media_type, extension, hashlib.sha256(body).hexdigest(),
                    stamp, basis, warnings=warnings)


def check_content(p: Prepared) -> Prepared:
    """Content checks (counted by the rate guard): no challenge, a decision page, the URL's document."""
    from .detect import classify_page
    from .store import finalise
    if p.media_type == "text/html":
        verdict = classify_page(p.body)
        if verdict:
            kind, evidence = verdict
            raise CaptureRefused("invalid_argument", f"the capture is a {kind} page, not the decision ({evidence}); "
                                 "solve the challenge in the browser and save the page again", reason=kind)
        named = p.source.page_doc_ids(p.body)
        if named and p.doc_id not in named:
            raise CaptureRefused("invalid_argument", f"the page is document {sorted(named)[0]}, the URL names "
                                 f"{p.doc_id}", reason="url_mismatch")
        if not named:
            p.warnings.append("the page names no document id: the URL is taken as given")
    item = {"source_doc_id": p.doc_id, "url": p.url, "listed_date": None, "listed_court": None,
            "listed_case_numbers": None, "extra": {"route": p.source.route}}
    try:
        p.parsed = finalise(p.source.parse(p.body, item, p.url))
    except UpstreamFailure as failure:
        raise CaptureRefused("invalid_argument", f"not a decision page of {p.source.label}: {failure.kind}: "
                             f"{failure.detail}", reason="not_a_decision_page") from None
    beck = p.parsed.metadata.get("beckrs")
    m = re.fullmatch(r"Y-300-Z-BECKRS-B-(\d{4})-N-0*(\d+)", p.doc_id)
    if beck and m and re.sub(r"\s", "", beck) != f"BeckRS{m.group(1)},{m.group(2)}":
        raise CaptureRefused("invalid_argument", f"the page is {beck}, the URL names {p.doc_id}", reason="url_mismatch")
    for key, label in (("images_omitted", "images without text were omitted"),
                       ("rn_sequence_irregular", "the Randnummern do not run 1, 2, 3 …")):
        if p.parsed.metadata.get(key):
            p.warnings.append(f"{label}: {json.dumps(p.parsed.metadata[key], ensure_ascii=False)[:200]}")
    return p


# --- inbox and rate guard ----------------------------------------------------------------------------------

def inbox_dir(home: Path | None = None) -> Path:
    return Path(home or data_home()) / "inbox"


def _dirs(home: Path | None) -> tuple[Path, Path, Path]:
    root = inbox_dir(home)
    pending, results, failed = root / "pending", root / "results", root / "failed"
    for d in (pending, results, failed):
        d.mkdir(parents=True, exist_ok=True)
    return pending, results, failed


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


@contextlib.contextmanager
def _guard_lock(home: Path | None):
    root = inbox_dir(home)
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".guard.lock", "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def ledger_entries(home: Path | None = None) -> list[dict]:
    path = inbox_dir(home) / "ledger.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    out = []
    for line in lines:
        with contextlib.suppress(ValueError):
            out.append(json.loads(line))
    return out


def _ledger_append(home: Path | None, entry: dict) -> None:
    path = inbox_dir(home) / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def rate_check(home: Path | None, now: float) -> None:
    attempts = sorted(e["t"] for e in ledger_entries(home) if e.get("attempt") and now - e.get("t", 0) < DAY)
    if attempts and now - attempts[-1] < MIN_INTERVAL:
        raise CaptureRefused("rate_limited", f"at most one ingest every {MIN_INTERVAL:.0f} s",
                             reason="min_interval", retry_after_s=MIN_INTERVAL - (now - attempts[-1]))
    if len(attempts) >= MAX_PER_DAY:
        raise CaptureRefused("rate_limited", f"at most {MAX_PER_DAY} ingests in 24 hours",
                             reason="daily_limit", retry_after_s=DAY - (now - attempts[-len(attempts)]))


def _manifest(p: Prepared, now: float) -> dict:
    return {"ticket": p.ticket, "source": p.source.source_id, "url": p.url, "url_given": p.url_given,
            "doc_id": p.doc_id, "media_type": p.media_type, "extension": p.extension, "sha256": p.sha256,
            "captured_at": p.captured_at, "captured_at_basis": p.captured_at_basis,
            "submitted_at": utc_iso(datetime.fromtimestamp(now, timezone.utc)), "submitted_by_pid": os.getpid(),
            "warnings": p.warnings}


def enqueue(p: Prepared, home: Path | None, now: float) -> str:
    pending, results, _ = _dirs(home)
    (results / f"{p.ticket}.json").unlink(missing_ok=True)   # an earlier refusal of the same bytes
    _atomic_write(pending / f"{p.ticket}.{p.extension}", p.body)
    _atomic_write(pending / f"{p.ticket}.json", json.dumps(_manifest(p, now), ensure_ascii=False).encode())
    return p.ticket


def read_result(home: Path | None, ticket: str) -> dict | None:
    path = inbox_dir(home) / "results" / f"{ticket}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def pending_tickets(home: Path | None = None) -> list[str]:
    pending = inbox_dir(home) / "pending"
    if not pending.is_dir():
        return []
    return sorted(p.stem for p in pending.glob("*.json"))


# --- ingest (writer side) ---------------------------------------------------------------------------------

def _provenance(manifest: dict, ingested_at: str) -> dict:
    return {"capture_origin": "user_browser", "captured_at": manifest["captured_at"],
            "captured_at_basis": manifest["captured_at_basis"], "ingested_at": ingested_at,
            "capture_url": manifest["url_given"], "canonical_url": manifest["url"], "media_type": manifest["media_type"],
            "sha256": manifest["sha256"], "ticket": manifest["ticket"]}


def _ingest_one(conn, home: Path | None, manifest: dict, body: bytes) -> dict:
    from .courtids import priority
    from .sources import get_adapter
    from .store import record_failure, register_source, store_decision, upsert_items, utcnow, write_capture
    from .sync import after_store
    # The submission checked the arguments and the content already; the writer re-runs both with its own code
    # (the capture is parsed by the writer's parser), keeping the submitted capture time and warnings.
    p = check_arguments(manifest["source"], manifest["url_given"], body, captured_at=None, captured_at_basis=None,
                        now=time.time())
    p.captured_at, p.captured_at_basis = manifest["captured_at"], manifest["captured_at_basis"]
    check_content(p)
    p.warnings = list(dict.fromkeys(list(manifest.get("warnings") or []) + [
        w for w in p.warnings if not w.startswith("captured_at not given")]))
    parsed = p.parsed
    ingested_at = utcnow()
    provenance = _provenance(manifest, ingested_at)
    parsed.metadata["capture_origin"] = "user_browser"
    parsed.metadata["capture"] = {k: v for k, v in provenance.items() if k != "capture_origin"}
    if conn.execute("SELECT 1 FROM sources WHERE source_id=?", (p.source.source_id,)).fetchone() is None:
        adapter = get_adapter(p.source.source_id)
        register_source(conn, adapter.source_id, adapter.label, adapter.base_url, adapter.land, adapter.adapter_name,
                        adapter.operator_policy())
    content_hash, relpath = write_capture(body, p.source.source_id, p.extension, home, conn=conn)
    extra = {"route": p.source.route, "portal_doc_id": p.doc_id} | provenance
    upsert_items(conn, p.source.source_id, [{
        "source_doc_id": p.doc_id, "url": p.url, "listed_date": parsed.decision_date, "listed_court": parsed.court_id,
        "listed_case_numbers": parsed.case_numbers_raw, "priority": priority(parsed.jurisdiction), "extra": extra}])
    warnings = list(p.warnings)
    try:
        decision_id, changed = store_decision(conn, parsed, content_hash=content_hash, capture_path=relpath,
                                              media_type=p.media_type, observed_at=p.captured_at.replace("Z", "+00:00"))
        if changed:
            after_store(conn, decision_id)
        stored = True
    except UpstreamFailure as failure:
        if failure.kind != "duplicate_identity":
            failure.url = failure.url or p.url
            record_failure(conn, None, p.source.source_id, p.doc_id, failure)
            raise
        conn.execute("""UPDATE source_items SET state='skipped', attempts=attempts+1, last_attempt=?,
                        failure_kind='duplicate_identity', failure_detail=?, content_hash=?, claimed_by=NULL,
                        lease_until=NULL WHERE source_id=? AND source_doc_id=?""",
                     (utcnow(), failure.detail[:500], content_hash, p.source.source_id, p.doc_id))
        conn.commit()
        decision_id, changed, stored = failure.detail.split(" already stored", 1)[0].strip(), False, False
        warnings.append(f"duplicate_identity: {failure.detail[:300]}; the capture is kept as a skipped item "
                        "(an offline verification pair)")
    row = conn.execute("SELECT numbering, (SELECT count(*) FROM paragraphs WHERE decision_id = d.id) AS n, "
                       "source_id, source_doc_id FROM decisions d WHERE id = ?", (decision_id,)).fetchone()
    return {"status": "ok", "decision_id": decision_id, "stored": stored, "changed": changed,
            "source": p.source.source_id, "source_doc_id": p.doc_id, "url": p.url,
            "numbering": row[0] if row else None, "paragraphs": row[1] if row else None,
            "stored_from": f"{row[2]}:{row[3]}" if row else None,
            "provenance": provenance, "warnings": warnings, "ticket": manifest["ticket"]}


def drain_inbox(conn, home: Path | None = None, *, log=None, limit: int | None = None) -> int:
    """Ingest every queued capture (oldest first); one result file per ticket.  Never raises for one item."""
    pending_dir = inbox_dir(home) / "pending"
    if not pending_dir.is_dir():
        return 0
    manifests = sorted(pending_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
    if not manifests:
        return 0
    _, results, failed = _dirs(home)
    done = 0
    for path in manifests[:limit]:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            body_path = pending_dir / f"{manifest['ticket']}.{manifest['extension']}"
            body = body_path.read_bytes()
        except (OSError, ValueError, KeyError) as exc:
            if log:
                log(f"inbox: unreadable manifest {path.name}: {exc}")
            path.rename(failed / path.name)
            continue
        ticket = manifest["ticket"]
        try:
            if hashlib.sha256(body).hexdigest() != manifest["sha256"]:
                raise CaptureRefused("invalid_argument", "queued capture does not match its sha256",
                                     reason="corrupt_queue_item")
            result = _ingest_one(conn, home, manifest, body)
            outcome = "stored" if result["stored"] else "duplicate"
        except CaptureRefused as refused:
            result, outcome = refused.as_dict() | {"ticket": ticket}, "rejected"
        except UpstreamFailure as failure:
            result = {"status": "invalid_argument", "reason": failure.kind, "ticket": ticket,
                      "detail": f"the capture could not be stored: {failure.kind}: {failure.detail[:300]}"}
            outcome = "failed"
        except OSError:
            raise                  # local I/O (ENOSPC …): the writer's guard handles it; the item stays queued
        except Exception as exc:  # noqa: BLE001 - a parser defect must not stop the writer
            result = {"status": "invalid_argument", "reason": "parser_defect", "ticket": ticket,
                      "detail": f"{type(exc).__name__}: {exc}"[:500]}
            outcome = "failed"
        _atomic_write(results / f"{ticket}.json", json.dumps(result, ensure_ascii=False).encode())
        if outcome in ("stored", "duplicate"):
            body_path.unlink(missing_ok=True)
            path.unlink(missing_ok=True)
        else:
            body_path.replace(failed / body_path.name)
            path.replace(failed / path.name)
        _ledger_append(home, {"t": time.time(), "event": outcome, "ticket": ticket,
                              "decision_id": result.get("decision_id"), "pid": os.getpid()})
        if log:
            log(f"inbox: {ticket} {outcome} {result.get('decision_id') or result.get('reason')}")
        done += 1
    _prune_results(results)
    return done


def _prune_results(results: Path) -> None:
    cutoff = time.time() - RESULT_RETENTION_DAYS * DAY
    for path in results.glob("*.json"):
        with contextlib.suppress(OSError):
            if path.stat().st_mtime < cutoff:
                path.unlink()


# --- the call ------------------------------------------------------------------------------------------

def _ingest_directly(home: Path | None) -> bool:
    """Take the writer lock if no writer runs and drain the inbox; False when another writer holds it."""
    from .space import check as check_space
    from .store import connect, init_db
    from .writer import WriterBusy, checkpoint, writer_lock
    try:
        with writer_lock("ingest-capture", home):
            report = check_space(Path(home or data_home()), starting=True)
            if report.problems:
                raise CaptureRefused("busy", "space guard: " + "; ".join(report.problems), reason="space_guard",
                                     retry_after_s=3600)
            from .config import db_path
            conn = connect(db_path(Path(home) if home else None))
            try:
                init_db(conn)
                drain_inbox(conn, home)
                checkpoint(conn)
            finally:
                conn.close()
            return True
    except WriterBusy:
        return False


def _deliver(result: dict) -> dict:
    if result.get("status") == "ok":
        return result
    raise CaptureRefused(result.get("status", "invalid_argument") if result.get("status") in
                         ("invalid_argument", "busy", "rate_limited") else "invalid_argument",
                         result.get("detail", "ingest failed"), reason=result.get("reason"),
                         **{k: v for k, v in result.items() if k in ("ticket",)})


def ingest_capture(source: str, url: str, body: bytes, *, captured_at: str | None = None,
                   captured_at_basis: str | None = None, home: Path | None = None, wait: float = WAIT_SECONDS,
                   clock=time.time, sleep=time.sleep) -> dict:
    """Ingest one user-browser capture; return the success JSON or raise :class:`CaptureRefused`."""
    home = Path(home) if home else None
    now = clock()
    p = check_arguments(source, url, body, captured_at=captured_at, captured_at_basis=captured_at_basis, now=now)
    ticket = p.ticket
    with _guard_lock(home):
        earlier = read_result(home, ticket)
        if earlier is not None and earlier.get("status") == "ok":
            return earlier | {"warnings": list(earlier.get("warnings") or []) + ["the same capture was ingested "
                                                                                    "before: first result returned"]}
        queued = ticket in pending_tickets(home)
        writer_sources = running_writer_sources(home)
        if writer_sources is not None and source not in writer_sources:
            # The running writer was started before this capture source existed: it would refuse the item.
            raise CaptureRefused("busy", f"the running writer cannot store {source} captures yet (it started "
                                 "before they were supported); it will after its next start", reason="writer_outdated",
                                 retry_after_s=3600)
        if not queued:
            rate_check(home, now)
            try:
                check_content(p)
            except CaptureRefused as refused:
                _ledger_append(home, {"t": now, "attempt": True, "event": "rejected", "ticket": ticket,
                                      "source": source, "url": p.url, "reason": refused.reason, "pid": os.getpid()})
                raise
            _ledger_append(home, {"t": now, "attempt": True, "event": "queued", "ticket": ticket, "source": source,
                                  "url": p.url, "sha256": p.sha256, "pid": os.getpid()})
            enqueue(p, home, now)
    deadline = clock() + max(0.0, wait)
    while True:
        result = read_result(home, ticket)
        if result is not None and ticket not in pending_tickets(home):
            return _deliver(result)
        if _ingest_directly(home):
            continue
        if clock() >= deadline:
            raise CaptureRefused("busy", f"queued in the inbox; the running writer did not ingest it within "
                                 f"{wait:.0f} s", reason="writer_busy", retry_after_s=30, ticket=ticket, queued=True)
        sleep(POLL_SECONDS)


# --- CLI ---------------------------------------------------------------------------------------------------

def _refuse(refused: CaptureRefused, stderr) -> int:
    print(json.dumps(refused.as_dict(), ensure_ascii=False), file=stderr)
    return EXIT_CODES[refused.status]


def cli(argv: list[str], *, stdout=None, stderr=None) -> int:
    """``python -m courts ingest-capture --source by --url <url> --file <path> [--captured-at ISO]``."""
    import argparse
    import sys
    stdout, stderr = stdout or sys.stdout, stderr or sys.stderr

    class Parser(argparse.ArgumentParser):
        def error(self, message):  # one JSON line on stderr, like every refusal
            raise CaptureRefused("invalid_argument", message, reason="usage")

    parser = Parser(prog="python -m courts ingest-capture", add_help=True,
                    description="ingest ONE decision page saved from the operator's browser")
    parser.add_argument("--source", action="append")
    parser.add_argument("--url", action="append")
    parser.add_argument("--file", action="append")
    parser.add_argument("--captured-at")
    parser.add_argument("--wait", type=float, default=WAIT_SECONDS, help="seconds to wait for a running writer")
    parser.add_argument("--status", action="store_true", help="pending tickets and the last results (JSON)")
    try:
        args = parser.parse_args(argv)
        if args.status:
            print(json.dumps(status(), ensure_ascii=False), file=stdout)
            return 0
        for name in ("source", "url", "file"):
            values = getattr(args, name) or []
            if len(values) != 1:
                raise CaptureRefused("invalid_argument", f"exactly one --{name} is required (one page per call)",
                                     reason="bulk_refused" if len(values) > 1 else "usage")
        path = Path(args.file[0]).expanduser()
        if not path.is_file():
            raise CaptureRefused("invalid_argument", f"--file must be one regular file: {str(path)!r}",
                                 reason="bulk_refused" if path.is_dir() else "file_not_found")
        body = path.read_bytes()
        captured_at, basis = args.captured_at, "given"
        if not captured_at:
            captured_at, basis = utc_iso(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)), "file_mtime"
        result = ingest_capture(args.source[0], args.url[0], body, captured_at=captured_at, captured_at_basis=basis,
                                wait=args.wait)
    except CaptureRefused as refused:
        return _refuse(refused, stderr)
    print(json.dumps(result, ensure_ascii=False), file=stdout)
    return 0


def status(home: Path | None = None, last: int = 20) -> dict:
    results_dir = inbox_dir(home) / "results"
    results = sorted(results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)[-last:] \
        if results_dir.is_dir() else []
    now = time.time()
    attempts = [e for e in ledger_entries(home) if e.get("attempt") and now - e.get("t", 0) < DAY]
    return {"pending": pending_tickets(home), "attempts_24h": len(attempts), "max_per_day": MAX_PER_DAY,
            "min_interval_s": MIN_INTERVAL,
            "results": [json.loads(p.read_text(encoding="utf-8")) for p in results]}
