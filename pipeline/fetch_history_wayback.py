"""Polite, resumable Wayback capture of historical GII consolidations.

For every requested act (default: the 13 federal priority acts) this

1. discovers captures with the Wayback CDX API
   * the full-act HTML page ``<slug>/<DOKNR>.html``,
   * the official XML package ``<slug>/xml.zip``,
   * other full-act ``BJNR*.html`` file names under the act folder
     (older doknr / renamed pages; bounded prefix query),
   on both hosts of the official service: ``www.gesetze-im-internet.de``
   and its juris-operated predecessor/mirror ``bundesrecht.juris.de``
   (CDX keys are scheme- and ``www.``-insensitive, so one query per path
   covers http/https and with/without ``www.``);
2. keeps only HTTP 200 captures, collapses identical CDX digests (the same
   payload captured again later is recorded as a ``skipped`` ledger row that
   points at the stored object, so every capture date stays evidence);
3. fetches raw bytes via ``/web/<timestamp>id_/<original>`` into
   ``R/raw/wayback_gii/sha256/..`` (gzip, named by the sha256 of the raw
   bytes) and appends one ledger row per attempt to
   ``R/ledger/wayback_gii.jsonl``.

Capture timestamps are *capture* dates (``known_at``), never legal dates.

Per-norm pages (``<slug>/__7.html``) are not captured by default.  Full-act
captures exist for 253 of 273 act-years 2006-2026 of the 13 priority acts;
kind ``norm_html_gap`` lists, with one bounded prefix query per remaining
gap year and host, every per-norm page captured in such a year.  The 2026-09
run fetched all 376 of them: 375 showed a text already known from full-act
captures before/after the gap (they only add capture dates), 1 was a new
version.  Per-norm pages outside gap years would multiply requests by the
number of norms for no new text, so the default kinds stay full-act only.

Politeness: at most one request per ``--delay`` seconds (default 2.5, never
below 2.0) to archive.org, exponential back-off on 429/5xx/connection errors,
and the run stops after ``--max-consecutive-failures`` failed requests in a
row (default 7: one broken capture costs 3 attempts and is skipped, an outage
stops the run within three captures).
Runs are bounded by ``--max-requests`` and ``--max-bytes`` and resume from
the ledger.

Usage::

    LEXGRAPH_WEB_DATA=/path/to/Gesetz/web/data \
    python3 pipeline/fetch_history_wayback.py [--acts fed_sgb_2,fed_gg] \
        [--kinds act_html,xml_zip] [--max-requests 500] [--max-bytes 2G]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from history_archive import (  # noqa: E402
    CAPTURE_SCHEMA, USER_AGENT, HistoryArchive, utc_now, wayback_digest,
    web_data_root)

SOURCE = "wayback_gii"
CDX_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
ID_URL = "https://web.archive.org/web/{ts}id_/{original}"
HOSTS = ("www.gesetze-im-internet.de", "bundesrecht.juris.de")
MAX_OBJECT_BYTES = 96 * 1024 ** 2
MIN_DELAY = 2.0

# act_id -> (GII slug, full-HTML doknr).  Verified against
# web/data/acts/<id>.json official_states[].source_doknr on 2026-09-28; the
# web data (when present) overrides this table.
PRIORITY_ACTS: dict[str, tuple[str, str]] = {
    "fed_asylblg": ("asylblg", "BJNR107410993"),
    "fed_sgb_2": ("sgb_2", "BJNR295500003"),
    "fed_sgb_12": ("sgb_12", "BJNR302300003"),
    "fed_sgb_10": ("sgb_10", "BJNR114690980"),
    "fed_sgg": ("sgg", "BJNR012390953"),
    "fed_vwgo": ("vwgo", "BJNR000170960"),
    "fed_vwvfg": ("vwvfg", "BJNR012530976"),
    "fed_aufenthg_2004": ("aufenthg_2004", "BJNR195010004"),
    "fed_asylvfg_1992": ("asylvfg_1992", "BJNR111260992"),
    "fed_azrg": ("azrg", "BJNR226500994"),
    "fed_bgb": ("bgb", "BJNR001950896"),
    "fed_zpo": ("zpo", "BJNR005330950"),
    "fed_gg": ("gg", "BJNR000010949"),
}

KINDS = ("act_html", "xml_zip", "act_html_alt", "norm_html", "norm_html_gap")
DEFAULT_KINDS = ("act_html", "xml_zip", "act_html_alt")
MEDIA_EXT = {"act_html": "html", "act_html_alt": "html", "norm_html": "html",
             "xml_zip": "zip"}


class StopRun(RuntimeError):
    """Raised when the run must end (budget exhausted or repeated failures)."""


@dataclass(frozen=True)
class Target:
    act_id: str
    slug: str
    kind: str
    timestamp: str
    original: str
    digest: str
    length: int
    mimetype: str

    @property
    def archive_url(self) -> str:
        return ID_URL.format(ts=self.timestamp, original=self.original)

    @property
    def key(self) -> tuple[str, str]:
        return (self.timestamp, self.original)


def captured_at(timestamp: str) -> str | None:
    try:
        moment = datetime.strptime(timestamp[:14], "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_original(original: str) -> str:
    """Stable https URL without the ``:80`` artefact (for ledger display)."""
    parts = urllib.parse.urlsplit(original)
    host = (parts.hostname or "").lower()
    return urllib.parse.urlunsplit(("https", host, parts.path, parts.query, ""))


def parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGT]?)i?B?\s*", value, re.I)
    if not match:
        raise argparse.ArgumentTypeError(f"invalid size: {value!r}")
    factor = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3,
              "T": 1024 ** 4}[match.group(2).upper()]
    return int(float(match.group(1)) * factor)


def load_acts(web_data: Path, act_ids: list[str]) -> dict[str, tuple[str, str]]:
    acts: dict[str, tuple[str, str]] = {}
    for act_id in act_ids:
        slug, doknr = PRIORITY_ACTS.get(act_id, (None, None))
        path = web_data / "acts" / f"{act_id}.json"
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            states = data.get("official_states") or []
            if states:
                slug = states[-1].get("source_slug") or slug
                doknr = states[-1].get("source_doknr") or doknr
            if not slug:
                match = re.search(r"gesetze-im-internet\.de/([^/]+)/",
                                  data.get("source_url") or "")
                slug = match.group(1) if match else None
        if not slug or not doknr:
            raise SystemExit(f"unknown act (no slug/doknr): {act_id}")
        acts[act_id] = (slug, doknr)
    return acts


# ------------------------------------------------------------------ network

class PoliteClient:
    """Single-connection client: >= delay seconds between any two requests."""

    def __init__(self, delay: float = 2.5, max_requests: int = 1000,
                 max_bytes: int = 2 * 1024 ** 3,
                 max_consecutive_failures: int = 7,
                 session: requests.Session | None = None,
                 sleep=time.sleep, clock=time.monotonic):
        self.delay = max(MIN_DELAY, float(delay))
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.max_consecutive_failures = max_consecutive_failures
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.sleep = sleep
        self.clock = clock
        self.requests = 0
        self.bytes = 0
        self.consecutive_failures = 0
        self._last = None

    def _throttle(self, extra: float = 0.0) -> None:
        if self._last is not None:
            wait = self.delay + extra - (self.clock() - self._last)
            if wait > 0:
                self.sleep(wait)
        elif extra:
            self.sleep(extra)

    def get(self, url: str, *, timeout: float = 120, attempts: int = 3,
            max_bytes: int = MAX_OBJECT_BYTES) -> tuple[int, bytes, str]:
        """Return ``(status, body, final_url)``; raises StopRun on budget/failure.

        404/403/other 4xx are returned (no retry).  429, 5xx and connection
        errors back off exponentially (30 s, 60 s, 120 s ...).
        """
        last_error = "unknown"
        for attempt in range(attempts):
            if self.requests >= self.max_requests:
                raise StopRun(f"request budget exhausted ({self.max_requests})")
            if self.bytes >= self.max_bytes:
                raise StopRun(f"byte budget exhausted ({self.max_bytes})")
            self._throttle(extra=0.0 if attempt == 0 else 30.0 * 2 ** (attempt - 1))
            self._last = self.clock()
            self.requests += 1
            try:
                with self.session.get(url, timeout=timeout, stream=True,
                                      allow_redirects=True) as response:
                    status = response.status_code
                    if status == 429 or status >= 500:
                        last_error = f"http_{status}"
                        self._record_failure(last_error)
                        continue
                    body = bytearray()
                    for chunk in response.iter_content(64 * 1024):
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            raise ValueError("response exceeds object size limit")
                    self.bytes += len(body)
                    self.consecutive_failures = 0
                    return status, bytes(body), response.url
            except (requests.RequestException, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"[:300]
                self._record_failure(last_error)
                continue
        return -1, b"", last_error

    def _record_failure(self, reason: str) -> None:
        self.consecutive_failures += 1
        print(f"  [backoff] {reason} (consecutive failures: "
              f"{self.consecutive_failures})", file=sys.stderr, flush=True)
        if self.consecutive_failures >= self.max_consecutive_failures:
            raise StopRun(f"stopping after {self.consecutive_failures} "
                          f"consecutive failures (last: {reason})")


# ---------------------------------------------------------------------- cdx

CDX_FIELDS = "timestamp,original,statuscode,digest,length,mimetype"


def cdx_url(url: str, *, prefix_filter: str | None = None,
            limit: int | None = None, collapse: str = "digest",
            year: int | None = None) -> str:
    params = [("url", url), ("output", "json"), ("fl", CDX_FIELDS)]
    if prefix_filter:
        params.append(("matchType", "prefix"))
        params.append(("filter", f"original:{prefix_filter}"))
    if year:
        params.extend([("from", str(year)), ("to", str(year))])
    if collapse:
        params.append(("collapse", collapse))
    if limit:
        params.append(("limit", str(limit)))
    return CDX_ENDPOINT + "?" + urllib.parse.urlencode(params)


def parse_cdx(body: bytes) -> list[dict]:
    text = body.decode("utf-8", "replace").strip()
    if not text:
        return []
    rows = json.loads(text)
    if not rows:
        return []
    header, *data = rows
    return [dict(zip(header, row)) for row in data]


def cdx_query(client: PoliteClient, archive: HistoryArchive, act_id: str,
              name: str, url: str, *, max_age_days: float,
              offline: bool = False, **kw) -> list[dict] | None:
    """CDX rows (cached under R/cdx).  ``None`` = unavailable this run."""
    path = archive.cdx_path(SOURCE, act_id, name)
    if path.is_file():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            fetched = datetime.strptime(cached["fetched_at"],
                                        "%Y-%m-%dT%H:%M:%SZ").replace(
                                            tzinfo=timezone.utc)
            fresh = datetime.now(timezone.utc) - fetched < timedelta(
                days=max_age_days)
            # An empty listing is never trusted as fresh: the CDX server
            # answers transient overload with empty 200 bodies.
            if (fresh and cached["rows"]) or offline:
                return cached["rows"]
        except (OSError, ValueError, KeyError):
            pass
    if offline:
        return None
    query = cdx_url(url, **kw)
    status, body, _ = client.get(query, timeout=180)
    if status != 200:
        print(f"  [cdx] {act_id} {name}: status {status}", file=sys.stderr)
        return None
    try:
        rows = parse_cdx(body)
    except ValueError:
        print(f"  [cdx] {act_id} {name}: unparsable response", file=sys.stderr)
        return None
    archive.write_json(path, {"query": query, "fetched_at": utc_now(),
                              "rows": rows, "empty": not rows})
    return rows


ALT_PAGE = re.compile(r"/([^/]+)/(BJNR\d{9})\.html?", re.I)


def is_alt_page(row: dict, slug: str, doknr: str) -> bool:
    """Another full-act page in exactly this act's folder (prefix queries
    also match sibling folders such as ``sgb_10_kap3/``)."""
    path = urllib.parse.urlsplit(row.get("original") or "").path
    match = ALT_PAGE.fullmatch(path)
    return bool(match and match.group(1).lower() == slug.lower()
                and match.group(2).upper() != doknr.upper())


NORM_PAGE = re.compile(r"/([^/]+)/(?:__|art_)[^/]*\.html?", re.I)


def is_norm_page(row: dict, slug: str) -> bool:
    path = urllib.parse.urlsplit(row.get("original") or "").path
    match = NORM_PAGE.fullmatch(path)
    return bool(match and match.group(1).lower() == slug.lower())


def gap_years(rows: list[dict], first: int = 2006,
              last: int | None = None) -> list[int]:
    """Years without any HTTP 200 full-act capture (on either host)."""
    last = last or datetime.now(timezone.utc).year
    covered = {int(r["timestamp"][:4]) for r in rows
               if r.get("statuscode") == "200"}
    return [y for y in range(first, last + 1) if y not in covered]


def discover(client: PoliteClient, archive: HistoryArchive, act_id: str,
             slug: str, doknr: str, kinds: tuple[str, ...], *,
             max_age_days: float, offline: bool = False,
             norm_pages: list[str] | None = None) -> list[Target]:
    targets: list[Target] = []

    def add(rows: list[dict] | None, kind: str) -> None:
        for row in rows or []:
            if row.get("statuscode") != "200":
                continue
            if (row.get("mimetype") or "").startswith("warc/"):
                continue
            try:
                length = int(row.get("length") or 0)
            except ValueError:
                length = 0
            targets.append(Target(act_id, slug, kind, row["timestamp"],
                                  row["original"], row.get("digest") or "",
                                  length, row.get("mimetype") or ""))

    for host in HOSTS:
        tag = "gii" if host.startswith("www.gesetze") else "juris"
        if "act_html" in kinds:
            add(cdx_query(client, archive, act_id, f"act_html.{tag}",
                          f"{host}/{slug}/{doknr}.html",
                          max_age_days=max_age_days, offline=offline),
                "act_html")
        if "xml_zip" in kinds:
            add(cdx_query(client, archive, act_id, f"xml_zip.{tag}",
                          f"{host}/{slug}/xml.zip",
                          max_age_days=max_age_days, offline=offline),
                "xml_zip")
        if "act_html_alt" in kinds:
            rows = cdx_query(
                client, archive, act_id, f"act_html_alt.{tag}",
                f"{host}/{slug}/",
                prefix_filter=r"(?i).*/BJNR[0-9]{9}\.html?$",
                collapse="digest", limit=5000,
                max_age_days=max_age_days, offline=offline)
            add([row for row in rows or [] if is_alt_page(row, slug, doknr)],
                "act_html_alt")
        if "norm_html" in kinds and norm_pages:
            for page in norm_pages:
                add(cdx_query(client, archive, act_id,
                              f"norm_html.{tag}.{page}",
                              f"{host}/{slug}/{page}",
                              max_age_days=max_age_days, offline=offline),
                    "norm_html")
    if "norm_html_gap" in kinds:
        # Per-norm pages only for years without any full-act capture: one
        # bounded prefix query per gap year and host (juris host until 2012).
        full: list[dict] = []
        for host in HOSTS:
            tag = "gii" if host.startswith("www.gesetze") else "juris"
            full += cdx_query(client, archive, act_id, f"act_html.{tag}",
                              f"{host}/{slug}/{doknr}.html",
                              max_age_days=max_age_days, offline=offline) or []
        for year in gap_years(full):
            for host in HOSTS:
                tag = "gii" if host.startswith("www.gesetze") else "juris"
                if tag == "juris" and year > 2012:
                    continue
                rows = cdx_query(
                    client, archive, act_id, f"norm_html_gap.{tag}.{year}",
                    f"{host}/{slug}/",
                    prefix_filter=r"(?i).*/(__|art_)[^/]*\.html?$",
                    collapse="digest", limit=5000, year=year,
                    max_age_days=max_age_days, offline=offline)
                add([row for row in rows or [] if is_norm_page(row, slug)],
                    "norm_html")
    targets.sort(key=lambda t: (t.timestamp, t.kind, t.original))
    return targets


# ------------------------------------------------------------------ capture

def ledger_index(archive: HistoryArchive) -> tuple[dict, dict, Counter]:
    """``(done_by_key, sha_by_digest, error_counts)`` from the ledger."""
    done: dict[tuple[str, str], dict] = {}
    by_digest: dict[str, str] = {}
    errors: Counter = Counter()
    for row in archive.read_ledger(SOURCE):
        key = (row.get("capture_timestamp"), row.get("wayback_original")
               or row.get("original_url"))
        status = row.get("status")
        if status in {"ok", "skipped"} and row.get("sha256"):
            done[key] = row
            # Newer crawls digest the transfer-encoded payload, so the CDX
            # digest need not equal the SHA-1 of the bytes id_ serves; equal
            # CDX digests still mean equal payloads.
            if row.get("payload_digest"):
                by_digest[row["payload_digest"]] = row["sha256"]
            if row.get("cdx_digest"):
                by_digest.setdefault(row["cdx_digest"], row["sha256"])
        elif status == "error":
            errors[key] += 1
    return done, by_digest, errors


def base_row(target: Target) -> dict:
    return {
        "schema": CAPTURE_SCHEMA,
        "source": SOURCE,
        "act_id": target.act_id,
        "kind": target.kind,
        "original_url": canonical_original(target.original),
        "wayback_original": target.original,
        "archive_url": target.archive_url,
        "capture_timestamp": target.timestamp,
        "captured_at": captured_at(target.timestamp),
        "cdx_digest": target.digest,
        "cdx_length": target.length,
        "cdx_mimetype": target.mimetype,
    }


def media_type_for(kind: str, data: bytes) -> str:
    if kind == "xml_zip":
        return "application/zip"
    return "text/html"


def looks_valid(kind: str, data: bytes) -> str | None:
    """Reason string when the payload is obviously not the expected object."""
    if not data:
        return "empty_body"
    if kind == "xml_zip":
        return None if data[:2] == b"PK" else "not_a_zip"
    head = data[:4096].lower()
    if b"<html" not in head and b"<!doctype" not in head and b"<head" not in head:
        return "not_html"
    return None


def capture(client: PoliteClient, archive: HistoryArchive,
            targets: list[Target], *, retry_errors: int = 3,
            stats: dict | None = None) -> dict:
    stats = stats if stats is not None else defaultdict(Counter)
    done, by_digest, errors = ledger_index(archive)
    for target in targets:
        counter = stats[target.act_id]
        counter["listed"] += 1
        if target.key in done:
            counter["already"] += 1
            continue
        if errors[target.key] >= retry_errors:
            counter["failed_before"] += 1
            continue
        known_sha = by_digest.get(target.digest) if target.digest else None
        if known_sha and archive.has_raw(SOURCE, known_sha):
            row = base_row(target)
            row.update({"status": "skipped", "reason": "duplicate_digest",
                        "sha256": known_sha, "http_status": 200,
                        "media_type": media_type_for(target.kind, b""),
                        "bytes": None, "fetched_at": utc_now(),
                        "error": None, "payload_digest": target.digest,
                        "digest_verified": True})
            archive.append_ledger(SOURCE, row)
            done[target.key] = row
            counter["duplicate"] += 1
            continue
        status, body, final_url = client.get(target.archive_url)
        row = base_row(target)
        row["fetched_at"] = utc_now()
        row["http_status"] = status
        if status != 200:
            row.update({"status": "error", "sha256": None, "bytes": None,
                        "media_type": None,
                        "error": (final_url if status == -1
                                  else f"http_{status}")})
            archive.append_ledger(SOURCE, row)
            errors[target.key] += 1
            counter["failed"] += 1
            print(f"  [error] {target.act_id} {target.timestamp} "
                  f"{row['error']}", flush=True)
            continue
        invalid = looks_valid(target.kind, body)
        digest = wayback_digest(body)
        final_ts = re.search(r"/web/(\d{14})", final_url or "")
        if final_ts and final_ts.group(1) != target.timestamp:
            row["redirected_to"] = final_url
            row["served_timestamp"] = final_ts.group(1)
        if invalid:
            row.update({"status": "error", "sha256": None,
                        "bytes": len(body), "media_type": None,
                        "error": invalid, "payload_digest": digest})
            archive.append_ledger(SOURCE, row)
            errors[target.key] += 1
            counter["failed"] += 1
            print(f"  [error] {target.act_id} {target.timestamp} {invalid}",
                  flush=True)
            continue
        sha, _ = archive.put_raw(SOURCE, body, MEDIA_EXT[target.kind])
        row.update({"status": "ok", "sha256": sha, "bytes": len(body),
                    "media_type": media_type_for(target.kind, body),
                    "error": None, "payload_digest": digest,
                    "digest_verified": digest == target.digest})
        archive.append_ledger(SOURCE, row)
        done[target.key] = row
        by_digest[digest] = sha
        if target.digest:
            by_digest.setdefault(target.digest, sha)
        counter["fetched"] += 1
        print(f"  [ok] {target.act_id} {target.kind} {target.timestamp} "
              f"{len(body) // 1024} KiB sha {sha[:12]}"
              f"{'' if row['digest_verified'] else ' (digest differs)'}",
              flush=True)
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--acts", help="comma list of act ids "
                    "(default: the 13 federal priority acts)")
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS),
                    help=f"comma list of {', '.join(KINDS)}")
    ap.add_argument("--norm-pages", default="",
                    help="comma list of per-norm page names (e.g. __7.html) "
                    "for kind norm_html; kind norm_html_gap instead lists "
                    "all per-norm pages captured in years without any "
                    "full-act capture")
    ap.add_argument("--archive", help="history archive root")
    ap.add_argument("--web-data", help="web/data dir with acts/<id>.json")
    ap.add_argument("--delay", type=float, default=2.5)
    ap.add_argument("--max-requests", type=int, default=2000)
    ap.add_argument("--max-bytes", type=parse_size, default="3G")
    ap.add_argument("--max-consecutive-failures", type=int, default=7)
    ap.add_argument("--retry-errors", type=int, default=3,
                    help="skip captures that already failed this many times")
    ap.add_argument("--cdx-max-age-days", type=float, default=7.0)
    ap.add_argument("--discover-only", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="use cached CDX listings only, fetch nothing")
    args = ap.parse_args(argv)

    kinds = tuple(k for k in args.kinds.split(",") if k)
    unknown = set(kinds) - set(KINDS)
    if unknown:
        ap.error(f"unknown kinds: {sorted(unknown)}")
    act_ids = (args.acts.split(",") if args.acts else list(PRIORITY_ACTS))
    acts = load_acts(web_data_root(args.web_data), act_ids)
    archive = HistoryArchive(args.archive)
    client = PoliteClient(delay=args.delay, max_requests=args.max_requests,
                          max_bytes=args.max_bytes,
                          max_consecutive_failures=args.max_consecutive_failures)
    norm_pages = [p for p in args.norm_pages.split(",") if p]
    print(f"[archive] {archive.root}")
    stats: dict = defaultdict(Counter)
    exit_code = 0
    try:
        for act_id, (slug, doknr) in acts.items():
            targets = discover(client, archive, act_id, slug, doknr, kinds,
                               max_age_days=args.cdx_max_age_days,
                               offline=args.offline, norm_pages=norm_pages)
            by_kind = Counter(t.kind for t in targets)
            print(f"[{act_id}] {len(targets)} captures listed "
                  f"{dict(by_kind)}", flush=True)
            if args.discover_only or args.offline:
                stats[act_id]["listed"] += len(targets)
                continue
            capture(client, archive, targets, retry_errors=args.retry_errors,
                    stats=stats)
    except StopRun as exc:
        print(f"[stop] {exc}", file=sys.stderr)
        exit_code = 2
    print(f"[done] requests={client.requests} "
          f"bytes={client.bytes / 1024 ** 2:.1f} MiB")
    for act_id, counter in stats.items():
        print(f"  {act_id:20} {dict(counter)}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
