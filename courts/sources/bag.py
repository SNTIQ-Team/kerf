"""Bundesarbeitsgericht (bundesarbeitsgericht.de): official HTML full texts.

robots.txt only disallows GPTBot/CCBot/ChatGPT-User and sets no Crawl-delay,
so PoliteHttp's 1.1 s floor applies.

Listing: ``/entscheidungen/`` and ``/entscheidungen/page/<n>/`` (WordPress,
50 rows per page, ~146 pages since 2010), sorted by default on the
"Bereitgestellt" (release) date, newest first.  Each row carries the
Aktenzeichen, the decision date, a keyword description, the release date, a
PDF link and the full-text link ``/entscheidung/<slug>/``.  Because the list is
ordered by release date and a decision is never released before it is decided,
the walk stops at the first row released before ``since``; with ``limit`` it
stops once enough rows that are not yet stored have been seen.

Document: ``/entscheidung/<slug>/`` (body class ``single-decision``).  The
"Details" card gives Aktenzeichen, ECLI (from ~2016 on), Art, Datum, Senat.
The full text is ``main.decision article.decision__body div.container``: one
``<h3>`` per section (Tenor, Leitsatz, Tatbestand, Entscheidungsgründe) followed
by its content.  Randnummern are
``<dl class="content__dl"><dt><a href="#rd-N">N</a></dt><dd id="rd-N">``;
unnumbered ``<p>``/``<table>`` siblings (quoted contract text, signatures)
continue the previous Rn.
"""
from __future__ import annotations

import json
import re
import sqlite3

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..casenumbers import case_key, split_case_numbers
from ..courtids import FEDERAL, priority
from ..detect import ensure_not_blocked
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import SECTION_ORDER, Block, ParsedDecision, Section
from .base import Adapter, Capture, Listing, decode_html, iso_date, squash

BASE = "https://www.bundesarbeitsgericht.de"
LIST_URL = BASE + "/entscheidungen/"
PAGE_URL = BASE + "/entscheidungen/page/{page}/"
DOC_URL = BASE + "/entscheidung/{slug}/"
SLUG_RE = re.compile(r"^https?://(?:www\.)?bundesarbeitsgericht\.de/entscheidung/([a-z0-9-]+)/?$")
MAX_PAGES = 400  # hard stop against a runaway pagination loop
COURT_NAME, BRANCH = FEDERAL["bag"]

# heading (folded) -> (canonical section, authorship, basis)
SECTIONS = {
    "tenor": ("tenor", "court", "BAG website <h3>Tenor</h3>: operative part of the decision"),
    "leitsatz": ("leitsatz", "court", "BAG website <h3>Leitsatz</h3>: headnote formulated by the deciding Senate"),
    "leitsätze": ("leitsatz", "court", "BAG website <h3>Leitsätze</h3>: headnotes formulated by the deciding Senate"),
    "orientierungssatz": ("orientierungssatz", "editorial",
                          "BAG website <h3>Orientierungssatz</h3>: documentation headnote, "
                          "not part of the decision"),
    "orientierungssätze": ("orientierungssatz", "editorial",
                           "BAG website <h3>Orientierungssätze</h3>: documentation headnotes, not part of the decision"),
    "tatbestand": ("tatbestand", "court", "BAG website <h3>Tatbestand</h3>"),
    "entscheidungsgründe": ("entscheidungsgruende", "court", "BAG website <h3>Entscheidungsgründe</h3>"),
    "gründe": ("gruende", "court", "BAG website <h3>Gründe</h3>"),
    "abweichende meinung": ("abweichende_meinung", "court", "BAG website <h3>Abweichende Meinung</h3>"),
}
GRUENDE_BASIS = ("BAG website <h3>Entscheidungsgründe</h3> of a decision without Tatbestand; the site template "
                 "labels every reasons body so, the source XML element leaked into the page is <gruende>")
TITLE_BASIS = ("BAG website subheading: keyword title (Stichworte) assigned by the court's documentation, "
               "not part of the decision text")
HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
BLOCK_TAGS = {"p", "table", "ul", "ol", "pre"} | HEADINGS
IGNORED = {"script", "style", "noscript", "img", "hr", "br"}
BREAK_TAGS = {"p", "div", "tr", "li", "dd", "dt", "dl", "blockquote", "table", "ul", "ol", "pre"} | HEADINGS
# "... gegen das Urteil des Landesarbeitsgerichts Hamburg vom 27. März 2025 - 1 SLa 24/24 - ..."
TENOR_REF_RE = re.compile(
    r"(?P<type>(?:Teil|Schluss|Zwischen|Versäumnis|Anerkenntnis)?[Uu]rteil|Beschluss|Teilurteil|Schlussurteil)"
    r"(?:e?s)?\s+des\s+(?P<court>(?:[A-ZÄÖÜ][a-zäöüß]+(?:ischen|er)\s+)?(?:Landesarbeitsgerichts|"
    r"Arbeitsgerichts)(?:\s+[A-ZÄÖÜ][\w.-]*(?:\s+(?:am|an\s+der|im|in)\s+[A-ZÄÖÜ][\w.-]*)?)*)\s+vom\s+"
    r"(?P<date>\d{1,2}\.\s*[A-Za-zäÄ]+\s+\d{4})\s*[-–—]\s*(?P<az>[^-–—]{3,60}?)\s*[-–—]")


# --- HTML helpers ---------------------------------------------------------------

def soup_of(body: bytes, headers: dict | None = None) -> BeautifulSoup:
    return BeautifulSoup(decode_html(body, headers), "html.parser")


def text_of(node: Tag) -> str:
    """Visible text: newline at block boundaries, tab between table cells."""
    parts: list[str] = []

    def walk(el: Tag, in_cell: bool) -> None:
        for child in el.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                parts.append(re.sub(r"\s+", " ", str(child)))  # source newlines are not breaks
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


def fold_heading(text: str) -> str:
    return " ".join(text.split()).strip(" :").lower()


def section_blocks(nodes: list, prefix: str) -> list[Block]:
    """Blocks of one section.  Only ``<dl class=content__dl>`` rows carry Rn labels."""
    blocks: list[Block] = []

    def visit(el, path: str, counts: dict) -> None:
        if isinstance(el, Comment):
            return
        if isinstance(el, NavigableString):
            text = " ".join(str(el).split())
            if text:
                blocks.append(Block(text=text, locator=path + "/text()"))
            return
        if not isinstance(el, Tag):
            return
        counts[el.name] = counts.get(el.name, 0) + 1
        here = f"{path}/{el.name}[{counts[el.name]}]"
        if el.name == "dl":
            for dt in el.find_all("dt", recursive=False):
                dd = dt.find_next_sibling()
                if dd is None or dd.name != "dd":
                    raise UpstreamFailure("parse_error", f"<dt> without <dd> at {here}")
                label = squash(dt.get_text(" "))
                anchor = dd.get("id") or ""
                blocks.append(Block(text=text_of(dd), label=label or None,
                                    locator=here + (f"#{anchor}" if anchor else "/dd")))
            return
        if el.name in HEADINGS:
            blocks.append(Block(text=text_of(el), kind="heading", locator=here))
            return
        if el.name in BLOCK_TAGS:
            if el.name == "p" and el.find(["table", "dl", "blockquote", "div"]):
                # html.parser keeps unclosed <p> open around following blocks
                inner: dict = {}
                for child in el.children:
                    visit(child, here, inner)
                return
            blocks.append(Block(text=text_of(el), locator=here))
            return
        if el.name not in IGNORED:  # div/blockquote/unknown wrappers: descend
            inner = {}
            for child in el.children:
                visit(child, here, inner)

    counts: dict = {}
    for node in nodes:
        visit(node, prefix, counts)
    return [b for b in blocks if b.text.strip()]


def canonical_order(sections: list[Section]) -> list[Section]:
    rank = {name: i for i, name in enumerate(SECTION_ORDER)}
    return sorted(sections, key=lambda s: rank.get(s.name, len(rank)))


# --- listing -------------------------------------------------------------------

def slug_of(url: str) -> str | None:
    m = SLUG_RE.match(url.split("?")[0].split("#")[0])
    return m.group(1) if m else None


def parse_listing(body: bytes, url: str = LIST_URL) -> tuple[list[dict], int | None]:
    """Rows of one listing page and the total page count."""
    soup = soup_of(body)
    table = soup.select_one("table.listing__table")
    if table is None:
        raise UpstreamFailure("unexpected_representation", "no decision table on listing page", url=url)
    items: list[dict] = []
    rows = table.select("tbody tr.listing__table--row")
    for row in rows:
        az_cell = row.select_one("td.is-filenumber")
        dates = row.select("td.is-date")
        if az_cell is None or len(dates) < 2:
            continue  # malformed row: skipped, the page fails only if no row parses
        az = squash(az_cell.get_text(" "))
        try:
            decided = iso_date(squash(dates[0].get_text(" ")))
            released_raw = squash(dates[1].get_text(" "))
            released = iso_date(released_raw) if released_raw else None
        except UpstreamFailure:
            continue
        full_text = row.select_one('a[title="Volltext"]') or next(
            (a for a in row.find_all("a", href=True) if slug_of(a["href"])), None)
        pdf = row.select_one('a[title="PDF herunterladen"]')
        description = row.select_one("td.is-description")
        slug = slug_of(full_text["href"]) if full_text is not None else None
        if not slug:
            continue  # PDF-only row: no HTML full text to parse
        items.append({
            "source_doc_id": slug, "url": DOC_URL.format(slug=slug), "listed_date": decided,
            "listed_court": "bag", "listed_case_numbers": az, "listed_modified": released,
            "priority": priority(BRANCH),
            "extra": {"released": released, "pdf_url": pdf["href"] if pdf is not None else None,
                      "description": squash(description.get_text(" ")) if description is not None else None},
        })
    if rows and not items:
        raise UpstreamFailure("parse_error", f"none of {len(rows)} listing rows could be parsed", url=url)
    pagination = soup.select_one(".listing__pagination[data-total]")
    total = int(pagination["data-total"]) if pagination is not None and \
        str(pagination["data-total"]).isdigit() else None
    return items, total


def _frontier_state(conn: sqlite3.Connection | None) -> dict[str, tuple[str, str | None]]:
    if conn is None:
        return {}
    try:
        rows = conn.execute("SELECT source_doc_id, state, fetched_modified FROM source_items WHERE source_id=?",
                            ("bag",)).fetchall()
    except sqlite3.Error:
        return {}
    return {r[0]: (r[1], r[2]) for r in rows}


# --- document ------------------------------------------------------------------

def parse_tenor_references(tenor_text: str) -> list[dict]:
    """Decisions of the lower instances named in the (court-authored) Tenor."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for m in TENOR_REF_RE.finditer(" ".join(tenor_text.split())):
        court = re.sub(r"gerichts\b", "gericht", m.group("court"))    # genitive -> nominative
        court = re.sub(r"^(\w+)ischen\b", r"\1isches", court)        # Hessischen -> Hessisches
        az = squash(m.group("az")).strip(" ,.")
        try:
            when = iso_date(m.group("date"))
        except UpstreamFailure:
            when = None
        key = (court, when, az)
        if key in seen:
            continue
        seen.add(key)
        dtype = m.group("type")
        out.append({"direction": "previous", "court": court, "date": when, "case_number": az,
                    "decision_type": dtype[0].upper() + dtype[1:], "raw": squash(m.group(0)),
                    "field": "bag:tenor", "origin": "decision_text"})
    return out


def parse_decision(body: bytes, url: str, headers: dict | None = None) -> ParsedDecision:
    soup = soup_of(body, headers)
    classes = (soup.body.get("class") or []) if soup.body else []
    if "single-decision" not in classes:
        raise UpstreamFailure("unexpected_representation", "page is not a BAG decision (no single-decision body)",
                              url=url)
    details: dict[str, str] = {}
    for li in soup.select("ul.card__list.is-details > li"):
        key, value = li.find("strong"), li.find("p")
        if key is not None and value is not None:
            details[squash(key.get_text(" "))] = squash(value.get_text(" "))
    raw_az = details.get("Aktenzeichen", "")
    if not raw_az or not details.get("Datum"):
        raise UpstreamFailure("parse_error", "decision page without Aktenzeichen/Datum details", url=url)
    heading = soup.select_one("h1.section__title--heading")
    if heading is not None and case_key(squash(heading.get_text(" "))) != case_key(raw_az):
        raise UpstreamFailure("identity_mismatch", f"page heading {heading.get_text(' ')!r} != Az {raw_az!r}",
                              url=url)
    decision_date = iso_date(details["Datum"])
    ecli = details.get("ECLI") or None
    if ecli:
        m = re.fullmatch(r"ECLI:DE:BAG:(\d{4}):(\d{2})(\d{2})(\d{2})\..+", ecli)
        if not m:
            raise UpstreamFailure("parse_error", f"unexpected ECLI {ecli!r}", url=url)
        if (m.group(4), m.group(3), m.group(2)) != (decision_date[2:4], decision_date[5:7], decision_date[8:10]):
            raise UpstreamFailure("identity_mismatch", f"ECLI {ecli} does not carry decision date {decision_date}",
                                  url=url)
    container = soup.select_one("main.decision article.decision__body div.container")
    if container is None:
        raise UpstreamFailure("parse_error", "no decision body container", url=url)

    # group the container's children by <h3> section headings
    groups: list[tuple[str, list]] = []
    for child in container.children:
        if isinstance(child, Tag) and child.name == "h3":
            groups.append((squash(child.get_text(" ")), []))
        elif groups:
            groups[-1][1].append(child)
        elif isinstance(child, Tag) and squash(child.get_text(" ")):
            groups.append(("", [child]))
    headings = {fold_heading(h) for h, _ in groups}
    decision_type = details.get("Art") or None
    sections: list[Section] = []
    for index, (head, nodes) in enumerate(groups, 1):
        folded = fold_heading(head)
        name, authorship, basis = SECTIONS.get(
            folded, ("sonstiger_langtext", "unknown", f"BAG website <h3>{head or '(none)'}</h3>: unmapped section"))
        leaked_gruende = any(isinstance(n, Tag) and (n.name == "gruende" or n.find("gruende")) for n in nodes)
        if name == "entscheidungsgruende" and "tatbestand" not in headings and (
                leaked_gruende or (decision_type or "").lower() != "urteil"):
            name, basis = "gruende", GRUENDE_BASIS
        blocks = section_blocks(nodes, f"/main/article/div.container/h3[{index}]+")
        if blocks:
            sections.append(Section(name=name, authorship=authorship, authorship_basis=basis, blocks=blocks,
                                    heading=head or None))
    if not any(s.name not in {"titel"} for s in sections):
        raise UpstreamFailure("empty_document", "no decision text sections", url=url)
    subheading = soup.select_one("p.section__title--subheading")
    title = squash(subheading.get_text(" ")) if subheading is not None else ""
    if title:
        sections.append(Section(name="titel", authorship="editorial", authorship_basis=TITLE_BASIS,
                                blocks=[Block(text=title, locator="/header//p.section__title--subheading")]))
    sections = canonical_order(sections)
    tenor_text = "\n".join(b.text for s in sections if s.name == "tenor" for b in s.blocks)
    pdf = soup.select_one(".block__button-list a[href$='.pdf']")
    post_id = next((c[len("postid-"):] for c in classes if c.startswith("postid-")), None)
    dc_date = soup.find("meta", attrs={"name": "DC.date"})
    dc_creator = soup.find("meta", attrs={"name": "DC.creator"})
    return ParsedDecision(
        source_id="bag", source_doc_id=slug_of(url) or url, source_url=url, court_name=COURT_NAME,
        decision_date=decision_date, case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az),
        decision_type=decision_type, sections=sections, ecli=ecli, court_id="bag", court_type="bag",
        jurisdiction=BRANCH, land="de", chamber=details.get("Senat") or None, title=title or None,
        metadata={"norms": [], "wp_post_id": post_id, "pdf_url": pdf["href"] if pdf is not None else None,
                  "dc_date": dc_date.get("content") if dc_date is not None else None,
                  "dc_creator": dc_creator.get("content") if dc_creator is not None else None,
                  "details": details},
        procedural=parse_tenor_references(tenor_text))


class BagAdapter(Adapter):
    source_id = "bag"
    label = "Bundesarbeitsgericht (bundesarbeitsgericht.de): Entscheidungen, HTML full text"
    base_url = BASE
    land = "de"
    adapter_name = "bag-html"

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        if courts and not any("bag".startswith(c.lower()) for c in courts):
            return Listing(items=[], upstream_total=None, complete=False)
        known = _frontier_state(conn) if limit else {}
        items: list[dict] = []
        seen: set[str] = set()
        wanted = 0
        page, total_pages, exhausted = 1, None, False
        while page <= MAX_PAGES:
            url = LIST_URL if page == 1 else PAGE_URL.format(page=page)
            response = http.get(url)
            rows, total = parse_listing(response.body, url)
            total_pages = total or total_pages
            if not rows and page == 1:
                raise UpstreamFailure("unexpected_representation", "first listing page has no rows", url=url)
            reached_since = False
            for item in rows:
                released = item["extra"]["released"]
                if since and released and released < since:
                    reached_since = True  # release order: everything below is older
                    break
                if since and item["listed_date"] < since:
                    continue
                if item["source_doc_id"] in seen:
                    continue
                seen.add(item["source_doc_id"])
                items.append(item)
                state = known.get(item["source_doc_id"])
                if state is None or state[0] == "pending" or (
                        item["listed_modified"] and state[1] != item["listed_modified"]):
                    wanted += 1
            if reached_since or not rows:
                exhausted = not rows
                break
            if total_pages is not None and page >= total_pages:
                exhausted = True
                break
            if limit and wanted >= limit:
                break
            page += 1
        complete = exhausted and not since
        return Listing(items=items, upstream_total=len(items) if complete else None, complete=complete)

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
        parsed = parse_decision(capture.body, url, capture.headers)
        final_slug = slug_of(capture.url) if capture.url else None
        if final_slug and final_slug != item["source_doc_id"]:
            raise UpstreamFailure("identity_mismatch", f"redirected to {capture.url}", url=url)
        parsed.source_doc_id = item["source_doc_id"]
        listed_date = item["listed_date"]
        if listed_date and listed_date != parsed.decision_date:
            raise UpstreamFailure("identity_mismatch",
                                  f"listed date {listed_date} != document date {parsed.decision_date}", url=url)
        listed_az = item["listed_case_numbers"]
        if listed_az and {case_key(a) for a in split_case_numbers(listed_az)} != \
                {case_key(a) for a in parsed.case_numbers}:
            raise UpstreamFailure("identity_mismatch", f"listed Az {listed_az!r} != document Az "
                                  f"{parsed.case_numbers_raw!r}", url=url)
        extra = item["extra"]
        if isinstance(extra, str):  # sqlite row from the frontier: JSON text
            try:
                extra = json.loads(extra or "{}")
            except ValueError:
                extra = {}
        parsed.metadata["released"] = (extra or {}).get("released")
        return parsed


def adapters() -> dict[str, Adapter]:
    return {"bag": BagAdapter()}
