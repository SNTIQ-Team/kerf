"""NRWE – Rechtsprechungsdatenbank der Gerichte in Nordrhein-Westfalen.

Listing: stateless POST search at ``nrwesuche.justiz.nrw.de/index.php``
(decision-date range ``von``/``bis``, posting-date range ``von2``/``bis2`` for
incremental runs, ``gerichtsbarkeit`` filter, newest first, 100 per page).
Document: static HTML at ``nrwe.justiz.nrw.de/<path>.html``.  Randnummern are
``<span class="absatzRechts">N</span>`` directly before the numbered element
(``p.absatzLinks``, ``ul``, ``table``).  Section headings (Tatbestand,
Entscheidungsgründe, Gründe) are ordinary numbered paragraphs.
"""
from __future__ import annotations

import math
import re
import sqlite3
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from ..casenumbers import split_case_numbers
from ..courtids import priority, resolve as resolve_court
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import Block, ParsedDecision, Section
from .base import Adapter, Capture, Listing, decode_html, iso_date, squash

SEARCH_URL = "https://nrwesuche.justiz.nrw.de/index.php"
DOC_HOST = "nrwe.justiz.nrw.de"
PAGE_SIZE = 100
# Backfill order: social and administrative courts first.
BRANCHES = [("Sozialgerichtsbarkeit", 0), ("Verwaltungsgerichtsbarkeit", 0),
            ("Ordentliche Gerichtsbarkeit", 1), ("Arbeitsgerichtsbarkeit", 1),
            ("Finanzgerichtsbarkeit", 1), ("Verfassungsgerichtsbarkeit", 1)]
HEADINGS = {"tatbestand": "tatbestand", "entscheidungsgründe": "entscheidungsgruende",
            "entscheidungsgruende": "entscheidungsgruende", "gründe": "gruende", "gruende": "gruende"}
BLOCK_TAGS = {"p", "ul", "ol", "table", "div", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6", "dl"}
BREAK_TAGS = {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "dd", "dt"}


def clean(text: str) -> str:
    return text.replace("­", "").replace(" ", " ")


def html_text(node) -> str:
    """Visible text with block boundaries as newlines and table cells as tabs."""
    def walk(el) -> str:
        if isinstance(el, NavigableString):
            return str(el)
        if not isinstance(el, Tag) or el.name in ("script", "style"):
            return ""
        out = "".join(walk(c) for c in el.children)
        if el.name in BREAK_TAGS:
            out += "\n"
        elif el.name in ("td", "th"):
            out += "\t"
        return out
    lines = []
    for line in clean(walk(node)).splitlines():
        line = "\t".join(" ".join(cell.split()) for cell in line.split("\t")).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def parse_result_list(markup: str) -> tuple[int, list[dict]]:
    m = re.search(r'id="anzahlGefunden">\s*Es wurden\s*<strong>(\d+)</strong>', markup)
    if not m:
        raise UpstreamFailure("unexpected_representation", "NRWE result page without hit count", url=SEARCH_URL)
    total = int(m.group(1))
    items = []
    for chunk in markup.split("<div class='einErgebnis'>")[1:]:
        href = re.search(r"href='([^']+)'", chunk)
        if not href:
            continue
        url = href.group(1).strip()
        parts = urlsplit(url)
        if parts.netloc != DOC_HOST or not parts.path.endswith(".html"):
            continue

        def field(label: str) -> str | None:
            f = re.search(label + r":\s*(.*?)\s*<br", chunk, re.S)
            return clean(squash(re.sub(r"<[^>]+>", " ", f.group(1)))) if f else None
        court = field("Gericht")
        raw_date = field("Entscheidungsdatum")
        ecli = re.search(r"(ECLI:[^<\s]+)", chunk)
        info = resolve_court(court or "", default_land="nw")
        items.append({
            "source_doc_id": parts.path.lstrip("/").removesuffix(".html"), "url": url,
            "listed_date": iso_date(raw_date) if raw_date else None,
            "listed_court": info.court_id, "listed_case_numbers": field("Aktenzeichen"),
            "listed_modified": None, "priority": priority(info.jurisdiction),
            "extra": {"court_name": court, "decision_type": field("Entscheidungsart"),
                      "ecli": ecli.group(1) if ecli else None}})
    return total, items


def search_form(*, branch: str, von: str | None, bis: str | None, von2: str | None, page: int) -> dict:
    form = {"gerichtstyp": "", "gerichtsbarkeit": branch, "gerichtsort": "", "entscheidungsart": "",
            "date": "", "von": von or "", "bis": bis or "", "validFrom": "", "von2": von2 or "", "bis2": "",
            "aktenzeichen": "", "schlagwoerter": "", "q": "", "method": "stem", "qSize": str(PAGE_SIZE),
            "sortieren_nach": "datum_absteigend", "advanced_search": "true"}
    if page == 1:
        form["absenden"] = "Suchen"
    else:
        form[f"page{page}"] = str(page)
    return form


def _metadata_pairs(soup: BeautifulSoup) -> dict[str, list[Tag]]:
    pairs: dict[str, list[Tag]] = {}
    for label in soup.select("div.feldbezeichnung"):
        value = label.find_next_sibling("div")
        if value is None or "feldinhalt" not in (value.get("class") or []):
            continue
        key = clean(label.get_text(" ", strip=True)).rstrip(":").strip()
        pairs.setdefault(key, []).append(value)
    return pairs


def _heading_section(text: str) -> str | None:
    letters = re.sub(r"[\s:.]", "", text).lower()
    return HEADINGS.get(letters)


def _body_blocks(container: Tag) -> tuple[list[tuple[str | None, Tag]], list[str]]:
    """(label, element) pairs in document order; wrapper Rn without own text are skipped."""
    out: list[tuple[str | None, Tag]] = []
    skipped: list[str] = []

    def visit(parent: Tag) -> None:
        pending: str | None = None
        for child in parent.children:
            if isinstance(child, NavigableString):
                if clean(str(child)).strip():
                    raise UpstreamFailure("parse_error", "unrepresented text between NRWE blocks")
                continue
            if not isinstance(child, Tag):
                continue
            classes = child.get("class") or []
            if child.name == "span" and "absatzRechts" in classes:
                pending = squash(child.get_text())
                continue
            if child.name in ("script", "style"):
                continue
            if child.select_one("span.absatzRechts"):
                # A numbered wrapper around numbered items (e.g. a list): its own
                # number has no text of its own; descend into the items.
                if pending:
                    skipped.append(pending)
                pending = None
                if child.name in ("ul", "ol"):
                    for li in child.find_all("li", recursive=False):
                        visit(li)
                else:
                    visit(child)
                continue
            if child.name in BLOCK_TAGS or child.name in ("li", "span", "strong", "b", "em", "i", "u", "a"):
                out.append((pending, child))
                pending = None
        if pending:
            skipped.append(pending)
    visit(container)
    return out, skipped


def parse_document(markup: str, url: str, source_doc_id: str) -> ParsedDecision:
    soup = BeautifulSoup(markup, "html.parser")
    pairs = _metadata_pairs(soup)

    def meta(key: str) -> str | None:
        values = pairs.get(key)
        return clean(squash(values[0].get_text(" ", strip=True))) if values else None
    court = meta("Gericht")
    raw_az = None
    if pairs.get("Aktenzeichen"):
        raw_az = clean(" ".join(pairs["Aktenzeichen"][0].get_text(" ").split()))
    raw_date = meta("Datum")
    if not court or not raw_az or not raw_date:
        raise UpstreamFailure("unexpected_representation", "NRWE page lacks Gericht/Aktenzeichen/Datum", url=url)
    sections: list[Section] = []
    if pairs.get("Leitsätze"):
        blocks = [Block(text=t, locator=f"div.feldinhalt[Leitsätze] > *:nth-child({i})")
                  for i, t in enumerate(_split_paragraphs(pairs["Leitsätze"][0]), 1)]
        sections.append(Section("leitsatz", "court", "NRWE field 'Leitsätze' (headnote published by the court)",
                                blocks))
    tenor = soup.select_one("div.feldinhalt.tenor") or (pairs.get("Tenor") or [None])[0]
    if tenor is not None:
        blocks = [Block(text=t, locator=f"div.feldinhalt.tenor > *:nth-child({i})")
                  for i, t in enumerate(_split_paragraphs(tenor), 1)]
        if blocks:
            sections.append(Section("tenor", "court", "NRWE field 'Tenor'", blocks))
    first = soup.select_one("span.absatzRechts")
    skipped: list[str] = []
    if first is not None:
        container = first.parent
        pairs_body, skipped = _body_blocks(container)
        current = Section("gruende", "court", "NRWE decision text (no explicit section heading before it)")
        for index, (label, element) in enumerate(pairs_body, 1):
            text = html_text(element)
            if not text:
                continue
            heading = _heading_section(text)
            if heading and len(text) < 40:
                if current.blocks:
                    sections.append(current)
                current = Section(heading, "court", f"NRWE decision text, heading '{text}'", heading=text)
            current.blocks.append(Block(text=text, label=label, kind="heading" if heading else "text",
                                        locator=f"body-block[{index}]"))
        if current.blocks:
            sections.append(current)
    else:
        body = soup.select_one("div.maindivInhalt")
        if body is not None:
            blocks = [Block(text=t, locator=f"div.maindivInhalt > *:nth-child({i})")
                      for i, t in enumerate(_split_paragraphs(body), 1)]
            sections.append(Section("gruende", "court", "NRWE decision text without Randnummern", blocks))
    norms = meta("Normen")
    procedural = []
    for value in pairs.get("Vorinstanz", []):
        for line in re.split(r"\n|;", html_text(value)):
            line = squash(line)
            m = re.match(r"(?P<court>[^,]+),\s*(?P<az>.+)$", line)
            if m:
                procedural.append({"direction": "previous", "court": m.group("court"), "date": None,
                                   "case_number": m.group("az"), "decision_type": None, "raw": line,
                                   "field": "nrwe:Vorinstanz", "origin": "source_metadata"})
    info = resolve_court(court, default_land="nw")
    return ParsedDecision(
        source_id="nrw", source_doc_id=source_doc_id, source_url=url, court_name=court,
        decision_date=iso_date(raw_date), case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az),
        decision_type=meta("Entscheidungsart"), sections=sections, ecli=meta("ECLI"), court_id=info.court_id,
        court_type=info.court_type, jurisdiction=info.jurisdiction, land="nw", chamber=meta("Spruchkörper"),
        title=None,
        metadata={"norms": [n.strip() for n in re.split(r",\s*(?=§|Art)", norms)] if norms else [],
                  "sachgebiet": meta("Sachgebiet"), "rechtskraft": meta("Rechtskraft"),
                  "schlagworte": meta("Schlagworte"), "rn_without_own_text": skipped},
        procedural=procedural)


def _split_paragraphs(node: Tag) -> list[str]:
    children = [c for c in node.children if isinstance(c, Tag) or clean(str(c)).strip()]
    if children and all(isinstance(c, Tag) and c.name in BLOCK_TAGS | {"ol", "ul"} for c in children):
        out = []
        for child in children:
            if child.name in ("ol", "ul"):
                out += [html_text(li) for li in child.find_all("li", recursive=False)]
            else:
                out.append(html_text(child))
        return [t for t in out if t]
    text = html_text(node)
    return [t for t in re.split(r"\n\s*\n", text) if t.strip()] or ([text] if text else [])


class NrwAdapter(Adapter):
    source_id = "nrw"
    label = "NRWE – Rechtsprechungsdatenbank der Gerichte in Nordrhein-Westfalen"
    base_url = "https://nrwe.justiz.nrw.de"
    land = "nw"
    adapter_name = "nrwe-html"

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        today = date.today().strftime("%d.%m.%Y")
        # An empty "von" with a "bis" fails NRWE's date validation; use an open start.
        von = datetime.fromisoformat(since).strftime("%d.%m.%Y") if since else "01.01.1900"
        von2 = None
        if not since:
            last = conn.execute("SELECT max(started_at) FROM sync_runs WHERE source_id='nrw' AND status IN "
                                "('ok','partial') AND limit_n IS NULL AND since IS NULL "
                                "AND coalesce(notes, '') NOT LIKE 'listing incomplete%'").fetchone()[0]
            if last:  # incremental: everything posted since the last complete run (7 days overlap)
                von2 = (datetime.fromisoformat(last) - timedelta(days=7)).strftime("%d.%m.%Y")
        branches = BRANCHES
        if courts:
            wanted = {c.lower() for c in courts}
            branches = [b for b in BRANCHES if b[0].split()[0].lower()[:5] in {w[:5] for w in wanted}] or BRANCHES
        items: list[dict] = []
        totals = 0
        # One request for the size of the whole archive (all courts), for coverage reporting.
        response = http.post(SEARCH_URL, data=search_form(branch="", von="01.01.1900", bis=today, von2=None, page=1),
                             timeout=90)
        archive_total, _ = parse_result_list(decode_html(response.body, response.headers))
        first_priority = [b for b in branches if b[1] == 0]
        quota = {b[0]: (math.ceil(limit / max(1, len(first_priority))) if limit and b[1] == 0 else limit)
                 for b in branches}
        for branch, _prio in branches:
            if limit and len(items) >= limit and _prio > 0:
                break
            wanted = quota[branch]
            page, got = 1, 0
            while True:
                response = http.post(SEARCH_URL, data=search_form(branch=branch, von=von, bis=today, von2=von2,
                                                                  page=page), timeout=90)
                total, rows = parse_result_list(decode_html(response.body, response.headers))
                if page == 1:
                    totals += total
                items.extend(rows)
                # Only rows not stored yet count towards the quota, so a later backfill run
                # pages past what the corpus already holds instead of stopping at the top.
                ids = [r["source_doc_id"] for r in rows]
                stored = {r[0] for r in conn.execute(
                    f"SELECT source_doc_id FROM source_items WHERE source_id='nrw' AND state='stored' "
                    f"AND source_doc_id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
                got += len(ids) - len(stored)
                if not rows or page >= math.ceil(total / PAGE_SIZE) or (wanted and got >= wanted):
                    break
                page += 1
        complete_listing = not von2 and branches == BRANCHES and not (limit and len(items) >= limit)
        return Listing(items=items, upstream_total=archive_total, complete=complete_listing)

    def fetch(self, http: PoliteHttp, item) -> Capture:
        response = http.get(item["url"], timeout=90)
        markup = decode_html(response.body, response.headers)
        if "feldbezeichnung" not in markup:
            raise UpstreamFailure("unexpected_representation", "not an NRWE decision page", url=item["url"])
        return Capture(body=response.body, url=item["url"], media_type="text/html", extension="html",
                       headers=response.headers)

    def parse(self, capture: Capture, item) -> ParsedDecision:
        parsed = parse_document(decode_html(capture.body, capture.headers), capture.url, item["source_doc_id"])
        listed = item["listed_date"]
        if listed and listed != parsed.decision_date:
            raise UpstreamFailure("identity_mismatch", f"listed date {listed} != page {parsed.decision_date}",
                                  url=capture.url)
        listed_az = item["listed_case_numbers"]
        if listed_az and squash(listed_az) != squash(parsed.case_numbers_raw):
            raise UpstreamFailure("identity_mismatch", f"listed Az {listed_az!r} != page {parsed.case_numbers_raw!r}",
                                  url=capture.url)
        return parsed


def adapters() -> dict[str, Adapter]:
    return {"nrw": NrwAdapter()}
