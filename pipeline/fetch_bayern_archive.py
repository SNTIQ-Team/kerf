"""Bavarian acts outside the live BAYERN.RECHT feed, from archived official copies.

gesetze-bayern.de answers automated requests with a Myra "Security Check" challenge
(HTTP 503) since 2026-09; it is never bypassed.  The Internet Archive holds captures
of the portal's own official XML export (``/Content/Zip/<KEY>``, the file
``pipeline/fetch_bayern_recht.py`` parses) and of its per-article pages
(``/Content/Document/<KEY>-<n>`` with the official "Text gilt ab" date).  Those
archived official bytes are used, with provenance for every one:

* every ZIP capture (one per distinct Wayback digest) is fetched raw (``id_``),
  CRC-checked (``zipfile.testzip``) and parsed with ``tools/bavarian_history.parse_zip``
  (``fetch_bayern_recht.parse_act_xml`` plus its old-export title repair); a corrupt,
  empty or unparsable capture is recorded as rejected, never repaired;
* for acts with ``pages``, the newest archived page of each article captured after
  the newest intact ZIP is read (``tools/bavarian_history.parse_bayern_html``, raw
  HTML cached) so a later amendment is not missed; its "Text gilt ab" dates the
  act's consolidated version shown (the wording is in force at the latest then);
* GVBl promulgation events (``gvbl_events`` snapshots, official RSS of
  verkuendung-bayern.de) that name the act's BayRS number after the newest capture
  are listed as ``unincorporated_gvbl_events``: the text may be stale.

Output: ``data/snapshots/bayern_recht_archive/<date>/acts.jsonl``.

Usage:
    python3 pipeline/fetch_bayern_archive.py [--acts BayDSG] [--offline]
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import sys
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

sys.path.insert(0, str(ROOT))

from common import latest_snapshot, read_jsonl, snapshot_dir, write_jsonl  # noqa: E402
import fetch_bayern_wayback as wayback  # noqa: E402
from tools import bavarian_history  # noqa: E402  (the workstation Bavarian parsers)

UA = "SNTIQ-lexgraph/0.1 (research; +https://sntiq.com/)"
ZIP_URL = "https://www.gesetze-bayern.de/Content/Zip/{key}"
CDX = ("https://web.archive.org/cdx/search/cdx?url=gesetze-bayern.de/Content/Zip/{key}"
       "&output=json&filter=statuscode:200&collapse=digest&fl=timestamp,digest,length,original")
CACHE = ROOT / "data" / "snapshots" / "wayback_by" / "zip"

# doc key -> options.  BayDSG: data-protection access cases (Art. 10, 20 BayDSG);
# BayBGG: disability equality (social cases); BayKiBiG: childcare (AsylbLG/SGB II
# families); BayLTGO: Geschäftsordnung des Landtags (petitions), ZIP only.
ACTS = {
    "BayDSG": {"pages": True},
    "BayBGG": {"pages": True},
    "BayKiBiG": {"pages": True},
    "BayLTGO": {"pages": False},
}


def _get(url: str, *, delay: float = 2.0) -> bytes | None:
    import requests
    for attempt in range(4):
        time.sleep(delay)
        try:
            response = requests.get(url, headers={"User-Agent": UA}, timeout=120)
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
            continue
        if response.status_code == 200:
            return response.content
        if response.status_code in {429, 500, 502, 503, 504}:
            time.sleep(10 * (attempt + 1))
            continue
        return None
    return None


def zip_captures(key: str, *, offline: bool) -> list[dict]:
    index = CACHE / f"{key}.cdx.json"
    rows = json.loads(index.read_text()) if index.is_file() else None
    if not offline:
        body = _get(CDX.format(key=key), delay=3.0)
        if not body:
            # main retains the last complete act; only explicit offline mode
            # may use a cached index without claiming a successful refresh.
            raise RuntimeError(f"{key}: archive index refresh failed")
        rows = json.loads(body)[1:]
        if not rows:
            raise RuntimeError(f"{key}: archive index refresh failed (no captures)")
        bavarian_history._atomic_write(index, json.dumps(rows).encode())
    captures = []
    for row in rows or []:
        capture = {"timestamp": row[0], "digest": row[1]}
        if len(row) > 3 and row[3]:
            capture["original_url"] = row[3]
        captures.append(capture)
    return captures


def fetch_zip(key: str, capture: dict, *, offline: bool) -> dict:
    ts = capture["timestamp"]
    original = capture.get("original_url")
    replay_target = original or ZIP_URL.format(key=key)
    if not re.fullmatch(r"https?://(?:www\.)?gesetze-bayern\.de(?::(?:80|443))?/Content/Zip/"
                        + re.escape(key), replay_target, re.I):
        raise ValueError(f"{key}: unexpected original URL in archive index")
    url = f"https://web.archive.org/web/{ts}id_/{replay_target}"
    path = CACHE / f"{key}-{ts}.zip"
    data = path.read_bytes() if path.is_file() else None
    if not offline and (not data or base64.b32encode(hashlib.sha1(data).digest()).decode()
                        != capture["digest"]):
        data = _get(url)
    row = {"timestamp": ts, "archive_url": url,
           "original_url": original, "wayback_digest": capture["digest"],
           "sha256": hashlib.sha256(data).hexdigest() if data else None,
           "bytes_len": len(data) if data else 0}
    if not data:
        return {**row, "status": "rejected", "reason": "not retrievable"}
    if base64.b32encode(hashlib.sha1(data).digest()).decode() != capture["digest"]:
        return {**row, "status": "rejected", "reason": "CDX digest mismatch"}
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        bad = archive.testzip()
        if bad:
            return {**row, "status": "rejected", "reason": f"CRC error in {bad}"}
        name = f"bayportalnorm/{key}.xml"
        xml = archive.read(name)
        parsed = bavarian_history.parse_zip(data, key, key, include_annexes=True)
    except Exception as exc:  # noqa: BLE001 — a damaged capture is evidence, not text
        return {**row, "status": "rejected", "reason": f"{type(exc).__name__}: {exc}"[:200]}
    norms, versions = parsed["norms"], parsed["aenderungsverlauf"]
    if not norms:
        return {**row, "status": "rejected", "reason": "no einzelnorm in the XML"}
    if not offline and (not path.is_file() or path.read_bytes() != data):
        bavarian_history._atomic_write(path, data)
    title = parsed["title"]
    text = xml.decode("utf-8", "replace")
    bayrs = re.search(r"<gliederungsNr\.BayRS>\s*(?:BayRS\s*)?([^<]+?)\s*</gliederungsNr\.BayRS>", text)
    signed = re.search(r"<ausfertigungsdatum>(\d{4}-\d{2}-\d{2})</ausfertigungsdatum>", text)
    fassung = re.search(r"<fassung>.*?<jahr>\s*(\d{4})\s*</jahr>\s*<seite>(?:S=)?(\d+)</seite>",
                        text, re.S)
    return {**row, "status": "ok", "builddate": parsed.get("builddate"),
            "ausfertigung": signed.group(1) if signed else None,
            "gvbl": (f"GVBl. {fassung.group(1)} S. {fassung.group(2)}" if fassung else None),
            "jurabk": parsed.get("jurabk"), "bayrs_nr": bayrs.group(1) if bayrs else None,
            "inkraft": parsed.get("inkraft"), "title": " ".join(title.split()),
            "parse_warnings": parsed.get("warnings") or [],
            "norms": [{"enbez": n["enbez"], "titel": n["titel"], "text": n["text"]}
                      for n in norms],
            "versions": versions}


PAGE_CACHE = ROOT / "data" / "snapshots" / "wayback_by" / "pages-html"


def fetch_page(doc: str, ts: str, *, offline: bool) -> dict | None:
    import gzip
    path = PAGE_CACHE / f"{doc}-{ts}.html.gz"
    url = wayback.PAGE.format(ts=ts, doc=doc)
    if path.is_file():
        raw = gzip.decompress(path.read_bytes())
    elif offline:
        return None
    else:
        raw = _get(url, delay=5.0)   # the Internet Archive throttles ~15 req/min
        if raw is None:
            return None
        PAGE_CACHE.mkdir(parents=True, exist_ok=True)
        path.write_bytes(gzip.compress(raw, 9, mtime=0))
    head = raw[:3000].decode("utf-8", "replace")
    if "Security Check" in head or "challenge" in head.lower():
        return None                     # an archived challenge page is no text
    key = doc.rsplit("-", 1)[0]
    parsed = bavarian_history.parse_bayern_html(raw, key, key, "norm")
    if not parsed.get("norms") or not parsed["norms"][0].get("text"):
        return None
    norm = parsed["norms"][0]
    fields = (parsed.get("metadata") or {}).get("fields") or {}
    valid = fields.get("Text gilt ab") or fields.get("in Kraft ab") or {}
    return {"timestamp": ts, "valid_from": valid.get("date"), "text": norm["text"],
            "titel": norm.get("titel") or "", "page_enbez": norm.get("enbez"),
            "archive_url": url, "sha256": hashlib.sha256(raw).hexdigest()}


def newer_pages(key: str, after: str, norms: list[dict], *, offline: bool) -> list[dict]:
    """Newest archived page per article captured after ``after`` (a Wayback ts)."""
    index = wayback._merge_capture_indexes(
        wayback.cdx_act(key, offline=offline), wayback.legacy_document_cdx_index(key),
        wayback.cached_page_index(key))
    numbers = {}
    for norm in norms:
        match = re.match(r"(?:Art\.|§)\s*(\d+[a-z]?)", norm["enbez"])
        if match:
            numbers[match.group(1)] = norm["enbez"]
    prefix = "§" if norms and norms[0]["enbez"].startswith("§") else "Art."
    out = []
    for doc, stamps in sorted(index.items()):
        nr = doc.rsplit("-", 1)[-1]
        later = sorted(ts for ts in stamps if ts > after)
        page = None
        for ts in reversed(later[-3:]):   # newest first; a failed capture falls back
            page = fetch_page(doc, ts, offline=offline)
            if page is not None:
                break
        if page is None:
            continue
        out.append({"doc": doc, "enbez": numbers.get(nr) or f"{prefix} {nr}", "number": nr,
                    "known_article": nr in numbers, "captures_after_zip": len(later), **page})
    return out


def gvbl_after(bayrs: str | None, after_day: str) -> list[dict]:
    if not bayrs:
        return []
    base = ROOT / "data" / "snapshots" / "gvbl_events"
    seen: dict[str, dict] = {}
    for path in sorted(base.glob("*/events.jsonl")) if base.is_dir() else []:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            numbers = [x.strip() for x in str(event.get("gliederungs_nr") or "").split(",")]
            if bayrs in numbers and str(event.get("time") or "") > after_day:
                seen[str(event.get("permalink"))] = {
                    "date": event.get("time"), "title": event.get("title"),
                    "gazette": event.get("gazette"), "url": event.get("permalink")}
    return sorted(seen.values(), key=lambda e: str(e["date"]))


def fetch_act(key: str, options: dict, *, offline: bool) -> dict:
    captures = [fetch_zip(key, c, offline=offline) for c in zip_captures(key, offline=offline)]
    good = [c for c in captures if c["status"] == "ok"]
    if not good:
        raise RuntimeError(f"{key}: no intact archived ZIP capture")
    head = good[-1]
    pages = newer_pages(key, head["timestamp"], head["norms"], offline=offline) \
        if options.get("pages") else []
    newest = max([head["timestamp"]] + [p["timestamp"] for p in pages])
    day = f"{newest[:4]}-{newest[4:6]}-{newest[6:8]}"
    return {
        "key": key, "jurabk": head["jurabk"], "bayrs_nr": head["bayrs_nr"],
        "title": head["title"], "inkraft": head["inkraft"],
        "ausfertigung": head.get("ausfertigung"), "gvbl": head.get("gvbl"),
        "permalink": f"https://www.gesetze-bayern.de/Content/Document/{key}",
        "zip_captures": captures, "pages": pages,
        "unincorporated_gvbl_events": gvbl_after(head["bayrs_nr"], day),
        "source": {"name": "Internet Archive captures of BAYERN.RECHT (Bayerische "
                           "Staatskanzlei)", "reason": "gesetze-bayern.de answers with a "
                   "Myra challenge; not bypassed"},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--acts", default="")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    wanted = [a for a in args.acts.split(",") if a] or list(ACTS)
    rows, failed = [], []
    for key in wanted:
        try:
            row = fetch_act(key, ACTS.get(key, {}), offline=args.offline)
        except Exception as exc:  # noqa: BLE001
            failed.append(key)
            print(f"[err] {key}: {exc}", file=sys.stderr)
            continue
        ok = sum(1 for c in row["zip_captures"] if c["status"] == "ok")
        print(f"  {key:<10} {row['jurabk']:<10} ZIP captures {ok}/{len(row['zip_captures'])} "
              f"intact, newer pages {len(row['pages'])}, GVBl after "
              f"{len(row['unincorporated_gvbl_events'])}", flush=True)
        rows.append(row)
    previous = latest_snapshot("bayern_recht_archive")
    if previous and (previous / "acts.jsonl").is_file():
        rows = retain_observations(read_jsonl(previous / "acts.jsonl"), rows)
    if not rows:
        return 1
    out = snapshot_dir("bayern_recht_archive")
    write_jsonl(out / "acts.jsonl", rows)
    print(f"{len(rows)} acts -> {out}")
    return 1 if failed else 0


def retain_observations(previous, fresh: list[dict]) -> list[dict]:
    """Keep untouched acts and previously verified ZIPs/pages across partial runs."""
    acts = {row["key"]: row for row in previous}
    for row in fresh:
        old = acts.get(row["key"])
        if old:
            captures = {c["timestamp"]: c for c in old["zip_captures"]}
            for c in row["zip_captures"]:
                prior = captures.get(c["timestamp"])
                if prior and prior["status"] == "ok":
                    if c["status"] == "ok" and c["sha256"] != prior["sha256"]:
                        raise ValueError(f"conflicting archived bytes at {c['archive_url']}")
                    continue
                captures[c["timestamp"]] = c
            pages = {(p["doc"], p["timestamp"]): p for p in old["pages"]}
            for p in row["pages"]:
                pages.setdefault((p["doc"], p["timestamp"]), p)
            head = max((c for c in captures.values() if c["status"] == "ok"),
                       key=lambda c: c["timestamp"])
            # A partial or stale index may omit a newer retained export.
            row = {**row, **{k: head.get(k) for k in
                              ("jurabk", "bayrs_nr", "title", "inkraft", "ausfertigung", "gvbl")},
                   "zip_captures": [captures[t] for t in sorted(captures)],
                   "pages": [pages[k] for k in sorted(pages)]}
            if old.get("quoted_amendment_fragments"):
                row["quoted_amendment_fragments"] = old["quoted_amendment_fragments"]
        acts[row["key"]] = row
    return list(acts.values())


if __name__ == "__main__":
    raise SystemExit(main())
