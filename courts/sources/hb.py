"""Bremen: court websites (SixCMS) with an "Entscheidungsübersicht" and PDFs.

No central portal.  OVG, VG and OLG Bremen publish a server-rendered list
``/entscheidungen/entscheidungsuebersicht-<id>?max=N&skip=M`` (parameter order
max, skip; newest decision date first; "Anzahl der Einträge: N").  Rows are
``tr.search-result[data-date]`` with date, Aktenzeichen, Normen, Rechtsgebiet,
Entscheidungsart, the PDF link and a truncated Leitsatz.  robots.txt is empty;
terms: free for non-commercial use.

The capture is the PDF itself; text comes from ``pdftotext -layout``.  Page
furniture (page numbers "2", "- 2 -", "Seite 2/7", repeated running lines) is
dropped, and a paragraph interrupted by a page break is re-joined, so page
breaks never create paragraphs.  The PDFs carry no Randnummern: decisions are
``numbering: none``.  The rubrum (parties, representatives) is not a section;
the Tenor starts after "... beschlossen:" / "... für Recht erkannt:".
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import quote, urljoin

from ..casenumbers import split_case_numbers
from ..courtids import priority, resolve as resolve_court
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import Block, ParsedDecision, Section
from .base import Adapter, Capture, Listing, iso_date
from .htmlblocks import clean_line, ensure_document, incremental_since, is_outline_marker, item_extra
from .pdftext import join_lines, pdf_text

INCREMENTAL_OVERLAP_DAYS = 365


@dataclass(frozen=True)
class Court:
    key: str
    base: str
    list_id: int
    name: str           # official name
    short: str          # used for the court id and for citations ("OVG Bremen")


COURTS = [
    Court("ovg", "https://www.oberverwaltungsgericht.bremen.de", 11265,
          "Oberverwaltungsgericht der Freien Hansestadt Bremen", "OVG Bremen"),
    Court("vg", "https://www.verwaltungsgericht.bremen.de", 13039,
          "Verwaltungsgericht der Freien Hansestadt Bremen", "VG Bremen"),
    Court("olg", "https://www.oberlandesgericht.bremen.de", 2335,
          "Hanseatisches Oberlandesgericht in Bremen", "OLG Bremen"),
]
COURT_BY_KEY = {c.key: c for c in COURTS}
LIST_PATH = "/entscheidungen/entscheidungsuebersicht-{id}"
TOTAL_RE = re.compile(r"Anzahl der Eintr(?:ä|&auml;)ge:\s*(\d+)")
GSID_RE = re.compile(r"detail\.php\?gsid=(bremen\d+\.c\.\d+)\.de")
PDF_TIMEOUT = 120

# --- listing -------------------------------------------------------------------


def list_url(court: Court, max_n: int, skip: int) -> str:
    return f"{court.base}{LIST_PATH.format(id=court.list_id)}?max={max_n}&skip={skip}"


def _html_lines(fragment: str) -> list[str]:
    from html import unescape
    parts = re.split(r"<br\s*/?>", fragment)
    return [clean_line(unescape(re.sub(r"<[^>]+>", " ", p))) for p in parts]


def parse_list(markup: str, court: Court, *, url: str = "") -> tuple[int, list[dict]]:
    """(total entries, items) of one Entscheidungsübersicht page."""
    total_m = TOTAL_RE.search(markup)
    ensure_document(markup, total_m is not None, url=url, what="Bremen Entscheidungsübersicht")
    info = resolve_court(court.short, default_land="hb")
    items = []
    for chunk in markup.split('<tr class="search-result"')[1:]:
        chunk = chunk.split("</table>")[0]
        date_m = re.search(r'data-date="(\d{4}-\d{2}-\d{2})"', chunk)
        cells = re.split(r"</td>", chunk)
        if not date_m or len(cells) < 2:
            continue
        lines = _html_lines(re.sub(r"^.*?<td[^>]*>", "", cells[0], count=1, flags=re.S))
        while lines and not lines[-1]:
            lines.pop()
        # date, '', Az, Normen..., Rechtsgebiet, Entscheidungsart
        if len(lines) < 3:
            continue
        az = lines[2]
        tail = lines[3:]
        decision_type = tail[-1] if tail else None
        area = tail[-2] if len(tail) >= 2 else None
        norms = [n for n in tail[:-2] if n]
        pdf_m = re.search(r'<a href="([^"]+?\.pdf)"[^>]*>(.*?)</a>', chunk, re.S | re.I)
        gsid_m = GSID_RE.search(chunk)
        if not pdf_m or not gsid_m or not az:
            continue
        href = pdf_m.group(1).replace("&amp;", "&")
        pdf_url = urljoin(court.base + "/", quote(href, safe="/%:?&=()"))
        link_text = _html_lines(pdf_m.group(2))[0] if pdf_m.group(2) else ""
        title = re.sub(r"\s*\(pdf,\s*[\d.,]+\s*[KM]B\)\s*$", "", link_text) or None
        after = chunk[pdf_m.end():]
        excerpt = _html_lines(re.split(r"<a [^>]*detail\.php", after)[0])
        excerpt_text = " ".join(x for x in excerpt if x) or None
        detail = re.search(r'href="(detail\.php\?[^"]+)"', chunk)
        items.append({
            "source_doc_id": gsid_m.group(1), "url": pdf_url, "listed_date": date_m.group(1),
            "listed_court": info.court_id, "listed_case_numbers": az, "listed_modified": None,
            "priority": priority(info.jurisdiction),
            "extra": {"court": court.key, "decision_type": decision_type, "rechtsgebiet": area, "norms": norms,
                      "title": title, "listing_leitsatz_excerpt": excerpt_text,
                      "detail_url": urljoin(court.base + "/entscheidungen/", detail.group(1).replace("&amp;", "&"))
                      if detail else None}})
    return int(total_m.group(1)), items


# --- PDF text ------------------------------------------------------------------

PAGE_NO = re.compile(r"(?:-\s*)?\d{1,3}(?:\s*-)?|Seite\s+\d{1,3}(?:\s*(?:/|von)\s*\d{1,3})?|\d{1,3}\s*/\s*\d{1,3}",
                     re.I)
TERMINAL = re.compile(r"[.:!?][\"“”'’)\]]*$")
ABBREVIATIONS = {"abs.", "nr.", "s.", "art.", "vgl.", "bzw.", "u.", "a.", "z.b.", "d.h.", "v.", "urt.", "beschl.",
                 "rn.", "rdnr.", "az.", "i.v.m.", "f.", "ff.", "buchst.", "alt.", "hs.", "var.", "bl.", "dr.", "ca.",
                 "nds.", "kap.", "anm.", "aufl.", "bd.", "lit.", "ziff.", "gem.", "i.s.d.", "i.s.v.", "m.w.n.",
                 "o.", "u.a.", "sog.", "st.", "str.", "rspr.", "prof.", "ggf.", "evtl.", "zit.", "a.a.o.", "hrsg.",
                 "vorb.", "einl.", "e.v.", "m.", "n."}
SECTION_HEADINGS = {
    "tenor": "tenor", "tatbestand": "tatbestand", "entscheidungsgründe": "entscheidungsgruende",
    "gründe": "gruende", "leitsatz": "leitsatz", "leitsätze": "leitsatz",
    "abweichende meinung": "abweichende_meinung", "sondervotum": "abweichende_meinung",
}
INNER_HEADINGS = {"rechtsmittelbelehrung", "hinweis", "hinweise"}
DOCTYPE_LINES = {"beschluss", "urteil", "im namen des volkes", "gerichtsbescheid", "verfügung", "teilurteil",
                 "endurteil", "versäumnisurteil", "schlussurteil", "zwischenurteil"}
RUBRUM_END = re.compile(r"(?:beschlossen|erkannt|entschieden|verfügt|beschließt|erkennt|angeordnet)\s*:$", re.I)
DATE_AM = re.compile(r"\bam\s+(\d{1,2}\.\s*(?:\d{1,2}\.\s*\d{4}|[A-Za-zäÄ]+\s+\d{4}))")


def _norm_heading(line: str) -> str:
    text = line.strip()
    if re.fullmatch(r"(?:\S ){2,}\S:?", text):          # letter-spaced "G r ü n d e"
        text = text.replace(" ", "")
    return clean_line(text).rstrip(":").strip().lower()


def heading_key(line: str) -> str | None:
    """Canonical section for a section-heading line, 'inner' for in-section headings, else None."""
    key = _norm_heading(line)
    if key in SECTION_HEADINGS:
        return SECTION_HEADINGS[key]
    if key in INNER_HEADINGS:
        return "inner"
    return None


@dataclass
class Chunk:
    lines: list[str]
    start: tuple[int, int]     # (page, line) 1-based
    end: tuple[int, int]
    heading: str | None = None   # section key / 'inner'

    @property
    def text(self) -> str:
        return join_lines(self.lines)       # shared with pdftext: spaces + safe de-hyphenation

    @property
    def locator(self) -> str:
        (p1, l1), (p2, l2) = self.start, self.end
        return f"pdf:p{p1}:l{l1}-" + (f"l{l2}" if p1 == p2 else f"p{p2}:l{l2}")


def _furniture(pages: list[list[str]]) -> set[tuple[int, int]]:
    """(page index, line index) of page numbers and running headers/footers."""
    drop: set[tuple[int, int]] = set()
    edges: dict[str, list[tuple[int, int]]] = {}
    for p, lines in enumerate(pages):
        content = [i for i, line in enumerate(lines) if line.strip()]
        if not content:
            continue
        for i in {content[0], content[-1]}:
            text = clean_line(lines[i])
            if PAGE_NO.fullmatch(text):
                drop.add((p, i))
            else:
                edges.setdefault(re.sub(r"\d+", "#", text), []).append((p, i))
    if len(pages) >= 3:
        for key, spots in edges.items():
            pages_hit = {p for p, _ in spots}
            if len(pages_hit) >= max(2, (len(pages) - 1) // 2) and len(key) < 120:
                drop.update(spots)
    return drop


def _ends_sentence(line: str) -> bool:
    text = clean_line(line)
    if not TERMINAL.search(text):
        return False
    last = text.split(" ")[-1].lower()
    return last not in ABBREVIATIONS and not re.fullmatch(r"\d{1,2}\.", last)


def continues(prev: Chunk, nxt: Chunk) -> bool:
    """Does ``nxt`` (first chunk of a page) continue ``prev`` (last chunk of the previous page)?"""
    if prev.heading or nxt.heading or is_outline_marker(prev.text) or is_outline_marker(nxt.text):
        return False
    last, first = clean_line(prev.lines[-1]), clean_line(nxt.lines[0])
    if not last or not first:
        return False
    if last.endswith(("-", "‐")) and not last.endswith(" -"):
        return True
    if not _ends_sentence(last):
        return True
    if is_outline_marker(first.split(" ")[0]):     # "aa) Mit ...", "2. Die ..." after a full stop
        return False
    return bool(re.match(r"[a-zäöüß]", first))


def pdf_chunks(text: str) -> tuple[list[Chunk], dict]:
    """Paragraph chunks in reading order, page furniture removed, page-split paragraphs re-joined."""
    pages = [page.split("\n") for page in text.split("\f")]
    while pages and not any(line.strip() for line in pages[-1]):
        pages.pop()
    drop = _furniture(pages)
    chunks: list[Chunk] = []
    joined = 0
    for p, lines in enumerate(pages):
        page_chunks: list[Chunk] = []
        current: Chunk | None = None
        for i, line in enumerate(lines):
            if (p, i) in drop or not line.strip():
                current = None
                continue
            key = heading_key(line)
            if key:
                page_chunks.append(Chunk([line], (p + 1, i + 1), (p + 1, i + 1), heading=key))
                current = None
                continue
            if current is None:
                current = Chunk([], (p + 1, i + 1), (p + 1, i + 1))
                page_chunks.append(current)
            current.lines.append(line)
            current.end = (p + 1, i + 1)
        if page_chunks and chunks and continues(chunks[-1], page_chunks[0]):
            head = page_chunks.pop(0)
            chunks[-1].lines += head.lines
            chunks[-1].end = head.end
            joined += 1
        chunks += page_chunks
    info = {"pages": len(pages), "page_furniture_lines_dropped": len(drop), "page_break_joins": joined}
    return chunks, info


# --- decision ------------------------------------------------------------------

def _court_for(item) -> Court:
    extra = item_extra(item)
    court = COURT_BY_KEY.get(extra.get("court") or "")
    if court is None:
        listed = item["listed_court"]
        court = next((c for c in COURTS if resolve_court(c.short, default_land="hb").court_id == listed), None)
    if court is None:
        raise UpstreamFailure("parse_error", f"unknown Bremen court for item {item['source_doc_id']}")
    return court


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


def parse_pdf_decision(body: bytes, item, url: str) -> ParsedDecision:
    court = _court_for(item)
    extra = item_extra(item)
    text = pdf_text(body, url=url)
    chunks, info = pdf_chunks(text)
    raw_az = clean_line(item["listed_case_numbers"] or "")
    if not raw_az:
        raise UpstreamFailure("parse_error", "listing without Aktenzeichen", url=url)
    first_page = " ".join(c.text for c in chunks if c.start[0] == 1)
    if _compact(raw_az) not in _compact(first_page):
        if _compact(raw_az) not in _compact(text):
            raise UpstreamFailure("identity_mismatch", f"listed Az {raw_az!r} not found in the PDF", url=url)
    header_lines = [clean_line(line) for line in chunks[0].lines if clean_line(line)] if chunks else []
    sections: list[Section] = []
    rubrum: list[Chunk] = []
    state = "rubrum"
    current: Section | None = None
    date_in_text = None

    def open_section(name: str, basis: str, heading: str | None) -> Section:
        section = Section(name, "court", basis, heading=heading)
        sections.append(section)
        return section

    for chunk in chunks:
        if chunk.heading and chunk.heading != "inner":
            if state == "rubrum" and chunk.heading == "leitsatz":
                state = "leitsatz"
            elif state == "rubrum" and chunk.heading not in ("tenor", "tatbestand", "gruende",
                                                             "entscheidungsgruende"):
                rubrum.append(chunk)
                continue
            else:
                state = "body"
            heading = clean_line(chunk.lines[0])
            if chunk.heading == "tenor" and current is not None and current.name == "tenor" and not current.blocks:
                current.heading = heading
                continue
            current = open_section(chunk.heading, f"PDF heading '{heading}'", heading)
            continue
        if state == "leitsatz" and _norm_heading(chunk.text) in DOCTYPE_LINES:
            state, current = "rubrum", None
        if state == "rubrum":
            rubrum.append(chunk)
            flat = chunk.text
            if RUBRUM_END.search(flat):
                dates = DATE_AM.findall(flat)
                if dates:
                    try:
                        date_in_text = iso_date(dates[-1])
                    except UpstreamFailure:
                        date_in_text = None
                state = "body"
                current = open_section("tenor", f"PDF text after '… {flat.split(' ')[-1]}' (Tenor)", None)
            continue
        if current is None:
            current = open_section("sonstiger_langtext", "PDF text without recognised heading", None)
        kind = "heading" if chunk.heading == "inner" or is_outline_marker(chunk.text) else "text"
        current.blocks.append(Block(text=chunk.text, kind=kind, locator=chunk.locator))
    if not any(s.blocks for s in sections):
        # No recognisable structure: keep the whole text rather than guessing sections.
        sections = [Section("sonstiger_langtext", "court", "PDF text without recognisable Tenor/Gründe structure",
                            blocks=[Block(text=c.text, locator=c.locator) for c in chunks])]
        rubrum = []
    sections = [s for s in sections if s.blocks]
    listed_date = item["listed_date"]
    if date_in_text and listed_date and date_in_text != listed_date:
        raise UpstreamFailure("identity_mismatch", f"listed date {listed_date} != PDF 'am {date_in_text}'", url=url)
    procedural = []
    for line in header_lines[1:6]:
        m = re.fullmatch(r"(OVG|VG|OLG|LG|AG|SG|LSG|ArbG|LAG)\s*:\s*(.+)", line)
        if m and m.group(1) != court.key.upper():
            procedural.append({"direction": "previous", "court": f"{m.group(1)} Bremen", "date": None,
                               "case_number": clean_line(m.group(2)), "decision_type": None, "raw": line,
                               "field": "pdf:header", "origin": "decision_text"})
    info_court = resolve_court(court.short, default_land="hb")
    metadata = {
        "norms": extra.get("norms") or [], "rechtsgebiet": extra.get("rechtsgebiet"),
        "listing_title": extra.get("title"), "listing_leitsatz_excerpt": extra.get("listing_leitsatz_excerpt"),
        "listing_leitsatz_truncated": True, "detail_url": extra.get("detail_url"),
        "pdf": info | {"header_lines": header_lines[:6], "rubrum_chunks": len(rubrum)},
        "identity": {"az_in_pdf": True, "date_in_rubrum": date_in_text},
        "rn": "none published (PDF without Randnummern)",
        "leitsatz": "published only on the court's detail page (not part of the PDF capture)"}
    return ParsedDecision(
        source_id="hb", source_doc_id=item["source_doc_id"], source_url=url, court_name=court.name,
        decision_date=listed_date or date_in_text or "", case_numbers_raw=raw_az,
        case_numbers=split_case_numbers(raw_az), decision_type=extra.get("decision_type"), sections=sections,
        ecli=None, court_id=info_court.court_id, court_type=info_court.court_type,
        jurisdiction=info_court.jurisdiction, land="hb", chamber=None, title=extra.get("title"),
        metadata=metadata, procedural=procedural)


class BremenAdapter(Adapter):
    source_id = "hb"
    label = "Bremen: OVG, VG and OLG Bremen (court websites, PDF decisions)"
    base_url = "https://www.oberverwaltungsgericht.bremen.de"
    land = "hb"
    adapter_name = "hb-pdf"
    min_interval = 3.0  # small court site (robots.txt Crawl-delay, if any, still applies)

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        window = since or (None if limit else incremental_since(conn, self.source_id, INCREMENTAL_OVERLAP_DAYS))
        selected = []
        for court in COURTS:
            info = resolve_court(court.short, default_land="hb")
            if courts and not any(info.court_id.startswith(c.lower()) for c in courts):
                continue
            selected.append((priority(info.jurisdiction), court))
        selected.sort(key=lambda s: s[0])
        page_size = 20 if limit and limit <= 20 else 100
        items: list[dict] = []
        seen: set[str] = set()
        total = 0
        for _prio, court in selected:
            skip = 0
            while True:
                url = list_url(court, page_size, skip)
                response = http.get(url, timeout=90)
                # The sites declare UTF-8 and truncate excerpts mid-character; never fall back to cp1252.
                markup = response.body.decode("utf-8", errors="replace")
                count, rows = parse_list(markup, court, url=url)
                if skip == 0:
                    total += count
                fresh = [r for r in rows if (not window or r["listed_date"] >= window)
                         and r["source_doc_id"] not in seen]
                seen.update(r["source_doc_id"] for r in fresh)
                items.extend(fresh)
                on_page = markup.count('<tr class="search-result"')   # incl. rows without a PDF
                skip += on_page                  # advance by what the server returned, not by what we asked
                if (not on_page or skip >= count or any(window and r["listed_date"] < window for r in rows)
                        or (limit and len(items) >= limit)):
                    break
            if limit and len(items) >= limit:
                break
        full = not (window or limit or courts)
        return Listing(items=items, upstream_total=total if full else None, complete=full)

    def fetch(self, http: PoliteHttp, item) -> Capture:
        response = http.get(item["url"], timeout=PDF_TIMEOUT)
        if not response.body.startswith(b"%PDF"):
            raise UpstreamFailure("unexpected_representation",
                                  f"expected PDF, got {response.content_type or 'unknown'}", url=item["url"])
        return Capture(body=response.body, url=item["url"], media_type="application/pdf", extension="pdf",
                       headers=response.headers)

    def parse(self, capture: Capture, item) -> ParsedDecision:
        return parse_pdf_decision(capture.body, item, capture.url)


def adapters() -> dict[str, Adapter]:
    return {"hb": BremenAdapter()}
