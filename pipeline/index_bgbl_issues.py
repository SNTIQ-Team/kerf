"""Complete index of electronic BGBl I issues (recht.bund.de, 2023+).

Lexgraph used to capture only the BGBl documents that GII's "zuletzt
geändert durch" note cited at some point, so an amendment that was never an
act's *latest* amendment during Lexgraph's lifetime was missing from the
event history.  Since 2023 every promulgation has an official page
(``/bgbl/1/<year>/<nr>/VO.html``) whose metadata lists the FNA numbers of
every amended act.  This tool enumerates every issue of a year, parses that
metadata (the same parser as pipeline/fetch_bgbl_documents.py) and keeps a
cumulative index; ``fetch_bgbl_documents.py --from-index`` then captures the
final documents that amend corpus acts.

Output: ``data/bgbl_documents/index/bgbl1-<year>.jsonl`` (one row per issue,
``status`` ok | missing), resumable; issues already indexed as ``ok`` are
not requested again.

Usage:
    python3 pipeline/index_bgbl_issues.py --years 2023,2024,2025,2026 \
        [--delay 1.0] [--max-requests 2000] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from common import ROOT, Http
from fetch_bgbl_documents import SourceIntegrityError, canonical_urls, \
    parse_vo_html

INDEX_ROOT = ROOT / "data" / "bgbl_documents" / "index"
MISSING_STOP = 12          # consecutive missing issue numbers end a year


def _load(path: Path) -> dict[int, dict]:
    rows: dict[int, dict] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                rows[int(row["issue"])] = row
    return rows


def _write(path: Path, rows: dict[int, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for issue in sorted(rows):
            handle.write(json.dumps(rows[issue], ensure_ascii=False,
                                    sort_keys=True) + "\n")
    os.replace(tmp, path)


def fna_list(value: str | None) -> list[str]:
    """``"860-2, 860-2, neu: 202-5-25"`` → ``["860-2", "202-5-25"]``."""
    out: list[str] = []
    for part in str(value or "").replace(";", ",").split(","):
        part = part.strip().removeprefix("neu:").strip()
        if part and part not in out:
            out.append(part)
    return out


def index_year(http: Http, year: int, out: Path, *, budget: list[int],
               refresh_tail: int = 5) -> dict:
    path = out / f"bgbl1-{year}.jsonl"
    rows = _load(path)
    known_ok = [i for i, r in rows.items() if r.get("status") == "ok"]
    # re-check the tail: new issues appear during the current year
    start = 1
    missing_run = 0
    issue = start
    fetched = 0
    while budget[0] > 0:
        row = rows.get(issue)
        if row and row.get("status") == "ok" and issue < max(
                known_ok or [0]) - refresh_tail:
            missing_run = 0
            issue += 1
            continue
        urls = canonical_urls(year, issue)
        budget[0] -= 1
        response = http.get(urls["html"], timeout=60)
        fetched += 1
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if response.status_code == 404:
            rows[issue] = {"year": year, "issue": issue, "status": "missing",
                           "checked_at": now}
            missing_run += 1
            if missing_run >= MISSING_STOP:
                break
            issue += 1
            continue
        response.raise_for_status()
        missing_run = 0
        try:
            parsed = parse_vo_html(response.content, urls["html"])
        except SourceIntegrityError as exc:
            rows[issue] = {"year": year, "issue": issue,
                           "status": "unparsed", "error": str(exc),
                           "checked_at": now}
            issue += 1
            continue
        fields = parsed["official_fields"]
        rows[issue] = {
            "year": year, "issue": issue, "status": "ok",
            "document_id": f"bgbl-1-{year}-{issue}",
            "title": parsed["title"], "type": fields.get("Typ"),
            "publication_date": parsed["publication_date"],
            "execution_date": parsed["execution_date"],
            "fna": fna_list(fields.get("FNA")),
            "fna_raw": fields.get("FNA"),
            "sachgebiet": fields.get("Sachgebiet"),
            "official_html_url": parsed["canonical_url"],
            "official_pdf_url": parsed["pdf_url"],
            "eli": parsed["eli"],
            "advertised_md5": parsed["advertised_md5"],
            "official_fields": fields,
            "checked_at": now,
        }
        if fetched % 25 == 0:
            _write(path, rows)
        issue += 1
    # trailing "missing" rows beyond the last real issue are not facts
    last_ok = max((i for i, r in rows.items() if r.get("status") == "ok"),
                  default=0)
    rows = {i: r for i, r in rows.items() if i <= last_ok or
            r.get("status") != "missing"}
    _write(path, rows)
    return {"year": year, "issues": sum(1 for r in rows.values()
                                        if r.get("status") == "ok"),
            "last_issue": last_ok, "requests": fetched}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--years", default="2023,2024,2025,2026")
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--max-requests", type=int, default=2500)
    parser.add_argument("--out", type=Path, default=INDEX_ROOT)
    args = parser.parse_args(argv)
    if args.delay < 0.5:
        parser.error("--delay must be at least 0.5 seconds")
    http = Http(delay=args.delay, retries=4)
    budget = [args.max_requests]
    results = []
    for year in [int(y) for y in args.years.split(",") if y.strip()]:
        if year < 2023:
            parser.error("recht.bund.de promulgation starts in 2023")
        results.append(index_year(http, year, args.out, budget=budget))
        print(json.dumps(results[-1]), flush=True)
        if budget[0] <= 0:
            print("request budget exhausted; rerun to resume", flush=True)
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
