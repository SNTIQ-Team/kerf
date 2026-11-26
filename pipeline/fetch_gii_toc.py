"""Capture the official GII table of contents for the deep federal corpus.

GII's per-norm pages do not follow one numeric grammar: ``§ 37`` is
``__37.html``, ``Art 1`` is ``art_1.html``, the Präambel is ``pr_ambel.html``
and a single Anlage is ``anlage.html``.  Building such URLs from a number
produces plausible but wrong links (``gg/__1.html``).  This fetcher records the
links GII itself publishes in each act's ``index.html`` so the API can resolve
per-norm source URLs from evidence, and report ``norm_not_in_toc`` otherwise.

Usage::

    python3 pipeline/fetch_gii_toc.py --acts web/data/wiki.json \
        --out api/data/gii_toc.json [--only gg,sgb_14] [--delay 1.0]

The output is idempotent: unchanged TOC bytes keep their ``fetched_at``.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api.norm_identity import act_slug, designator_unit, gii_norm_href  # noqa: E402

USER_AGENT = ("SNTIQ-Kerf/1.5 (+https://sntiq.com/dt/kerf; "
              "table-of-contents capture, 1 request/s)")
_LINK = re.compile(r'<a\s+href="([A-Za-z0-9_.-]+\.html(?:#[A-Za-z0-9_-]+)?)"[^>]*>(.*?)</a>',
                   re.I | re.S)
_DESIGNATOR = re.compile(
    r"^(§+\s*\d+\s*[a-z]{0,3}|Art(?:ikel)?\.?\s*\d+\s*[a-z]{0,3}|"
    r"Anlage(?:\s+[0-9IVXLC]+[a-z]?)?|Anhang(?:\s+[A-Z0-9]+)?)(?=\s|$|\()",
    re.I)


def parse_toc(raw: bytes) -> dict[str, str]:
    """Map ``unit/number`` keys to hrefs from one GII ``index.html``."""
    text = raw.decode("iso-8859-1")
    norms: dict[str, str] = {}
    for href, label_html in _LINK.findall(text):
        if href == "index.html" or not gii_norm_href(href):
            continue
        label = " ".join(html.unescape(re.sub(r"<[^>]+>", " ",
                                              label_html)).split())
        if not label or ("#" in href and not _DESIGNATOR.fullmatch(label)):
            continue
        match = _DESIGNATOR.match(label)
        designator = match.group(1) if match else label
        unit, number = designator_unit(designator)
        key = f"{unit}/{number}"
        # Keep the first occurrence; duplicates are reported, not merged.
        norms.setdefault(key, href)
    return norms


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--acts", type=Path,
                        default=ROOT / "web" / "data" / "wiki.json")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "api" / "data" / "gii_toc.json")
    parser.add_argument("--only", default="",
                        help="comma-separated GII slugs")
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()

    import requests  # pipeline dependency only

    acts = json.loads(args.acts.read_text(encoding="utf-8"))
    wanted = {slug for slug in args.only.split(",") if slug}
    previous = {}
    if args.out.is_file():
        previous = json.loads(args.out.read_text(encoding="utf-8")).get(
            "acts", {})
    result = dict(previous)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    failures = []
    for act in acts:
        if act.get("juris") != "DE" or not act.get("source_url"):
            continue
        _, slug = act_slug(act)
        if wanted and slug not in wanted:
            continue
        url = f"https://www.gesetze-im-internet.de/{slug}/index.html"
        try:
            response = session.get(url, timeout=30)
        except requests.RequestException as exc:
            failures.append({"slug": slug, "error": str(exc)[:200]})
            continue
        finally:
            time.sleep(args.delay)
        # A challenge/error page is not a TOC: demand the act's own full-text
        # link (BJNR…) besides HTTP 200, and at least one norm link below.
        if response.status_code != 200 or b'href="BJNR' not in \
                response.content:
            failures.append({"slug": slug, "status": response.status_code,
                             "error": "unexpected representation"})
            continue
        digest = hashlib.sha256(response.content).hexdigest()
        norms = parse_toc(response.content)
        if not norms:
            failures.append({"slug": slug, "error": "no norm links"})
            continue
        old = previous.get(slug) or {}
        fetched = (old.get("fetched_at") if old.get("sha256") == digest
                   else datetime.now(timezone.utc).isoformat(
                       timespec="seconds").replace("+00:00", "Z"))
        result[slug] = {"act_id": act.get("id"), "url": url,
                        "sha256": digest, "fetched_at": fetched,
                        "norms": dict(sorted(norms.items()))}
        print(f"{slug}: {len(norms)} links", file=sys.stderr)
    payload = {
        "schema_version": 1,
        "kind": "kerf-gii-table-of-contents",
        "source_policy": {
            "source": "GII index.html per act",
            "url_construction": "never from a bare number; only links "
                                "published in the captured TOC"},
        "acts": dict(sorted(result.items())),
        "failures": failures,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=0,
                              sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(args.out)
    print(f"wrote {args.out} ({len(result)} acts, {len(failures)} failures)",
          file=sys.stderr)
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
