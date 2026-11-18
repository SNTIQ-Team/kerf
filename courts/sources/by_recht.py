"""Public, paginated BAYERN.RECHT decision list. One paced connection per host."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..courtids import resolve
from ..errors import UpstreamFailure
from ..hostgov import Concurrency
from . import by_portal
from .base import Adapter, Capture, Listing, iso_date, squash

START = by_portal.PORTAL + "/Search/Filter/DOKTYP/rspr"


def parse_listing(body, page):
    soup = BeautifulSoup(body, "html.parser")
    hits = []
    for link in soup.select(".hltitel a[href]"):
        try:
            doc, url = by_portal.canonical_url(urljoin(by_portal.PORTAL, link["href"]))
        except ValueError:
            continue
        subtitle = link.parent.find_next_sibling(class_="hlSubTitel")
        details = squash(subtitle.get_text(" ", strip=True)) if subtitle else ""
        match = re.fullmatch(r"(.+?) vom (\d{2}\.\d{2}\.\d{4})\s+[–-]\s+(.+)", details)
        if not match:
            continue
        short = link.get_text(" ", strip=True).partition(":")[0]
        court = resolve(by_portal.court_long_name(short), default_land="by")
        if court.court_type == "unknown" or court.land != "by":
            continue
        hits.append(dict(source_doc_id=doc, url=url, listed_date=iso_date(match[2]),
                         listed_court=court.court_id, listed_case_numbers=match[3],
                         extra={"route": "official_hitlist", "court_as_listed": short}))
    following = f"/Search/Page/{page + 1}"
    next_url = by_portal.PORTAL + following if soup.find("a", href=following) else None
    count = re.search(r"([\d.]+) Treffer in [\d.]+ Gerichtsentscheidungen", soup.get_text(" ", strip=True))
    if not count:
        raise UpstreamFailure("unexpected_representation", "BAYERN.RECHT decision hit list unavailable", url=START)
    return hits, next_url, int(count[1].replace(".", ""))


class BayernRecht(Adapter):
    source_id = "by-recht"
    base_url = by_portal.PORTAL
    label = "BAYERN.RECHT – Gerichtsentscheidungen"
    adapter_name = "by-recht-html"
    land = "by"
    concurrency = Concurrency(1, 1)

    def list_items(self, http, conn, *, since, limit, courts):
        wanted = limit or 1000
        seen = {row[0] for row in conn.execute(
            "SELECT source_doc_id FROM source_items WHERE source_id=? AND state IN ('stored','skipped','failed')",
            (self.source_id,))}
        items, pages = [], set()
        url = START
        total = None
        # Repeated bounded runs may have already consumed many pages at the head.
        page_budget = 200 + (len(seen) + wanted) // 10
        for page in range(1, page_budget + 1):
            response = http.get(url)
            hits, next_url, total = parse_listing(response.body, page)
            signature = tuple(hit["source_doc_id"] for hit in hits)
            if signature and signature in pages:
                raise UpstreamFailure("unexpected_representation", "repeated BAYERN.RECHT results page", url=url)
            pages.add(signature)
            for hit in hits:
                if hit["source_doc_id"] in seen or (courts and hit["listed_court"] not in courts):
                    continue
                if since and hit["listed_date"] < since:
                    continue
                seen.add(hit["source_doc_id"])
                items.append(hit)
                if len(items) >= wanted:
                    return Listing(items, total, complete=False)
            if not next_url or (since and hits and max(hit["listed_date"] for hit in hits) < since):
                break
            url = next_url
        return Listing(items, total, complete=False)

    def fetch(self, http, item):
        response = http.get(item["url"])
        if response.url != item["url"] or "text/html" not in response.content_type.lower():
            raise UpstreamFailure("unexpected_representation", "expected canonical decision HTML", url=item["url"])
        return Capture(response.body, response.url, "text/html", "html", response.headers)

    def parse(self, capture, item):
        doc, url = by_portal.canonical_url(capture.url)
        if doc != item["source_doc_id"] or doc not in by_portal.page_doc_ids(capture.body):
            raise UpstreamFailure("identity_mismatch", "wrong BAYERN.RECHT document identifier", url=url)
        parsed = by_portal.parse_portal_html(capture.body, item, url)
        if (parsed.land != "by" or parsed.court_id != item["listed_court"] or
                parsed.decision_date != item["listed_date"] or
                squash(parsed.case_numbers_raw) != squash(item["listed_case_numbers"])):
            raise UpstreamFailure("identity_mismatch", "decision header differs from the hit list", url=url)
        parsed.source_id = self.source_id
        return parsed


def adapters():
    return {"by-recht": BayernRecht()}
