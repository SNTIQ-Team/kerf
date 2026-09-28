"""Wayback Machine (web.archive.org): archived copies of primary pages the corpus cannot fetch itself.

The live sites answer automated requests with a bot challenge that is never bypassed (gesetze-bayern.de:
Myra; www.bverwg.de: Cloudflare).  Archived copies involve no challenge: the Internet Archive's CDX API lists
the captures, ``https://web.archive.org/web/<timestamp>id_/<original URL>`` returns the original bytes.

Routes (``sync --source wayback --court by,bverwg``; default both):

* ``by`` - BAYERN.RECHT decision pages (``/Content/Document/Y-300-Z-BECKRS-…``), parsed by
  ``courts.sources.by_portal`` (the same parser as user-browser captures);
* ``bverwg`` - bverwg.de decision pages of the ``bverwg`` items still pending (the decisions RII does not
  publish), both URL schemes (``/de/<id>``, ``/entscheidungen/entscheidung.php?ent=<id>``), parsed by
  ``courts.sources.bverwg``.

These are secondary copies (``sources.source_kind = 'archive'``, ``decisions.metadata.source_kind``): a copy
from the primary source always replaces them (``store.source_rank``); a Wayback copy of a decision the corpus
already holds from a primary source is skipped before fetching or at store time and stays an offline
verification pair.  Provenance: ``decisions.metadata.archive`` = {service, timestamp, archived_url,
original_url}.  An archived challenge page, error page or older page layout is not a decision: the next
capture of the same URL is tried; none usable gives a typed failure.  web.archive.org has no robots.txt
(HTTP 404); requests are paced like any small site.
"""
from __future__ import annotations

import gzip
import json
import re
import sqlite3
from urllib.parse import quote

from ..courtids import priority
from ..detect import classify_page, looks_like_html
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import ParsedDecision
from . import by_portal
from .base import Adapter, Capture, Listing
from ..hostgov import Concurrency

SOURCE_ID = "wayback"
ARCHIVE = "https://web.archive.org"
CDX = ARCHIVE + "/cdx/search/cdx"
ROUTES = ("by", "bverwg")
CDX_QUERIES = {
    "by": ["www.gesetze-bayern.de/Content/Document/Y-300-Z-BECKRS-"],
    "bverwg": ["www.bverwg.de/de/", "www.bverwg.de/entscheidungen/entscheidung.php"],
}
MIN_BY_LENGTH = 6000        # CDX length (compressed bytes); Myra challenge answers are ~3-5 kB
MAX_CAPTURES = 4            # alternatives tried per document, newest first
BY_DOC_RE = re.compile(r"/Content/Document/(" + by_portal.DOC_ID + r")(?:[?#]|$)")
BVERWG_DOC_RE = re.compile(r"(?:/de/|[?&/]ent=)([0-9]{6}[A-Z][0-9]{1,2}[A-Z][A-Za-z]*[0-9]{1,4}\.[0-9]{2}\.[0-9]+)"
                           r"(?:$|[&#%?])")


def archived_url(timestamp: str, original: str) -> str:
    return f"{ARCHIVE}/web/{timestamp}id_/{original}"


def cdx_lines(http: PoliteHttp, prefix: str) -> list[list[str]]:
    """Every capture (status 200, HTML, one row per URL key) under ``prefix``: [original, timestamp, length]."""
    base = (f"{CDX}?url={quote(prefix, safe='/:._-')}&matchType=prefix&filter=statuscode:200"
            f"&filter=mimetype:text/html")
    pages = int(http.get(base + "&collapse=urlkey&showNumPages=true", timeout=180, detect=False).body.strip() or 0)
    rows = []
    for page in range(pages):
        body = http.get(f"{base}&fl=original,timestamp,length&page={page}", timeout=300, detect=False).body
        rows.extend(line.split() for line in body.decode("utf-8", "replace").splitlines() if line.strip())
    return [r for r in rows if len(r) >= 3]


def all_captures(http: PoliteHttp, original: str) -> list[list[str]]:
    """All captures of one URL (newest first), for alternatives beyond the collapsed CDX row."""
    body = http.get(f"{CDX}?url={quote(original, safe='/:._-?=&')}&filter=statuscode:200&fl=original,timestamp,length",
                    timeout=120, detect=False).body
    rows = [line.split() for line in body.decode("utf-8", "replace").splitlines() if line.strip()]
    return sorted([r for r in rows if len(r) >= 3], key=lambda r: r[1], reverse=True)


def by_items(rows: list[list[str]]) -> list[dict]:
    captures: dict[str, list[tuple[str, str, int]]] = {}
    for original, timestamp, length in rows:
        m = BY_DOC_RE.search(original)
        if m and length.isdigit():
            captures.setdefault(m.group(1), []).append((timestamp, original, int(length)))
    items = []
    for doc, caps in sorted(captures.items()):
        usable = sorted([c for c in caps if c[2] >= MIN_BY_LENGTH], key=lambda c: c[0], reverse=True)
        if not usable:
            continue
        items.append({"source_doc_id": f"by:{doc}", "url": archived_url(usable[0][0], usable[0][1]),
                      "listed_date": None, "listed_court": None, "listed_case_numbers": None,
                      "listed_modified": None, "priority": 1,
                      "extra": {"route": "by", "doc": doc, "original": f"{by_portal.PORTAL}/Content/Document/{doc}",
                                "captures": [list(c[:2]) for c in usable[:MAX_CAPTURES]]}})
    return items


def bverwg_items(rows: list[list[str]], wanted: dict[str, dict]) -> list[dict]:
    """``wanted``: bverwg document id -> its pending bverwg item (listed date, Az)."""
    captures: dict[str, list[tuple[str, str]]] = {}
    for original, timestamp, _length in rows:
        m = BVERWG_DOC_RE.search(original)
        if m and m.group(1) in wanted:
            captures.setdefault(m.group(1), []).append((timestamp, original))
    items = []
    for doc, caps in sorted(captures.items()):
        caps = sorted(caps, key=lambda c: c[0], reverse=True)[:MAX_CAPTURES]
        listed = wanted[doc]
        items.append({"source_doc_id": f"bverwg:{doc}", "url": archived_url(*caps[0]),
                      "listed_date": listed.get("listed_date"), "listed_court": "bverwg",
                      "listed_case_numbers": listed.get("listed_case_numbers"), "listed_modified": None,
                      "priority": priority("verwaltung"),
                      "extra": {"route": "bverwg", "doc": doc, "original": f"https://www.bverwg.de/de/{doc}",
                                "captures": [list(c) for c in caps]}})
    return items


def _extra(item) -> dict:
    extra = item["extra"]
    return extra if isinstance(extra, dict) else json.loads(extra or "{}")


def _body(data: bytes) -> bytes:
    # The archive returns the original bytes; a page stored gzip-encoded without the header stays gzip.
    return gzip.decompress(data) if data[:2] == b"\x1f\x8b" else data


def usable(route: str, doc: str, body: bytes) -> str | None:
    """None when the archived page is the decision; else why not (then the next capture is tried)."""
    if not looks_like_html(body):
        return "not HTML"
    verdict = classify_page(body)
    if verdict:
        return f"archived {verdict[0]} page ({verdict[1][:80]})"
    text = body.decode("utf-8", "replace")
    if route == "by":
        return None if 'id="content"' in text and ("rdblock" in text or "absatz" in text) \
            else "not a BAYERN.RECHT decision page"
    from .bverwg import is_legacy_page
    if f'id="{doc.replace(".", "")}"' in text or is_legacy_page(body, doc):
        return None
    return "not a bverwg.de decision page of this document"


class WaybackAdapter(Adapter):
    source_id = SOURCE_ID
    label = ("Wayback Machine (web.archive.org): archived BAYERN.RECHT and bverwg.de decision pages "
             "(secondary copies; the primary source wins)")
    base_url = ARCHIVE
    land = None
    adapter_name = "wayback-archived-primary-pages"
    min_interval = 1.5
    concurrency = Concurrency(1, 2)
    robots_policy = "respect"          # web.archive.org has no robots.txt (404)
    source_kind = "archive"
    licence = ("archived copies of official publications: decisions are amtliche Werke (§ 5 UrhG); BAYERN.RECHT "
               "editorial parts (Titel, redaktionelle Leitsätze) belong to the portal; Internet Archive terms of use")

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        routes = [r for r in ROUTES if not courts or any(c.lower().startswith(r) for c in courts)]
        items: list[dict] = []
        if "bverwg" in routes:
            wanted = {row[0]: {"listed_date": row[1], "listed_case_numbers": row[2]} for row in conn.execute(
                "SELECT source_doc_id, listed_date, listed_case_numbers FROM source_items "
                "WHERE source_id='bverwg' AND state IN ('pending','failed')").fetchall()}
            rows = [r for prefix in CDX_QUERIES["bverwg"] for r in cdx_lines(http, prefix)]
            items.extend(bverwg_items(rows, wanted))
        if "by" in routes:
            have = {row[0] for row in conn.execute(
                "SELECT source_doc_id FROM source_items WHERE source_id='by' AND state IN ('stored','skipped')")}
            rows = [r for prefix in CDX_QUERIES["by"] for r in cdx_lines(http, prefix)]
            items.extend(i for i in by_items(rows) if i["extra"]["doc"] not in have)
        if since:
            items = [i for i in items if not i["listed_date"] or i["listed_date"] >= since]
        return Listing(items=items[:limit] if limit else items, upstream_total=len(items), complete=not limit)

    prefetch_ok = True                 # fetch has no side effects: `python -m courts prefetch` may stage captures

    def fetch(self, http: PoliteHttp, item) -> Capture:
        from ..prefetch import staged_capture
        staged = staged_capture(SOURCE_ID, item)
        if staged is not None:
            return staged
        extra = _extra(item)
        reasons = []
        for timestamp, original in extra.get("captures") or []:
            url = archived_url(timestamp, original)
            try:
                response = http.get(url, timeout=120, detect=False)
            except UpstreamFailure as failure:
                if failure.kind in ("rate_limited", "network"):
                    raise
                reasons.append(f"{timestamp}: {failure.kind}")
                continue
            body = _body(response.body)
            why = usable(extra["route"], extra["doc"], body)
            if why is None:
                return Capture(body=body, url=url, media_type="text/html", extension="html",
                               headers={"content-type": "text/html; charset=utf-8", "x-archive-timestamp": timestamp,
                                        "x-archive-original": original})
            reasons.append(f"{timestamp}: {why}")
        raise UpstreamFailure("unexpected_representation",
                              "no usable archived capture: " + "; ".join(reasons)[:400], url=item["url"])

    def parse(self, capture: Capture, item) -> ParsedDecision:
        extra = _extra(item)
        m = re.search(r"/web/(\d{14})id_/(.+)$", capture.url)
        timestamp, original = (m.group(1), m.group(2)) if m else (None, extra.get("original"))
        body = _body(capture.body)
        why = usable(extra["route"], extra["doc"], body)
        if why:
            raise UpstreamFailure("unexpected_representation", why, url=capture.url)
        if extra["route"] == "by":
            parsed = by_portal.parse_capture(body, {"source_doc_id": extra["doc"]}, extra["original"])
        else:
            from .bverwg import check_identity, is_legacy_page, parse_decision_html, parse_legacy_decision_html
            reader = parse_legacy_decision_html if is_legacy_page(body, extra["doc"]) else parse_decision_html
            parsed = reader(body, extra["doc"], extra["original"], {"content-type": "text/html"})
            check_identity(parsed, item)
        parsed.source_id, parsed.source_doc_id = SOURCE_ID, item["source_doc_id"]
        parsed.source_url = extra["original"]
        parsed.metadata["source_kind"] = "archive"
        parsed.metadata["archive"] = {"service": "web.archive.org", "timestamp": timestamp,
                                      "archived_url": capture.url, "original_url": original}
        return parsed


    def on_stored(self, item) -> None:
        from ..prefetch import discard_staged
        discard_staged(SOURCE_ID, item["source_doc_id"])


def adapters() -> dict[str, Adapter]:
    return {SOURCE_ID: WaybackAdapter()}
