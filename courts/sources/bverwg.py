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
from .base import OPERATOR_DECISION_DATE, Adapter, Capture, Listing, decode_html, iso_date, squash

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
                if blocks and (current is None or current.name == "tenor"):
                    # Older pages (e.g. 2006) print the reasons after the tenor under an empty heading.
                    current = section("gruende", "court", "BVerwG HTML div.rdn after the tenor: the reasons "
                                      "(no 'Gründe' heading on the page)")
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


LEGACY_TYPES = {"BESCHLUSS": "Beschluss", "URTEIL": "Urteil", "GERICHTSBESCHEID": "Gerichtsbescheid"}


def is_legacy_page(body: bytes, doc: str) -> bool:
    """The bverwg.de layout before 2017 (``/entscheidungen/entscheidung.php?ent=``), e.g. in Wayback captures."""
    text = body.decode("utf-8", "replace")
    return 'class="artikel"' in text and f'data-ecli="ECLI:DE:BVerwG:' in text and doc in text \
        and 'class="ent ' in text


def parse_legacy_decision_html(body: bytes, doc: str, source_url: str, headers: dict | None = None) -> ParsedDecision:
    """bverwg.de before 2017: ``div.artikel`` > ``h2`` (Az + ``a#ecli[data-ecli]``), ``div.ent_topbox1``
    ("vom dd.mm.yyyy"), ``div.ent`` with ``h3.bverwg2`` (type), the Az and Vorinstanz lines, the rubrum, the
    tenor as ``blockquote``, ``h4`` headings and ``div.ent_text`` paragraphs whose Randnummer is
    ``<a name="N"><span class="num">N </span></a>``."""
    soup = BeautifulSoup(decode_html(body, headers), "html.parser")
    art = soup.find("div", class_="artikel")
    link = art.find(id="ecli") if art is not None else None
    ecli = squash(link.get("data-ecli") or link.get_text()) if link is not None else ""
    if art is None or not ecli:
        raise UpstreamFailure("unexpected_representation", f"{doc}: not a BVerwG decision page (legacy layout)",
                              url=source_url)
    if not re.fullmatch(r"ECLI:DE:BVerwG:\d{4}:" + re.escape(doc), ecli):
        raise UpstreamFailure("identity_mismatch", f"{doc}: page ECLI {ecli!r}", url=source_url)
    heading = art.find("h2")
    raw_az = squash(re.sub(r"^BVerwG\s+", "", squash(heading.get_text(" ")).split("[")[0]))
    when = art.find("div", class_="ent_topbox1")
    m = re.search(r"(\d{1,2}\.\d{1,2}\.\d{4})", when.get_text(" ") if when is not None else "")
    if not m or not raw_az:
        raise UpstreamFailure("parse_error", f"{doc}: legacy page without date or Az", url=source_url)
    decision_date = iso_date(m.group(1))
    ent = art.find("div", class_="ent")
    if ent is None:
        raise UpstreamFailure("unexpected_representation", f"{doc}: legacy page without div.ent", url=source_url)
    kind = ent.find("h3", class_="bverwg2")
    decision_type = LEGACY_TYPES.get(squash(kind.get_text()).upper()) if kind is not None else None
    decision_type = decision_type or TYPE_CODES.get(id_facts(doc)["type_code"] or "")
    sections: list[Section] = []
    metadata: dict = {"bverwg_id": doc, "az_published": "BVerwG " + raw_az, "layout": "bverwg.de before 2017"}
    procedural: list[dict] = []
    current: Section | None = None

    def open_section(name: str, authorship: str, basis: str, heading_text: str | None = None) -> Section:
        new = Section(name, authorship, basis, heading=heading_text)
        sections.append(new)
        return new

    def paragraph_blocks(container: Tag) -> list[Block]:
        out = []
        for p in container.find_all("p"):
            num = p.find("span", class_="num")
            label = squash(num.get_text()) if num is not None else None
            if num is not None:
                (num.find_parent("a") or num).extract()
            text = node_text(p)
            if text.strip():
                out.append(Block(text=text, label=label or None, locator=locator(p, art) + (f"#rn-{label}" if label else "")))
        return out

    for child in ent.children:
        if not isinstance(child, Tag) or child.name in ("script", "style"):
            continue
        cls = classes(child)
        if child.name == "h3":
            continue
        if child.name == "blockquote" and current is None:
            current = open_section("tenor", "court", "BVerwG legacy HTML blockquote: operative part of the decision")
            current.blocks.extend(paragraph_blocks(child))
            continue
        if child.name in ("h4", "h5"):
            text = squash(child.get_text(" "))
            key = heading_key(text)
            if key in SECTION_HEADINGS:
                current = open_section(*SECTION_HEADINGS[key], heading_text=text)
            elif key in HEADNOTE_LABELS:
                current = open_section(*HEADNOTE_LABELS[key], heading_text=text)
            elif text and current is not None:
                current.blocks.append(Block(text=text, kind="heading", locator=locator(child, art)))
            continue
        if child.name == "p" and current is None:
            text = squash(child.get_text(" "))
            if not text or text.replace("BVerwG ", "") == raw_az:
                continue
            refs = [parse_instanz(line) for line in node_text(child).split("\n") if line.strip()]
            if refs and all(refs):
                procedural.extend(dict(r, field="bverwg:legacy:instanzen") for r in refs)
            elif re.search(r"\bSenat\b", text):
                metadata["rubrum"] = text
            else:
                metadata.setdefault("preamble", []).append(text)
            continue
        if current is None:
            current = open_section("sonstiger_langtext", "unknown", "BVerwG legacy HTML: text before the tenor")
        if child.name == "div" and "ent_text" in cls or child.name in ("p", "blockquote", "div"):
            if current.name == "tenor" and child.name == "div":
                current = open_section("gruende", "court", "BVerwG legacy HTML div.ent_text after the tenor")
            blocks = paragraph_blocks(child) if child.name != "p" else [
                b for b in [Block(text=node_text(child), locator=locator(child, art))] if b.text.strip()]
            current.blocks.extend(blocks)
    sections = [s for s in sections if any(b.text.strip() for b in s.blocks)]
    if not any(s.name not in ("titel", "leitsatz", "orientierungssatz") for s in sections):
        raise UpstreamFailure("empty_document", f"{doc}: no decision text", url=source_url)
    return ParsedDecision(
        source_id=SOURCE_ID, source_doc_id=doc, source_url=source_url, court_name=COURT_NAME,
        decision_date=decision_date, case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az),
        decision_type=decision_type, sections=sections, ecli=ecli, court_id="bverwg", court_type="bverwg",
        jurisdiction=BRANCH, land="de", title=None, metadata=metadata, procedural=procedural)


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
    min_interval = 3.0  # small court site: per slot; at most 2 slots (base.Adapter.concurrency)
    robots_policy = "ignore_by_operator_decision"
    policy_decided_at = OPERATOR_DECISION_DATE
    policy_basis = ("operator decision (user), 2026-09-28: the Crawl-delay of www.bverwg.de/robots.txt is not "
                    "applied; at most 2 documents in flight (adaptive, 1 request per 3 s per slot); only items RII "
                    "does not already list are fetched (in practice 2002-2009 and gaps); bot challenges, CAPTCHAs "
                    "and logins are never bypassed; 429/503 and Retry-After are honoured")

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
        extra = item["extra"] if "extra" in (item.keys() if hasattr(item, "keys") else item) else None
        extra = extra if isinstance(extra, dict) else __import__("json").loads(extra or "{}")
        if extra.get("capture_origin"):
            # A user-browser capture (courts.capture): re-parses keep its provenance.
            parsed.metadata["capture_origin"] = extra["capture_origin"]
            parsed.metadata["capture"] = {k: extra[k] for k in (
                "captured_at", "captured_at_basis", "ingested_at", "capture_url", "canonical_url", "media_type",
                "sha256", "ticket") if k in extra}
        return parsed


CAPTURE_URL_RE = re.compile(r"^https?://(?:www\.)?bverwg\.de(?::443)?/(?:de/|entscheidungen/entscheidung\.php\?(?:.*&)?ent=)?"
                            r"(?P<doc>\d{6}[A-Z]\d{1,2}[A-Z][A-Za-z]*\d{1,4}\.\d{2}\.\d+)(?:$|[&#?/])")


def capture_canonical_url(url: str) -> tuple[str, str]:
    """(document id, canonical URL) of a bverwg.de decision page URL (user-browser capture); ValueError else.
    Accepted: /de/<id>, /<id>, /entscheidungen/entscheidung.php?ent=<id> (id = the ECLI ordinal)."""
    m = CAPTURE_URL_RE.match((url or "").strip())
    if not m or not DOC_ID_RE.match(m.group("doc")):
        raise ValueError(f"not a single bverwg.de decision URL (https://www.bverwg.de/de/<ECLI ordinal>): {url!r}")
    return m.group("doc"), DOC_URL.format(doc=m.group("doc"))


def capture_parse(body: bytes, item: dict, url: str) -> ParsedDecision:
    return BverwgAdapter().parse(Capture(body, url, "text/html", "html", {"content-type": "text/html"}), item)


def capture_page_doc_ids(body: bytes) -> set[str]:
    text = body.decode("utf-8", "replace")
    return set(re.findall(r"ECLI:DE:BVerwG:\d{4}:(\d{6}[A-Z]\d{1,2}[A-Z][A-Za-z]*\d{1,4}\.\d{2}\.\d+)", text)) or \
        set(re.findall(r'data-ecli="(\d{6}[A-Z][0-9A-Za-z.]+)"', text))


def adapters() -> dict[str, Adapter]:
    return {SOURCE_ID: BverwgAdapter()}
