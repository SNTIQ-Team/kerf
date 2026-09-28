"""Bundesverwaltungsgericht (www.bverwg.de): official decision pages (HTML).

robots.txt: ``Crawl-delay: 30``; ``/suche`` (the search) and the year folders
(``/2016/`` ...) are disallowed, so neither is used.

Listing (newest first):
  * ``/rss/entscheidungen.rss``: the 50 most recently published Urteile and
    Beschlüsse (title ``BVerwG 8 C 5.25 - Urteil - <Titel>``, link, pubDate).
    Enough whenever ``limit``/``since`` fall inside that window.
  * otherwise the decision sitemaps ``sitemap_index_dyn.xml`` ->
    ``sitemap_ent_<Senat>.xml`` (one per Senat, complete archive, no lastmod).
Document ids are the ECLI suffix, e.g. ``080726U8C5.25.0`` =
ECLI:DE:BVerwG:2026:080726U8C5.25.0 (ddmmyy, U|B|G, Az compacted, ".0").

Document: ``https://www.bverwg.de/de/<id>``.  The decision is
``<span id="<id without dots>" class="target">``: ``h2.h1`` (type, date, Az,
ECLI), ``h2.unline`` (documentation title), ``<strong>Leitsatz:</strong>``
paragraphs, ``ul.accordeon`` (Rechtsquellen, Instanzenzug, Zitiervorschlag),
then ``div.row.entscheidung`` with ``div.az`` (Az + ``div.instanz``),
``div.rubrum``, ``div.tenor`` and the reasons.  Randnummern are explicit:
``<div class="rdn" data-rdn="N"><p><span class="rdn">N</span> text</p></div>``;
headings (``h2.entscheidung-zwischen[-sub]`` "I", "II") are unnumbered blocks.
"""
from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..casenumbers import case_key, split_case_numbers
from ..courtids import FEDERAL, priority
from ..detect import ensure_not_blocked
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import Block, ParsedDecision, Section
from .base import Adapter, Capture, Listing, decode_html, iso_date, squash

SOURCE_ID = "bverwg"
BASE = "https://www.bverwg.de"
RSS_URL = BASE + "/rss/entscheidungen.rss"
SITEMAP_INDEX_URL = BASE + "/sitemap_index_dyn.xml"
DOC_URL = BASE + "/de/{doc}"
COURT_NAME, BRANCH = FEDERAL["bverwg"]
# 080726U8C5.25.0 -> dd mm yy, type letter, Senat, register (C, BN, WDB, KSt ...), number, year, suffix
DOC_ID_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})([A-Z])(\d{1,2})([A-Z][A-Za-z]*)(\d{1,4})\.(\d{2})\.(\d+)$")
TYPE_CODES = {"U": "Urteil", "B": "Beschluss", "G": "Gerichtsbescheid"}
SECTION_HEADINGS = {
    "gründe": ("gruende", "court", "BVerwG HTML: 'Gründe' of the decision"),
    "tatbestand": ("tatbestand", "court", "BVerwG HTML: 'Tatbestand' of the decision"),
    "entscheidungsgründe": ("entscheidungsgruende", "court", "BVerwG HTML: 'Entscheidungsgründe' of the decision"),
    "sondervotum": ("abweichende_meinung", "court", "BVerwG HTML: separate opinion"),
    "abweichende meinung": ("abweichende_meinung", "court", "BVerwG HTML: separate opinion"),
}
HEADNOTE_LABELS = {
    "leitsatz": ("leitsatz", "court", "BVerwG HTML 'Leitsatz': official headnote adopted by the Senat"),
    "leitsätze": ("leitsatz", "court", "BVerwG HTML 'Leitsätze': official headnotes adopted by the Senat"),
    "orientierungssatz": ("orientierungssatz", "editorial", "BVerwG HTML 'Orientierungssatz' (documentation)"),
    "orientierungssätze": ("orientierungssatz", "editorial", "BVerwG HTML 'Orientierungssätze' (documentation)"),
}
BLOCK_TAGS = {"p", "div", "ol", "ul", "li", "table", "blockquote", "pre", "dl", "dd", "dt",
              "h1", "h2", "h3", "h4", "h5", "h6", "section"}
BREAK_TAGS = BLOCK_TAGS | {"tr", "br"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
# Cloudflare's passive bot-scoring beacon, present on every bverwg.de page.  It
# is not a challenge, but courts.detect counts "challenge-platform" as one on
# short pages (see ``get_page``).
CF_BEACON_RE = re.compile(
    rb"<script\b[^>]*>(?:(?!</script>).)*?/cdn-cgi/challenge-platform/scripts/jsd/main\.js"
    rb"(?:(?!</script>).)*</script>", re.S | re.I)


# --- small HTML helpers ------------------------------------------------------

def node_text(node: Tag | NavigableString) -> str:
    """Text with newline block boundaries (``<br>``, blocks) and tab-separated cells."""
    if isinstance(node, NavigableString):
        return squash(str(node))
    parts: list[str] = []

    def walk(el: Tag) -> None:
        for child in el.children:
            if isinstance(child, Comment) or (isinstance(child, NavigableString) and type(child) is not NavigableString):
                continue
            if isinstance(child, NavigableString):
                parts.append(str(child))
            elif child.name in ("script", "style"):
                continue
            elif child.name == "br":
                parts.append("\n")
            else:
                walk(child)
                if child.name in BREAK_TAGS:
                    parts.append("\n")
                elif child.name in ("td", "th"):
                    parts.append("\t")
    walk(node)
    lines = []
    for line in "".join(parts).split("\n"):
        line = "\t".join(" ".join(cell.split()) for cell in line.split("\t")).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def has_block_child(el: Tag) -> bool:
    return any(isinstance(c, Tag) and c.name in BLOCK_TAGS for c in el.descendants)


def locator(el: Tag, root: Tag) -> str:
    """CSS-like path from ``root`` (by id) to ``el``: ``#id>div[3]>p[1]``."""
    steps = []
    node = el
    while node is not None and node is not root:
        parent = node.parent
        if parent is None:
            break
        index = 1 + sum(1 for s in node.previous_siblings if isinstance(s, Tag) and s.name == node.name)
        steps.append(f"{node.name}[{index}]")
        node = parent
    return f"#{root.get('id', '')}>" + ">".join(reversed(steps))


def leaf_blocks(el: Tag, root: Tag) -> list[Block]:
    """One Block per leaf text block below ``el`` (paragraphs, list items, tables)."""
    if el.name == "table" or not has_block_child(el):
        text = node_text(el)
        return [Block(text=text, locator=locator(el, root))] if text else []
    blocks: list[Block] = []
    inline: list[str] = []

    def flush() -> None:
        text = "\n".join(t for t in inline if t)
        if text.strip():
            blocks.append(Block(text=text, locator=locator(el, root)))
        inline.clear()

    for child in el.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            inline.append(squash(str(child)))
        elif child.name in ("script", "style"):
            continue
        elif child.name in BLOCK_TAGS:
            flush()
            blocks.extend(leaf_blocks(child, root))
        else:
            inline.append(node_text(child))
    flush()
    return blocks


def classes(el: Tag) -> set[str]:
    return set(el.get("class") or [])


def heading_key(text: str) -> str:
    """'G r ü n d e :' -> 'gründe'."""
    text = squash(text).rstrip(":").strip()
    if re.fullmatch(r"(?:\S ){2,}\S", text):
        text = text.replace(" ", "")
    return text.lower()


# --- identity helpers --------------------------------------------------------

def id_facts(doc: str) -> dict:
    """Date, type and Aktenzeichen encoded in a document id (None when unknown)."""
    m = DOC_ID_RE.match(doc)
    if not m:
        return {"date": None, "type_code": None, "case_number": None}
    dd, mm, yy, code, senat, register, number, year, _ = m.groups()
    century = 2000 if 2000 + int(yy) <= date.today().year + 1 else 1900
    try:
        when = date(century + int(yy), int(mm), int(dd)).isoformat()
    except ValueError:
        when = None
    return {"date": when, "type_code": code, "case_number": f"{senat} {register} {number}.{year}"}


def doc_id_from_url(url: str) -> str | None:
    doc = url.rstrip("/").rsplit("/", 1)[-1]
    return doc if DOC_ID_RE.match(doc) else None


def safe_xml(data: bytes, label: str) -> ET.Element:
    if re.search(rb"<!ENTITY\b", data, re.I):
        raise UpstreamFailure("unexpected_representation", f"{label}: entity declarations not allowed")
    data = re.sub(rb"<!DOCTYPE\b.*?>", b"", data, count=1, flags=re.I | re.S)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise UpstreamFailure("parse_error", f"{label}: invalid XML: {exc}") from exc


# --- listing -----------------------------------------------------------------

def make_item(doc: str, *, case_number: str | None = None, extra: dict | None = None) -> dict:
    facts = id_facts(doc)
    return {"source_doc_id": doc, "url": DOC_URL.format(doc=doc), "listed_date": facts["date"],
            "listed_court": SOURCE_ID, "listed_case_numbers": case_number or facts["case_number"],
            "listed_modified": None, "priority": priority(BRANCH),
            "extra": {"type_code": facts["type_code"], **(extra or {})}}


def parse_rss(data: bytes) -> list[dict]:
    """``/rss/entscheidungen.rss`` -> items in feed order (newest publication first)."""
    root = safe_xml(data, "bverwg rss")
    channel = root.find("channel")
    if root.tag != "rss" or channel is None:
        raise UpstreamFailure("unexpected_representation", "BVerwG feed is not RSS", url=RSS_URL)
    items = []
    for node in channel.findall("item"):
        link = squash(node.findtext("link"))
        doc = doc_id_from_url(link)
        if not doc:
            continue
        title = squash(node.findtext("title"))
        m = re.match(r"^BVerwG\s+(?P<az>.+?)\s+-\s+(?P<type>[^-]+?)(?:\s+-\s+(?P<title>.*))?$", title)
        pub = squash(node.findtext("pubDate"))
        try:
            published = parsedate_to_datetime(pub).isoformat() if pub else None
        except (TypeError, ValueError):
            published = None
        description = squash(node.findtext("description"))
        keywords = squash(description.split("(Diese Entscheidung")[0]).rstrip(";").strip() or None
        items.append(make_item(doc, case_number=squash(m.group("az")) if m else None,
                               extra={"via": "rss", "rss_title": title, "published": published,
                                      "listed_type": squash(m.group("type")) if m else None,
                                      "keywords": keywords}))
    if not items and channel.find("item") is not None:
        raise UpstreamFailure("parse_error", "BVerwG RSS items without decision links", url=RSS_URL)
    return items


def parse_sitemap_index(data: bytes) -> list[str]:
    root = safe_xml(data, "bverwg sitemap index")
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    if root.tag != ns + "sitemapindex":
        raise UpstreamFailure("unexpected_representation", "not a sitemap index", url=SITEMAP_INDEX_URL)
    locs = [squash(n.text) for n in root.iter(ns + "loc")]
    return [loc for loc in locs if re.match(r"^https://www\.bverwg\.de/sitemap_ent_\d+\.xml$", loc)]


def parse_sitemap(data: bytes, url: str) -> list[dict]:
    root = safe_xml(data, url)
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    if root.tag != ns + "urlset":
        raise UpstreamFailure("unexpected_representation", "not a sitemap urlset", url=url)
    items = []
    for loc in root.iter(ns + "loc"):
        doc = doc_id_from_url(squash(loc.text))
        if doc:
            items.append(make_item(doc, extra={"via": "sitemap", "sitemap": url}))
    return items


def wants_court(courts: list[str] | None) -> bool:
    return not courts or any(SOURCE_ID.startswith(c.strip().lower()) for c in courts if c.strip())


def open_states(conn: sqlite3.Connection | None, source_id: str, docs: list[str]) -> set[str]:
    """Ids that the frontier could still pick (new, or pending in the store)."""
    if conn is None or not docs:
        return set(docs)
    done: set[str] = set()
    for start in range(0, len(docs), 500):
        chunk = docs[start:start + 500]
        rows = conn.execute(
            f"SELECT source_doc_id FROM source_items WHERE source_id=? AND state<>'pending' "
            f"AND source_doc_id IN ({','.join('?' * len(chunk))})", [source_id, *chunk]).fetchall()
        done.update(r[0] for r in rows)
    return {d for d in docs if d not in done}


def newest_first(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda i: (i.get("listed_date") or "", i["source_doc_id"]), reverse=True)


# --- document ----------------------------------------------------------------

def parse_instanz(line: str) -> dict | None:
    """'VGH Mannheim - 01.12.2015 - AZ: VGH A 11 S 490/15' -> procedural reference."""
    line = squash(line)
    m = re.match(r"^(?P<court>.+?)\s+-\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{4})\s+-\s+AZ:?\s*(?P<az>.+)$", line)
    if not m:
        return None
    court, az = m.group("court"), m.group("az").strip()
    prefix = court.split(" ")[0]
    if az.startswith(prefix + " ") and len(az) > len(prefix) + 1:
        az = az[len(prefix) + 1:]         # "VG A 6 K 3840/14" at "VG Stuttgart"
    try:
        when = iso_date(m.group("date"))
    except UpstreamFailure:
        when = None
    return {"direction": "previous", "court": court, "date": when, "case_number": az,
            "decision_type": None, "raw": line, "field": "bverwg:instanzenzug", "origin": "source_metadata"}


def norm_string(act: str, refs: str) -> str:
    """Rechtsquellen row ('BetrAVG', '§§ 10, 17') -> '§§ 10, 17 BetrAVG'."""
    act, refs = squash(act), squash(refs)
    return f"{refs} {act}".strip() if refs else act


def parse_decision_html(body: bytes, doc: str, source_url: str, headers: dict | None = None) -> ParsedDecision:
    soup = BeautifulSoup(decode_html(body, headers), "html.parser")
    target = soup.find(id=doc.replace(".", ""))
    if target is None or target.find("h2", class_="h1") is None:
        others = [t.get("id") for t in soup.select("span.target[id]")]
        if others:
            raise UpstreamFailure("identity_mismatch", f"{doc}: page holds {others}, not this decision",
                                  url=source_url)
        raise UpstreamFailure("unexpected_representation", f"{doc}: not a BVerwG decision page", url=source_url)
    heading = target.find("h2", class_="h1")
    ecli_node = heading.find("span", class_="inconsolata")
    ecli = squash(ecli_node.get_text()) if ecli_node else ""
    if ecli_node is not None:
        ecli_node.extract()
    if not re.fullmatch(r"ECLI:DE:BVerwG:\d{4}:" + re.escape(doc), ecli):
        raise UpstreamFailure("identity_mismatch", f"{doc}: page ECLI {ecli!r}", url=source_url)
    head_text = squash(heading.get_text(" "))
    m = re.match(r"^(?P<type>.+?)\s+vom\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{4})\s*-\s*(?P<az>.+)$", head_text)
    if not m:
        raise UpstreamFailure("parse_error", f"{doc}: unexpected heading {head_text!r}", url=source_url)
    decision_type = m.group("type")
    decision_date = iso_date(m.group("date"))
    if ecli.split(":")[3] != decision_date[:4] or doc[:6] != decision_date[8:10] + decision_date[5:7] + decision_date[2:4]:
        raise UpstreamFailure("identity_mismatch", f"{doc}: ECLI {ecli} vs heading date {decision_date}",
                              url=source_url)

    sections: list[Section] = []
    metadata: dict = {"norms": [], "bverwg_id": doc}
    procedural: list[dict] = []

    title_node = target.find("h2", class_="unline")
    title = squash(title_node.get_text(" ")) if title_node else None
    if title:
        sections.append(Section("titel", "editorial", "BVerwG HTML h2.unline: documentation title of the decision",
                                [Block(text=title, locator=locator(title_node, target))]))

    # Headnotes and document information (first div.entscheidung, before the decision).
    info = next((d for d in target.find_all("div", class_="entscheidung") if classes(d) == {"entscheidung"}
                 and d.find("h2", class_="entscheidung-upper") is None), None)
    if info is not None:
        current: Section | None = None
        for child in info.children:
            if not isinstance(child, Tag):
                continue
            if child.name == "ul" and "accordeon" in classes(child):
                current = None
                for li in child.find_all("li", recursive=False):
                    label_node = li.find("h5")
                    label = squash(label_node.get_text()) if label_node else ""
                    wrapper = li.find("div", class_="text-wrapper") or li
                    if label == "Rechtsquellen":
                        rows = [[squash(td.get_text(" ")) for td in tr.find_all("td")] for tr in wrapper.find_all("tr")]
                        rows = [r for r in rows if any(r)]
                        metadata["rechtsquellen"] = rows
                        metadata["norms"] = [norm_string(r[0], r[1] if len(r) > 1 else "") for r in rows]
                    elif label == "Instanzenzug":
                        metadata["instanzenzug"] = node_text(wrapper).split("\n")
                    elif label == "Zitiervorschlag":
                        metadata["zitiervorschlag"] = squash(wrapper.get_text(" "))
                    elif label:
                        metadata.setdefault("document_info", {})[label] = node_text(wrapper)
                continue
            strong = child.find("strong") if child.name == "p" else None
            key = heading_key(strong.get_text()) if strong is not None else ""
            if key in HEADNOTE_LABELS and node_text(child).startswith(squash(strong.get_text())):
                name, authorship, basis = HEADNOTE_LABELS[key]
                current = Section(name, authorship, basis)
                sections.append(current)
                rest = node_text(child)[len(squash(strong.get_text())):].strip()
                if rest:                              # "<p><strong>Leitsatz:</strong> text</p>"
                    current.blocks.append(Block(text=rest, locator=locator(child, target)))
                continue
            if current is not None:
                current.blocks.extend(leaf_blocks(child, target))

    # The decision itself.
    body_div = target.find(lambda t: t.name == "div" and {"row", "entscheidung"} <= classes(t))
    if body_div is None:
        raise UpstreamFailure("parse_error", f"{doc}: no div.row.entscheidung", url=source_url)
    upper = body_div.find("h2", class_="entscheidung-upper")
    if upper is not None and squash(upper.get_text()) and heading_key(upper.get_text()) != decision_type.lower():
        metadata["document_heading"] = squash(upper.get_text())
    az_div = body_div.find("div", class_="az")
    az_p = az_div.find("p") if az_div else None
    az_published = squash(az_p.get_text(" ")) if az_p else squash(m.group("az"))
    raw_az = re.sub(r"^BVerwG\s+", "", az_published).strip()
    metadata["az_published"] = az_published
    if az_div is not None:
        for node in az_div.find_all("div", class_="instanz"):
            ref = parse_instanz(node.get_text(" "))
            if ref:
                procedural.append(ref)
    if not procedural:
        for line in metadata.get("instanzenzug", []):
            ref = parse_instanz(line)
            if ref:
                ref["field"] = "bverwg:accordeon:instanzenzug"
                procedural.append(ref)
    rubrum = body_div.find("div", class_="rubrum")
    chamber = None
    if rubrum is not None:
        metadata["rubrum"] = squash(rubrum.get_text(" "))
        cm = re.search(r"\b(?:der|den)\s+((?:\d+\.\s*)?(?:Wehrdienst|Fach|Große[rn]?\s+)?[Ss]enat\b[^,]*?)\s+"
                       r"(?:des|beim)\s+Bundesverwaltungsgericht", metadata["rubrum"])
        chamber = squash(cm.group(1)) if cm else None

    current: Section | None = None

    def section(name: str, authorship: str, basis: str, heading_text: str | None = None) -> Section:
        new = Section(name, authorship, basis, heading=heading_text)
        sections.append(new)
        return new

    def add(blocks: list[Block]) -> None:
        nonlocal current
        if not blocks:
            return
        if current is None:
            current = section("sonstiger_langtext", "unknown",
                              "BVerwG HTML: text outside a named part of the decision")
        current.blocks.extend(blocks)

    def walk(container: Tag) -> None:
        nonlocal current
        for child in container.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                text = squash(str(child))
                if text:
                    add([Block(text=text, locator=locator(container, target))])
                continue
            if child.name in ("script", "style", "button"):
                continue
            cls = classes(child)
            if child.name == "div" and cls & {"az", "rubrum"}:
                continue
            if child.name == "div" and cls == {"entscheidung"} and child.find("h2", class_="entscheidung-upper"):
                continue
            if child.name == "div" and "tenor" in cls:
                current = section("tenor", "court", "BVerwG HTML div.tenor: operative part of the decision")
                current.blocks.extend(leaf_blocks(child, target))
                continue
            if child.name in HEADING_TAGS:
                text = squash(child.get_text(" "))
                key = heading_key(text)
                if key in SECTION_HEADINGS:
                    current = section(*SECTION_HEADINGS[key], heading_text=text)
                elif text:
                    add([Block(text=text, kind="heading", locator=locator(child, target))])
                continue
            if child.name == "div" and "rdn" in cls:
                number = squash(child.get("data-rdn"))
                marker = child.find("span", class_="rdn")
                if marker is not None:
                    shown = squash(marker.get_text())
                    if number and shown and shown != number:
                        raise UpstreamFailure("parse_error", f"{doc}: Rn marker {shown!r} != data-rdn {number!r}",
                                              url=source_url)
                    number = number or shown
                    marker.extract()
                blocks = leaf_blocks(child, target)
                if blocks:
                    blocks[0].label = number or None
                    blocks[0].locator += f"#rn-{number}" if number else ""
                add(blocks)
                continue
            if child.name in BLOCK_TAGS and child.name not in ("div", "section") or not has_block_child(child):
                add(leaf_blocks(child, target))
                continue
            walk(child)

    walk(body_div)
    sections = [s for s in sections if any(b.text.strip() for b in s.blocks)]
    if not any(s.name not in ("titel", "leitsatz", "orientierungssatz") for s in sections):
        raise UpstreamFailure("empty_document", f"{doc}: no decision text", url=source_url)
    return ParsedDecision(
        source_id=SOURCE_ID, source_doc_id=doc, source_url=source_url, court_name=COURT_NAME,
        decision_date=decision_date, case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az),
        decision_type=decision_type, sections=sections, ecli=ecli, court_id="bverwg", court_type="bverwg",
        jurisdiction=BRANCH, land="de", chamber=chamber, title=title, metadata=metadata, procedural=procedural)


def check_identity(parsed: ParsedDecision, item) -> None:
    """The document must be the listed decision (date and Aktenzeichen)."""
    listed_date = item["listed_date"]
    if listed_date and listed_date != parsed.decision_date:
        raise UpstreamFailure("identity_mismatch", f"{parsed.source_doc_id}: listed date {listed_date} "
                              f"!= document date {parsed.decision_date}", url=parsed.source_url)
    listed = item["listed_case_numbers"]
    if listed:
        listed_keys = {case_key(c) for c in split_case_numbers(listed)}
        own = {case_key(c) for c in parsed.case_numbers}
        if listed_keys and not listed_keys <= own:
            raise UpstreamFailure("identity_mismatch", f"{parsed.source_doc_id}: listed Az {listed!r} "
                                  f"!= document Az {parsed.case_numbers_raw!r}", url=parsed.source_url)


def get_page(http: PoliteHttp, url: str):
    """GET with core challenge/error detection (the passive Cloudflare beacon is handled in courts.detect)."""
    return http.get(url, timeout=120)


class BverwgAdapter(Adapter):
    source_id = SOURCE_ID
    label = "Bundesverwaltungsgericht (bverwg.de): Urteile und Beschlüsse"
    base_url = BASE
    land = "de"
    adapter_name = "bverwg-html"
    min_interval = 3.0  # small court site (robots.txt Crawl-delay, if any, still applies)

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        if not wants_court(courts):
            return Listing(items=[], upstream_total=None, complete=False)
        rss = parse_rss(http.get(RSS_URL, timeout=120).body)
        window = [i for i in rss if not since or (i["listed_date"] or "") >= since]
        if self._rss_suffices(rss, window, conn, since, limit):
            return Listing(items=newest_first(window), upstream_total=None, complete=False)
        by_id = {i["source_doc_id"]: i for i in rss}
        missing = []
        for sitemap in parse_sitemap_index(http.get(SITEMAP_INDEX_URL, timeout=120).body):
            try:
                body = http.get(sitemap, timeout=300).body
            except UpstreamFailure as failure:
                # One unavailable per-Senat sitemap (seen: sitemap_ent_10.xml -> 404) must not
                # discard the others; it is recorded and the listing is marked incomplete.
                missing.append(sitemap)
                conn.execute("INSERT INTO failures(run_id, source_id, source_doc_id, url, kind, detail, http_status, "
                             "at) VALUES(NULL, ?, NULL, ?, ?, ?, ?, datetime('now'))",
                             (SOURCE_ID, failure.url, failure.kind, "sitemap: " + failure.detail[:300], failure.status))
                continue
            for item in parse_sitemap(body, sitemap):
                by_id.setdefault(item["source_doc_id"], item)
        items = newest_first(list(by_id.values()))
        total = len(items)
        if since:
            items = [i for i in items if (i["listed_date"] or since) >= since]
        return Listing(items=items, upstream_total=None if missing else total, complete=not since and not missing)

    @staticmethod
    def _rss_suffices(rss: list[dict], window: list[dict], conn, since: str | None, limit: int | None) -> bool:
        if not rss or (since is None and limit is None):
            return False
        if limit is not None and len(open_states(conn, SOURCE_ID, [i["source_doc_id"] for i in window])) >= limit:
            return True
        if since is not None:
            # A decision dated >= since was published >= since; the feed holds every
            # publication after its oldest pubDate.
            published = [i["extra"].get("published") for i in rss]
            if all(published) and since > min(p[:10] for p in published):
                return True
        return False

    def fetch(self, http: PoliteHttp, item) -> Capture:
        response = get_page(http, item["url"])
        if "html" not in response.content_type.lower():
            raise UpstreamFailure("unexpected_representation",
                                  f"expected HTML, got {response.content_type or 'unknown'}", url=item["url"])
        return Capture(body=response.body, url=item["url"], media_type="text/html", extension="html",
                       headers=response.headers)

    def parse(self, capture: Capture, item) -> ParsedDecision:
        ensure_not_blocked(CF_BEACON_RE.sub(b"", capture.body), url=capture.url,
                           content_type=capture.media_type)
        parsed = parse_decision_html(capture.body, item["source_doc_id"], capture.url, capture.headers)
        check_identity(parsed, item)
        return parsed


def adapters() -> dict[str, Adapter]:
    return {SOURCE_ID: BverwgAdapter()}
