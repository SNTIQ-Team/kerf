"""Polite capture of historical Bavarian law sources into the history archive.

Subcommands (all resumable; every attempt is one ledger row):

  wayback  CDX-discover and capture archived gesetze-bayern.de copies of
           /Content/Zip/<KEY> (complete XML incl. aenderungsverlauf),
           /Content/Document/<KEY>/true (full view), /Content/Document/<KEY>
           (landing page) and /Content/Document/<KEY>-<n> (per-norm pages).
           One fetch per distinct (document, CDX digest); byte-identical
           captures are ledgered as ``skipped`` with the representative's
           sha256 so their capture dates still count as observations.
  gvbl     Capture the GVBl documents of every amending act named in the
           official version log from www.verkuendung-bayern.de: the
           /gvbl/<year>-<page>/ detail page (full HTML text) or, where no
           detail page exists, the issue PDF found via the volume listing.
  current  Try the live BAYERN.RECHT zip once per act.  A Myra "Security
           Check" page is recorded as an error and never worked around.

Politeness: descriptive User-Agent; web.archive.org <= 1 request / 2 s,
other hosts <= 1 request / s; exponential back-off on 429/5xx/network errors
(30 s, 60 s, 120 s, 240 s); the run stops after 6 consecutive failed URLs.

    python3 pipeline/fetch_bavarian_history.py wayback [--acts by_agsg] [--limit N]
    python3 pipeline/fetch_bavarian_history.py gvbl
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path
from urllib.parse import urlsplit

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools import bavarian_history as bh  # noqa: E402

WAYBACK_INTERVAL = 2.0
DEFAULT_INTERVAL = 1.0
BACKOFF = (30, 60, 120, 240)
MAX_CONSECUTIVE_FAILURES = 6

CDX = ("https://web.archive.org/cdx/search/cdx?url={target}&matchType=prefix"
       "&output=json&fl=original,timestamp,statuscode,mimetype,digest,length")
ARCHIVE = "https://web.archive.org/web/{ts}id_/{original}"
VB = "https://www.verkuendung-bayern.de"


class StopRun(RuntimeError):
    """Too many consecutive failures: stop politely, resume later."""


class Polite:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers["User-Agent"] = bh.UA
        self.last: dict[str, float] = {}
        self.consecutive_failures = 0

    def _wait(self, host: str) -> None:
        interval = WAYBACK_INTERVAL if host.endswith("archive.org") \
            else DEFAULT_INTERVAL
        delta = time.monotonic() - self.last.get(host, 0.0)
        if delta < interval:
            time.sleep(interval - delta)
        self.last[host] = time.monotonic()

    def get(self, url: str, timeout: int = 120) -> tuple[requests.Response | None, str | None]:
        host = urlsplit(url).hostname or ""
        error = None
        for attempt in range(len(BACKOFF) + 1):
            self._wait(host)
            try:
                response = self.session.get(url, timeout=timeout)
            except requests.RequestException as exc:
                error = f"{type(exc).__name__}: {exc}"[:300]
                response = None
            if response is not None:
                if response.status_code in (429, 500, 502, 503, 504):
                    error = f"HTTP {response.status_code}"
                    if b"Security Check" in response.content[:4000]:
                        # a bot challenge: record it, never retry around it
                        return self._fail(response, "security_check_challenge")
                else:
                    self.consecutive_failures = 0
                    return response, None
            if attempt < len(BACKOFF):
                print(f"    [backoff {BACKOFF[attempt]}s] {error} {url}",
                      file=sys.stderr)
                time.sleep(BACKOFF[attempt])
        return self._fail(None, error)

    def _fail(self, response, error):
        self.consecutive_failures += 1
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            raise StopRun(f"{self.consecutive_failures} consecutive failures "
                          f"(last: {error})")
        return response, error


def _media(response: requests.Response) -> str:
    return (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()


def _ext(media: str, url: str) -> str:
    if "zip" in media:
        return "zip"
    if "pdf" in media or url.lower().split("#")[0].endswith(".pdf"):
        return "pdf"
    if "json" in media:
        return "json"
    if "xml" in media:
        return "xml"
    return "html"


# ------------------------------------------------------------------ wayback

def classify(original: str, key: str) -> tuple[str, str] | None:
    """-> (kind, document) for one CDX original URL of this act, else None."""
    match = re.match(r"https?://(?:www\.)?gesetze-bayern\.de(?::\d+)?"
                     r"/Content/(Zip|Document)/([^?#]*)$", original, re.I)
    if not match:
        return None
    area, path = match.group(1), match.group(2)
    if area.lower() == "zip":
        return ("zip", key) if path == key else None
    if path == key:
        return "landing", key
    if path.lower() == f"{key}/true".lower():
        return "full", key
    if re.fullmatch(re.escape(key) + r"-\d+[a-z]?", path):
        return "norm", path
    if re.fullmatch(re.escape(key) + r"-ANL_\d+", path):
        return "anlage", path
    return None


def cdx_rows(polite: Polite, act_id: str, key: str, root) -> list[list[str]]:
    rows: list[list[str]] = []
    for area in ("Zip", "Document"):
        target = f"gesetze-bayern.de/Content/{area}/{key}"
        url = CDX.format(target=target)
        response, error = polite.get(url, timeout=180)
        row = {"act_id": act_id, "kind": "cdx", "original_url": target,
               "archive_url": url, "capture_timestamp": None,
               "captured_at": None, "fetched_at": bh.utc_now(),
               "cdx_digest": None}
        if response is None or response.status_code != 200:
            bh.append_ledger("wayback_bayern", {
                **row, "http_status": getattr(response, "status_code", None),
                "media_type": None, "sha256": None, "bytes": 0,
                "status": "error", "error": error or "cdx failed"}, root)
            continue
        digest = bh.put_raw("wayback_bayern", response.content, "json", root)
        bh.append_ledger("wayback_bayern", {
            **row, "http_status": 200, "media_type": "application/json",
            "sha256": digest, "bytes": len(response.content), "status": "ok",
            "error": None}, root)
        try:
            payload = json.loads(response.content)
        except ValueError:
            continue
        rows.extend(r for r in payload[1:] if isinstance(r, list) and len(r) >= 5)
    return rows


def latest_cdx(act_id: str, root) -> list[list[str]]:
    """CDX rows from the ledgered raw CDX responses (offline replanning)."""
    seen, rows = set(), []
    for row in bh.read_ledger("wayback_bayern", root):
        if row.get("kind") != "cdx" or row.get("act_id") != act_id \
                or row.get("status") != "ok":
            continue
        payload = json.loads(bh.get_raw("wayback_bayern", row["sha256"], "json", root))
        for item in payload[1:]:
            ident = tuple(item[:2])
            if ident not in seen:
                seen.add(ident)
                rows.append(item)
    return rows


KIND_ORDER = {"zip": 0, "full": 1, "landing": 2, "norm": 3, "anlage": 3}


def plan_captures(rows: list[list[str]], key: str) -> list[dict]:
    """Group 200 captures by (kind, document, digest); newest first."""
    groups: dict[tuple, list[tuple[str, str]]] = defaultdict(list)
    for original, timestamp, status, _mime, digest, *_ in rows:
        if status != "200" or not re.fullmatch(r"\d{14}", timestamp):
            continue
        kind = classify(original, key)
        if kind is None:
            continue
        groups[(kind[0], kind[1].lower(), kind[1], digest)].append(
            (timestamp, original))
    plan = []
    for (kind, _doc_lower, document, digest), captures in groups.items():
        captures.sort(reverse=True)
        plan.append({"kind": kind, "document": document, "cdx_digest": digest,
                     "captures": captures})
    plan.sort(key=lambda item: (KIND_ORDER[item["kind"]],
                                -int(item["captures"][0][0])))
    return plan


def _done(root) -> dict[str, dict]:
    done = {}
    for row in bh.read_ledger("wayback_bayern", root):
        if row.get("status") in ("ok", "skipped") and row.get("capture_timestamp"):
            done[row["archive_url"]] = row
    return done


def capture_wayback(polite: Polite, act_id: str, root, limit: int | None,
                    kinds: set[str] | None) -> dict:
    key = bh.ACTS[act_id]["key"]
    rows = cdx_rows(polite, act_id, key, root)
    if not rows:
        rows = latest_cdx(act_id, root)
    plan = [p for p in plan_captures(rows, key)
            if kinds is None or p["kind"] in kinds]
    done = _done(root)
    stats = defaultdict(int)
    fetched = 0
    for item in plan:
        urls = [(ts, orig, ARCHIVE.format(ts=ts, original=orig))
                for ts, orig in item["captures"]]
        representative = next((done[u] for _, _, u in urls if u in done
                                and done[u]["status"] == "ok"), None)
        if representative is None:
            if limit is not None and fetched >= limit:
                stats["deferred"] += 1
                continue
            for ts, orig, url in urls:
                base = {"act_id": act_id, "kind": item["kind"],
                        "document": item["document"], "original_url": orig,
                        "archive_url": url, "capture_timestamp": ts,
                        "captured_at": bh.wayback_captured_at(ts),
                        "cdx_digest": item["cdx_digest"]}
                response, error = polite.get(url)
                fetched += 1
                if response is None or response.status_code != 200 \
                        or not response.content:
                    bh.append_ledger("wayback_bayern", {
                        **base, "http_status": getattr(response, "status_code", None),
                        "media_type": None, "sha256": None, "bytes": 0,
                        "fetched_at": bh.utc_now(), "status": "error",
                        "error": error or f"HTTP {getattr(response, 'status_code', None)}"},
                        root)
                    stats["error"] += 1
                    continue
                media = _media(response)
                ext = _ext(media, orig)
                if item["kind"] == "zip":
                    try:
                        zipfile.ZipFile(io.BytesIO(response.content)).testzip()
                        ext = "zip"
                    except zipfile.BadZipFile:
                        bh.append_ledger("wayback_bayern", {
                            **base, "http_status": 200, "media_type": media,
                            "sha256": None, "bytes": len(response.content),
                            "fetched_at": bh.utc_now(), "status": "error",
                            "error": "not a zip archive"}, root)
                        stats["error"] += 1
                        continue
                digest = bh.put_raw("wayback_bayern", response.content, ext, root)
                representative = bh.append_ledger("wayback_bayern", {
                    **base, "http_status": 200, "media_type": media,
                    "ext": ext, "sha256": digest, "bytes": len(response.content),
                    "fetched_at": bh.utc_now(), "status": "ok", "error": None},
                    root)
                done[url] = representative
                stats["ok"] += 1
                break
            if representative is None:
                continue
        for ts, orig, url in urls:
            if url in done:
                continue
            done[url] = bh.append_ledger("wayback_bayern", {
                "act_id": act_id, "kind": item["kind"],
                "document": item["document"], "original_url": orig,
                "archive_url": url, "capture_timestamp": ts,
                "captured_at": bh.wayback_captured_at(ts),
                "cdx_digest": item["cdx_digest"], "http_status": None,
                "media_type": representative.get("media_type"),
                "ext": representative.get("ext"),
                "sha256": representative["sha256"],
                "bytes": representative["bytes"], "fetched_at": bh.utc_now(),
                "status": "skipped", "error": None,
                "skip_reason": "same_cdx_digest",
                "same_as": representative["archive_url"]}, root)
            stats["skipped_same_digest"] += 1
    stats["planned_groups"] = len(plan)
    return dict(stats)


# --------------------------------------------------------------------- gvbl

def _gvbl_done(root) -> dict[str, dict]:
    return {row["original_url"]: row for row in bh.read_ledger("gvbl", root)
            if row.get("status") == "ok" or (row.get("status") == "error"
                                             and row.get("http_status") == 404)}


def fetch_gvbl_url(polite: Polite, url: str, kind: str, act_id: str | None,
                   root, done: dict, extra: dict | None = None) -> dict | None:
    if url in done:
        return done[url]
    response, error = polite.get(url)
    base = {"act_id": act_id, "kind": kind, "original_url": url,
            "archive_url": None, "capture_timestamp": None,
            "captured_at": bh.utc_now(), "cdx_digest": None,
            "fetched_at": bh.utc_now(), **(extra or {})}
    if response is None or response.status_code != 200:
        row = bh.append_ledger("gvbl", {
            **base, "http_status": getattr(response, "status_code", None),
            "media_type": None, "sha256": None, "bytes": 0,
            "status": "error", "error": error or
            f"HTTP {getattr(response, 'status_code', None)}"}, root)
        done[url] = row
        return row
    media = _media(response)
    ext = _ext(media, url)
    digest = bh.put_raw("gvbl", response.content, ext, root)
    row = bh.append_ledger("gvbl", {
        **base, "http_status": 200, "media_type": media, "ext": ext,
        "sha256": digest, "bytes": len(response.content), "status": "ok",
        "error": None}, root)
    done[url] = row
    return row


def capture_gvbl(polite: Polite, act_ids: list[str], root) -> dict:
    """Per amending act: try the candidate (year, page) detail pages in order
    and keep the first whose printed date matches the version log; without a
    detail page, fetch the issue PDF that contains the page."""
    stats = defaultdict(int)
    done = _gvbl_done(root)
    volumes: dict[int, list[dict]] = {}
    seen: set[tuple] = set()
    for act_id in act_ids:
        for entry in bh.load_version_log(act_id):
            key = (entry["page"], entry["act_date_text"])
            if key in seen or not entry["gvbl_candidates"]:
                continue
            seen.add(key)
            hint = bh._dmy(entry["act_date_text"])
            verified = False
            for cand in entry["gvbl_candidates"]:
                year, page = cand["year"], cand["page"]
                url = f"{VB}/gvbl/{year}-{page}/"
                row = fetch_gvbl_url(polite, url, "gvbl_detail", act_id, root,
                                     done, {"gvbl_year": year, "gvbl_page": page})
                if row and row.get("status") == "ok":
                    parsed = bh.parse_gvbl_detail(
                        bh.get_raw("gvbl", row["sha256"], row.get("ext", "html"), root))
                    dates = {parsed.get("platform_date"), parsed.get("title_date"),
                             (parsed.get("signature") or {}).get("date")}
                    hints = {hint.isoformat()} if hint else set()
                    hints |= {d.isoformat() for f in entry.get("ffn") or []
                              if (d := bh._dmy(f.get("act_date_text")))}
                    dates.add(parsed.get("decision_date"))
                    if hints & dates:
                        verified = True
                        stats["detail_verified"] += 1
                        if bh.commencement_section(parsed) is None and parsed.get("pdf_url"):
                            # detail text truncated: keep the issue PDF as well
                            pdf = VB + parsed["pdf_url"].split("#")[0]
                            prow = fetch_gvbl_url(polite, pdf, "gvbl_issue_pdf", act_id,
                                                  root, done, {
                                                      "gvbl_year": year,
                                                      "gvbl_issue": parsed.get("issue"),
                                                      "gvbl_issue_date": parsed.get("issue_date"),
                                                      "gvbl_first_page": None,
                                                      "gvbl_last_page": None,
                                                      "linked_from": url})
                            stats["linked_pdf_" + str(prow.get("status"))] += 1
                        break
                    stats["detail_date_mismatch"] += 1
                    continue
                # no detail page: the issue PDF of that volume
                if year not in volumes:
                    vurl = (f"{VB}/gesetz-und-verordnungsblatt/"
                            f"alle-ausgaben-des-gvbl-ab-1945/?volume={year}")
                    vrow = fetch_gvbl_url(polite, vurl, "gvbl_volume", None, root,
                                          done, {"gvbl_year": year})
                    volumes[year] = bh.parse_gvbl_volume(bh.get_raw(
                        "gvbl", vrow["sha256"], vrow.get("ext", "html"), root)) \
                        if vrow and vrow.get("status") == "ok" else []
                issue = next((i for i in volumes[year]
                              if i["first_page"] <= page <= i["last_page"]), None)
                if issue is None:
                    stats["issue_not_found"] += 1
                    continue
                prow = fetch_gvbl_url(polite, VB + issue["pdf"].split("#")[0],
                                      "gvbl_issue_pdf", act_id, root, done,
                                      {"gvbl_year": year, "gvbl_issue": issue["issue"],
                                       "gvbl_first_page": issue["first_page"],
                                       "gvbl_last_page": issue["last_page"],
                                       "gvbl_issue_date": issue["date"]})
                if prow and prow.get("status") == "ok":
                    act = bh.parse_gvbl_pdf_act(
                        bh.get_raw("gvbl", prow["sha256"], "pdf", root),
                        issue["first_page"], page,
                        hint.isoformat() if hint else None)
                    if not any("heading date not found" in w for w in act["warnings"]):
                        verified = True
                        stats["issue_pdf_verified"] += 1
                        break
                    stats["issue_pdf_unverified"] += 1
                else:
                    stats["issue_pdf_error"] += 1
            if not verified:
                stats["unverified"] += 1
                print(f"  [unverified] {act_id} GVBl. S. {entry['page']} "
                      f"({entry['act_date_text']})", file=sys.stderr)
    return dict(stats)


# ------------------------------------------------------------------ current

def capture_current(polite: Polite, act_ids: list[str], root) -> dict:
    stats = defaultdict(int)
    for act_id in act_ids:
        key = bh.ACTS[act_id]["key"]
        url = f"https://www.gesetze-bayern.de/Content/Zip/{key}"
        response, error = polite.get(url)
        base = {"act_id": act_id, "kind": "zip", "document": key,
                "original_url": url, "archive_url": None,
                "capture_timestamp": None, "captured_at": bh.utc_now(),
                "cdx_digest": None, "fetched_at": bh.utc_now()}
        ok = response is not None and response.status_code == 200
        if ok:
            try:
                zipfile.ZipFile(io.BytesIO(response.content)).testzip()
            except zipfile.BadZipFile:
                ok, error = False, "not a zip archive"
        if not ok:
            if response is not None and b"Security Check" in response.content[:4000]:
                error = "security_check_challenge (Myra); not bypassed"
            bh.append_ledger("bayern_recht", {
                **base, "http_status": getattr(response, "status_code", None),
                "media_type": None, "sha256": None, "bytes": 0,
                "status": "error", "error": error}, root)
            stats["error"] += 1
            if error and "security_check" in error:
                break               # the host challenges us: stop at once
            continue
        digest = bh.put_raw("bayern_recht", response.content, "zip", root)
        bh.append_ledger("bayern_recht", {
            **base, "http_status": 200, "media_type": _media(response),
            "ext": "zip", "sha256": digest, "bytes": len(response.content),
            "status": "ok", "error": None}, root)
        stats["ok"] += 1
    return dict(stats)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("wayback", "gvbl", "current"))
    parser.add_argument("--acts", default=",".join(bh.ACTS))
    parser.add_argument("--limit", type=int, default=None,
                        help="max new fetches per act (wayback)")
    parser.add_argument("--kinds", default=None,
                        help="comma list of zip,full,landing,norm,anlage")
    parser.add_argument("--root", default=None, help="archive root override")
    args = parser.parse_args()
    act_ids = [a.strip() for a in args.acts.split(",") if a.strip()]
    unknown = [a for a in act_ids if a not in bh.ACTS]
    if unknown:
        print(f"unknown acts: {unknown}", file=sys.stderr)
        return 2
    polite = Polite()
    try:
        if args.command == "wayback":
            kinds = set(args.kinds.split(",")) if args.kinds else None
            for act_id in act_ids:
                stats = capture_wayback(polite, act_id, args.root, args.limit, kinds)
                print(f"{act_id}: {stats}", flush=True)
        elif args.command == "gvbl":
            print(capture_gvbl(polite, act_ids, args.root), flush=True)
        else:
            print(capture_current(polite, act_ids, args.root), flush=True)
    except StopRun as exc:
        print(f"stopped: {exc}; rerun to resume", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
