"""Sachsen: Sächs. OVG Entscheidungssammlung and ESAMOSplus (OLG Dresden, LG, AG).

Both official databases live on www.justiz.sachsen.de, whose robots.txt reads
``User-agent: * / Crawl-delay: 3600``.  By operator decision (2026-09-28) the
Crawl-delay is not applied to this source (``robots_policy``); requests stay
paced at >= 3 s per host, 429/503/Retry-After are honoured and challenge,
CAPTCHA or login pages are never bypassed (typed failures).

Other Saxon courts: VG Dresden/Leipzig/Chemnitz, LAG and FG refer to juris
("Wesentliche Entscheidungen ... in der Datenbank der Juris GmbH"), the LSG
site links sozialgerichtsbarkeit.de; none publishes into an official free
Saxon database.  The OVG (priority 0) is listed first, ESAMOSplus (ordinary
courts, priority 1) second.

Sächs. OVG (``/ovgentschweb/``, PHP, frameset)
  Listing: ``POST searchlist.phtml`` with ``datum`` = ``YYYY`` or
  ``DD.MM.YYYY-DD.MM.YYYY`` (decision date); all hits on one page, newest
  first ("N Dokumente wurden gefunden"; none: "Leider entsprach kein
  Dokument ...").  Rows: ``popupDocument('<id>')`` + "Aktenzeichen: <Az>
  (<Gericht> / <Entscheidungsart> / DD.MM.YYYY)", truncated Leitsatz,
  Schlagworte, Vorschriften.
  Document: ``document.phtml?id=<id>`` (Gericht, Entscheidungsart, Az, Datum,
  full Leitsatz, Schlagwörter, Rechtsvorschriften, link ``documents/*.pdf``)
  plus the Volltext PDF.  The capture is a deterministic ZIP (stored, fixed
  timestamps) holding the exact bytes of both responses and a manifest.
  The PDF (``pdftotext -layout``) carries explicit Randnummern in the left
  margin (``^12   Text``), right-aligned (" 9" next to "10"; see
  ``align_margin_numbers``); an appended Streitwertbeschluss restarts at 1
  and is kept as its own section, as is the Rechtsmittelbelehrung.  The
  first page names the lower instance ("11 L 612/26 VG Dresden").  ECLIs are
  not published.  Full listing: one request per year (1991..today) plus one
  window for older decisions.

ESAMOSplus (``/esamosplus/pages/``, Scopeland ASP.NET WebForms, session + ViewState)
  Listing: ``GET suchen.aspx`` (form, and the ten newest decisions), ``POST
  suchen.aspx`` with "Entscheidung von/bis" -> ``treffer.aspx`` result table
  (6 rows per page, Aktenzeichen order); further pages by posting the pager
  button "Vorwärts".  Rows: date, Az, court, a "Dokument öffnen" button whose
  value is the document key + "..." (``4U374-26_02...``); the Leitsatz is the
  row's title attribute.
  Document: no permalink.  The PDF is the response to posting the row's
  button (``Content-Disposition: attachment; filename="<key>.pdf"``), so a
  fetch re-searches the listed decision date and opens the row with the listed
  key.  The PDF starts with "Leitsatz:", an editorial citation line ("OLG
  Dresden, 4. Zivilsenat, Beschluss vom 5. August 2026, Az.: 4 U 374/26"),
  "Aktenzeichen: ..." and the lower instance ("Landgericht Dresden, 8 O
  2006/22"); older PDFs (1990s) put the citation before the Leitsatz text and
  the lower instance on the Aktenzeichen line ("1 Ss 421/99 5 Ns 951 Js
  19175/98 LG Görlitz") and may lack a "beschlossen:" tenor marker (the text
  before "Gründe" is then kept as one section).  OLG decisions usually carry
  no Randnummern (numbering none; fewer than 3 margin numbers are rejected).
  Full listing: one search per year (2000..today, plus one window for older
  decisions) and one request per result page (about 3,100 decisions, 514
  pages on 2026-09-28).
"""
from __future__ import annotations

import codecs
import html as htmlmod
import io
import json
import re
import sqlite3
import zipfile
from datetime import date
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup, Tag

from ..casenumbers import case_key, split_case_numbers
from ..courtids import priority, resolve as resolve_court
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import Block, ParsedDecision, Section
from .base import (OPERATOR_DECISION_BASIS, OPERATOR_DECISION_DATE, Adapter, Capture, Listing, iso_date,
                   squash)
from .htmlblocks import (checked_ecli, clean_line, ensure_document, german_date, incremental_since,
                         is_outline_marker, item_extra)
from .pdftext import MARGIN_NUMBER, align_margin_numbers, join_lines, layout_blocks, pdf_text

BASE = "https://www.justiz.sachsen.de"
OVG_BASE = BASE + "/ovgentschweb/"
OVG_SEARCH = OVG_BASE + "searchlist.phtml"
OVG_DOC = OVG_BASE + "document.phtml?id={id}"
ES_BASE = BASE + "/esamosplus/pages/"
ES_SEARCH = ES_BASE + "suchen.aspx"
ES_RESULTS = ES_BASE + "treffer.aspx"
# ESAMOSplus has no document permalinks; this identifies the document on its entry page.
ES_DOC = ES_BASE + "index.aspx#dokument={key}"

OVG_FIRST_YEAR = 1991         # older decisions are covered by one final window
ES_FIRST_YEAR = 2000
INCREMENTAL_OVERLAP_DAYS = 365
MAX_RESULT_PAGES = 2000
OVG_COURT = "Sächsisches Oberverwaltungsgericht"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
HTML_MEMBER = "document.html"
MANIFEST = "manifest.json"


# --- decoding --------------------------------------------------------------------

def _cp1252_fallback(err: UnicodeError):
    if isinstance(err, UnicodeDecodeError):
        return err.object[err.start:err.end].decode("cp1252", errors="replace"), err.end
    raise err


codecs.register_error("sn-cp1252-fallback", _cp1252_fallback)


def decode(body: bytes) -> str:
    """UTF-8; the OVG templates contain stray Latin-1 bytes ("\xf6ffnen") next to UTF-8 data."""
    return body.decode("utf-8", errors="sn-cp1252-fallback")


def _text(fragment: str) -> str:
    return clean_line(htmlmod.unescape(re.sub(r"<[^>]+>", " ", fragment)))


def _br_lines(fragment: str) -> list[str]:
    return [_text(part) for part in re.split(r"<br\s*/?>", fragment, flags=re.I)]


# --- Sächs. OVG: listing -----------------------------------------------------------

OVG_HITS = re.compile(r"(\d+)\s+Dokumente?\s+wurden?\s+gefunden")
OVG_NONE = "kein Dokument Ihrer Suchanfrage"
OVG_ROW = re.compile(r"popupDocument\('(\d+)'\);\">\s*Aktenzeichen:\s*(.*?)\s*\(([^()]*?)\s*/\s*([^/()]*?)\s*/\s*"
                     r"(\d{1,2}\.\d{1,2}\.\d{4})\)\s*</A>", re.S | re.I)


def ovg_search_form(datum: str) -> dict:
    return {"aktenzeichen": "", "datum": datum, "stichwort": "", "rules": "", "ok": "Suche starten"}


def parse_ovg_list(markup: str, *, url: str = OVG_SEARCH) -> tuple[int, list[dict]]:
    """(hit count, items) of one OVG result page (all hits, newest first)."""
    hits_m = OVG_HITS.search(markup)
    if hits_m is None:
        if OVG_NONE in markup:
            return 0, []
        ensure_document(markup, False, url=url, what="Sächs. OVG result list")
    items = []
    chunks = re.split(r"<TABLE\b", markup, flags=re.I)
    for chunk in chunks:
        m = OVG_ROW.search(chunk)
        if not m:
            continue
        doc, az, court, dtype, raw_date = (squash(g) for g in m.groups())
        rest = chunk[m.end():]
        leitsatz_m = re.match(r"\s*<BR\s*/?>(.*?)(?:<BR\s*/?>\s*<B>|</TD>)", rest, re.S | re.I)
        excerpt = _text(leitsatz_m.group(1)) if leitsatz_m else ""
        excerpt = re.sub(r"^Leits(?:atz|ätze):?\s*", "", excerpt).strip()

        def labelled(label: str) -> list[str]:
            f = re.search(r"<B>" + label + r":</B>(.*?)(?:<BR\s*/?>\s*<B>|</SPAN>|</TD>)", rest, re.S | re.I)
            return [x.strip(" ,;") for x in re.split(r"[\r\n]+", htmlmod.unescape(re.sub(r"<[^>]+>", "", f.group(1))))
                    if x.strip(" ,;")] if f else []
        info = resolve_court(court, default_land="sn")
        items.append({
            "source_doc_id": f"ovg:{doc}", "url": OVG_DOC.format(id=doc), "listed_date": iso_date(raw_date),
            "listed_court": info.court_id, "listed_case_numbers": az, "listed_modified": None,
            "priority": priority(info.jurisdiction),
            "extra": {"db": "ovgentschweb", "court_name": court, "decision_type": dtype or None,
                      "leitsatz_excerpt": None if excerpt in ("", "...") else excerpt,
                      "schlagworte": labelled("Schlagworte"), "vorschriften": labelled("Vorschriften")}})
    hits = int(hits_m.group(1))
    if hits and not items:
        raise UpstreamFailure("parse_error", f"OVG result list reports {hits} hits but no rows parsed", url=url)
    return hits, items


# --- Sächs. OVG: document page + capture ------------------------------------------------

OVG_HEAD = re.compile(r"<div style=\"font-weight:bold\";?>(.*?)</div>\s*</TD>\s*<TD ALIGN=\"right\">(.*?)</TD>",
                      re.S | re.I)
OVG_FIELD = re.compile(r"<TD WIDTH=\"120\">\s*(.*?)\s*</TD>\s*<TD[^>]*>(.*?)</TD>", re.S | re.I)


def parse_ovg_page(markup: str, url: str) -> dict:
    """Metadata of ``document.phtml``; typed failure for an empty/unknown record."""
    head = OVG_HEAD.search(markup)
    if head is None:
        ensure_document(markup, False, url=url, what="Sächs. OVG document page")
    parts = [p for p in _br_lines(head.group(1)) if p]
    raw_date = _text(head.group(2))
    if len(parts) < 2 or not raw_date:
        raise UpstreamFailure("empty_document", "OVG document page without court/Aktenzeichen/date "
                              "(unknown or withdrawn id)", url=url)
    court, dtype, az = (parts[0], parts[1], " ".join(parts[2:])) if len(parts) >= 3 else (parts[0], None, parts[1])
    after = markup[head.end():]
    box = re.search(r"<TABLE>\s*<TD>(.*?)</TD>\s*</TABLE>", after, re.S | re.I)
    headnote_label, headnote = None, []
    if box:
        raw = box.group(1).strip()
        m = re.match(r"(Leits(?:atz|ätze)|Orientierungss(?:atz|ätze))\s*:?\s*(?:<br\s*/?>\s*)*", raw, re.I)
        if m:
            headnote_label, raw = m.group(1), raw[m.end():]
        headnote = [t for t in (_text(p) for p in re.split(r"(?:<br\s*/?>\s*){2,}", raw, flags=re.I)) if t]
    fields: dict[str, str] = {}
    for label, value in OVG_FIELD.findall(after):
        fields[_text(label).rstrip(":").strip()] = value
    links = [(href, _text(text)) for href, text in re.findall(r"<A HREF=\"([^\"]+)\"[^>]*>(.*?)</A>",
                                                               fields.get("Verweise / Links", ""), re.S | re.I)]
    pdfs = [(h, t) for h, t in links if h.lower().endswith(".pdf")]
    fulltext = next((h for h, t in pdfs if "volltext" in t.lower()), pdfs[0][0] if pdfs else None)
    return {"court": court, "decision_type": dtype, "az": az, "date": iso_date(raw_date),
            "headnote_label": headnote_label, "headnote": headnote,
            "schlagwoerter": [x.strip(" ,;") for x in _br_lines(fields.get("Schlagwörter", "")) if x.strip(" ,;")],
            "norms": [x.strip(" ,;") for x in _br_lines(fields.get("Rechtsvorschriften", "")) if x.strip(" ,;")],
            "links": links, "fulltext": fulltext}


def pack_capture(members: list[tuple[str, bytes]], manifest: dict) -> bytes:
    """Deterministic ZIP (stored, fixed timestamps) of exact upstream response bodies."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in [(MANIFEST, json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=1).encode())
                           ] + members:
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buf.getvalue()


def unpack_capture(blob: bytes, url: str) -> tuple[dict, dict[str, bytes]]:
    if not blob.startswith(b"PK"):
        raise UpstreamFailure("unexpected_representation", "OVG capture is not a ZIP container", url=url)
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            members = {i.filename: archive.read(i) for i in archive.infolist() if not i.is_dir()}
    except zipfile.BadZipFile as exc:
        raise UpstreamFailure("unexpected_representation", f"bad OVG capture: {exc}", url=url) from exc
    manifest = json.loads(members.pop(MANIFEST, b"{}") or b"{}")
    return manifest, members


def ovg_capture(page_body: bytes, page_url: str, pdf_body: bytes, pdf_url: str, pdf_member: str) -> bytes:
    manifest = {"source": "Sächs. OVG Entscheidungssammlung", "members": [
        {"name": HTML_MEMBER, "url": page_url, "role": "document page (metadata, Leitsatz)"},
        {"name": pdf_member, "url": pdf_url, "role": "Volltext PDF"}]}
    return pack_capture([(HTML_MEMBER, page_body), (pdf_member, pdf_body)], manifest)


# --- PDF text: parts, sections ------------------------------------------------------

PDF_HEADINGS = {"gründe": "gruende", "tatbestand": "tatbestand", "entscheidungsgründe": "entscheidungsgruende",
                "tenor": "tenor", "beschlossen": "tenor", "für recht erkannt": "tenor",
                "abweichende meinung": "abweichende_meinung", "sondervotum": "abweichende_meinung"}
REASON_LINE = re.compile(r"^\s*(?:G\s*r\s*ü\s*n\s*d\s*e|T\s*a\s*t\s*b\s*e\s*s\s*t\s*a\s*n\s*d|"
                         r"Entscheidungsgründe|E n t s c h e i d u n g s g r ü n d e)\s*:?\s*$")
RMB_LINE = re.compile(r"^\s*(?:Rechtsmittelbelehrung(?:en)?|Belehrung über (?:die )?Rechtsmittel)\s*:?\s*$", re.I)
APPENDIX_LINE = re.compile(r"^\s*(?:Beschluss|B\s*e\s*s\s*c\s*h\s*l\s*u\s*s\s*s)\s*:?\s*$")
VOM_LINE = re.compile(r"^\s*vom\s+\d{1,2}\.\s*(?:\d{1,2}\.\s*\d{4}|[A-Za-zäÄ]+\s+\d{4})\s*$")
RUBRUM_END = re.compile(r"(?:beschlossen|erkannt|entschieden|verfügt|beschließt|erkennt|angeordnet)\s*:$", re.I)
DATE_AM = re.compile(r"\bam\s+(\d{1,2}\.\s*(?:\d{1,2}\.\s*\d{4}|[A-Za-zäÄ]+\s+\d{4}))")
ANY_DATE = re.compile(r"\b(\d{1,2}\.\s*(?:\d{1,2}\.\s*\d{4}|[A-Za-zäÄ]+\s+\d{4}))")
CHAMBER_RE = re.compile(r"\b(?:hat|haben)\s+(?:der|die|das)\s+(\d+\.\s*(?:Senat|Zivilsenat|Strafsenat|Familiensenat|"
                        r"Kartellsenat|Vergabesenat|Disziplinarsenat|Kammer|Zivilkammer|Strafkammer|"
                        r"Beschwerdekammer|Fachsenat[\wäöü ]*?))\b")
DOCTYPES = ("urteil", "beschluss", "teilurteil", "endurteil", "schlussurteil", "zwischenurteil",
            "versäumnisurteil", "anerkenntnisurteil", "gerichtsbescheid", "verfügung", "hinweisbeschluss")
ECLI_IN_TEXT = re.compile(r"ECLI:DE:[A-Z0-9]{2,}:\d{4}:[0-9A-Z.]+")




def pdf_pages(text: str) -> list[list[str]]:
    pages = [page.split("\n") for page in text.split("\f")]
    while pages and not any(line.strip() for line in pages[-1]):
        pages.pop()
    return pages


def _flat(pages: list[list[str]]) -> list[tuple[int, int, str]]:
    return [(p, i, line) for p, lines in enumerate(pages) for i, line in enumerate(lines)]


def _slice(pages: list[list[str]], flat: list[tuple[int, int, str]], start: int, end: int) -> str:
    """Text of flat lines [start, end) with every other line blanked, so that layout_blocks
    reports the original page and line numbers in its locators."""
    if end <= start:
        return ""
    keep = {(p, i) for p, i, _ in flat[start:end]}
    last_page = flat[end - 1][0]
    return "\f".join("\n".join(line if (p, i) in keep else "" for i, line in enumerate(lines))
                     for p, lines in enumerate(pages[:last_page + 1]))


def _blocks(pages, flat, start: int, end: int, *, detect_rn: bool) -> list[Block]:
    text = _slice(pages, flat, start, end)
    blocks = layout_blocks(text, headings=PDF_HEADINGS, detect_rn=detect_rn) if text.strip() else []
    while blocks and blocks[0].kind == "pagebreak":
        blocks.pop(0)
    out = []
    for block in blocks:
        if block.kind == "text" and block.label is None and is_outline_marker(block.text):
            block = Block(text=block.text, kind="heading", locator=block.locator)
        out.append(block)
    return out


def _locator(page: int, line: int) -> str:
    """Same format as pdftext.layout_blocks (1-based page and line of the block start)."""
    return f"page[{page + 1}]/line[{line + 1}]"


def pdf_parts(flat: list[tuple[int, int, str]], start: int) -> list[tuple[str, int, int]]:
    """[(kind, start, end)]: main decision, Rechtsmittelbelehrung, appended decision (e.g. Streitwert)."""
    n = len(flat)
    anchor = next((i for i in range(start, n) if REASON_LINE.match(flat[i][2])), None)
    if anchor is None:
        return [("main", start, n)]
    rmb = next((i for i in range(anchor + 1, n) if RMB_LINE.match(flat[i][2])), None)
    appendix = None
    for i in range(rmb if rmb is not None else anchor + 1, n):
        if APPENDIX_LINE.match(flat[i][2]):
            following = [line for _, _, line in flat[i + 1:i + 8] if line.strip()][:3]
            if any(VOM_LINE.match(line) for line in following):
                appendix = i
                break
    main_end = min(x for x in (rmb, appendix, n) if x is not None)
    parts = [("main", start, main_end)]
    if rmb is not None:
        parts.append(("rmb", rmb + 1, appendix if appendix is not None else n))
    if appendix is not None:
        parts.append(("appendix", appendix, n))
    return parts


def body_sections(pages: list[list[str]], start: int, *, min_rn: int, db: str) -> tuple[list[Section], dict]:
    """Sections of the decision text from flat line ``start`` (rubrum first)."""
    flat = _flat(pages)
    info: dict = {"tenor_found": False, "rubrum": ""}
    sections: list[Section] = []
    rubrum: list[str] = []
    rn_rejected: list[str] = []
    for kind, a, b in pdf_parts(flat, start):
        blocks = _blocks(pages, flat, a, b, detect_rn=True)
        labels = [x.label for x in blocks if x.label]
        if labels and len(labels) < min_rn:
            # Too few margin numbers to be a Randnummern sequence (justified text can look alike).
            rn_rejected += labels
            blocks = _blocks(pages, flat, a, b, detect_rn=False)
        if kind == "rmb":
            sections.append(Section("sonstiger_langtext", "court",
                                    f"{db} PDF: 'Rechtsmittelbelehrung' after the reasons", blocks=blocks,
                                    heading="Rechtsmittelbelehrung"))
            continue
        if kind == "appendix":
            sections.append(Section("sonstiger_langtext", "court",
                                    f"{db} PDF: decision appended after the main decision "
                                    f"('{blocks[0].text if blocks else 'Beschluss'}', e.g. Streitwert)",
                                    blocks=blocks, heading=blocks[0].text if blocks else None))
            continue
        state = "rubrum"
        current: Section | None = None
        pending: list[Block] = []          # rubrum blocks (dropped once a tenor marker is found)
        for block in blocks:
            key = block.locator.rsplit("#", 1)[1] if block.kind == "heading" and "#" in block.locator else None
            if block.kind == "pagebreak":
                if current is not None:
                    current.blocks.append(block)
                continue
            if key == "tenor":
                rubrum.append(block.text)
                current = Section("tenor", "court", f"{db} PDF: text after '{block.text}' (Tenor)")
                sections.append(current)
                state, info["tenor_found"], pending = "body", True, []
                continue
            if key in ("gruende", "tatbestand", "entscheidungsgruende", "abweichende_meinung"):
                if state == "rubrum" and pending:
                    # No tenor marker ('beschlossen:'): keep rubrum + tenor rather than dropping the tenor.
                    sections.append(Section("sonstiger_langtext", "court",
                                            f"{db} PDF text before '{block.text}' without a tenor marker "
                                            "(rubrum and tenor)", blocks=pending))
                    pending = []
                current = Section(key, "court", f"{db} PDF heading '{block.text}'", heading=block.text)
                sections.append(current)
                state = "body"
                continue
            if state == "rubrum":
                rubrum.append(block.text)
                pending.append(block)
                if RUBRUM_END.search(block.text):
                    current = Section("tenor", "court", f"{db} PDF: text after '… {block.text.split(' ')[-1]}' "
                                      "(Tenor)")
                    sections.append(current)
                    state, info["tenor_found"], pending = "body", True, []
                continue
            if current is None:
                current = Section("sonstiger_langtext", "court", f"{db} PDF text without recognised heading")
                sections.append(current)
            current.blocks.append(block)
        if state == "rubrum":
            # No recognisable Tenor/Gründe: keep the whole text rather than guessing sections.
            sections.append(Section("sonstiger_langtext", "court",
                                    f"{db} PDF text without recognisable Tenor/Gründe structure (incl. rubrum)",
                                    blocks=blocks))
            rubrum = []
    info["rubrum"] = " ".join(rubrum)
    info["rn_rejected"] = rn_rejected
    return [s for s in sections if any(x.kind != "pagebreak" and x.text.strip() for x in s.blocks)], info


def rubrum_facts(rubrum: str) -> dict:
    facts: dict = {"date_am": None, "dates": [], "chamber": None, "doctype": None}
    for raw in DATE_AM.findall(rubrum):
        try:
            facts["date_am"] = iso_date(raw)
        except UpstreamFailure:
            pass
    for raw in ANY_DATE.findall(rubrum):
        try:
            facts["dates"].append(iso_date(raw))
        except UpstreamFailure:
            pass
    m = CHAMBER_RE.search(rubrum)
    if m:
        facts["chamber"] = clean_line(m.group(1))
    return facts


def check_rubrum_date(facts: dict, decision_date: str, url: str) -> None:
    """The decision date must be the 'am <date>' of the rubrum unless it is named there anyway."""
    if facts["date_am"] and facts["date_am"] != decision_date and decision_date not in facts["dates"]:
        raise UpstreamFailure("identity_mismatch", f"listed date {decision_date} != PDF rubrum 'am "
                              f"{facts['date_am']}'", url=url)


def _doctype(lines: list[str]) -> str | None:
    for line in lines:
        word = re.sub(r"\s+", "", line) if re.fullmatch(r"(?:\S\s)+\S", line.strip()) else clean_line(line)
        if word.lower().rstrip(":") in DOCTYPES:
            return word.rstrip(":").capitalize()
    return None


def _same_case(a: str, b: str) -> bool:
    keys_a = {case_key(x) for x in split_case_numbers(a)} or {case_key(a)}
    keys_b = {case_key(x) for x in split_case_numbers(b)} or {case_key(b)}
    return bool(keys_a & keys_b)


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value).upper()


# --- Sächs. OVG: decision ------------------------------------------------------------------

OVG_AZ_LINE = re.compile(r"^\s*Az\.?\s*:?\s*(\S.*?)\s*$")
LOWER_COURT = re.compile(r"(?P<court>(?:Verwaltungsgerichts?|Sozialgerichts?|VG|SG)\s+[A-ZÄÖÜ][\wäöüß-]+)")


def _ovg_header(pages: list[list[str]], text: str) -> tuple[str | None, list[dict], list[str]]:
    """'Az.: 2 B 264/26' and the lower instance line below it ('11 L 612/26 VG Dresden')."""
    lines = [line for line in pages[0] if line.strip()] if pages else []
    pdf_az, procedural, header = None, [], []
    for index, line in enumerate(lines[:4]):
        m = OVG_AZ_LINE.match(line)
        if not m:
            continue
        pdf_az = clean_line(m.group(1))
        header.append(clean_line(line))
        below = lines[index + 1] if index + 1 < len(lines) else ""
        low = re.match(r"^\s*(?P<az>[^/]*?\d+\s*/\s*\d{2,4}(?:\.[A-Za-z]+)?)\s*(?P<court>\S.*?)?\s*$", below)
        if low and not re.search(r"GERICHT", below):
            header.append(clean_line(below))
            az = clean_line(low.group("az"))
            court = clean_line(low.group("court") or "") or None
            when = None
            if court is None:
                near = re.search(LOWER_COURT.pattern + r"\s+vom\s+(?P<date>\d{1,2}\.\s*(?:\d{1,2}\.\s*\d{4}|"
                                 r"[A-Za-zäÄ]+\s+\d{4}))\s*[-–(]\s*" + re.escape(az), " ".join(text.split()))
                if near:
                    court = re.sub(r"gerichts\b", "gericht", near.group("court"))
                    try:
                        when = iso_date(near.group("date"))
                    except UpstreamFailure:
                        when = None
            procedural.append({"direction": "previous", "court": court, "date": when, "case_number": az,
                               "decision_type": None, "raw": clean_line(below), "field": "pdf:header",
                               "origin": "decision_text"})
        break
    return pdf_az, procedural, header


def parse_ovg_decision(capture_body: bytes, item, url: str) -> ParsedDecision:
    manifest, members = unpack_capture(capture_body, url)
    if HTML_MEMBER not in members:
        raise UpstreamFailure("parse_error", "OVG capture without document page", url=url)
    page = parse_ovg_page(decode(members[HTML_MEMBER]), url)
    pdf_names = [n for n in members if n.lower().endswith(".pdf")]
    if len(pdf_names) != 1:
        raise UpstreamFailure("parse_error", f"OVG capture holds {len(pdf_names)} PDFs", url=url)
    pdf_url = next((m.get("url") for m in manifest.get("members", []) if m.get("name") == pdf_names[0]), None)
    text, realigned = align_margin_numbers(pdf_text(members[pdf_names[0]], url=pdf_url or url))
    pages = pdf_pages(text)
    decision_date = page["date"]
    raw_az = page["az"]
    listed_az, listed_date = item["listed_case_numbers"], item["listed_date"]
    if listed_az and squash(listed_az) != squash(raw_az):
        raise UpstreamFailure("identity_mismatch", f"listed Az {listed_az!r} != document page {raw_az!r}", url=url)
    if listed_date and listed_date != decision_date:
        raise UpstreamFailure("identity_mismatch", f"listed date {listed_date} != document page {decision_date}",
                              url=url)
    pdf_az, procedural, header = _ovg_header(pages, text)
    if pdf_az is not None and not _same_case(pdf_az, raw_az):
        raise UpstreamFailure("identity_mismatch", f"PDF 'Az.: {pdf_az}' != document page {raw_az!r}", url=url)
    if pdf_az is None and _compact(raw_az) not in _compact(" ".join(pages[0] if pages else [])):
        raise UpstreamFailure("identity_mismatch", f"Az {raw_az!r} not found on the first PDF page", url=url)
    info = resolve_court(page["court"], default_land="sn")
    listed_court = item["listed_court"]
    if listed_court and listed_court != info.court_id:
        raise UpstreamFailure("identity_mismatch", f"listed court {listed_court} != {info.court_id}", url=url)
    body, body_info = body_sections(pages, 0, min_rn=1, db="OVG")
    facts = rubrum_facts(body_info["rubrum"])
    check_rubrum_date(facts, decision_date, url)
    sections: list[Section] = []
    if page["headnote"]:
        label = (page["headnote_label"] or "").lower()
        if label.startswith("orientierung"):
            name, authorship, basis = "orientierungssatz", "editorial", "OVG document page 'Orientierungssatz'"
        elif label:
            name, authorship, basis = ("leitsatz", "court", f"OVG document page '{page['headnote_label']}' "
                                       "(headnote in the court's own Entscheidungssammlung)")
        else:
            name, authorship, basis = "leitsatz", "unknown", "OVG document page headnote box without label"
        sections.append(Section(name, authorship, basis, blocks=[
            Block(text=t, locator=f"{HTML_MEMBER}#leitsatz[{i}]") for i, t in enumerate(page["headnote"], 1)]))
    sections += body
    metadata: dict = {}
    ecli_m = ECLI_IN_TEXT.search(text)
    ecli = checked_ecli(ecli_m.group(0) if ecli_m else None, decision_date, metadata)
    labels = [b.label for s in body for b in s.blocks if b.label]
    extra = item_extra(item)
    metadata.update({
        "database": "Sächs. OVG Entscheidungssammlung (ovgentschweb)", "court_raw": page["court"],
        "norms": page["norms"], "schlagwoerter": page["schlagwoerter"], "pdf_url": pdf_url,
        "links": [{"href": h, "text": t} for h, t in page["links"]],
        "pdf": {"pages": len(pages), "header_lines": header, "margin_numbers_realigned": realigned},
        "identity": {"pdf_az": pdf_az, "rubrum_date_am": facts["date_am"]},
        "rn": f"explicit margin Randnummern in the PDF ({len(labels)})" if labels else "none in the PDF",
        "rn_rejected": body_info["rn_rejected"] or None, "tenor_found": body_info["tenor_found"],
        "listed_decision_type": extra.get("decision_type")})
    return ParsedDecision(
        source_id="sn", source_doc_id=item["source_doc_id"], source_url=url, court_name=OVG_COURT
        if info.court_id == "ovg-sn" else page["court"], decision_date=decision_date, case_numbers_raw=raw_az,
        case_numbers=split_case_numbers(raw_az), decision_type=page["decision_type"] or extra.get("decision_type"),
        sections=sections, ecli=ecli, court_id=info.court_id, court_type=info.court_type,
        jurisdiction=info.jurisdiction, land=info.land or "sn", chamber=facts["chamber"], title=None,
        metadata=metadata, procedural=procedural)


# --- ESAMOSplus: forms, result tables ---------------------------------------------------

ES_LABELS = {"court_text": "Gericht (Freitext)", "date_from": "Entscheidung von", "date_to": "bis",
             "az": "Aktenzeichen", "term": "Suchbegriff (Leitsatz)"}
ES_NO_HITS = re.compile(r"kein\w*\s+(?:Treffer|Entscheidung\w*|Dokument\w*|Ergebnis\w*|Datens\w*)", re.I)


def form_fields(soup: BeautifulSoup) -> dict[str, str]:
    """Successful controls of the page form except buttons (ASP.NET ViewState included)."""
    form = soup.find("form") or soup
    out: dict[str, str] = {}
    for el in form.find_all("input"):
        name, kind = el.get("name"), (el.get("type") or "text").lower()
        if not name or el.has_attr("disabled"):
            continue
        if kind in ("hidden", "text"):
            out[name] = el.get("value", "")
        elif kind == "checkbox" and el.has_attr("checked"):
            out[name] = el.get("value", "on")
    for sel in form.find_all("select"):
        if sel.get("name"):
            opt = sel.find("option", selected=True) or sel.find("option")
            out[sel["name"]] = opt.get("value", "") if opt else ""
    for area in form.find_all("textarea"):
        if area.get("name"):
            out[area["name"]] = area.get_text()
    return out


def search_controls(soup: BeautifulSoup, *, url: str = ES_SEARCH) -> tuple[dict[str, str], str, str]:
    """Names of the search inputs (found by their labels) and of the 'Suchen' button."""
    labels = {squash(label.get_text(" ")): label.get("for") for label in soup.find_all("label") if label.get("for")}
    names = {key: labels.get(text) for key, text in ES_LABELS.items()}
    button = soup.find("input", attrs={"type": "submit", "value": "Suchen"})
    if not all(names.values()) or button is None or not button.get("name"):
        raise UpstreamFailure("unexpected_representation", "ESAMOSplus search form without the expected controls",
                              url=url)
    return names, button["name"], button.get("value", "Suchen")


def parse_rows(soup: BeautifulSoup, table_id: str) -> list[dict] | None:
    """Rows of a Scopeland result table (date, Az, court, document button); None without table."""
    table = soup.find("table", id=table_id)
    if table is None:
        return None
    rows = []
    for tr in (table.find("tbody") or table).find_all("tr", recursive=False):
        if any("pager" in c for c in tr.get("class") or []):
            continue
        cells: list[tuple[Tag, str]] = []
        for i in range(4):
            div = tr.find("div", id=re.compile(rf"_Col{i}$"))
            element = div.find(["input", "span"]) if div is not None else None
            if element is None:
                break
            value = element.get("value", "") if element.name == "input" else element.get_text(" ")
            cells.append((element, clean_line(value)))
        if len(cells) < 4:
            continue
        (_, raw_date), (az_el, az), (_, court), (button, value) = cells
        if button.name != "input" or not value or not button.get("name"):
            continue
        key = value[:-3] if value.endswith("...") else value
        title = clean_line(az_el.get("title") or "")
        rows.append({"date": raw_date, "az": az, "court": court, "doc_key": key.strip(), "button": button["name"],
                     "button_value": button.get("value", ""),
                     "leitsatz": re.sub(r"^Leits(?:atz|ätze)\s*:\s*", "", title) or None})
    return rows


def pager(soup: BeautifulSoup) -> tuple[int, int, str | None]:
    """(current page, last page, name of the enabled 'Vorwärts' button)."""
    buttons = [b for b in soup.find_all("input", attrs={"type": "submit"})
               if any(c.startswith("pagerDivCtls") for c in b.get("class") or [])]
    numbered = [(int(b["value"]), b) for b in buttons if b.get("value", "").isdigit()]
    last = max((n for n, _ in numbered), default=1)
    current = next((n for n, b in numbered if "Pager_Selection" in (b.get("class") or [])), 1)
    forward = next((b.get("name") for b in buttons if b.get("value") == "Vorwärts" and not b.has_attr("disabled")),
                   None)
    return current, last, forward


def esamos_item(row: dict) -> dict:
    info = resolve_court(row["court"], default_land="sn")
    try:
        listed_date = iso_date(row["date"])
    except UpstreamFailure:
        listed_date = None
    return {"source_doc_id": f"esamos:{row['doc_key']}", "url": ES_DOC.format(key=quote(row["doc_key"])),
            "listed_date": listed_date, "listed_court": info.court_id, "listed_case_numbers": row["az"],
            "listed_modified": None, "priority": priority(info.jurisdiction),
            "extra": {"db": "esamosplus", "doc_key": row["doc_key"], "court_name": row["court"],
                      "listing_leitsatz": row["leitsatz"]}}


def is_no_hits_page(soup: BeautifulSoup) -> bool:
    """The search form came back with a 'no hits' message (visible text only: the Leitsätze
    live in title attributes and may contain the same words)."""
    return soup.find("input", attrs={"type": "submit", "value": "Suchen"}) is not None and \
        bool(ES_NO_HITS.search(soup.get_text(" ")))


def parse_esamos_results(markup: str, *, url: str = ES_RESULTS) -> tuple[list[dict], int, int, str | None]:
    soup = BeautifulSoup(markup, "html.parser")
    rows = parse_rows(soup, "DV13_Table")
    if rows is None:
        if is_no_hits_page(soup):
            return [], 1, 1, None
        ensure_document(markup, False, url=url, what="ESAMOSplus result list")
    current, last, forward = pager(soup)
    return [esamos_item(r) | {"_row": r} for r in rows], current, last, forward


# --- ESAMOSplus: decision -------------------------------------------------------------------

LEITSATZ_LINE = re.compile(r"^\s*Leits(?:atz|ätze)\s*:?\s*$")
# "OLG Dresden, 4. Zivilsenat, Beschluss vom 5. August 2026, Az.: 4 U 374/26";
# older documents: "Beschl. vom 28.10.1999, Az. 1 Ss 421/99".
CITATION = re.compile(r"^\s*(?:(?P<court>(?:OLG|LG|AG|KG|Oberlandesgericht|Landgericht|Amtsgericht)\b[^,]*),\s*)?"
                      r"(?:(?P<chamber>[^,]+?),\s*)?(?P<type>[A-ZÄÖÜ][\wäöüß-]*\.?(?:\s+[\wäöüß-]+)?)\s+vom\s+"
                      r"(?P<date>\d{1,2}\.\s*(?:\d{1,2}\.\s*\d{2,4}|[A-Za-zäÄ]+\s+\d{4})),?\s*"
                      r"Az\.?\s*:?\s*(?P<az>\S.*?)\s*$")
TYPE_ABBREVIATIONS = {"beschl.": "Beschluss", "urt.": "Urteil", "teilurt.": "Teilurteil"}
ES_AZ_LINE = re.compile(r"^\s*Aktenzeichen\s*:\s*(\S.*?)\s*$")
ES_LOWER = re.compile(r"^\s*(?P<court>(?:Amtsgericht|Landgericht|Oberlandesgericht|Arbeitsgericht|Sozialgericht|"
                      r"Verwaltungsgericht|Staatsanwaltschaft|Generalstaatsanwaltschaft|AG|LG|OLG)\b[^,]*),\s*"
                      r"(?:Az\.?\s*:?\s*)?(?P<az>[^,]*\d[^,]*?)\s*$")
TRAILING_COURT = re.compile(r"^(?P<az>.*?\d.*?)\s+(?P<court>(?:LG|AG|OLG|Landgericht|Amtsgericht)\s+\S.*)$")
LETTERHEAD_INDENT = 30
LETTERHEAD = re.compile(r"^\s*(?:(?:Oberlandesgericht|Landgericht|Amtsgericht)(?:\s+\S+){0,2}|"
                        r"\d+\.\s*\S*(?:senat|kammer)|\S*(?:senat|kammer))\s*$", re.I)
CHAMBER_LINE = re.compile(r"^\s*(\d+\.\s*\S*(?:senat|kammer))\s*$", re.I)


def _letterhead(line: str) -> bool:
    return line.startswith(" " * LETTERHEAD_INDENT) or bool(LETTERHEAD.match(line))


def _paragraphs(lines: list[tuple[int, int, str]]) -> list[list[tuple[int, int, str]]]:
    out: list[list[tuple[int, int, str]]] = [[]]
    for entry in lines:
        if entry[2].strip():
            out[-1].append(entry)
        elif out[-1]:
            out.append([])
    return [p for p in out if p]


def esamos_head(pages: list[list[str]]) -> dict:
    """Leitsatz, citation line, editorial notes, 'Aktenzeichen:' and lower-instance lines (pages 1-2).

    Two layouts: (new) "Leitsatz:" + text, citation line, notes, right-aligned
    letterhead, "Aktenzeichen: ..." and the lower instance; (older) "Leitsatz",
    citation line, Leitsatz text, letterhead "Oberlandesgericht Dresden" /
    "1. Strafsenat", "Aktenzeichen: <Az> <lower Az> <lower court>".
    """
    flat = _flat(pages)
    limit = next((i for i, (p, _, _) in enumerate(flat) if p >= 2), len(flat))
    head: dict = {"leitsatz": [], "citation": None, "citation_line": None, "citation_block": None, "notes": [],
                  "pdf_az": None, "lower": [], "body_start": 0, "chamber": None}
    az_index = next((i for i in range(limit) if ES_AZ_LINE.match(flat[i][2])), None)
    zone_end = az_index if az_index is not None else next(
        (i for i in range(limit) if _doctype([flat[i][2]]) or re.match(r"^\s*IM NAMEN DES VOLKES", flat[i][2], re.I)),
        0)
    paragraphs = _paragraphs(flat[:zone_end])
    heading = bool(paragraphs) and len(paragraphs[0]) == 1 and bool(LEITSATZ_LINE.match(paragraphs[0][0][2]))
    before: list[Block] = []
    after: list[Block] = []
    for para in paragraphs[1 if heading else 0:]:
        kept = [x for x in para if not _letterhead(x[2])]
        if kept:
            text = join_lines([x[2] for x in kept])
            m = CITATION.match(text)
            if m and head["citation"] is None:
                head["citation"] = {k: clean_line(v) if v else None for k, v in m.groupdict().items()}
                head["citation_line"] = clean_line(m.group(0))
                head["citation_block"] = Block(text=head["citation_line"], locator=_locator(kept[0][0], kept[0][1]))
            else:
                block = Block(text=text, locator=_locator(kept[0][0], kept[0][1]))
                (after if head["citation"] is not None else before).append(block)
        if len(kept) < len(para):
            break                 # the letterhead starts: the rest of the zone is rubrum
    head["chamber"] = next((clean_line(m.group(1)) for m in (CHAMBER_LINE.match(x[2]) for x in flat[:zone_end])
                            if m), None)
    if heading:
        # new layout: Leitsatz before the citation, notes after it; older layout: Leitsatz after it
        head["leitsatz"], head["notes"] = (before, after) if before else (after, [])
    elif head["citation"] is not None:
        head["notes"] = after
    if az_index is not None:
        head["pdf_az"] = clean_line(ES_AZ_LINE.match(flat[az_index][2]).group(1))
    if not heading and head["citation"] is None:
        return head | {"body_start": 0}
    start = az_index + 1 if az_index is not None else zone_end
    for i in range(start, min(limit, start + 4)):
        line = flat[i][2]
        m = ES_LOWER.match(line)
        if not line.strip() or not m or line.startswith(" " * LETTERHEAD_INDENT):
            break
        head["lower"].append({"direction": "previous", "court": clean_line(m.group("court")), "date": None,
                              "case_number": clean_line(m.group("az")), "decision_type": None,
                              "raw": clean_line(line), "field": "pdf:rubrum", "origin": "decision_text"})
        start = i + 1
    head["body_start"] = start
    return head


def names_case(container: str, az: str) -> tuple[bool, str]:
    """Does ``container`` name the docket ``az`` (token sequence or matching key)?  Also returns
    the text after it ("1 Ss 421/99 5 Ns 951 Js 19175/98 LG Görlitz" -> "5 Ns ... LG Görlitz")."""
    tokens, wanted = clean_line(container).split(), clean_line(az).split()
    upper = [t.upper() for t in tokens]
    for i in range(len(tokens) - len(wanted) + 1):
        if upper[i:i + len(wanted)] == [t.upper() for t in wanted]:
            return True, " ".join(tokens[i + len(wanted):])
    return _same_case(container, az), ""


def parse_esamos_decision(pdf: bytes, item, url: str) -> ParsedDecision:
    extra = item_extra(item)
    text, realigned = align_margin_numbers(pdf_text(pdf, url=url))
    pages = pdf_pages(text)
    head = esamos_head(pages)
    raw_az = clean_line(item["listed_case_numbers"] or "")
    listed_date = item["listed_date"]
    if not raw_az or not listed_date:
        raise UpstreamFailure("parse_error", "ESAMOSplus item without listed Az/date", url=url)
    cited = head["citation"] or {}
    procedural = list(head["lower"])
    for label, value in (("PDF 'Aktenzeichen:'", head["pdf_az"]), ("PDF citation", cited.get("az"))):
        if not value:
            continue
        found, rest = names_case(value, raw_az)
        if not found:
            raise UpstreamFailure("identity_mismatch", f"listed Az {raw_az!r} != {label} {value!r}", url=url)
        lower = TRAILING_COURT.match(rest) if label.endswith("'Aktenzeichen:'") else None
        if lower and not procedural:
            procedural.append({"direction": "previous", "court": clean_line(lower.group("court")), "date": None,
                               "case_number": clean_line(lower.group("az")), "decision_type": None,
                               "raw": value, "field": "pdf:Aktenzeichen", "origin": "decision_text"})
    first_pages = " ".join(" ".join(p) for p in pages[:2])
    if not head["pdf_az"] and not cited.get("az") and _compact(raw_az) not in _compact(first_pages):
        raise UpstreamFailure("identity_mismatch", f"listed Az {raw_az!r} not found in the PDF", url=url)
    cited_date = None
    if cited.get("date"):
        try:
            cited_date = iso_date(cited["date"])
        except UpstreamFailure:
            cited_date = None
        if cited_date and cited_date != listed_date:
            raise UpstreamFailure("identity_mismatch", f"listed date {listed_date} != PDF citation {cited_date}",
                                  url=url)
    body, body_info = body_sections(pages, head["body_start"], min_rn=3, db="ESAMOSplus")
    facts = rubrum_facts(body_info["rubrum"])
    check_rubrum_date(facts, listed_date, url)
    sections: list[Section] = []
    titel = ([head["citation_block"]] if head["citation_block"] else []) + head["notes"]
    if titel:
        sections.append(Section("titel", "editorial", "ESAMOSplus PDF head: documentation citation line "
                                "('<court>, <chamber>, <type> vom <date>, Az.: ...') and editorial notes",
                                blocks=titel))
    if head["leitsatz"]:
        sections.append(Section("leitsatz", "court", "ESAMOSplus PDF head 'Leitsatz' (headnote published with "
                                "the decision in the courts' official database)", blocks=head["leitsatz"]))
    elif extra.get("listing_leitsatz"):
        sections.append(Section("leitsatz", "court", "ESAMOSplus result list: Leitsatz (row title attribute)",
                                blocks=[Block(text=extra["listing_leitsatz"], locator="listing:title")]))
    sections += body
    info = resolve_court(extra.get("court_name") or cited.get("court") or "", default_land="sn")
    listed_court = item["listed_court"]
    if listed_court and listed_court != info.court_id:
        raise UpstreamFailure("identity_mismatch", f"listed court {listed_court} != {info.court_id}", url=url)
    metadata: dict = {}
    ecli_m = ECLI_IN_TEXT.search(text)
    ecli = checked_ecli(ecli_m.group(0) if ecli_m else None, listed_date, metadata)
    cited_type = cited.get("type")
    doctype = (TYPE_ABBREVIATIONS.get(cited_type.lower(), cited_type) if cited_type else None) or \
        _doctype([ln for p in pages[:2] for ln in p if ln.strip()][:60])
    labels = [b.label for s in body for b in s.blocks if b.label]
    metadata.update({
        "database": "ESAMOSplus (Entscheidungssammlung der ordentlichen Gerichtsbarkeit Sachsen)",
        "doc_key": extra.get("doc_key"), "court_raw": extra.get("court_name"), "norms": [],
        "citation_line": head["citation_line"], "editorial_notes": [b.text for b in head["notes"]] or None,
        "pdf": {"pages": len(pages), "margin_numbers_realigned": realigned},
        "identity": {"pdf_az": head["pdf_az"], "citation_az": cited.get("az"), "citation_date": cited_date,
                     "rubrum_date_am": facts["date_am"]},
        "rn": f"explicit margin Randnummern in the PDF ({len(labels)})" if labels else "none in the PDF",
        "rn_rejected": body_info["rn_rejected"] or None, "tenor_found": body_info["tenor_found"],
        "listing_leitsatz": extra.get("listing_leitsatz")})
    return ParsedDecision(
        source_id="sn", source_doc_id=item["source_doc_id"], source_url=url,
        court_name=extra.get("court_name") or cited.get("court") or "", decision_date=listed_date,
        case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az), decision_type=doctype, sections=sections,
        ecli=ecli, court_id=info.court_id, court_type=info.court_type, jurisdiction=info.jurisdiction,
        land=info.land or "sn", chamber=cited.get("chamber") or head["chamber"] or facts["chamber"],
        title=head["citation_line"], metadata=metadata, procedural=procedural)


# --- adapter ------------------------------------------------------------------------------------

class SachsenAdapter(Adapter):
    source_id = "sn"
    label = "Sachsen: Sächs. OVG Entscheidungssammlung + ESAMOSplus (OLG Dresden, LG, AG)"
    base_url = BASE
    land = "sn"
    adapter_name = "sn-ovg-esamos"
    robots_policy = "ignore_by_operator_decision"
    policy_decided_at = OPERATOR_DECISION_DATE
    policy_basis = OPERATOR_DECISION_BASIS + (" | www.justiz.sachsen.de/robots.txt: 'User-agent: * / "
                                              "Crawl-delay: 3600' (not applied)")
    min_interval = 3.0

    def __init__(self) -> None:
        self._esamos: tuple[PoliteHttp, dict, dict, str, str, str] | None = None

    # listing ---------------------------------------------------------------------------------
    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        window = since or (None if limit else incremental_since(conn, self.source_id, INCREMENTAL_OVERLAP_DAYS))
        prefixes = [c.lower() for c in courts or []]
        done = self._done_ids(conn)
        items: dict[str, dict] = {}
        state = {"complete": True, "found": 0}

        def wanted(court_id: str) -> bool:
            return not prefixes or any(court_id.startswith(p) for p in prefixes)

        def add(rows: list[dict]) -> None:
            for row in rows:
                row.pop("_row", None)
                if window and row["listed_date"] and row["listed_date"] < window:
                    continue
                if row["source_doc_id"] in items:
                    continue
                items[row["source_doc_id"]] = row
                if row["source_doc_id"] not in done and wanted(row["listed_court"]):
                    state["found"] += 1

        def enough() -> bool:
            return bool(limit) and state["found"] >= limit

        totals = []
        if wanted("ovg-sn"):
            totals.append(self._list_ovg(http, window, add, enough, state))
        esamos_courts = not prefixes or any(p.startswith(("olg", "lg", "ag")) or "olg-dresden".startswith(p)
                                            for p in prefixes)
        if esamos_courts and not enough():
            totals.append(self._list_esamos(http, window, limit, add, enough, state))
        elif esamos_courts:
            state["complete"] = False
        full = state["complete"] and not (window or limit or courts)
        return Listing(items=list(items.values()),
                       upstream_total=sum(totals) if full and None not in totals else None, complete=full)

    @staticmethod
    def _done_ids(conn: sqlite3.Connection | None) -> set[str]:
        if conn is None:
            return set()
        try:
            rows = conn.execute("SELECT source_doc_id FROM source_items WHERE source_id='sn' AND state<>'pending'")
            return {r[0] for r in rows}
        except sqlite3.Error:
            return set()

    def _list_ovg(self, http: PoliteHttp, window: str | None, add, enough, state) -> int | None:
        today = date.today()
        if window:
            windows = [f"{german_date(window)}-{today.strftime('%d.%m.%Y')}"]
        else:
            windows = [str(y) for y in range(today.year, OVG_FIRST_YEAR - 1, -1)]
            windows.append(f"01.01.1900-31.12.{OVG_FIRST_YEAR - 1}")
        total = 0
        for index, datum in enumerate(windows):
            hits, rows = self._ovg_window(http, datum)
            if len(rows) < hits and re.fullmatch(r"\d{4}", datum):
                # A capped result page: fall back to monthly windows for that year.
                rows, hits = [], 0
                for month in range(12, 0, -1):
                    m_hits, m_rows = self._ovg_window(http, f"{month:02d}.{datum}")
                    hits += m_hits
                    rows += m_rows
            if len(rows) < hits:
                state["complete"] = False
            total += hits
            add(rows)
            if enough():
                if index + 1 < len(windows):
                    state["complete"] = False
                return None
        return total

    def _ovg_window(self, http: PoliteHttp, datum: str) -> tuple[int, list[dict]]:
        response = http.post(OVG_SEARCH, data=ovg_search_form(datum), timeout=120)
        return parse_ovg_list(decode(response.body), url=OVG_SEARCH + f"#datum={datum}")

    # ESAMOSplus session ---------------------------------------------------------------------------
    def _esamos_form(self, http: PoliteHttp, *, refresh: bool = False):
        if refresh or self._esamos is None or self._esamos[0] is not http:
            response = http.get(ES_SEARCH, timeout=90)
            markup = decode(response.body)
            soup = BeautifulSoup(markup, "html.parser")
            ensure_document(markup, soup.find("input", attrs={"name": "__VIEWSTATE"}) is not None,
                            url=ES_SEARCH, what="ESAMOSplus search form")
            names, button, value = search_controls(soup)
            fields = {k: v for k, v in form_fields(soup).items() if not k.startswith("DV16_Table")}
            self._esamos = (http, fields, names, button, value, markup)
        return self._esamos

    def _esamos_search(self, http: PoliteHttp, von: str, bis: str) -> str:
        for attempt in range(2):
            _, fields, names, button, value, _ = self._esamos_form(http, refresh=attempt > 0)
            data = dict(fields)
            data.update({names["court_text"]: "", names["date_from"]: von, names["date_to"]: bis, names["az"]: "",
                         names["term"]: "", button: value})
            response = http.post(ES_SEARCH, data=data, timeout=120)
            markup = decode(response.body)
            soup = BeautifulSoup(markup, "html.parser")
            if soup.find("table", id="DV13_Table") is not None or is_no_hits_page(soup):
                return markup
        ensure_document(markup, False, url=ES_SEARCH, what="ESAMOSplus result list")
        return markup  # unreachable

    def _esamos_next(self, http: PoliteHttp, markup: str, forward: str) -> str:
        data = form_fields(BeautifulSoup(markup, "html.parser"))
        data[forward] = "Vorwärts"
        response = http.post(ES_RESULTS, data=data, timeout=120)
        return decode(response.body)

    def _esamos_window(self, http: PoliteHttp, von: str, bis: str, add) -> tuple[int, bool]:
        """All result pages of one date window; (rows listed, reached the last page)."""
        markup = self._esamos_search(http, von, bis)
        count = 0
        for _ in range(MAX_RESULT_PAGES):
            rows, current, last, forward = parse_esamos_results(markup)
            count += len(rows)
            add(rows)
            if current >= last:
                return count, True
            if forward is None:
                return count, False
            markup = self._esamos_next(http, markup, forward)
            nxt = pager(BeautifulSoup(markup, "html.parser"))[0]
            if nxt != current + 1:
                raise UpstreamFailure("unexpected_representation", f"ESAMOSplus pager moved from page {current} "
                                      f"to {nxt}", url=ES_RESULTS)
        return count, False

    def _list_esamos(self, http: PoliteHttp, window: str | None, limit, add, enough, state) -> int | None:
        _, _, _, _, _, markup = self._esamos_form(http, refresh=True)
        if limit:
            # The search page lists the ten newest decisions: often enough for a bounded run.
            newest = parse_rows(BeautifulSoup(markup, "html.parser"), "DV16_Table") or []
            add([esamos_item(r) for r in newest])
            if enough():
                state["complete"] = False
                return None
        today = date.today()
        if window:
            windows = [(german_date(window), today.strftime("%d.%m.%Y"))]
        else:
            windows = [(f"01.01.{y}", f"31.12.{y}") for y in range(today.year, ES_FIRST_YEAR - 1, -1)]
            windows.append(("01.01.1900", f"31.12.{ES_FIRST_YEAR - 1}"))
        total = 0
        for index, (von, bis) in enumerate(windows):
            count, reached_end = self._esamos_window(http, von, bis, add)
            total += count
            if not reached_end:
                state["complete"] = False
            if enough():
                if index + 1 < len(windows):
                    state["complete"] = False
                return None
        return total

    # documents -------------------------------------------------------------------------------------
    def fetch(self, http: PoliteHttp, item) -> Capture:
        if str(item["source_doc_id"]).startswith("esamos:"):
            return self._fetch_esamos(http, item)
        return self._fetch_ovg(http, item)

    def _fetch_ovg(self, http: PoliteHttp, item) -> Capture:
        url = item["url"]
        response = http.get(url, timeout=90)
        page = parse_ovg_page(decode(response.body), url)
        if not page["fulltext"]:
            raise UpstreamFailure("empty_document", "OVG document page links no Volltext PDF", url=url)
        pdf_url = urljoin(OVG_BASE, quote(page["fulltext"], safe="/%:?&=()"))
        pdf = http.get(pdf_url, timeout=120)
        if not pdf.body.startswith(b"%PDF"):
            raise UpstreamFailure("unexpected_representation",
                                  f"expected PDF, got {pdf.content_type or 'unknown'}", url=pdf_url)
        member = page["fulltext"].lstrip("/")
        return Capture(body=ovg_capture(response.body, url, pdf.body, pdf_url, member), url=url,
                       media_type="application/zip", extension="zip", headers=pdf.headers)

    def _fetch_esamos(self, http: PoliteHttp, item) -> Capture:
        # The ESAMOSplus search form is shared state: one fetch at a time (the OVG route is stateless).
        import threading
        lock = self.__dict__.setdefault("_esamos_lock", threading.Lock())
        with lock:
            return self._fetch_esamos_locked(http, item)

    def _fetch_esamos_locked(self, http: PoliteHttp, item) -> Capture:
        key = item_extra(item).get("doc_key") or str(item["source_doc_id"]).split(":", 1)[1]
        if not item["listed_date"]:
            raise UpstreamFailure("parse_error", f"ESAMOSplus document {key!r} without listed date cannot be "
                                  "re-found (no permalink)", url=item["url"])
        day = german_date(item["listed_date"])
        for attempt in range(2):
            if attempt:
                self._esamos_form(http, refresh=True)
            markup = self._esamos_search(http, day, day)
            for _ in range(MAX_RESULT_PAGES):
                rows, current, last, forward = parse_esamos_results(markup)
                row = next((r["_row"] for r in rows if r["_row"]["doc_key"] == key), None)
                if row is not None:
                    data = form_fields(BeautifulSoup(markup, "html.parser"))
                    data[row["button"]] = row["button_value"]
                    response = http.post(ES_RESULTS, data=data, timeout=120)
                    if response.body.startswith(b"%PDF"):
                        disposition = response.headers.get("content-disposition", "")
                        m = re.search(r'filename="?([^";]+)', disposition)
                        if m and re.sub(r"\.pdf$", "", m.group(1), flags=re.I) != key:
                            raise UpstreamFailure("identity_mismatch", f"ESAMOSplus delivered {m.group(1)!r} for "
                                                  f"document {key!r}", url=item["url"])
                        return Capture(body=response.body, url=item["url"], media_type="application/pdf",
                                       extension="pdf", headers=response.headers)
                    break          # not a PDF (session state lost?): search again once
                if current >= last or forward is None:
                    raise UpstreamFailure("identity_mismatch", f"ESAMOSplus lists no document {key!r} on "
                                          f"{item['listed_date']}", url=item["url"])
                markup = self._esamos_next(http, markup, forward)
        raise UpstreamFailure("unexpected_representation", f"ESAMOSplus did not deliver the PDF for {key!r}",
                              url=item["url"])

    def parse(self, capture: Capture, item) -> ParsedDecision:
        if str(item["source_doc_id"]).startswith("esamos:"):
            return parse_esamos_decision(capture.body, item, capture.url)
        return parse_ovg_decision(capture.body, item, capture.url)


def adapters() -> dict[str, Adapter]:
    return {"sn": SachsenAdapter()}
