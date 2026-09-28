"""Brandenburg: gerichtsentscheidungen.brandenburg.de (own server-rendered app).

Covers the Brandenburg courts and the joint Berlin-Brandenburg courts (OVG,
LSG, FG, LArbG).  robots.txt answers 404 (no rules); Nutzungshinweise:
decisions "dürfen unentgeltlich ausgedruckt und gespeichert werden".

Listing: ``GET /suche?input_date_promulgation_from=DD.MM.YYYY&select_source=<court>&page=N``
-> HTML table ``#resultlist`` (20 rows, newest decision date first, "Seite N/M"),
rows link ``/gerichtsentscheidung/<int>``.  The list shows no Aktenzeichen.
Document: metadata table (Gericht, Entscheidungsdatum, Aktenzeichen, ECLI,
Dokumententyp, Verfahrensgang, Normen), juris-style sections introduced by
``<h4 class="doc">`` followed by ``<dl class="RspDL"><dt></dt><dd>..``.  The
``<dt>`` is always empty: the portal publishes no Randnummern, so blocks are
unlabelled and the decision is ``numbering: none`` (``#p-<n>`` ids).
"""
from __future__ import annotations

import re
import sqlite3
from urllib.parse import urlencode

from bs4 import BeautifulSoup, Tag

from ..casenumbers import split_case_numbers
from ..courtids import priority, resolve as resolve_court
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import ParsedDecision, Section
from .base import Adapter, Capture, Listing, decode_html, iso_date, squash
from .htmlblocks import (checked_ecli, clean_line, ensure_document, german_date, incremental_since,
                         item_extra, leaf_blocks)

BASE = "https://gerichtsentscheidungen.brandenburg.de"
SEARCH_URL = BASE + "/suche"
DOC_URL = BASE + "/gerichtsentscheidung/{doc}"
PAGE_SIZE = 20
INCREMENTAL_OVERLAP_DAYS = 180

# (select_source option value, name on document pages, official name, name used for the court id)
COURTS: list[tuple[str, str, str, str]] = [
    ("LSG Berlin-Potsdam", "LSG Berlin-Brandenburg", "Landessozialgericht Berlin-Brandenburg",
     "LSG Berlin-Brandenburg"),
    ("SG Cottbus", "SG Cottbus", "Sozialgericht Cottbus", "SG Cottbus"),
    ("SG Frankfurt (Oder)", "SG Frankfurt (Oder)", "Sozialgericht Frankfurt (Oder)", "SG Frankfurt (Oder)"),
    ("SG Neuruppin", "SG Neuruppin", "Sozialgericht Neuruppin", "SG Neuruppin"),
    ("SG Potsdam", "SG Potsdam", "Sozialgericht Potsdam", "SG Potsdam"),
    ("OVG Berlin-Brandenburg", "OVG Berlin-Brandenburg", "Oberverwaltungsgericht Berlin-Brandenburg",
     "OVG Berlin-Brandenburg"),
    ("VG Cottbus", "VG Cottbus", "Verwaltungsgericht Cottbus", "VG Cottbus"),
    ("VG Frankfurt (Oder)", "VG Frankfurt (Oder)", "Verwaltungsgericht Frankfurt (Oder)", "VG Frankfurt (Oder)"),
    ("VG Potsdam", "VG Potsdam", "Verwaltungsgericht Potsdam", "VG Potsdam"),
    ("FG Berlin-Brandenburg", "FG Berlin-Brandenburg", "Finanzgericht Berlin-Brandenburg",
     "Finanzgericht Berlin-Brandenburg"),
    ("LArbG Berlin-Brandenburg", "LArbG Berlin-Brandenburg", "Landesarbeitsgericht Berlin-Brandenburg",
     "Landesarbeitsgericht Berlin-Brandenburg"),
    ("ArbG Brandenburg", "ArbG Brandenburg", "Arbeitsgericht Brandenburg an der Havel", "Arbeitsgericht Brandenburg"),
    ("ArbG Cottbus", "ArbG Cottbus", "Arbeitsgericht Cottbus", "Arbeitsgericht Cottbus"),
    ("ArbG Potsdam", "ArbG Potsdam", "Arbeitsgericht Potsdam", "Arbeitsgericht Potsdam"),
    ("OLG Brandenburg", "OLG Brandenburg", "Brandenburgisches Oberlandesgericht", "OLG Brandenburg"),
    ("LG Cottbus", "LG Cottbus", "Landgericht Cottbus", "LG Cottbus"),
    ("LG Frankfurt (Oder)", "LG Frankfurt (Oder)", "Landgericht Frankfurt (Oder)", "LG Frankfurt (Oder)"),
    ("LG Neuruppin", "LG Neuruppin", "Landgericht Neuruppin", "LG Neuruppin"),
    ("LG Potsdam", "LG Potsdam", "Landgericht Potsdam", "LG Potsdam"),
    ("AG Bad Liebenwerda", "AG Bad Liebenwerda", "Amtsgericht Bad Liebenwerda", "AG Bad Liebenwerda"),
    ("AG Bernau", "AG Bernau", "Amtsgericht Bernau bei Berlin", "AG Bernau"),
    ("AG Brandenburg", "AG Brandenburg", "Amtsgericht Brandenburg an der Havel", "AG Brandenburg"),
    ("AG Frankfurt (Oder)", "AG Frankfurt (Oder)", "Amtsgericht Frankfurt (Oder)", "AG Frankfurt (Oder)"),
    ("AG Königs Wusterhausen", "AG Königs Wusterhausen", "Amtsgericht Königs Wusterhausen",
     "AG Königs Wusterhausen"),
    ("AG Oranienburg", "AG Oranienburg", "Amtsgericht Oranienburg", "AG Oranienburg"),
    ("AG Potsdam", "AG Potsdam", "Amtsgericht Potsdam", "AG Potsdam"),
    ("AG Schwedt", "AG Schwedt", "Amtsgericht Schwedt/Oder", "AG Schwedt"),
    ("AG Senftenberg", "AG Senftenberg", "Amtsgericht Senftenberg", "AG Senftenberg"),
    ("AG Zossen", "AG Zossen", "Amtsgericht Zossen", "AG Zossen"),
    ("VerfG Potsdam", "VerfG Potsdam", "Verfassungsgericht des Landes Brandenburg",
     "Verfassungsgericht des Landes Brandenburg"),
    ("Vergabekammer Potsdam", "Vergabekammer Potsdam", "Vergabekammer des Landes Brandenburg",
     "Vergabekammer des Landes Brandenburg"),
]
# <h4 class="doc"> heading -> (canonical section, authorship, basis)
HEADINGS = {
    "leitsatz": ("leitsatz", "court", "BB portal (juris format) heading 'Leitsatz': headnote of the court"),
    "leitsätze": ("leitsatz", "court", "BB portal (juris format) heading 'Leitsätze': headnote of the court"),
    "orientierungssatz": ("orientierungssatz", "editorial",
                          "BB portal (juris format) heading 'Orientierungssatz': documentation headnote"),
    "tenor": ("tenor", "court", "BB portal heading 'Tenor'"),
    "tatbestand": ("tatbestand", "court", "BB portal heading 'Tatbestand'"),
    "entscheidungsgründe": ("entscheidungsgruende", "court", "BB portal heading 'Entscheidungsgründe'"),
    "gründe": ("gruende", "court", "BB portal heading 'Gründe'"),
    "abweichende meinung": ("abweichende_meinung", "court", "BB portal heading 'Abweichende Meinung'"),
    "sondervotum": ("abweichende_meinung", "court", "BB portal heading 'Sondervotum'"),
}
CHAMBER_RE = re.compile(r"\s+(?:(?:Der|Die|Das)\s+)?(\d+\.\s*(?:Senat|Kammer|Zivilsenat|Strafsenat|Familiensenat|"
                        r"Vergabesenat|Große Strafkammer|Kleine Strafkammer|Zivilkammer|Strafkammer)\b.*)$")
PAGES_RE = re.compile(r"Seite\s+(\d+)\s*/\s*(\d+)")
EMPTY_RESULT = "Es wurden keine Einträge gefunden"


def court_entry(name: str) -> tuple[str, str, str, str] | None:
    key = clean_line(name)
    for entry in COURTS:
        if key in (entry[0], entry[1]):
            return entry
    return None


def court_info(name: str):
    entry = court_entry(name)
    return resolve_court(entry[3] if entry else name, default_land="bb")


def search_url(court_value: str, von: str | None, page: int) -> str:
    params = {}
    if von:
        params["input_date_promulgation_from"] = von
    params["select_source"] = court_value
    params["page"] = str(page)
    return SEARCH_URL + "?" + urlencode(params)


def parse_result_list(markup: str, court_value: str, *, url: str = SEARCH_URL) -> tuple[int, list[dict]]:
    """(page count, items) of one result page."""
    soup = BeautifulSoup(markup, "html.parser")
    table = soup.find("table", id="resultlist")
    if table is None:
        if EMPTY_RESULT in markup:
            return 0, []
        ensure_document(markup, False, url=url, what="BB result list")
    pages_m = PAGES_RE.search(markup)
    pages = int(pages_m.group(2)) if pages_m else 1
    info = court_info(court_value)
    items = []
    for tr in (table.find("tbody") or table).find_all("tr", recursive=False):
        cells = tr.find_all("td", recursive=False)
        link = tr.find("a", href=re.compile(r"^/gerichtsentscheidung/\d+$"))
        if len(cells) < 5 or link is None:
            continue
        doc = link["href"].rsplit("/", 1)[1]
        raw_date = squash(cells[2].get_text())
        items.append({
            "source_doc_id": doc, "url": DOC_URL.format(doc=doc),
            "listed_date": iso_date(raw_date) if raw_date else None,
            "listed_court": info.court_id, "listed_case_numbers": None, "listed_modified": None,
            "priority": priority(info.jurisdiction),
            "extra": {"select_source": court_value, "decision_type": squash(cells[1].get_text()) or None,
                      "title": squash(link.get_text()) or None, "court_chamber": squash(cells[4].get_text()) or None}})
    if not items and pages:
        raise UpstreamFailure("unexpected_representation", "BB result table without rows", url=url)
    return pages, items


def _metadata(soup: BeautifulSoup) -> dict[str, Tag]:
    table = soup.select_one("#metadata table")
    pairs: dict[str, Tag] = {}
    if table is None:
        return pairs
    for th in table.find_all("th"):
        td = th.find_next_sibling("td")
        if td is not None:
            pairs[clean_line(th.get_text(" "))] = td
    return pairs


def parse_verfahrensgang(raw: str) -> list[dict]:
    """Best effort for juris-style 'vorgehend SG Berlin, 19. Juni 2024, Az: S 83 KA 12/22'."""
    out = []
    for line in re.split(r"\n|;", raw):
        line = clean_line(line)
        m = re.match(r"(?:(?P<dir>vorgehend|nachgehend)\s+)?(?P<court>[^,]+?),?\s+(?:vom\s+)?"
                     r"(?P<date>\d{1,2}\.\s*(?:\d{1,2}\.\s*|[A-Za-zäÄ]+\s+)\d{4}),?\s*(?:Az\.?:?\s*)?"
                     r"(?P<az>[^,]+?)(?:,\s*(?P<type>.+))?$", line)
        if not m:
            continue
        try:
            when = iso_date(m.group("date"))
        except UpstreamFailure:
            when = None
        out.append({"direction": "subsequent" if m.group("dir") == "nachgehend" else "previous",
                    "court": m.group("court"), "date": when, "case_number": clean_line(m.group("az")),
                    "decision_type": m.group("type"), "raw": line, "field": "bb:Verfahrensgang",
                    "origin": "source_metadata"})
    return out


def parse_document(markup: str, url: str, source_doc_id: str) -> ParsedDecision:
    soup = BeautifulSoup(markup, "html.parser")
    meta = _metadata(soup)
    detail = soup.find(id="gerichtsentscheidung-detail")
    ensure_document(markup, bool(meta.get("Gericht") and meta.get("Aktenzeichen") and detail is not None),
                    url=url, what="BB decision page")

    def value(key: str) -> str | None:
        node = meta.get(key)
        text = clean_line(node.get_text(" ")) if node is not None else ""
        return text if text and text != "-" else None
    court_raw = value("Gericht") or ""
    chamber = None
    m = CHAMBER_RE.search(court_raw)
    court_short = court_raw[:m.start()].strip() if m else court_raw
    if m:
        chamber = m.group(1)
    entry = court_entry(court_short)
    info = resolve_court(entry[3] if entry else court_short, default_land="bb")
    raw_az = clean_line(meta["Aktenzeichen"].get_text(" "))
    raw_date = value("Entscheidungsdatum")
    if not raw_az or not raw_date:
        raise UpstreamFailure("parse_error", "BB page lacks Aktenzeichen or Entscheidungsdatum", url=url)
    decision_date = iso_date(raw_date)
    metadata: dict = {}
    sections: list[Section] = []
    h1 = soup.find("h1", id="header")
    title = clean_line(h1.get_text(" ")) if h1 is not None else None
    if title:
        sections.append(Section("titel", "editorial",
                                "BB portal <h1>: documentation title/keywords (not part of the decision text)",
                                blocks=leaf_blocks(h1, "h1#header")))
    container = detail.find("div", class_="dokutyp") or detail
    current: Section | None = None
    unknown_headings = []
    n_headings = 0
    for el in container.find_all(["h4", "dl"]):
        if el.name == "h4" and "doc" in (el.get("class") or []):
            n_headings += 1
            heading = clean_line(el.get_text(" "))
            name, authorship, basis = HEADINGS.get(heading.lower().rstrip(":"), (
                "sonstiger_langtext", "unknown", f"BB portal heading '{heading}' (unmapped)"))
            if name == "sonstiger_langtext":
                unknown_headings.append(heading)
            current = Section(name, authorship, basis, heading=heading)
            sections.append(current)
        elif el.name == "dl" and "RspDL" in (el.get("class") or []) and el.find_parent("dl") is None:
            if current is None:
                current = Section("sonstiger_langtext", "unknown", "BB portal text before any heading")
                sections.append(current)
            for index, dd in enumerate(el.find_all("dd", recursive=False), 1):
                dt = dd.find_previous_sibling("dt")
                if dt is not None and clean_line(dt.get_text()):
                    # The portal's <dt> is always empty; a filled one would be an unmodelled Rn.
                    metadata.setdefault("unexpected_dt_labels", []).append(clean_line(dt.get_text()))
                current.blocks += leaf_blocks(dd, f"h4[{n_headings}]/dl.RspDL/dd[{index}]")
    sections = [s for s in sections if s.blocks]
    norms_raw = value("Normen")
    norms = [clean_line(n) for n in re.split(r"\s*,\s*", norms_raw or "") if clean_line(n)]
    verfahrensgang = value("Verfahrensgang")
    ecli = checked_ecli(value("ECLI"), decision_date, metadata)
    metadata.update({"norms": norms, "norms_raw": norms_raw, "court_raw": court_raw,
                     "verfahrensgang_raw": verfahrensgang, "unmapped_headings": unknown_headings or None,
                     "rn": "none published (portal <dt> always empty)"})
    return ParsedDecision(
        source_id="bb", source_doc_id=source_doc_id, source_url=url,
        court_name=entry[2] if entry else court_short, decision_date=decision_date,
        case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az),
        decision_type=value("Dokumententyp"), sections=sections, ecli=ecli, court_id=info.court_id,
        court_type=info.court_type, jurisdiction=info.jurisdiction, land=info.land or "bb", chamber=chamber,
        title=title, metadata=metadata, procedural=parse_verfahrensgang(verfahrensgang) if verfahrensgang else [])


class BrandenburgAdapter(Adapter):
    source_id = "bb"
    label = "Brandenburg: gerichtsentscheidungen.brandenburg.de (incl. OVG/LSG/FG/LArbG Berlin-Brandenburg)"
    base_url = BASE
    land = "bb"
    adapter_name = "bb-html"
    min_interval = 2.0  # small court site (robots.txt Crawl-delay, if any, still applies)

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        window = since or (None if limit else incremental_since(conn, self.source_id, INCREMENTAL_OVERLAP_DAYS))
        von = german_date(window) if window else None
        selected = []
        for entry in COURTS:
            info = court_info(entry[0])
            if courts and not any(info.court_id.startswith(c.lower()) for c in courts):
                continue
            selected.append((priority(info.jurisdiction), entry[0]))
        selected.sort(key=lambda s: s[0])          # social/administrative courts first (stable)
        items: list[dict] = []
        for _prio, value in selected:
            page = 1
            while True:
                url = search_url(value, von, page)
                response = http.get(url, timeout=90)
                pages, rows = parse_result_list(decode_html(response.body, response.headers), value, url=url)
                items.extend(rows)
                if not rows or page >= pages or (limit and len(items) >= limit):
                    break
                page += 1
            if limit and len(items) >= limit:
                break
        full = not (window or limit or courts)
        return Listing(items=items, upstream_total=len(items) if full else None, complete=full)

    def fetch(self, http: PoliteHttp, item) -> Capture:
        response = http.get(item["url"], timeout=90)
        markup = decode_html(response.body, response.headers)
        ensure_document(markup, 'id="gerichtsentscheidung-detail"' in markup and 'id="metadata"' in markup,
                        url=item["url"], what="BB decision page")
        return Capture(body=response.body, url=item["url"], media_type="text/html", extension="html",
                       headers=response.headers)

    def parse(self, capture: Capture, item) -> ParsedDecision:
        parsed = parse_document(decode_html(capture.body, capture.headers), capture.url, item["source_doc_id"])
        listed_date = item["listed_date"]
        if listed_date and listed_date != parsed.decision_date:
            raise UpstreamFailure("identity_mismatch", f"listed date {listed_date} != page {parsed.decision_date}",
                                  url=capture.url)
        listed_court = item["listed_court"]
        if listed_court and listed_court != parsed.court_id:
            raise UpstreamFailure("identity_mismatch", f"listed court {listed_court} != page {parsed.court_id}",
                                  url=capture.url)
        listed_az = item["listed_case_numbers"]
        if listed_az and squash(listed_az) != squash(parsed.case_numbers_raw):
            raise UpstreamFailure("identity_mismatch", f"listed Az {listed_az!r} != page {parsed.case_numbers_raw!r}",
                                  url=capture.url)
        extra = item_extra(item)
        if extra.get("decision_type") and parsed.decision_type and extra["decision_type"] != parsed.decision_type:
            parsed.metadata["listed_decision_type"] = extra["decision_type"]
        return parsed


def adapters() -> dict[str, Adapter]:
    return {"bb": BrandenburgAdapter()}
