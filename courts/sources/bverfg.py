"""Bundesverfassungsgericht (bundesverfassungsgericht.de): official HTML full texts.

robots.txt: ``Crawl-delay: 30`` (honoured by PoliteHttp) and ``Disallow:
/SiteGlobals/`` which covers the decision search (``/SiteGlobals/Forms/Suche/
Entscheidungssuche/``) and the press-release search.  The sitemap
(``Sitemap_Index.xml`` -> ``Sitemap_Basepage.xml``) lists 242 base pages and no
decisions.  The compliant listing routes, newest first, are therefore:

1. the home page ``/DE/Home/home_node.html``: teaser of the newest decisions;
2. press releases ``/SharedDocs/Pressemitteilungen/DE/<yyyy>/bvg<yy>-<nnn>.html``
   (numbered consecutively per year; each links the decision(s) it reports),
   walked downwards from the newest number linked on the home page, for the
   current and the previous year (further back only when ``since`` asks);
3. the tables of contents of the official collection (BVerfGE), one page per ten
   volumes under ``/DE/Entscheidungen/EntscheidungenAusDerAmtlichenSammlung/
   _functions/akkordeon_band<a>-<b>.html``, newest volumes first.

Kammer decisions that had neither a press release nor a place in BVerfGE are
only findable through the disallowed search and are not listed.

Document: ``/SharedDocs/Entscheidungen/DE/<yyyy>/<mm>/<kind><yyyymmdd>_<az>.html``.
``div.c-decision`` holds ``c-decision__principles`` (Leitsätze),
``c-decision__rubrum``, ``c-decision__tenor``, ``c-decision__reasons`` and
``ul.c-decision__judges``; ECLI and Zitiervorschlag follow in ``div.l-widget``.
Randnummern are separate markers ``<p class="is-anchor" id="N">N</p>``; the
blocks after a marker up to the next marker form that Rn (headings such as
``<h3>II.</h3>`` therefore continue the previous Rn).
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..casenumbers import case_key, split_case_numbers
from ..courtids import FEDERAL, priority
from ..detect import ensure_not_blocked
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import Block, ParsedDecision, Section
from .base import Adapter, Capture, Listing, decode_html, iso_date, squash

BASE = "https://www.bundesverfassungsgericht.de"
HOME_URL = BASE + "/DE/Home/home_node.html"
BVERFGE_URL = (BASE + "/DE/Entscheidungen/EntscheidungenAusDerAmtlichenSammlung/"
               "entscheidungenAusDerAmtlichenSammlung_node.html")
PRESS_URL = BASE + "/SharedDocs/Pressemitteilungen/DE/{year}/bvg{yy:02d}-{number:03d}.html"
DOC_URL = BASE + "/SharedDocs/Entscheidungen/DE/{year}/{month}/{doc}.html"
DOC_RE = re.compile(r"/SharedDocs/Entscheidungen/DE/(\d{4})/(\d{2})/"
                    r"(([a-z]{2})(\d{8})_([0-9a-z]+))\.html")
PRESS_RE = re.compile(r"/SharedDocs/Pressemitteilungen/DE/(\d{4})/bvg(\d{2})-(\d{3})\.html")
BAND_RE = re.compile(r"/_functions/akkordeon_band(\d+)-(\d+)\.html")
AZ_RE = re.compile(r"\b\d\s*P?Bv[A-Z]{1,2}\s+\d{1,4}/\d{2}\b|\bVz\s+\d{1,4}/\d{2}\b")
AZ_CODE_RE = re.compile(r"^(\d)\s*(P?Bv[A-Z]{1,2})\s+(\d{1,4})/(\d{2})$")
HEADLINE_RE = re.compile(r"^(?P<type>.+?)\s+vom\s+(?P<date>\d{1,2}\.\s*[A-Za-zäÄ]+\s+\d{4})\s*(?:[-–]\s*(?P<az>.+))?$")
CITE_RE = re.compile(r"BVerfG,\s*(?P<type>[\w-]+)\s+(?:der|des)\s+(?P<chamber>.+?)\s+vom\s+"
                     r"(?P<date>\d{1,2}\.\s*[A-Za-zäÄ]+\s+\d{4})")
RUBRUM_REF_RE = re.compile(
    r"(?P<type>(?:[A-ZÄÖÜ][\w-]*\s+und\s+)?[A-ZÄÖÜ]?[\w-]*(?:[Uu]rteil|[Bb]eschluss|[Bb]escheid|[Vv]erfügung))"
    r"\s+(?:des|der)\s+(?P<court>[A-ZÄÖÜ][^,;()]*?)\s+vom\s+(?P<date>\d{1,2}\.\s*[A-Za-zäÄ]+\s+\d{4})"
    r"\s*[-–]\s*(?P<az>[^-–]{2,80}?)\s*[-–]")
COURT_NAME, BRANCH = FEDERAL["bverfg"]
PRESS_YEARS_UNBOUNDED = 2        # current + previous year when no `since` is given
MAX_PRESS_NUMBER = 400
CHAMBERS = {"Ersten Senats": "Erster Senat", "Zweiten Senats": "Zweiter Senat", "Plenums": "Plenum"}
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BREAK_TAGS = {"p", "div", "tr", "li", "dd", "dt", "dl", "blockquote", "table", "ul", "ol", "pre"} | HEADINGS
DISSENT_RE = re.compile(r"^(?:Abweichende Meinung|Sondervotum)\b", re.I)
GRUENDE_RE = re.compile(r"^G\s*r\s*ü\s*n\s*d\s*e\s*:?$")
RUBRUM_CLASSES = {"az1", "az2", "adler", "vb", "bvm1", "bvm2", "bvm3", "vgt1", "vgt2", "vgt3", "rr1", "rr2", "rr3",
                  "bf", "p--heading-0", "p--heading-1", "p--heading-3"}


# --- HTML helpers ---------------------------------------------------------------

def soup_of(body: bytes, headers: dict | None = None) -> BeautifulSoup:
    return BeautifulSoup(decode_html(body, headers), "html.parser")


def text_of(node: Tag) -> str:
    """Visible text: newline at block boundaries and <br>, tab between table cells."""
    parts: list[str] = []

    def walk(el: Tag, in_cell: bool) -> None:
        for child in el.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                parts.append(re.sub(r"\s+", " ", str(child)))
                continue
            if not isinstance(child, Tag) or child.name in {"script", "style", "noscript"}:
                continue
            if child.name == "br":
                parts.append(" " if in_cell else "\n")
                continue
            cell = child.name in {"td", "th"}
            walk(child, in_cell or cell)
            if cell:
                parts.append("\t")
            elif child.name == "tr" or (child.name in BREAK_TAGS and not in_cell):
                parts.append("\n")
            elif child.name in BREAK_TAGS:
                parts.append(" ")
    walk(node, False)
    lines = []
    for line in "".join(parts).split("\n"):
        cells = [" ".join(c.split()) for c in line.split("\t")]
        line = "\t".join(c for c in cells if c)
        if line:
            lines.append(line)
    return "\n".join(lines)


def inline_text(node: Tag) -> str:
    return squash(node.get_text(" "))


def doc_from_url(url: str) -> tuple[str, str] | None:
    """(source_doc_id, canonical url) for a decision link, else None."""
    m = DOC_RE.search(url)
    if not m or m.group(3).endswith("en"):
        return None
    year, month, doc = m.group(1), m.group(2), m.group(3)
    return doc, DOC_URL.format(year=year, month=month, doc=doc)


def date_of_doc(doc: str) -> str | None:
    m = re.match(r"^[a-z]{2}(\d{4})(\d{2})(\d{2})_", doc)
    if not m:
        return None
    try:
        return date(*map(int, m.groups())).isoformat()
    except ValueError:
        return None


def az_code(az: str) -> str | None:
    """'1 BvR 2232/24' -> '1bvr223224' (the code used in URLs and ECLIs)."""
    m = AZ_CODE_RE.match(" ".join(az.split()))
    if not m:
        return None
    return f"{m.group(1)}{m.group(2).lower()}{int(m.group(3)):04d}{m.group(4)}"


def headline(text: str) -> tuple[str | None, str | None, str | None]:
    """'Beschluss vom 5. August 2026 - 2 BvL 7/14, 2 BvL 8/14' -> (type, iso date, az)."""
    m = HEADLINE_RE.match(squash(text))
    if not m:
        return None, None, None
    try:
        when = iso_date(m.group("date"))
    except UpstreamFailure:
        when = None
    return m.group("type"), when, squash(m.group("az")) or None


def make_item(href: str, *, listed_date: str | None, az: str | None, via: str, extra: dict) -> dict | None:
    found = doc_from_url(href)
    if not found:
        return None
    doc, url = found
    return {"source_doc_id": doc, "url": url, "listed_date": listed_date or date_of_doc(doc),
            "listed_court": "bverfg", "listed_case_numbers": az, "listed_modified": None,
            "priority": priority(BRANCH), "extra": {"via": [via], "kind": doc[:2], **extra}}


# --- listing pages ---------------------------------------------------------------

def parse_home(body: bytes, url: str = HOME_URL) -> tuple[list[dict], list[tuple[int, int]]]:
    """Decision teasers and (year, number) of press releases linked on the home page."""
    soup = soup_of(body)
    items = []
    for teaser in soup.select("div.c-teaser"):
        link = teaser.select_one("a.c-teaser__headline.is-decision[href]")
        if link is None:
            continue
        dtype, when, _ = headline(inline_text(link))
        az_node = teaser.select_one(".c-teaser__decision")
        text_node = teaser.select_one(".c-teaser__text")
        press = teaser.find("a", href=PRESS_RE)
        item = make_item(link["href"], listed_date=when, az=inline_text(az_node) if az_node else None, via="home",
                         extra={"decision_type": dtype,
                                "description": inline_text(text_node) if text_node else None,
                                "press_release": press_id(press["href"]) if press else None})
        if item:
            items.append(item)
    presses = sorted({(int(m.group(1)), int(m.group(3))) for a in soup.find_all("a", href=True)
                      for m in [PRESS_RE.search(a["href"])] if m}, reverse=True)
    if soup.select_one("div.c-teaser") is None:
        raise UpstreamFailure("unexpected_representation", "home page without teasers", url=url)
    return items, presses


def press_id(href: str) -> str | None:
    m = PRESS_RE.search(href)
    return f"bvg{m.group(2)}-{m.group(3)}" if m else None


def parse_press_release(body: bytes, url: str) -> tuple[str | None, list[dict]]:
    """(release date, decisions linked from the release)."""
    soup = soup_of(body)
    if soup.select_one("div.c-article") is None:
        raise UpstreamFailure("unexpected_representation", "press release page without article", url=url)
    released = None
    stamp = soup.select_one("div.c-article__intro time")
    if stamp is not None:
        try:
            released = iso_date(inline_text(stamp))
        except UpstreamFailure:
            released = None
    pid = press_id(url)
    items = []
    for link in soup.select("div.c-article a[href]"):
        if not DOC_RE.search(link["href"]):
            continue
        dtype, when, az = headline(inline_text(link))
        item = make_item(link["href"], listed_date=when, az=az, via=f"press:{pid}",
                         extra={"decision_type": dtype, "press_release": pid, "press_date": released})
        if item:
            items.append(item)
    return released, items


def parse_bverfge_node(body: bytes, url: str = BVERFGE_URL) -> list[str]:
    """Links of the BVerfGE table-of-contents pages, newest volumes first."""
    soup = soup_of(body)
    pages: dict[int, str] = {}
    for a in soup.find_all("a", href=BAND_RE):
        m = BAND_RE.search(a["href"])
        pages[int(m.group(1))] = urljoin(BASE + "/", a["href"]).split("?")[0]
    if not pages:
        raise UpstreamFailure("unexpected_representation", "BVerfGE overview without volume pages", url=url)
    return [pages[k] for k in sorted(pages, reverse=True)]


def parse_bverfge_index(body: bytes, url: str) -> list[dict]:
    """Rows (Fundstelle, Entscheidung, Aktenzeichen, Bezeichnung) of one BVerfGE contents page."""
    soup = soup_of(body)
    tables = soup.select("#content table")
    if not tables:
        raise UpstreamFailure("unexpected_representation", "BVerfGE contents page without tables", url=url)
    items = []
    for table in tables:
        for row in table.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) != 4:
                continue  # header or a colspan note ("... wurde bereits in BVerfGE 172, 345 abgedruckt")
            link = cells[0].find("a", href=True)
            if link is None:
                continue
            dtype, when, _ = headline(inline_text(cells[1]))
            item = make_item(link["href"], listed_date=when, az=inline_text(cells[2]),
                             via=f"bverfge:{inline_text(cells[0])}",
                             extra={"decision_type": dtype, "bverfge": inline_text(cells[0]),
                                    "name": inline_text(cells[3])})
            if item:
                items.append(item)
    return items


# --- document ------------------------------------------------------------------

@dataclass
class _Body:
    """Template-specific extraction result."""
    template: str
    sections: list[Section] = field(default_factory=list)
    dockets: list[str] = field(default_factory=list)
    header_lines: list[str] = field(default_factory=list)
    name: str | None = None
    judges: list[str] = field(default_factory=list)
    references: list[dict] = field(default_factory=list)


def is_gruende_heading(text: str) -> bool:
    return bool(GRUENDE_RE.match(squash(text)))


def reason_blocks(container: Tag, prefix: str) -> list[tuple[Block, bool]]:
    """Blocks with pending Rn markers applied; the flag marks a dissent heading."""
    out: list[tuple[Block, bool]] = []
    pending: list[str | None] = [None]

    def emit(text: str, locator: str, kind: str = "text") -> None:
        if not text.strip():
            return
        label, pending[0] = pending[0], None
        out.append((Block(text=text, label=label, kind=kind, locator=locator),
                    kind == "heading" and bool(DISSENT_RE.match(text))))

    def visit(el: Tag, path: str) -> None:
        counts: dict[str, int] = {}
        for child in el.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                text = " ".join(str(child).split())
                if text:
                    emit(text, path + "/text()")
                continue
            if not isinstance(child, Tag) or child.name in {"script", "style", "noscript", "br", "hr", "img"}:
                continue
            counts[child.name] = counts.get(child.name, 0) + 1
            here = f"{path}/{child.name}[{counts[child.name]}]"
            classes = child.get("class") or []
            if child.name == "p" and "is-anchor" in classes:
                pending[0] = inline_text(child) or child.get("id")
                continue
            if child.name in HEADINGS:
                emit(text_of(child), here, "heading")
            elif child.name == "p":
                text = text_of(child)
                if is_gruende_heading(text) and not out:
                    continue  # "G r ü n d e :" is the section heading itself
                strong = child.find(["strong", "b"])
                bold_only = strong is not None and squash(strong.get_text(" ")) == squash(child.get_text(" "))
                emit(text, here, "heading" if bold_only and DISSENT_RE.match(squash(text)) else "text")
            elif child.name in {"table", "ul", "ol", "pre", "dl"}:
                emit(text_of(child), here)
            else:  # blockquote, div, span, ...: one block per inner paragraph
                visit(child, here)
    visit(container, prefix)
    return out


def split_principles(node: Tag) -> tuple[str | None, list[Block], list[str]]:
    """(decision name, Leitsatz blocks, header lines) of ``c-decision__principles``."""
    name, blocks, header = None, [], []
    intro = True
    for k, p in enumerate(node.find_all(["p", "ol", "ul"], recursive=False), 1):
        text = text_of(p)
        if not text:
            continue
        locator = f"/div.c-decision__principles/{p.name}[{k}]"
        emphatic = p.find(["strong", "b"]) is not None or re.match(r"^\d+\.\s", text) is not None
        if intro and (is_leitsatz_header(text) or is_docket_lines(text)):
            header.append(squash(text))
            continue
        if intro and not emphatic and name is None and len(text) <= 200:
            name = squash(text)
            continue
        intro = False
        blocks.append(Block(text=text, locator=locator))
    if not blocks and name:
        blocks, name = [Block(text=name, locator="/div.c-decision__principles")], None
    return name, blocks, header


def is_leitsatz_header(text: str) -> bool:
    folded = re.sub(r"\s+", "", text)
    return folded.startswith(("Leitsatz", "Leitsätze", "zumBeschluss", "zumUrteil"))


def is_docket_lines(text: str) -> bool:
    lines = [line.strip(" -–,") for line in re.split(r"\n|(?<=-)\s+(?=-)", text) if line.strip(" -–,")]
    return bool(lines) and all(AZ_RE.fullmatch(line) or re.fullmatch(r"u\.\s*a\.", line) for line in lines)


def dockets_in(texts: list[str]) -> list[str]:
    found: list[str] = []
    for m in AZ_RE.finditer(" ".join(" ".join(texts).split())):
        az = " ".join(m.group(0).split())
        if az not in found:
            found.append(az)
    return found


def rubrum_references(text: str) -> list[dict]:
    """Attacked or referring decisions named in the court-authored rubrum."""
    text = " ".join(text.split())
    out, seen = [], set()
    for m in RUBRUM_REF_RE.finditer(text):
        court = re.sub(r"(gericht|gerichtshof|hof)s\b", r"\1", m.group("court").strip())
        az = squash(m.group("az")).strip(" ,.")
        try:
            when = iso_date(m.group("date"))
        except UpstreamFailure:
            when = None
        if (court, when, az) in seen or not re.search(r"\d", az):
            continue
        seen.add((court, when, az))
        out.append({"direction": "previous", "court": court, "date": when, "case_number": az,
                    "decision_type": m.group("type"), "raw": squash(m.group(0)), "field": "bverfg:rubrum",
                    "origin": "decision_text"})
    return out


def _gruende_sections(blocks: list[tuple[Block, bool]], basis: str) -> list[Section]:
    sections: list[Section] = []
    current = Section(name="gruende", authorship="court", authorship_basis=basis, heading="Gründe")
    for block, dissent in blocks:
        if dissent:
            if current.blocks:
                sections.append(current)
            current = Section(name="abweichende_meinung", authorship="court",
                              authorship_basis="BVerfG: separate opinion (§ 30 Abs. 2 BVerfGG) printed after "
                                               "the Gründe", heading=block.text.split("\n")[0])
        current.blocks.append(block)
    if current.blocks:
        sections.append(current)
    return sections


def _unlabelled(blocks: list[Block]) -> list[Block]:
    for block in blocks:
        block.label = None  # list markers in headnotes/tenor are not Randnummern
    return blocks


def _new_template(decision: Tag) -> _Body:
    """2020s template: ``div.c-decision`` with principles/rubrum/tenor/reasons blocks."""
    body = _Body(template="c-decision")
    rubrum = decision.select_one("div.c-decision__rubrum")
    principles = decision.select_one("div.c-decision__principles")
    if rubrum is not None:
        head: list[str] = []
        for child in rubrum.children:
            if isinstance(child, Tag) and child.name in HEADINGS:
                break
            head.append(text_of(child) if isinstance(child, Tag) else str(child))
        body.dockets = dockets_in(head)
        body.references = rubrum_references(text_of(rubrum))
    leitsatz: list[Block] = []
    if principles is not None:
        body.name, leitsatz, body.header_lines = split_principles(principles)
    if body.name:
        body.sections.append(Section(
            name="titel", authorship="court",
            authorship_basis="BVerfG Leitsatz sheet: decision name printed by the court above the Leitsätze",
            blocks=[Block(text=body.name, locator="/div.c-decision__principles/p[name]")]))
    if leitsatz:
        body.sections.append(Section(
            name="leitsatz", authorship="court",
            authorship_basis="BVerfG div.c-decision__principles: Leitsätze of the Senate",
            blocks=_unlabelled(leitsatz), heading=body.header_lines[0] if body.header_lines else None))
    tenor = decision.select_one("div.c-decision__tenor")
    if tenor is not None:
        blocks = _unlabelled([b for b, _ in reason_blocks(tenor, "/div.c-decision__tenor")])
        if blocks:
            body.sections.append(Section(name="tenor", authorship="court",
                                         authorship_basis="BVerfG div.c-decision__tenor: operative part",
                                         blocks=blocks))
    reasons = decision.select_one("div.c-decision__reasons")
    if reasons is not None:
        body.sections += _gruende_sections(reason_blocks(reasons, "/div.c-decision__reasons"),
                                           "BVerfG div.c-decision__reasons: Gründe")
    known = {"c-decision__principles", "c-decision__rubrum", "c-decision__tenor", "c-decision__reasons",
             "c-decision__judges", "c-related"}
    for extra in decision.find_all(["div", "section"], recursive=False):
        classes = set(extra.get("class") or [])
        if classes & known or not text_of(extra):
            continue
        label = " ".join(sorted(classes)) or extra.name
        dissent = any("dissent" in c or "opinion" in c or "abweich" in c for c in classes)
        blocks = [b for b, _ in reason_blocks(extra, f"/div.{label}")]
        if blocks:
            body.sections.append(Section(name="abweichende_meinung" if dissent else "sonstiger_langtext",
                                         authorship="court" if dissent else "unknown",
                                         authorship_basis=f"BVerfG div.{label}", blocks=blocks))
    body.judges = [inline_text(li) for li in decision.select("ul.c-decision__judges li")]
    return body


def _old_template(root: Tag) -> _Body:
    """1998-2010s template: ``div.entscheidung`` rows ``div.absatz`` = ``div.rechts`` (Rn) + ``div.links``."""
    body = _Body(template="entscheidung")
    rows = []
    # Flat editions pair adjacent number and text columns. Keep their XPath tied to
    # the actual sibling position, including blank columns beside unnumbered text.
    flat = [column for column in root.select("div.text div.links, div.text div.rechts")
            if column.find_parent("div", class_="absatz") is None]
    flat_ids = {id(column) for column in flat}
    for column in flat:
        if "links" in (column.get("class") or ()):
            previous = column.find_previous_sibling()
            if previous is None or previous.name != "div" or "rechts" not in (previous.get("class") or ()):
                raise UpstreamFailure("parse_error", "BVerfG text column without an adjacent number column")
    markers = [row for row in root.select("div.absatz, div.text div.rechts")
               if "absatz" in (row.get("class") or ()) or id(row) in flat_ids]
    wrapped_index = 0
    for row in markers:
        if "absatz" in (row.get("class") or ()):
            wrapped_index += 1
            rechts, links = row.find("div", class_="rechts"), row.find("div", class_="links")
            if links is None:
                continue
            locator = f"/div.entscheidung//div.absatz[{wrapped_index}]"
        else:
            # The official electronic edition can print alternating number and text columns without a row wrapper.
            rechts, links = row, row.find_next_sibling()
            if links is None or links.name != "div" or "links" not in (links.get("class") or ()):
                raise UpstreamFailure("parse_error", "BVerfG number column without an adjacent text column")
            current = links
            steps = []
            while current.parent is not None and current.name != "[document]":
                position = 1 + len(current.find_previous_siblings(current.name))
                steps.append(f"{current.name}[{position}]")
                current = current.parent
            locator = "/" + "/".join(reversed(steps))
            if inline_text(rechts) and not text_of(links):
                raise UpstreamFailure("parse_error", f"empty published BVerfG paragraph {inline_text(rechts)}")
        first = next((c for c in links.children if isinstance(c, Tag)), None)
        classes = set(first.get("class") or []) if first is not None else set()
        rows.append({"rn": inline_text(rechts) if rechts is not None else "", "links": links, "first": first,
                     "classes": classes, "text": text_of(links), "locator": locator,
                     "heading": first is not None and first.name in HEADINGS})
    if not rows:
        raise UpstreamFailure("parse_error", "div.entscheidung without paragraph rows")
    az1 = next((i for i, r in enumerate(rows) if "az1" in r["classes"]), None)
    gruende = next((i for i, r in enumerate(rows) if r["heading"] and is_gruende_heading(r["text"])), None)
    if gruende is None:
        first_rn = next((i for i, r in enumerate(rows) if r["rn"]), None)
        gruende = first_rn - 1 if first_rn is not None else len(rows)
    start = az1 if az1 is not None else 0
    # Leitsätze: everything before the court header "BUNDESVERFASSUNGSGERICHT"
    leitsatz = []
    for r in rows[:az1 or 0]:
        if r["first"] is not None and r["first"].name in {"ol", "ul"} or not (
                is_leitsatz_header(r["text"]) or is_docket_lines(r["text"])):
            if r["text"]:
                leitsatz.append(Block(text=r["text"], locator=r["locator"]))
        elif r["text"]:
            body.header_lines.append(squash(r["text"]))
    if leitsatz:
        body.sections.append(Section(name="leitsatz", authorship="court",
                                     authorship_basis="BVerfG div.entscheidung ol.ls: Leitsätze of the Senate",
                                     blocks=leitsatz, heading=body.header_lines[0] if body.header_lines else None))
    region = rows[start:max(gruende, start)]
    last_rubrum = max((i for i, r in enumerate(region) if r["classes"] & RUBRUM_CLASSES), default=-1)
    rubrum_rows, tenor_rows = region[:last_rubrum + 1], region[last_rubrum + 1:]
    court_header = []
    for r in rubrum_rows:
        if r["heading"]:
            break
        court_header.append(r["text"])
    body.dockets = dockets_in(court_header)
    body.references = rubrum_references(" ".join(r["text"] for r in rubrum_rows))
    tenor = [Block(text=r["text"], kind="heading" if r["heading"] else "text", locator=r["locator"])
             for r in tenor_rows if r["text"]]
    if tenor:
        body.sections.append(Section(name="tenor", authorship="court",
                                     authorship_basis="BVerfG div.entscheidung: operative part after the rubrum",
                                     blocks=tenor))
    reasons: list[tuple[Block, bool]] = []
    for r in rows[gruende + 1:]:
        if r["first"] is not None and r["first"].name == "table" and "st" in r["classes"]:
            names = [squash(c.get_text(" ")) for c in r["first"].find_all(["td", "th"])]
            if not body.judges:
                body.judges = [n for n in names if n]
            continue  # signature table
        if not r["text"]:
            continue
        kind = "heading" if r["heading"] else "text"
        reasons.append((Block(text=r["text"], label=r["rn"] or None, kind=kind, locator=r["locator"]),
                        kind == "heading" and bool(DISSENT_RE.match(r["text"]))))
    body.sections += _gruende_sections(reasons, "BVerfG div.entscheidung after 'G r ü n d e': Gründe")
    return body


def widget(soup: BeautifulSoup, title_prefix: str) -> Tag | None:
    for box in soup.select("div.l-widget"):
        head = box.select_one(".l-widget__headline")
        if head is not None and inline_text(head).startswith(title_prefix):
            return box.select_one(".l-widget__content")
    return None


def parse_decision(body: bytes, url: str, headers: dict | None = None) -> ParsedDecision:
    found = doc_from_url(url)
    if not found:
        raise UpstreamFailure("unexpected_representation", "not a BVerfG decision URL", url=url)
    doc, canonical = found
    soup = soup_of(body, headers)
    head = soup.select_one("h1.c-article__headline")
    new, old = soup.select_one("div.c-decision"), soup.select_one("div.entscheidung")
    if head is None or (new is None and old is None):
        raise UpstreamFailure("unexpected_representation", "page is not a BVerfG decision", url=url)
    dtype, when, _ = headline(inline_text(head))
    if not when:
        raise UpstreamFailure("parse_error", f"unparseable headline {inline_text(head)!r}", url=url)
    if date_of_doc(doc) and date_of_doc(doc) != when:
        raise UpstreamFailure("identity_mismatch", f"headline date {when} != document id {doc}", url=url)
    ecli_box = widget(soup, "European Case Law Identifier")
    ecli = inline_text(ecli_box) if ecli_box is not None else None
    if ecli:
        if not re.fullmatch(r"ECLI:DE:BVerfG:\d{4}:[a-z]{2}\d{8}\.[0-9a-z]+", ecli):
            raise UpstreamFailure("parse_error", f"unexpected ECLI {ecli!r}", url=url)
        if ecli.split(":")[-1].replace(".", "_") != doc:
            raise UpstreamFailure("identity_mismatch", f"ECLI {ecli} does not name document {doc}", url=url)
    try:
        parts = _new_template(new) if new is not None else _old_template(old)
    except UpstreamFailure as failure:
        failure.url = failure.url or url
        raise
    cite_box = widget(soup, "Zitiervorschlag")
    citation = squash(cite_box.get_text(" ")) if cite_box is not None else ""
    dockets = parts.dockets or dockets_in(parts.header_lines) or AZ_RE.findall(citation)[:1]
    if not dockets:
        raise UpstreamFailure("parse_error", "no Aktenzeichen in rubrum", url=url)
    raw_az = ", ".join(dict.fromkeys(" ".join(a.split()) for a in dockets))
    code = doc.split("_", 1)[1]
    codes = {az_code(a) for a in split_case_numbers(raw_az)} - {None}
    if codes and re.fullmatch(r"\d[a-z]+\d{6}", code) and code not in codes:
        raise UpstreamFailure("identity_mismatch", f"document id {doc} names none of {raw_az!r}", url=url)
    sections = parts.sections
    if not any(s.name not in {"titel"} for s in sections):
        raise UpstreamFailure("empty_document", "no decision text sections", url=url)
    chamber = None
    m = CITE_RE.search(citation)
    if m:
        chamber = CHAMBERS.get(m.group("chamber"), m.group("chamber"))
    fundstelle = widget(soup, "Fundstelle")
    pdf = soup.select_one("a.is-pdf-link[href]")
    related = [(inline_text(a), a["href"]) for a in soup.select("div.c-related a[href]")]
    description = soup.find("meta", attrs={"name": "description"})
    description = description.get("content") if description is not None else None
    return ParsedDecision(
        source_id="bverfg", source_doc_id=doc, source_url=canonical, court_name=COURT_NAME, decision_date=when,
        case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az), decision_type=dtype, sections=sections,
        ecli=ecli, court_id="bverfg", court_type="bverfg", jurisdiction=BRANCH, land="de", chamber=chamber,
        title=parts.name or description or None,
        metadata={"norms": [], "doc_kind": doc[:2], "template": parts.template, "judges": parts.judges,
                  "citation_suggestion": citation or None,
                  "bverfge": inline_text(fundstelle) if fundstelle is not None else None,
                  "pdf_url": pdf["href"] if pdf is not None else None,
                  "press_releases": [press_id(h) for _, h in related if press_id(h)],
                  "english_version": next((h for _, h in related if "/EN/" in h), None),
                  "leitsatz_header": parts.header_lines or None, "rubrum_dockets": dockets,
                  "description": description},
        procedural=parts.references)


# --- adapter -------------------------------------------------------------------

class _Collector:
    """De-duplicates items across routes and counts those the frontier would pick."""

    def __init__(self, since: str | None, limit: int | None, known: dict):
        self.since, self.limit, self.known = since, limit, known
        self.items: dict[str, dict] = {}
        self.wanted = 0

    def add(self, item: dict) -> None:
        if self.since and item["listed_date"] and item["listed_date"] < self.since:
            return
        doc = item["source_doc_id"]
        if doc in self.items:
            merged = self.items[doc]
            merged["extra"]["via"] += [v for v in item["extra"]["via"] if v not in merged["extra"]["via"]]
            for key, value in item["extra"].items():
                if key != "via" and value and not merged["extra"].get(key):
                    merged["extra"][key] = value
            if not merged["listed_case_numbers"] and item["listed_case_numbers"]:
                merged["listed_case_numbers"] = item["listed_case_numbers"]
            return
        self.items[doc] = item
        state = self.known.get(doc)
        if state is None or state == "pending":
            self.wanted += 1

    @property
    def done(self) -> bool:
        return bool(self.limit) and self.wanted >= self.limit


def _known(conn: sqlite3.Connection | None) -> dict[str, str]:
    if conn is None:
        return {}
    try:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT source_doc_id, state FROM source_items WHERE source_id=?", ("bverfg",))}
    except sqlite3.Error:
        return {}


def _press_exists(http: PoliteHttp, year: int, number: int) -> bytes | None:
    url = PRESS_URL.format(year=year, yy=year % 100, number=number)
    try:
        return http.get(url).body
    except UpstreamFailure as failure:
        if failure.kind in {"http_error", "error_page"} and failure.status in (404, 410):
            return None
        raise


def _highest_press_number(http: PoliteHttp, year: int) -> int:
    """Highest existing press release number of a past year (numbers are consecutive)."""
    low, high = 0, 64
    while high <= MAX_PRESS_NUMBER and _press_exists(http, year, high) is not None:
        low, high = high, high * 2
    high = min(high, MAX_PRESS_NUMBER + 1)
    while high - low > 1:
        mid = (low + high) // 2
        if _press_exists(http, year, mid) is not None:
            low = mid
        else:
            high = mid
    return low


class BverfgAdapter(Adapter):
    source_id = "bverfg"
    label = "Bundesverfassungsgericht (bundesverfassungsgericht.de): home, press releases, BVerfGE contents"
    base_url = BASE
    land = "de"
    adapter_name = "bverfg-html"
    min_interval = 3.0  # small court site (robots.txt Crawl-delay, if any, still applies)

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        if courts and not any("bverfg".startswith(c.lower()) for c in courts):
            return Listing(items=[], upstream_total=None, complete=False)
        acc = _Collector(since, limit, _known(conn) if limit else {})
        home = http.get(HOME_URL)
        teasers, presses = parse_home(home.body, HOME_URL)
        for item in teasers:
            acc.add(item)
        if not acc.done:
            self._walk_press_releases(http, acc, presses, since)
        if not acc.done:
            self._walk_bverfge(http, acc, since)
        return Listing(items=list(acc.items.values()), upstream_total=None, complete=False)

    def _walk_press_releases(self, http: PoliteHttp, acc: _Collector, presses: list[tuple[int, int]],
                             since: str | None) -> None:
        if not presses:
            return
        newest_year, newest_number = presses[0]
        first_year = int(since[:4]) if since else newest_year - PRESS_YEARS_UNBOUNDED + 1
        for year in range(newest_year, max(first_year, 1998) - 1, -1):
            top = newest_number if year == newest_year else _highest_press_number(http, year)
            misses = 0
            for number in range(top, 0, -1):
                url = PRESS_URL.format(year=year, yy=year % 100, number=number)
                body = _press_exists(http, year, number)
                if body is None:
                    misses += 1
                    if misses >= 3:
                        break
                    continue
                misses = 0
                released, items = parse_press_release(body, url)
                for item in items:
                    acc.add(item)
                if acc.done or (since and released and released < since):
                    return

    def _walk_bverfge(self, http: PoliteHttp, acc: _Collector, since: str | None) -> None:
        for url in parse_bverfge_node(http.get(BVERFGE_URL).body, BVERFGE_URL):
            rows = parse_bverfge_index(http.get(url).body, url)
            for item in rows:
                acc.add(item)
            if acc.done:
                return
            if since and rows and all((i["listed_date"] or "9999") < since for i in rows):
                return  # volumes are chronological: older pages hold older decisions

    def fetch(self, http: PoliteHttp, item) -> Capture:
        response = http.get(item["url"])
        if "html" not in response.content_type.lower():
            raise UpstreamFailure("unexpected_representation",
                                  f"expected HTML, got {response.content_type or 'unknown'}", url=item["url"])
        return Capture(body=response.body, url=response.url or item["url"], media_type="text/html",
                       extension="html", headers=response.headers)

    def parse(self, capture: Capture, item) -> ParsedDecision:
        url = item["url"]
        ensure_not_blocked(capture.body, url=url, content_type=capture.headers.get("content-type", "text/html"))
        final = doc_from_url(capture.url or url)
        if final and final[0] != item["source_doc_id"]:
            raise UpstreamFailure("identity_mismatch", f"redirected to {capture.url}", url=url)
        parsed = parse_decision(capture.body, url, capture.headers)
        if parsed.source_doc_id != item["source_doc_id"]:
            raise UpstreamFailure("identity_mismatch", f"document {parsed.source_doc_id} is not "
                                  f"{item['source_doc_id']}", url=url)
        listed_date = item["listed_date"]
        if listed_date and listed_date != parsed.decision_date:
            raise UpstreamFailure("identity_mismatch",
                                  f"listed date {listed_date} != document date {parsed.decision_date}", url=url)
        listed_az = item["listed_case_numbers"]
        if listed_az:
            listed = {case_key(a) for a in split_case_numbers(listed_az)}
            if listed and not listed <= {case_key(a) for a in parsed.case_numbers}:
                raise UpstreamFailure("identity_mismatch", f"listed Az {listed_az!r} not in document Az "
                                      f"{parsed.case_numbers_raw!r}", url=url)
        return parsed


def adapters() -> dict[str, Adapter]:
    return {"bverfg": BverfgAdapter()}
