"""Sachsen: ESAMOSplus (OLG Dresden) and the Sächs. OVG Entscheidungssammlung.

Both databases live on www.justiz.sachsen.de, whose robots.txt (fetched
2026-09-28) reads::

    User-agent: *
    Crawl-delay: 3600

i.e. at most one request per hour for the whole host.  A listing plus
documents cannot be done at a useful volume under that delay, and no other
host with the same decisions (bulk export, open data) was found.  Therefore
``list_items`` records a typed ``robots_disallowed`` failure without sending
any request, which ``sync`` stores as source status ``blocked`` for the
coverage table.  ``fetch`` still goes through PoliteHttp, whose robots.txt
handling raises the per-host interval to the Crawl-delay, so any single
document fetch (e.g. a manually queued ``ovgentschweb/document.phtml?id=N``)
is paced at one request per hour.
"""
from __future__ import annotations

import sqlite3

from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import ParsedDecision
from .base import Adapter, Capture, Listing, decode_html
from .htmlblocks import ensure_document

BASE = "https://www.justiz.sachsen.de"
ROBOTS_URL = BASE + "/robots.txt"
CRAWL_DELAY_SECONDS = 3600
BLOCK_DETAIL = ("robots.txt Crawl-delay 3600 s: at most one request per hour; "
                "not crawlable at useful volume")


class SachsenAdapter(Adapter):
    source_id = "sn"
    label = "Sachsen: ESAMOSplus (OLG Dresden) / Sächs. OVG Entscheidungssammlung (robots Crawl-delay 3600)"
    base_url = BASE
    land = "sn"
    adapter_name = "sn-crawl-delay"

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        # Deliberately no request: even the listing would need hours at one request per hour.
        raise UpstreamFailure("robots_disallowed", BLOCK_DETAIL, url=ROBOTS_URL)

    def fetch(self, http: PoliteHttp, item) -> Capture:
        # PoliteHttp loads robots.txt for the host and paces it to the Crawl-delay (3600 s).
        response = http.get(item["url"], timeout=120)
        if response.body.startswith(b"%PDF"):
            return Capture(body=response.body, url=item["url"], media_type="application/pdf", extension="pdf",
                           headers=response.headers)
        markup = decode_html(response.body, response.headers)
        ensure_document(markup, "Aktenzeichen" in markup or "Az." in markup, url=item["url"],
                        what="Sachsen decision page")
        return Capture(body=response.body, url=item["url"], media_type="text/html", extension="html",
                       headers=response.headers)

    def parse(self, capture: Capture, item) -> ParsedDecision:
        raise UpstreamFailure("parse_error", "no verified Sachsen document parser: no sample document can be "
                              "fetched at a useful rate under robots.txt Crawl-delay 3600", url=capture.url)


def adapters() -> dict[str, Adapter]:
    return {"sn": SachsenAdapter()}
