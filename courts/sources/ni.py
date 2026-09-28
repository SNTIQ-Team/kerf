"""Niedersachsen: NI-VORIS (voris.wolterskluwer-online.de, Drupal behind Cloudflare).

www.rechtsprechung.niedersachsen.de redirects here.  robots.txt allows
``/browse/`` (it disallows ``/search/``, ``/node/`` ...).  The site sends
``tdm-reservation: 1`` with the policy https://voris.wolterskluwer-online.de/nutzungsvorbehalt:
commercial text and data mining is reserved (§ 44b Abs. 3 UrhG); this corpus is
a local, non-commercial research corpus, and every decision records the
reservation in ``metadata["rights"]``.  Cloudflare challenges are never
bypassed: PoliteHttp/detect turn them into typed ``challenge`` failures.

Listing: the browse tree ``Rechtsprechung -> Gerichtsbarkeit -> Instanzgruppe ->
Gerichtstyp -> Gericht -> Jahr -> Monat`` (depth varies; a year with few
decisions lists documents directly).  Document links read
``OVG Niedersachsen, 18.08.2026 - 7 KS 34/22``.
Document: ``article.wkde-document`` with ``section.wkde-bibliography`` (Gericht,
Datum, Aktenzeichen, Entscheidungsform, Referenz, ECLI), catchwords,
Verfahrensgang, Rechtsgrundlagen, ``section.wkde-case-headnote-official``
(Amtlicher Leitsatz) and ``section.wkde-document-body`` with ``div.tenor``,
``div.tatbestand``, ``div.gruende`` of plain ``<p id>``: no Randnummern, so the
decision is ``numbering: none`` (``#p-<n>`` ids).
"""
from __future__ import annotations

import html as htmlmod
import re
import sqlite3
from datetime import date

from bs4 import BeautifulSoup, NavigableString, Tag

from ..casenumbers import split_case_numbers
from ..courtids import priority, resolve as resolve_court
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import ParsedDecision, Section
from .base import MONTHS, Adapter, Capture, Listing, decode_html, iso_date, squash
from .htmlblocks import checked_ecli, clean_line, ensure_document, incremental_since, leaf_blocks

BASE = "https://voris.wolterskluwer-online.de"
ROOT = "/browse/csh-da-filter%216c5b57fb-d0b6-49c9-b3e0-89e2a4e81639"   # "Rechtsprechung"
DOC_URL = BASE + "/browse/document/{doc}"
TDM_POLICY = BASE + "/nutzungsvorbehalt"
INCREMENTAL_OVERLAP_DAYS = 180
MAX_DEPTH = 10
# Backfill order: social and administrative branches first.
BRANCH_PRIORITY = {"Sozialgerichte": 0, "Verwaltungsgerichte": 0}
NODE_RE = re.compile(r'<a href="(/browse/csh-da-filter[^"]+)"[^>]*class="wk-tree-node-label[^"]*"[^>]*>(.*?)</a>',
                     re.S)
DOC_RE = re.compile(r'<a href="/browse/document/([0-9a-f-]{36})"[^>]*document-link[^>]*>(.*?)</a>', re.S)
LABEL_RE = re.compile(r"^(?P<court>.+?),\s*(?P<date>\d{1,2}\.\d{1,2}\.\d{4})\s*-\s*(?P<az>.+)$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
HEADINGS = {
    "tenor": ("tenor", "court"),
    "tatbestand": ("tatbestand", "court"),
    "entscheidungsgründe": ("entscheidungsgruende", "court"),
    "gründe": ("gruende", "court"),
    "abweichende meinung": ("abweichende_meinung", "court"),
    "sondervotum": ("abweichende_meinung", "court"),
}
CLASS_SECTIONS = {"tenor": "tenor", "tatbestand": "tatbestand", "gruende": "gruende",
                  "entscheidungsgruende": "entscheidungsgruende"}


def _label(fragment: str) -> str:
    return clean_line(htmlmod.unescape(re.sub(r"<[^>]+>", " ", fragment)))


def parse_browse(markup: str, *, url: str = BASE) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(child tree nodes [(path, label)], documents [(uuid, label)]) of one browse page."""
    ensure_document(markup, "wk-tree" in markup or "document-link" in markup, url=url, what="VORIS browse page")
    nodes = [(m.group(1), _label(m.group(2))) for m in NODE_RE.finditer(markup)]
    docs = [(m.group(1), _label(m.group(2))) for m in DOC_RE.finditer(markup)]
    return nodes, docs


def document_item(uuid: str, label: str, branch_priority: int | None, trail: list[str]) -> dict | None:
    m = LABEL_RE.match(label)
    if not m:
        return None
    info = resolve_court(m.group("court"), default_land="ni")
    prio = priority(info.jurisdiction) if info.jurisdiction != "unknown" else (branch_priority or 1)
    return {"source_doc_id": uuid, "url": DOC_URL.format(doc=uuid), "listed_date": iso_date(m.group("date")),
            "listed_court": info.court_id, "listed_case_numbers": clean_line(m.group("az")),
            "listed_modified": None, "priority": prio,
            "extra": {"court": m.group("court"), "label": label, "tree": trail}}


def _kind(nodes: list[tuple[str, str]]) -> str:
    labels = [label for _, label in nodes]
    if labels and all(re.fullmatch(r"\d{4}", label) for label in labels):
        return "years"
    if labels and all(label.lower() in MONTHS for label in labels):
        return "months"
    return "groups"


class _Walk:
    """Depth-first walk of the browse tree, newest first, bounded by since/limit/courts."""

    def __init__(self, http: PoliteHttp, *, since: str | None, limit: int | None, courts: list[str] | None):
        self.http, self.limit, self.courts = http, limit, [c.lower() for c in courts or []]
        self.since = date.fromisoformat(since) if since else None
        self.items: list[dict] = []
        self.seen: set[str] = set()
        self.pages: dict[str, tuple[list, list]] = {}
        self.requests = 0

    @property
    def done(self) -> bool:
        return bool(self.limit and len(self.items) >= self.limit)

    def page(self, path: str) -> tuple[list, list]:
        if path not in self.pages:
            response = self.http.get(BASE + path, timeout=90)
            self.requests += 1
            self.pages[path] = parse_browse(decode_html(response.body, response.headers), url=BASE + path)
        return self.pages[path]

    def court_wanted(self, label: str) -> bool:
        if not self.courts:
            return True
        court_id = resolve_court(label, default_land="ni").court_id
        return any(court_id.startswith(c) for c in self.courts)

    def walk(self, path: str, trail: list[str], prio: int | None, depth: int = 0) -> None:
        if self.done or depth > MAX_DEPTH:
            return
        nodes, docs = self.page(path)
        for uuid, label in docs:
            item = document_item(uuid, label, prio, trail)
            if item is None or uuid in self.seen:
                continue
            if self.since and item["listed_date"] and item["listed_date"] < self.since.isoformat():
                continue
            if self.courts and not any(item["listed_court"].startswith(c) for c in self.courts):
                continue
            self.seen.add(uuid)
            self.items.append(item)
        kind = _kind(nodes)
        if kind == "years":
            years = sorted(((int(label), p) for p, label in nodes), reverse=True)
            for year, child in years:
                if self.done or (self.since and year < self.since.year):
                    break
                self.walk(child, trail + [str(year)], prio, depth + 1)
        elif kind == "months":
            year = int(trail[-1]) if trail and trail[-1].isdigit() else None
            months = sorted(((MONTHS[label.lower()], p, label) for p, label in nodes), reverse=True)
            for month, child, label in months:
                if self.done:
                    break
                if self.since and year is not None and (year, month) < (self.since.year, self.since.month):
                    break
                self.walk(child, trail + [label], prio, depth + 1)
        elif nodes:
            self.walk_group(nodes, trail, prio, depth)

    def walk_group(self, nodes: list[tuple[str, str]], trail: list[str], prio: int | None, depth: int) -> None:
        first_nodes, first_docs = self.page(nodes[0][0])
        courts_level = _kind(first_nodes) == "years" or (bool(first_docs) and not first_nodes)
        order = list(nodes)
        if courts_level:
            # Sibling courts: visit the court with the most recent year first (newest-first listing).
            wanted = [n for n in nodes if self.court_wanted(n[1])]

            def newest(node: tuple[str, str]) -> int:
                if self.limit is None and not self.since:
                    return 0
                child_nodes, _ = self.page(node[0])
                years = [int(label) for _, label in child_nodes if re.fullmatch(r"\d{4}", label)]
                return max(years, default=0)
            order = sorted(wanted, key=newest, reverse=True)
        for path, label in order:
            if self.done:
                break
            self.walk(path, trail + [label], prio, depth + 1)


def _procedural(article: Tag) -> list[dict]:
    out = []
    history = article.find("section", class_="wkde-case-history")
    if history is None:
        return out
    for dt in history.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd is None:
            continue
        direction = clean_line(dt.get_text(" ")).lower()
        raw = clean_line(dd.get_text(" "))
        m = re.match(r"(?P<court>.+?)\s+-\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{4})\s+-\s+AZ:?\s*(?P<az>.+)$", raw, re.I)
        if not m:
            continue
        out.append({"direction": "subsequent" if direction.startswith("nachgehend") else "previous",
                    "court": m.group("court"), "date": iso_date(m.group("date")),
                    "case_number": clean_line(m.group("az")), "decision_type": None, "raw": raw,
                    "field": "voris:Verfahrensgang", "origin": "editorial_metadata"})
    return out


def _merge_list_numbers(node: Tag) -> None:
    """``<li><div class=listnum>1.</div><div class=listenpunkt><p>..`` -> '1. ..' in one block."""
    for num in node.find_all("div", class_="listnum"):
        body = num.find_next_sibling("div", class_="listenpunkt")
        label = clean_line(num.get_text(" "))
        target = body.find("p") if body is not None else None
        if target is not None and label:
            target.insert(0, NavigableString(label + " "))
            num.decompose()


def parse_document(markup: str, url: str, source_doc_id: str, headers: dict | None = None) -> ParsedDecision:
    soup = BeautifulSoup(markup, "html.parser")
    article = soup.find("article", class_="wkde-document")
    bib = article.find("section", class_="wkde-bibliography") if article is not None else None
    ensure_document(markup, bib is not None, url=url, what="VORIS decision document")
    pairs: dict[str, str] = {}
    for dt in bib.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        if dd is not None and dd.find(class_="wkde-empty") is None:
            pairs[clean_line(dt.get_text(" "))] = clean_line(dd.get_text(" "))
    court_short, raw_date, raw_az = pairs.get("Gericht"), pairs.get("Datum"), pairs.get("Aktenzeichen")
    if not court_short or not raw_date or not raw_az:
        raise UpstreamFailure("parse_error", "VORIS bibliography lacks Gericht/Datum/Aktenzeichen", url=url)
    article_uuid = (article.get("id") or "").removesuffix("_wkde-document")
    if UUID_RE.match(source_doc_id) and article_uuid and article_uuid != source_doc_id:
        raise UpstreamFailure("identity_mismatch", f"document {article_uuid} served for {source_doc_id}", url=url)
    decision_date = iso_date(raw_date)
    info = resolve_court(court_short, default_land="ni")
    title_node = article.find("h1", class_="wkde-doctitle")
    court_long = None
    if title_node is not None:
        first = title_node.find(string=True)
        court_long = clean_line(str(first)) if first else None
    metadata: dict = {}
    sections: list[Section] = []
    for headnote in article.find_all("section", class_=re.compile(r"headnote")):
        classes = headnote.get("class") or []
        heading_node = headnote.find(["h2", "h3"])
        heading = clean_line(heading_node.get_text(" ")) if heading_node is not None else None
        if heading_node is not None:
            heading_node.extract()
        _merge_list_numbers(headnote)
        official = any("official" in c for c in classes) or (heading or "").lower().startswith("amtliche")
        name, authorship = ("leitsatz", "court") if official else ("orientierungssatz", "editorial")
        basis = (f"VORIS section '{heading}' ({' '.join(classes)})" if heading else f"VORIS {' '.join(classes)}")
        sections.append(Section(name, authorship, basis + (": headnote of the court" if official else
                                                           ": editorial headnote"),
                                blocks=leaf_blocks(headnote, f"section.{classes[0]}"), heading=heading))
    body = article.find("section", class_="wkde-document-body")
    unmapped = []
    if body is not None:
        _merge_list_numbers(body)
        for div in body.find_all("div", recursive=False):
            classes = div.get("class") or []
            if "doc_footer" in classes:
                metadata["notice"] = clean_line(div.get_text(" ")) or None
                continue
            heading_node = div.find(["h2", "h3"], recursive=False)
            heading = clean_line(heading_node.get_text(" ")).rstrip(":").strip() if heading_node is not None else None
            if heading_node is not None:
                heading_node.extract()
            mapped = HEADINGS.get((heading or "").lower())
            if mapped is None:
                by_class = next((CLASS_SECTIONS[c] for c in classes if c in CLASS_SECTIONS), None)
                mapped = (by_class, "court") if by_class else ("sonstiger_langtext", "unknown")
                if not by_class:
                    unmapped.append(heading or " ".join(classes))
            name, authorship = mapped
            locator = f"#{div['id']}" if div.get("id") else f"div.{'.'.join(classes)}"
            blocks = leaf_blocks(div, locator)
            if blocks:
                sections.append(Section(name, authorship,
                                        f"VORIS div.{'.'.join(classes) or '-'} heading '{heading or '-'}'",
                                        blocks=blocks, heading=heading))
    order = {"titel": 0, "leitsatz": 1, "orientierungssatz": 2}
    sections.sort(key=lambda s: order.get(s.name, 3))  # headnotes first; body order kept (stable sort)
    catchwords = article.find("section", class_="wkde-case-catchwords")
    legal = article.find("section", class_="wkde-legal-bases")
    norms = [clean_line(li.get_text(" ")) for li in legal.find_all("li")] if legal is not None else []
    headers = headers or {}
    metadata.update({
        "norms": [n for n in norms if n],
        "keywords": [k for k in (clean_line(x) for x in catchwords.get_text(" ").split(";")) if k]
        if catchwords is not None else [],
        "reference": pairs.get("Referenz"), "decision_name": pairs.get("Entscheidungsname"),
        "court_short": court_short, "unmapped_sections": unmapped or None,
        "rn": "none published (plain <p id> paragraphs)",
        "rights": {"tdm_reservation": headers.get("tdm-reservation", "1"),
                   "tdm_policy": headers.get("tdm-policy", TDM_POLICY),
                   "note": "commercial TDM reserved (§ 44b Abs. 3 UrhG); non-commercial research corpus"}})
    ecli = checked_ecli(pairs.get("ECLI"), decision_date, metadata)
    return ParsedDecision(
        source_id="ni", source_doc_id=source_doc_id, source_url=url, court_name=court_long or court_short,
        decision_date=decision_date, case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az),
        decision_type=pairs.get("Entscheidungsform"), sections=sections, ecli=ecli, court_id=info.court_id,
        court_type=info.court_type, jurisdiction=info.jurisdiction, land=info.land or "ni", chamber=None,
        title=clean_line(title_node.get_text(" ")) if title_node is not None else None, metadata=metadata,
        procedural=_procedural(article))


class NiedersachsenAdapter(Adapter):
    source_id = "ni"
    label = "Niedersachsen: NI-VORIS (Wolters Kluwer; tdm-reservation for commercial TDM)"
    base_url = BASE
    land = "ni"
    adapter_name = "ni-voris-html"

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        window = since or (None if limit else incremental_since(conn, self.source_id, INCREMENTAL_OVERLAP_DAYS))
        walker = _Walk(http, since=window, limit=limit, courts=courts)
        branches, _ = walker.page(ROOT)
        if not branches:
            raise UpstreamFailure("unexpected_representation", "VORIS Rechtsprechung root without branches",
                                  url=BASE + ROOT)
        ordered = sorted(branches, key=lambda b: BRANCH_PRIORITY.get(b[1], 1))  # stable
        for path, label in ordered:
            if walker.done:
                break
            walker.walk(path, [label], BRANCH_PRIORITY.get(label, 1), 1)
        full = not (window or limit or courts)
        return Listing(items=walker.items, upstream_total=len(walker.items) if full else None, complete=full)

    def fetch(self, http: PoliteHttp, item) -> Capture:
        response = http.get(item["url"], timeout=90)
        markup = decode_html(response.body, response.headers)
        ensure_document(markup, "wkde-document" in markup and "wkde-bibliography" in markup,
                        url=item["url"], what="VORIS decision document")
        return Capture(body=response.body, url=item["url"], media_type="text/html", extension="html",
                       headers=response.headers)

    def parse(self, capture: Capture, item) -> ParsedDecision:
        parsed = parse_document(decode_html(capture.body, capture.headers), capture.url, item["source_doc_id"],
                                capture.headers)
        listed_date = item["listed_date"]
        if listed_date and listed_date != parsed.decision_date:
            raise UpstreamFailure("identity_mismatch", f"listed date {listed_date} != page {parsed.decision_date}",
                                  url=capture.url)
        listed_az = item["listed_case_numbers"]
        if listed_az and squash(listed_az) != squash(parsed.case_numbers_raw):
            raise UpstreamFailure("identity_mismatch", f"listed Az {listed_az!r} != page {parsed.case_numbers_raw!r}",
                                  url=capture.url)
        listed_court = item["listed_court"]
        if listed_court and listed_court != parsed.court_id:
            raise UpstreamFailure("identity_mismatch", f"listed court {listed_court} != page {parsed.court_id}",
                                  url=capture.url)
        return parsed


def adapters() -> dict[str, Adapter]:
    return {"ni": NiedersachsenAdapter()}
