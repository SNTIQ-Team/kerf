"""Bundessozialgericht (www.bsg.bund.de): official decision pages (HTML).

robots.txt: ``Crawl-delay: 30``; ``/SiteGlobals/`` (which holds the decision
search ``Expertensuche_Formular.html``), ``/DE/Service/`` (RSS) are disallowed,
and the sitemap lists no decisions.  The compliant listing is therefore the
yearly table "Übersicht Verhandlungstermine und Entscheidungen <Jahr>"
(``/DE/Verfahren/Verhandlungstermine/verhandlungstermine_uebersicht_<Jahr>.html``,
2018 onwards, newest hearing first).  Each row (date, Senat, Sachgebiet,
Terminvorschau/-bericht, Verhandlungen) links published decisions as
``a.CourtDecision`` -> ``SharedDocs/Entscheidungen/DE/<Jahr>/<yyyy_mm_dd_Az>.html``.
Limitation: decisions without an oral hearing (most Beschlüsse) appear only
in the disallowed search and are not reachable by this route.

Document: ``div#readme_<n>`` with ``h1.entscheidungsHeadline`` (court, "Urteil
vom dd.mm.yyyy", Aktenzeichen), ``h2.centerHeadline`` parts (Tenor,
Tatbestand, Entscheidungsgründe / Gründe) and plain ``<p>`` paragraphs; the
ECLI is ``div.ecli`` in the side box.  The HTML (like the PDF) carries no
Randnummern, so blocks are unlabelled (numbering "none").
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from urllib.parse import unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from ..casenumbers import case_key, split_case_numbers
from ..courtids import FEDERAL, priority
from ..detect import ensure_not_blocked
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import Block, ParsedDecision, Section
from .base import Adapter, Capture, Listing, decode_html, iso_date, squash

SOURCE_ID = "bsg"
BASE = "https://www.bsg.bund.de/"
OVERVIEW_URL = BASE + "DE/Verfahren/Verhandlungstermine/verhandlungstermine_uebersicht_{year}.html"
FIRST_YEAR = 2018
COURT_NAME, BRANCH = FEDERAL["bsg"]
DECISION_PATH = re.compile(r"/SharedDocs/Entscheidungen/DE/(\d{4})/([^/?#;]+)\.html$")
SECTION_HEADINGS = {
    "tenor": ("tenor", "court", "BSG HTML 'Tenor': operative part of the decision"),
    "tatbestand": ("tatbestand", "court", "BSG HTML 'Tatbestand' of the decision"),
    "entscheidungsgründe": ("entscheidungsgruende", "court", "BSG HTML 'Entscheidungsgründe' of the decision"),
    "gründe": ("gruende", "court", "BSG HTML 'Gründe' of the decision"),
    "leitsatz": ("leitsatz", "court", "BSG HTML 'Leitsatz': headnote adopted by the Senat"),
    "leitsätze": ("leitsatz", "court", "BSG HTML 'Leitsätze': headnotes adopted by the Senat"),
    "orientierungssatz": ("orientierungssatz", "editorial", "BSG HTML 'Orientierungssatz' (documentation)"),
    "sondervotum": ("abweichende_meinung", "court", "BSG HTML: separate opinion"),
}
BLOCK_TAGS = {"p", "div", "ol", "ul", "li", "table", "blockquote", "pre", "dl", "dd", "dt",
              "h1", "h2", "h3", "h4", "h5", "h6", "section"}
BREAK_TAGS = BLOCK_TAGS | {"tr", "br"}
HEADING_TAGS = {"h2", "h3", "h4", "h5", "h6"}


def current_year() -> int:
    return date.today().year


# --- HTML helpers ------------------------------------------------------------

def node_text(node: Tag) -> str:
    """Text with newline block boundaries (``<br>``, blocks) and tab-separated cells."""
    parts: list[str] = []

    def walk(el: Tag) -> None:
        for child in el.children:
            if isinstance(child, NavigableString):
                if type(child) is NavigableString:
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


def locator(el: Tag, root: Tag) -> str:
    steps = []
    node = el
    while node is not None and node is not root and node.parent is not None:
        index = 1 + sum(1 for s in node.previous_siblings if isinstance(s, Tag) and s.name == node.name)
        steps.append(f"{node.name}[{index}]")
        node = node.parent
    return f"#{root.get('id', '')}>" + ">".join(reversed(steps))


def heading_key(text: str) -> str:
    text = squash(text).rstrip(":").strip()
    if re.fullmatch(r"(?:\S ){2,}\S", text):
        text = text.replace(" ", "")
    return text.lower()


# --- listing -----------------------------------------------------------------

def doc_id_from_href(href: str) -> tuple[str, str] | None:
    """href -> (source_doc_id, canonical url) for a decision page link."""
    url = urljoin(BASE, href)
    parts = urlsplit(url)
    path = parts.path.split(";")[0]
    m = DECISION_PATH.search(path)
    if parts.netloc != "www.bsg.bund.de" or not m:
        return None
    return unquote(m.group(2)), f"https://www.bsg.bund.de{path}"


def parse_overview(data: bytes, url: str) -> list[dict]:
    """Yearly overview table -> decision items (page order: newest hearing first)."""
    soup = BeautifulSoup(decode_html(data), "html.parser")
    tables = [t for t in soup.find_all("table")
              if t.find("caption") and "Verhandlungstermine" in t.find("caption").get_text()]
    if not tables:
        raise UpstreamFailure("unexpected_representation", "no BSG overview table", url=url)
    items: list[dict] = []
    seen: set[str] = set()
    for table in tables:
        for row in table.find_all("tr"):
            cells = row.find_all("td", recursive=False)
            if len(cells) < 2:
                continue
            row_date_text = squash(cells[0].get_text(" "))
            try:
                row_date = iso_date(row_date_text)
            except UpstreamFailure:
                continue                                  # header row
            lines = [squash(x) for x in cells[1].get_text("\n").split("\n")]
            lines = [x for x in lines if x and not x.startswith(("Terminvorschau", "Terminbericht"))]
            senat = next((x for x in lines if re.search(r"Senat\b", x)), None)
            areas = [x for x in lines if x != senat]
            for a in row.find_all("a", href=True):
                if "CourtDecision" not in (a.get("class") or []) and "/Entscheidungen/DE/" not in a["href"]:
                    continue
                found = doc_id_from_href(a["href"])
                if not found:
                    continue
                doc, doc_url = found
                if doc in seen:
                    continue
                seen.add(doc)
                dm = re.match(r"^(\d{4})_(\d{2})_(\d{2})_", doc)
                try:
                    listed_date = date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3))).isoformat() \
                        if dm else row_date
                except ValueError:
                    listed_date = row_date
                items.append({"source_doc_id": doc, "url": doc_url, "listed_date": listed_date,
                              "listed_court": SOURCE_ID, "listed_case_numbers": squash(a.get_text(" ")) or None,
                              "listed_modified": None, "priority": priority(BRANCH),
                              "extra": {"hearing_date": row_date, "senat": senat, "sachgebiete": areas,
                                        "overview": url}})
    return items


def open_states(conn: sqlite3.Connection | None, docs: list[str]) -> set[str]:
    """Ids that the frontier could still pick (new, or pending in the store)."""
    if conn is None or not docs:
        return set(docs)
    done: set[str] = set()
    for start in range(0, len(docs), 500):
        chunk = docs[start:start + 500]
        rows = conn.execute(
            f"SELECT source_doc_id FROM source_items WHERE source_id=? AND state<>'pending' "
            f"AND source_doc_id IN ({','.join('?' * len(chunk))})", [SOURCE_ID, *chunk]).fetchall()
        done.update(r[0] for r in rows)
    return {d for d in docs if d not in done}


# --- document ----------------------------------------------------------------

def item_extra(item) -> dict:
    try:
        extra = item["extra"]
    except (KeyError, IndexError):
        return {}
    if isinstance(extra, str):
        try:
            return json.loads(extra) or {}
        except ValueError:
            return {}
    return extra or {}


def parse_decision_html(body: bytes, doc: str, source_url: str, headers: dict | None = None,
                        extra: dict | None = None) -> ParsedDecision:
    soup = BeautifulSoup(decode_html(body, headers), "html.parser")
    heads = soup.select("h1.entscheidungsHeadline")
    if len(heads) != 1:
        raise UpstreamFailure("unexpected_representation",
                              f"{doc}: {len(heads)} decision headlines, not a BSG decision page", url=source_url)
    h1 = heads[0]
    root = h1.parent
    court_node, date_node = h1.find("span", class_="court"), h1.find("span", class_="date")
    court = squash(court_node.get_text()) if court_node else ""
    if court != COURT_NAME:
        raise UpstreamFailure("identity_mismatch", f"{doc}: headline court {court!r}", url=source_url)
    dm = re.match(r"^(?P<type>.+?)\s+vom\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{4})$",
                  squash(date_node.get_text()) if date_node else "")
    if not dm:
        raise UpstreamFailure("parse_error", f"{doc}: unexpected headline {squash(h1.get_text(' '))!r}",
                              url=source_url)
    decision_type, decision_date = dm.group("type"), iso_date(dm.group("date"))
    raw_az = squash("".join(str(s) if isinstance(s, NavigableString) else s.get_text()
                            for s in date_node.next_siblings))
    raw_az = raw_az.lstrip(",").strip()
    if not raw_az:
        raise UpstreamFailure("parse_error", f"{doc}: headline without Aktenzeichen", url=source_url)

    ecli_node = soup.find("div", class_="ecli")
    ecli = squash(ecli_node.get_text()) if ecli_node else None
    if ecli:
        em = re.fullmatch(r"ECLI:DE:BSG:(\d{4}):(\d{6})\w+", ecli)
        expected = decision_date[8:10] + decision_date[5:7] + decision_date[2:4]
        if not em or em.group(1) != decision_date[:4] or em.group(2) != expected:
            raise UpstreamFailure("identity_mismatch", f"{doc}: ECLI {ecli!r} vs headline date {decision_date}",
                                  url=source_url)

    sections: list[Section] = []
    current: Section | None = None

    def add(block: Block) -> None:
        nonlocal current
        if current is None:
            current = Section("sonstiger_langtext", "unknown", "BSG HTML: text before the first part heading")
            sections.append(current)
        current.blocks.append(block)

    def walk(container: Tag) -> None:
        nonlocal current
        for child in container.children:
            if isinstance(child, Comment):
                continue
            if isinstance(child, NavigableString):
                text = squash(str(child))
                if text:
                    add(Block(text=text, locator=locator(container, root)))
                continue
            if child is h1 or child.name in ("script", "style"):
                continue
            if child.name in HEADING_TAGS:
                text = squash(child.get_text(" "))
                key = heading_key(text)
                if key in SECTION_HEADINGS:
                    name, authorship, basis = SECTION_HEADINGS[key]
                    current = Section(name, authorship, basis, heading=text)
                    sections.append(current)
                elif text:
                    add(Block(text=text, kind="heading", locator=locator(child, root)))
                continue
            if child.name in ("div", "section", "ol", "ul") and any(
                    isinstance(c, Tag) and c.name in BLOCK_TAGS for c in child.children):
                walk(child)
                continue
            text = node_text(child)
            if text:
                add(Block(text=text, locator=locator(child, root)))

    walk(root)
    sections = [s for s in sections if any(b.text.strip() for b in s.blocks)]
    if not any(s.name in ("tenor", "tatbestand", "entscheidungsgruende", "gruende") for s in sections):
        raise UpstreamFailure("empty_document", f"{doc}: no decision parts", url=source_url)

    metadata: dict = {"norms": [], "bsg_doc": doc, "headline": squash(h1.get_text(" "))}
    box = ecli_node.parent if ecli_node is not None else None
    title = None
    if box is not None:
        pdf = box.find("a", class_="downloadLink", href=True)
        if pdf is not None:
            metadata["pdf_url"] = urljoin(BASE, pdf["href"])
            vm = re.search(r"[?&]v=(\d+)", pdf["href"])
            metadata["pdf_version"] = vm.group(1) if vm else None
        for a in box.find_all("a", href=True):
            if "/Verhandlungen/" in a["href"]:
                metadata["verhandlung_url"] = urljoin(BASE, a["href"].split("?")[0])
                tm = re.search(r'"(.+)"', a.get("title") or "")
                if tm:
                    title = squash(tm.group(1))
                    metadata["schlagworte"] = title
                break
    extra = extra or {}
    # BSG Aktenzeichen "B <Senat>[/<former Senat>] <Sachgebiet> <Nr>/<Jahr> <Art>": the
    # first number is the deciding Senat ("B 11 AL 8/24 R" from a "7./11. Senat" row).
    cm = re.match(r"^B\s+(\d+[a-z]?)[\s/]", raw_az)
    if cm:
        chamber, metadata["chamber_basis"] = f"{cm.group(1)}. Senat", "Aktenzeichen (B <Senat> ...)"
    else:
        chamber = extra.get("senat")
        metadata["chamber_basis"] = "overview table" if chamber else None
    if extra.get("senat"):
        metadata["overview_senat"] = extra["senat"]
    if extra.get("sachgebiete"):
        metadata["sachgebiete"] = extra["sachgebiete"]
    if extra.get("hearing_date"):
        metadata["hearing_date"] = extra["hearing_date"]
    return ParsedDecision(
        source_id=SOURCE_ID, source_doc_id=doc, source_url=source_url, court_name=COURT_NAME,
        decision_date=decision_date, case_numbers_raw=raw_az, case_numbers=split_case_numbers(raw_az),
        decision_type=decision_type, sections=sections, ecli=ecli, court_id="bsg", court_type="bsg",
        jurisdiction=BRANCH, land="de", chamber=chamber, title=title, metadata=metadata, procedural=[])


def check_identity(parsed: ParsedDecision, item) -> None:
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


class BsgAdapter(Adapter):
    source_id = SOURCE_ID
    label = "Bundessozialgericht (bsg.bund.de): Entscheidungen aus den Verhandlungsübersichten ab 2018"
    base_url = BASE.rstrip("/")
    land = "de"
    adapter_name = "bsg-html"
    min_interval = 3.0  # small court site (robots.txt Crawl-delay, if any, still applies)

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        if courts and not any(SOURCE_ID.startswith(c.strip().lower()) for c in courts if c.strip()):
            return Listing(items=[], upstream_total=None, complete=False)
        first = max(FIRST_YEAR, int(since[:4])) if since else FIRST_YEAR
        by_id: dict[str, dict] = {}
        exhausted = True
        for year in range(current_year(), first - 1, -1):
            url = OVERVIEW_URL.format(year=year)
            try:
                response = http.get(url, timeout=120)
            except UpstreamFailure as failure:
                if failure.status == 404 and year == current_year():
                    continue                              # the new year's page is not up yet
                raise
            for item in parse_overview(response.body, url):
                by_id.setdefault(item["source_doc_id"], item)
            if limit is not None:
                candidates = [d for d, i in by_id.items() if not since or (i["listed_date"] or "") >= since]
                if len(open_states(conn, candidates)) >= limit and year > first:
                    exhausted = False
                    break
        items = sorted(by_id.values(), key=lambda i: (i["listed_date"] or "", i["source_doc_id"]), reverse=True)
        total = len(items) if exhausted and first == FIRST_YEAR else None
        if since:
            items = [i for i in items if (i["listed_date"] or since) >= since]
        return Listing(items=items, upstream_total=total, complete=exhausted and not since)

    def fetch(self, http: PoliteHttp, item) -> Capture:
        response = http.get(item["url"], timeout=120)
        if "html" not in response.content_type.lower():
            raise UpstreamFailure("unexpected_representation",
                                  f"expected HTML, got {response.content_type or 'unknown'}", url=item["url"])
        if b"entscheidungsHeadline" not in response.body:
            raise UpstreamFailure("unexpected_representation", "no decision headline in page", url=item["url"])
        return Capture(body=response.body, url=item["url"], media_type="text/html", extension="html",
                       headers=response.headers)

    def parse(self, capture: Capture, item) -> ParsedDecision:
        ensure_not_blocked(capture.body, url=capture.url, content_type=capture.media_type)
        parsed = parse_decision_html(capture.body, item["source_doc_id"], capture.url, capture.headers,
                                     extra=item_extra(item))
        check_identity(parsed, item)
        return parsed


def adapters() -> dict[str, Adapter]:
    return {SOURCE_ID: BsgAdapter()}
