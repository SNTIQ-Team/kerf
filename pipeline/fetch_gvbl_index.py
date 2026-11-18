"""Capture the Bavarian promulgation index for links by exact BayRS number."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from common import Http, SNAPSHOTS, write_jsonl

BASE = "https://www.verkuendung-bayern.de/gvbl/"


def official_date(cell):
    text = cell.get_text(strip=True)
    if text in {"", "–", "-"}:
        return None
    return datetime.strptime(text, "%d.%m.%Y").date().isoformat()


def page(body, offset):
    soup = BeautifulSoup(body, "html.parser")
    rows = []
    for tr in soup.select("table#search-results-table tbody tr"):
        cells = {td.get("data-label"): td for td in tr.select("td[data-label]")}
        link = cells["Fundstelle"].find("a", href=True)
        path = link["href"]
        match = re.fullmatch(r"/gvbl/(\d{4}-[\w.-]+)/", path)
        if not match:
            raise ValueError(f"unexpected official GVBl permalink: {path}")
        title = " ".join(cells["Titel"].stripped_strings)
        title = " ".join(title.split())
        publication = official_date(cells["Verkündung"])
        if not title or not publication:
            raise ValueError(f"incomplete GVBl identity: {path}")
        numbers = " ".join(cells["Gl-Nr."].get_text(" ", strip=True).split()).strip()
        rows.append({
            "event_id": f"event:gvbl:{match.group(1)}:published",
            "gazette": "GVBl", "time": publication, "title": title,
            "ausfertigung_date": official_date(cells["Ausfertigung"]),
            "gliederungs_nr": numbers or None, "permalink": urljoin(BASE, path),
            "authenticity": "nachrichtlich",
        })
    if not rows:
        raise ValueError(f"empty official result table at {offset}")
    forward = soup.select_one("a.page-link[aria-label=Weiterblättern][href]")
    if forward is None:
        return rows, None
    link = re.fullmatch(r"\?offset=(\d+)#page-browser", forward["href"])
    if not link or int(link.group(1)) != offset + len(rows):
        raise ValueError(f"inconsistent official pagination at {offset}")
    return rows, int(link.group(1))


def capture(directory, day):
    http = Http(delay=1.0)
    http.s.headers["User-Agent"] = "SNTIQ-Lexgraph/1 (+https://sntiq.com/)"
    pages = directory / "pages"
    pages.mkdir(exist_ok=True)
    seen = {}
    sources = []
    offset = 1
    while offset is not None:
        if len(sources) >= 250:
            raise ValueError("official result exceeds the bounded index capture")
        url = BASE if offset == 1 else f"{BASE}?offset={offset}"
        response = http.get(url, timeout=35)
        response.raise_for_status()
        if response.url.rstrip("/") != url.rstrip("/") or len(response.content) > 1_500_000:
            raise ValueError(f"unexpected index response at {url}")
        rows, following = page(response.content, offset)
        digest = hashlib.sha256(response.content).hexdigest()
        (pages / f"{offset}.html.gz").write_bytes(gzip.compress(response.content, mtime=0))
        sources.append({"offset": offset, "url": url, "count": len(rows),
                        "sha256": digest, "retrieved_at": datetime.now(timezone.utc).isoformat()})
        for row in rows:
            key = row["event_id"]
            if key in seen:
                raise ValueError(f"repeated document across official pages: {key}")
            seen[key] = row
        if len(sources) % 20 == 0:
            print(f"{len(sources)} pages, {len(seen)} publications", flush=True)
        offset = following
    write_jsonl(directory / "events.jsonl", seen.values())
    (directory / "coverage.json").write_text(json.dumps({
        "captured_on": day, "scope": "public GVBl document index", "count": len(seen),
        "first_published": min(r["time"] for r in seen.values()),
        "last_published": max(r["time"] for r in seen.values()), "pages": sources,
    }, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="new snapshot directory")
    args = parser.parse_args()
    day = datetime.now(timezone.utc).date().isoformat()
    target = args.output or SNAPSHOTS / "gvbl_index" / day
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        parser.error("snapshot already exists")
    # Keep incomplete generations outside the directory scanned by latest_snapshot.
    with tempfile.TemporaryDirectory(dir=target.parent.parent) as tmp:
        staging = Path(tmp) / "capture"
        staging.mkdir()
        capture(staging, day)
        os.rename(staging, target)
    print(target)


if __name__ == "__main__":
    main()
