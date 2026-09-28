"""Collect NeuRIS LegalDocML expressions of the priority acts into the
history archive (source ``neuris``).

1. **import**: expression zips already captured by
   ``fetch_neuris_changelog.py`` (``data/neuris_archive.jsonl`` +
   ``data/neuris_objects/<sha>.zip`` in the main checkout, read-only) are
   copied gzip-compressed into ``R/raw/neuris/`` with one ledger row each.
2. **current**: ``GET /v1/legislation?eli=<work>&size=100`` lists the
   expressions NeuRIS serves today (usually only the current one); every zip
   not yet stored is downloaded politely (>= 1.5 s between requests).
3. **resolve**: acts without a known work ELI are searched with the long
   title (``searchTerm``, exact phrase, a few pages) and accepted only when a
   result's abbreviation equals the act's GII abbreviation exactly.  Misses
   are recorded in ``R/neuris_work_resolution.json`` with the queries tried.

ELI point in time / manifestation date are source identifiers, never legal
dates.

Usage::

    python3 pipeline/fetch_history_neuris.py [--acts fed_sgg] [--no-fetch]
        [--main-data /path/to/Gesetz/data]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from history_archive import (  # noqa: E402
    CAPTURE_SCHEMA, REPO_ROOT, USER_AGENT, HistoryArchive, sha256_hex,
    utc_now)

SOURCE = "neuris"
BASE = "https://testphase.rechtsinformationen.bund.de"
ELI_RE = re.compile(
    r"(?P<work>eli/bund/[^/]+/\d{4}/[^/]+)/(?P<pit>\d{4}-\d{2}-\d{2})"
    r"/(?P<ver>\d+)/(?P<lang>[a-z]{3})(?:/(?P<manifested>\d{4}-\d{2}-\d{2}))?")

# act_id -> (NeuRIS work ELI or None, GII jurabk, long title for searching)
PRIORITY_WORKS: dict[str, tuple[str | None, tuple[str, ...], str]] = {
    "fed_asylblg": (None, ("AsylbLG",), "Asylbewerberleistungsgesetz"),
    "fed_sgb_2": ("eli/bund/bgbl-1/2003/s2955", ("SGB 2", "SGB II"),
                  "Grundsicherung für Arbeitsuchende"),
    "fed_sgb_12": ("eli/bund/bgbl-1/2003/s3023", ("SGB 12", "SGB XII"),
                   "Sozialhilfe"),
    "fed_sgb_10": (None, ("SGB 10", "SGB X"),
                   "Sozialverwaltungsverfahren und Sozialdatenschutz"),
    "fed_sgg": ("eli/bund/bgbl-1/1953/s1239", ("SGG",), "Sozialgerichtsgesetz"),
    "fed_vwgo": ("eli/bund/bgbl-1/1960/s17", ("VwGO",),
                 "Verwaltungsgerichtsordnung"),
    "fed_vwvfg": ("eli/bund/bgbl-1/1976/s1253", ("VwVfG",),
                  "Verwaltungsverfahrensgesetz"),
    "fed_aufenthg_2004": ("eli/bund/bgbl-1/2004/s1950",
                          ("AufenthG", "AufenthG 2004"), "Aufenthaltsgesetz"),
    "fed_asylvfg_1992": ("eli/bund/bgbl-1/1992/s1126",
                         ("AsylG", "AsylVfG 1992"), "Asylgesetz"),
    "fed_azrg": ("eli/bund/bgbl-1/1994/s2265", ("AZRG",),
                 "Gesetz über das Ausländerzentralregister"),
    "fed_bgb": (None, ("BGB",), "Bürgerliches Gesetzbuch"),
    "fed_zpo": (None, ("ZPO",), "Zivilprozessordnung"),
    "fed_gg": ("eli/bund/bgbl/1949/s1", ("GG",),
               "Grundgesetz für die Bundesrepublik Deutschland"),
}


# Promulgation ELIs probed with ``eli=`` for acts the title search misses
# (original promulgation and latest Neubekanntmachung); a hit still needs the
# exact abbreviation.
CANDIDATE_WORKS: dict[str, tuple[str, ...]] = {
    "fed_asylblg": ("eli/bund/bgbl-1/1993/s1074", "eli/bund/bgbl-1/1997/s2022"),
    "fed_sgb_10": ("eli/bund/bgbl-1/1980/s1469", "eli/bund/bgbl-1/2001/s130"),
    "fed_bgb": ("eli/bund/rgbl/1896/s195", "eli/bund/rgbl-1/1896/s195",
                "eli/bund/bgbl-1/2002/s42"),
    "fed_zpo": ("eli/bund/bgbl/1950/s533", "eli/bund/bgbl-1/1950/s533",
                "eli/bund/bgbl-1/2005/s3202"),
}


def norm_abbr(value: str | None) -> str:
    return " ".join(str(value or "").split()).casefold()


class Client:
    def __init__(self, delay: float = 1.5, max_requests: int = 200,
                 max_bytes: int = 512 * 1024 ** 2):
        self.delay = max(1.0, delay)
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.requests = 0
        self.bytes = 0
        self._last = 0.0

    def get(self, url: str, **params) -> requests.Response | None:
        if self.requests >= self.max_requests or self.bytes >= self.max_bytes:
            raise RuntimeError("NeuRIS request/byte budget exhausted")
        for attempt in range(3):
            wait = self.delay * (1 if attempt == 0 else 20 * attempt) - (
                time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.requests += 1
            try:
                response = self.session.get(url, params=params or None,
                                            timeout=120)
            except requests.RequestException as exc:
                print(f"  [neuris] {exc}", file=sys.stderr)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                print(f"  [neuris] http {response.status_code}", file=sys.stderr)
                continue
            self.bytes += len(response.content)
            return response
        return None


def ledger_keys(archive: HistoryArchive) -> tuple[set[str], set[str]]:
    urls, shas = set(), set()
    for row in archive.read_ledger(SOURCE):
        if row.get("status") == "ok":
            urls.add(row.get("original_url"))
            shas.add(row.get("sha256"))
    return urls, shas


def eli_parts(url: str) -> dict:
    match = ELI_RE.search(url or "")
    if not match:
        return {}
    work = match.group("work")
    return {"eli_work": work,
            "expression_eli": f"{work}/{match.group('pit')}/"
                              f"{match.group('ver')}/{match.group('lang')}",
            "point_in_time": match.group("pit"),
            "expression_version": match.group("ver"),
            "manifestation_date": match.group("manifested")}


def import_existing(archive: HistoryArchive, main_data: Path,
                    works: dict[str, str]) -> int:
    """Copy already captured expression zips of the priority works."""
    ledger = main_data / "neuris_archive.jsonl"
    if not ledger.is_file():
        print(f"[import] no {ledger}")
        return 0
    by_work = {work: act for act, work in works.items() if work}
    urls, shas = ledger_keys(archive)
    imported = 0
    with open(ledger, encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            act_id = by_work.get(row.get("eli_work"))
            if not act_id or row.get("capture_status") != "captured":
                continue
            url = row.get("content_source_url") or row.get("content_url")
            obj = row.get("content_object") or ""
            if not url or url in urls or not obj.endswith(".zip"):
                continue
            path = main_data / obj
            if not path.is_file():
                continue
            data = path.read_bytes()
            sha = sha256_hex(data)
            if row.get("content_sha256") and sha != row["content_sha256"]:
                print(f"  [import] sha mismatch {path}", file=sys.stderr)
                continue
            archive.put_raw(SOURCE, data, "zip")
            out = {"schema": CAPTURE_SCHEMA, "source": SOURCE,
                   "act_id": act_id, "kind": "expression_zip",
                   "original_url": url, "archive_url": None,
                   "capture_timestamp": None,
                   "captured_at": row.get("captured_at") or row.get("fetched_at"),
                   "http_status": 200, "media_type": "application/zip",
                   "sha256": sha, "bytes": len(data), "cdx_digest": None,
                   "fetched_at": row.get("captured_at"), "status": "ok",
                   "error": None,
                   "imported_from": f"data/{obj}",
                   "date_basis": "retrieval_observation_and_eli_identifiers_not_legal_effect"}
            out.update(eli_parts(url))
            archive.append_ledger(SOURCE, out)
            urls.add(url)
            shas.add(sha)
            imported += 1
            print(f"  [import] {act_id} {out.get('expression_eli')} "
                  f"{out.get('manifestation_date')} sha {sha[:12]}")
    return imported


def resolve_work(client: Client, act_id: str, abbrs: tuple[str, ...],
                 title: str, pages: int = 5) -> dict:
    wanted = {norm_abbr(a) for a in abbrs}
    found: dict[str, str] = {}
    seen = 0
    for page in range(pages):
        response = client.get(f"{BASE}/v1/legislation",
                              searchTerm=f'"{title}"', size=100,
                              pageIndex=page)
        if response is None or response.status_code != 200:
            break
        try:
            data = response.json()
        except ValueError:
            break
        members = data.get("member") or []
        seen += len(members)
        for wrapper in members:
            item = wrapper.get("item") or {}
            if norm_abbr(item.get("abbreviation")) in wanted:
                work = (item.get("exampleOfWork") or {}).get(
                    "legislationIdentifier")
                if work:
                    found[work] = item.get("abbreviation")
        if len(members) < 100:
            break
    probed = {}
    for eli in CANDIDATE_WORKS.get(act_id, ()):
        response = client.get(f"{BASE}/v1/legislation", eli=eli, size=100)
        if response is None or response.status_code != 200:
            probed[eli] = "request_failed"
            continue
        try:
            members = response.json().get("member") or []
        except ValueError:
            members = []
        listed = [(m.get("item") or {}).get("abbreviation") for m in members]
        probed[eli] = listed
        if any(norm_abbr(a) in wanted for a in listed):
            found[eli] = next(a for a in listed if norm_abbr(a) in wanted)
    result = {"act_id": act_id, "queries": [f'searchTerm="{title}"']
              + [f"eli={e}" for e in CANDIDATE_WORKS.get(act_id, ())],
              "abbreviations": list(abbrs), "results_scanned": seen,
              "eli_probes": probed, "checked_at": utc_now()}
    if len(found) == 1:
        result.update({"status": "resolved", "eli_work": next(iter(found))})
    else:
        result.update({"status": "not_found" if not found else "ambiguous",
                       "candidates": found})
    return result


def fetch_current(client: Client, archive: HistoryArchive, act_id: str,
                  work: str) -> int:
    response = client.get(f"{BASE}/v1/legislation", eli=work, size=100)
    if response is None or response.status_code != 200:
        print(f"  [current] {act_id}: listing failed")
        return 0
    try:
        members = response.json().get("member") or []
    except ValueError:
        return 0
    urls, shas = ledger_keys(archive)
    fetched = 0
    if not members:
        print(f"  [current] {act_id}: NeuRIS lists no expression for {work}")
    for wrapper in members:
        item = wrapper.get("item") or {}
        for enc in item.get("encoding") or []:
            if enc.get("encodingFormat") != "application/zip":
                continue
            url = BASE + enc["contentUrl"] if enc["contentUrl"].startswith("/") \
                else enc["contentUrl"]
            if url in urls:
                print(f"  [current] {act_id} already stored {url.rsplit('/', 5)[-5:]}")
                continue
            got = client.get(url)
            row = {"schema": CAPTURE_SCHEMA, "source": SOURCE,
                   "act_id": act_id, "kind": "expression_zip",
                   "original_url": url, "archive_url": None,
                   "capture_timestamp": None, "captured_at": utc_now(),
                   "fetched_at": utc_now(), "cdx_digest": None,
                   "listing_abbreviation": item.get("abbreviation"),
                   "legislation_legal_force": item.get("legislationLegalForce"),
                   "date_basis": "retrieval_observation_and_eli_identifiers_not_legal_effect"}
            row.update(eli_parts(url))
            if got is None or got.status_code != 200 or got.content[:2] != b"PK":
                row.update({"status": "error", "http_status":
                            got.status_code if got is not None else -1,
                            "sha256": None, "bytes": None, "media_type": None,
                            "error": "download failed or not a zip"})
                archive.append_ledger(SOURCE, row)
                continue
            sha, _ = archive.put_raw(SOURCE, got.content, "zip")
            row.update({"status": "ok", "http_status": 200, "sha256": sha,
                        "bytes": len(got.content),
                        "media_type": "application/zip", "error": None})
            archive.append_ledger(SOURCE, row)
            fetched += 1
            print(f"  [current] {act_id} {row.get('expression_eli')} "
                  f"{row.get('manifestation_date')} sha {sha[:12]}")
    return fetched


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--acts")
    ap.add_argument("--archive")
    ap.add_argument("--main-data", default=str(REPO_ROOT / "data"),
                    help="checkout data dir with neuris_archive.jsonl (read-only)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="import only; no network")
    ap.add_argument("--max-requests", type=int, default=120)
    ap.add_argument("--refresh-resolution", action="store_true",
                    help="re-run work searches checked less than 7 days ago")
    args = ap.parse_args(argv)
    archive = HistoryArchive(args.archive)
    acts = args.acts.split(",") if args.acts else list(PRIORITY_WORKS)
    resolution_path = archive.root / "neuris_work_resolution.json"
    try:
        resolution = json.loads(resolution_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        resolution = {}
    works = {}
    for act_id in acts:
        work = PRIORITY_WORKS[act_id][0] or (
            resolution.get(act_id, {}).get("eli_work"))
        works[act_id] = work
    imported = import_existing(archive, Path(args.main_data), works)
    print(f"[import] {imported} expression zips imported")
    if args.no_fetch:
        return 0
    client = Client(max_requests=args.max_requests)
    for act_id in acts:
        work, abbrs, title = PRIORITY_WORKS[act_id]
        if not work:
            previous = resolution.get(act_id, {})
            if previous.get("status") == "resolved":
                work = previous["eli_work"]
            elif (not args.refresh_resolution and previous.get("checked_at")
                  and previous["checked_at"][:10] >= (
                      datetime.now(timezone.utc) - timedelta(days=7)
                  ).strftime("%Y-%m-%d")):
                print(f"  [resolve] {act_id}: {previous.get('status')} "
                      f"(checked {previous['checked_at'][:10]}, not retried)")
            else:
                result = resolve_work(client, act_id, abbrs, title)
                resolution[act_id] = result
                archive.write_json(resolution_path, resolution)
                print(f"  [resolve] {act_id}: {result['status']} "
                      f"{result.get('eli_work') or result.get('candidates')}")
                work = result.get("eli_work")
        if work:
            fetch_current(client, archive, act_id, work)
    print(f"[done] requests={client.requests} "
          f"bytes={client.bytes / 1024 ** 2:.1f} MiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
