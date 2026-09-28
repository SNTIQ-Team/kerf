"""juris "Bürgerservice" portals of the Länder (jportal, recherche3 JSON API).

Ten Länder publish their case law through the same juris product; one generic
adapter serves them all, configured by ``PORTALS`` (host + portal code).

Protocol (verified on Hessen/LaReDa, identical product elsewhere):

1. ``GET https://{host}/jportal/portal/page/{code}`` -- the portal page itself
   sets the anonymous public session (JSESSIONID, jwtCookie).  We do not follow
   the redirect to the JavaScript app shell.
2. ``POST .../recherche3/init`` -> ``csrfToken``.
3. ``POST .../recherche3/search`` -> ``{"hits": n, "resultList": [...]}``,
   newest first (``sort: "date"`` = Entscheidungsdatum descending), 1-based
   ``start`` paging.
4. ``POST .../recherche3/document`` -> ``{"head": <html>, "text": <html>,
   "permalink": ...}``.  ``text`` holds ``<h4 data-juris-toc='{"label":..}'>``
   rubric headings and ``<dl class="RspDL">`` Randnummer blocks (the same
   markup as RII, parsed with :func:`rii.section_blocks`).

Every body carries ``clientID``/``clientVersion`` and an ``r3ID`` that must stay
constant for the whole session (a changing r3ID drops the session with HTTP 500
``security_notAuthenticated``).  ``security_*`` answers are typed ``login``
failures; the adapter re-establishes the anonymous session once, then fails.

robots.txt is enforced by :class:`PoliteHttp`: every juris host except the
Hessian LaReDa host disallows ``*``, so those sources fail at listing time with
``robots_disallowed`` after fetching nothing but robots.txt.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

from ..casenumbers import case_key, split_case_numbers
from ..courtids import COURT_TYPES, priority, resolve
from ..detect import classify_page
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import Block, ParsedDecision, Section
from .base import Adapter, Capture, Listing, iso_date, squash
from .rii import BLOCK_TAGS, section_blocks, text_content

API_PATH = "/jportal/wsrest/recherche3/"
PAGE_SIZE = 100                      # accepted by the API (verified on bshe)
PRIORITY_BRANCHES = ("Sozialgerichtsbarkeit", "Verwaltungsgerichtsbarkeit")
BRANCH_JURISDICTION = {"Sozialgerichtsbarkeit": "sozial", "Verwaltungsgerichtsbarkeit": "verwaltung"}
INCREMENTAL_OVERLAP_DAYS = 7         # Erstabgabe window overlap after a full listing
DOC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
ECLI_RE = re.compile(r"^ECLI:[A-Z]{2}:[A-Z0-9]+:\d{4}:[A-Za-z0-9.]+$")


@dataclass(frozen=True)
class JurisPortal:
    source_id: str      # = ISO 3166-2:DE subdivision code
    land: str
    host: str
    code: str           # juris portal id (JURIS-PORTALID / clientID)
    name: str
    version: str = "V08_35_00"

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    @property
    def api(self) -> str:
        return self.base_url + API_PATH

    @property
    def portal_page(self) -> str:
        return f"{self.base_url}/jportal/portal/page/{self.code}"

    def permalink(self, doc_id: str) -> str:
        return f"{self.base_url}/perma?d={doc_id}"


PORTALS: tuple[JurisPortal, ...] = (
    JurisPortal("bw", "bw", "www.landesrecht-bw.de", "bsbw", "Landesrecht Baden-Württemberg Bürgerservice"),
    JurisPortal("be", "be", "gesetze.berlin.de", "bsbe", "Vorschriften- und Rechtsprechungsdatenbank Berlin"),
    JurisPortal("hh", "hh", "www.landesrecht-hamburg.de", "bsha", "Landesrecht Hamburg"),
    JurisPortal("he", "he", "www.lareda.hessenrecht.hessen.de", "bshe",
                "Landesrechtsprechungsdatenbank Hessen (LaReDa)"),
    JurisPortal("mv", "mv", "www.landesrecht-mv.de", "bsmv", "Landesrecht Mecklenburg-Vorpommern"),
    JurisPortal("rp", "rp", "www.landesrecht.rlp.de", "bsrp", "Landesrecht Rheinland-Pfalz"),
    JurisPortal("sl", "sl", "recht.saarland.de", "bssl", "Recht Saarland"),
    JurisPortal("st", "st", "www.landesrecht.sachsen-anhalt.de", "bsst", "Landesrecht Sachsen-Anhalt"),
    JurisPortal("sh", "sh", "www.gesetze-rechtsprechung.sh.juris.de", "bssh",
                "Gesetze und Rechtsprechung Schleswig-Holstein"),
    JurisPortal("th", "th", "landesrecht.thueringen.de", "bsth", "Landesrecht Thüringen"),
)

# juris rubric label (data-juris-toc) -> (canonical section, authorship, basis).
# juris marks its own editorial headnotes "Orientierungssatz"; "Leitsatz" is the
# headnote published with the decision by the court (or its documentation office).
_LS = ("leitsatz", "court", "juris rubric 'Leitsatz': headnote supplied with the decision by the court "
                            "(juris labels its own editorial headnotes 'Orientierungssatz')")
_OS = ("orientierungssatz", "editorial", "juris rubric '{label}': editorial headnote of the documentation "
                                         "(juris / documentation office), not decision text")
SECTION_LABELS: dict[str, tuple[str, str, str]] = {
    "titelzeile": ("titel", "editorial", "juris rubric 'Titelzeile': documentation title line"),
    "leitsatz": _LS, "leitsätze": _LS, "amtlicher leitsatz": _LS, "amtliche leitsätze": _LS,
    "orientierungssatz": _OS, "orientierungssätze": _OS, "sonstiger orientierungssatz": _OS,
    "redaktioneller leitsatz": _OS, "redaktionelle leitsätze": _OS,
    "tenor": ("tenor", "court", "juris rubric 'Tenor'"),
    "tatbestand": ("tatbestand", "court", "juris rubric 'Tatbestand'"),
    "entscheidungsgründe": ("entscheidungsgruende", "court", "juris rubric 'Entscheidungsgründe'"),
    "gründe": ("gruende", "court", "juris rubric 'Gründe'"),
    "abweichende meinung": ("abweichende_meinung", "court", "juris rubric 'Abweichende Meinung': separate opinion"),
    "sondervotum": ("abweichende_meinung", "court", "juris rubric 'Sondervotum': separate opinion"),
    "sonstiger langtext": ("sonstiger_langtext", "unknown", "juris rubric 'Sonstiger Langtext'"),
}
TITLE_BASIS = "juris document title line (div.docLayoutTitel): documentation title, not decision text"
# Rubrics that are portal/documentation apparatus, never decision text.
PROCEDURAL_LABELS = {"verfahrensgang"}
APPARATUS_LABELS = {"permalink", "fundstellen", "fundstelle", "zitiervorschlag", "diese entscheidung zitiert",
                    "diese entscheidung wird zitiert von", "zitierungen", "literaturnachweise", "anmerkungen",
                    "praxisreporte", "kurztext", "querverweise", "verweise", "rechtskraft", "sachgebiet",
                    "normenkette"}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
             "track", "wbr"}


# --- HTML fragments -> ElementTree -------------------------------------------

class _TreeBuilder(HTMLParser):
    """Tolerant HTML fragment -> ElementTree (juris emits XHTML-like markup)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = ET.Element("fragment")
        self.stack = [self.root]

    def _open(self, tag: str, attrs: list[tuple[str, str | None]]) -> ET.Element:
        return ET.SubElement(self.stack[-1], tag, {k: (v if v is not None else "") for k, v in attrs})

    def handle_starttag(self, tag, attrs):
        element = self._open(tag, attrs)
        if tag not in VOID_TAGS:
            self.stack.append(element)

    def handle_startendtag(self, tag, attrs):
        self._open(tag, attrs)

    def handle_endtag(self, tag):
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth].tag == tag:
                del self.stack[depth:]
                return

    def handle_data(self, data):
        parent = self.stack[-1]
        if len(parent):
            parent[-1].tail = (parent[-1].tail or "") + data
        else:
            parent.text = (parent.text or "") + data


def parse_fragment(markup: str) -> ET.Element:
    builder = _TreeBuilder()
    builder.feed(markup or "")
    builder.close()
    return builder.root


def _classes(el: ET.Element) -> set[str]:
    return set((el.get("class") or "").split())


def _remove(parent: ET.Element, child: ET.Element) -> None:
    """Remove ``child`` but keep its tail text."""
    index = list(parent).index(child)
    if child.tail:
        if index:
            prev = parent[index - 1]
            prev.tail = (prev.tail or "") + child.tail
        else:
            parent.text = (parent.text or "") + child.tail
    parent.remove(child)


def _strip_invisible_rn_labels(root: ET.Element) -> None:
    """``<dt><span class="unsichtbar">Randnummer</span><a name="rd_1">1</a></dt>`` -> label "1"."""
    for dt in root.iter("dt"):
        for span in [c for c in dt if c.tag == "span" and "unsichtbar" in _classes(c)]:
            _remove(dt, span)


def _heading_label(el: ET.Element) -> str | None:
    if el.tag not in HEADING_TAGS:
        return None
    raw = el.get("data-juris-toc")
    if raw is not None:
        try:
            label = json.loads(raw).get("label")
        except (ValueError, AttributeError):
            label = None
        return squash(label or "".join(el.itertext())) or "?"
    if "doc" in _classes(el):
        text = squash("".join(el.itertext()))
        if text.lower() in SECTION_LABELS or text.lower() in PROCEDURAL_LABELS:
            return text
    return None


def _skipped(el: ET.Element) -> bool:
    if el.tag in {"script", "style"}:
        return True
    if el.get("id") == "permalink" or "docLayoutPermalink" in _classes(el):
        return True
    return el.tag in HEADING_TAGS and "unsichtbar" in _classes(el)


def _contains_heading(el: ET.Element) -> bool:
    return any(_heading_label(d) is not None for d in el.iter())


def _events(el: ET.Element, path: str, out: list[tuple]) -> None:
    """Flatten the document into rubric headings and content nodes in document order."""
    counts: dict[str, int] = {}
    for child in el:
        counts[child.tag] = counts.get(child.tag, 0) + 1
        cpath = f"{path}/{child.tag}[{counts[child.tag]}]"
        if _skipped(child):
            continue
        label = _heading_label(child)
        if label is not None:
            out.append(("heading", label, child, cpath))
        elif _contains_heading(child):
            _events(child, cpath, out)
        else:
            out.append(("content", None, child, cpath))


def _has_blocks(el: ET.Element) -> bool:
    return any(d.tag == "dl" or d.tag in BLOCK_TAGS for d in el.iter())


def _node_blocks(node: ET.Element, cpath: str) -> list[Block]:
    """Blocks of one content node, via :func:`rii.section_blocks` (dl/dt/dd Randnummern)."""
    if node.tag == "dl" or node.tag in BLOCK_TAGS:
        holder = ET.Element("holder")
        holder.append(node)
        blocks = section_blocks(holder, "")
        prefix = f"/{node.tag}[1]"
        for block in blocks:
            block.locator = cpath + block.locator[len(prefix):]
        return blocks
    if not _has_blocks(node):
        text = text_content(node)
        return [Block(text=text, locator=cpath)] if text else []
    return section_blocks(node, cpath)


def _dense_len(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))


def _visible_len(el: ET.Element) -> int:
    total = _dense_len(el.text)
    for child in el:
        if not _skipped(child):
            total += _visible_len(child)
        total += _dense_len(child.tail)
    return total


# --- document ------------------------------------------------------------------

def parse_head(head_html: str) -> dict[str, ET.Element]:
    """Header table rows ``<th>Gericht:</th><td>..</td>`` -> {label: td element}."""
    root = parse_fragment(head_html)
    rows: dict[str, ET.Element] = {}
    for tr in root.iter("tr"):
        th, td = tr.find("th"), tr.find("td")
        if th is None or td is None:
            continue
        label = squash("".join(th.itertext())).rstrip(":").strip()
        if label and label not in rows:
            rows[label] = td
    return rows


def _norms(td: ET.Element | None) -> tuple[list[str], bool]:
    """Editorial norm list; the portal truncates long lists behind a '... mehr' link."""
    if td is None:
        return [], False
    truncated = False
    for a in [c for c in td.iter("a")]:
        if re.fullmatch(r"(?:\.\.\.|…)?\s*mehr", squash("".join(a.itertext()))):
            truncated = True
            a.text = ""
            for sub in list(a):
                a.remove(sub)
    text = squash("".join(td.itertext()))
    return [n.strip() for n in re.split(r",\s+", text) if n.strip()], truncated


def split_court(raw: str) -> tuple[str, str | None]:
    """'Hessischer Verwaltungsgerichtshof 8. Senat' -> (court, '8. Senat')."""
    raw = squash(raw)
    m = re.search(r"\s(\d+\s*\.\s*\S.*)$", raw)
    if m and m.start() > 0:
        return raw[:m.start()].strip(), m.group(1).strip()
    return raw, None


def parse_verfahrensgang(text: str) -> list[dict]:
    """'vorgehend VG Gießen, 26. August 2024, 8 K 678/23.GI, Urteil' -> procedural references."""
    out = []
    for line in (text or "").splitlines():
        line = squash(line)
        m = re.match(r"(?P<dir>vorgehend|nachgehend)\s+(?P<court>.+?),\s*"
                     r"(?P<date>\d{1,2}\.\s*[A-Za-zäÄ]+\s+\d{4}|\d{1,2}\.\d{1,2}\.\d{4}),\s*"
                     r"(?:Az\.?:?\s*)?(?P<az>.+?)(?:,\s*(?P<type>[^,\d]+))?$", line)
        if not m:
            continue
        try:
            when = iso_date(m.group("date"))
        except UpstreamFailure:
            when = None
        out.append({"direction": "previous" if m.group("dir") == "vorgehend" else "subsequent",
                    "court": m.group("court").strip(), "date": when, "case_number": squash(m.group("az")),
                    "decision_type": squash(m.group("type")) or None, "raw": line,
                    "field": "juris:verfahrensgang", "origin": "editorial_metadata"})
    return out


def parse_document(data: dict, doc_id: str, portal: JurisPortal) -> ParsedDecision:
    """Parse one recherche3 ``document`` response (already JSON-decoded)."""
    if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not isinstance(data.get("head"), str):
        raise UpstreamFailure("unexpected_representation", f"{doc_id}: document JSON without head/text")
    permalink = data.get("permalink") or ""
    stated_ids = set(re.findall(r"data-juristest-docid=\"([^\"]+)\"", data["text"]))
    m = re.search(r"[?&]d=([A-Za-z0-9_-]+)", permalink)
    if m:
        stated_ids.add(m.group(1))
    if not stated_ids:
        raise UpstreamFailure("identity_mismatch", f"{doc_id}: document states no docId")
    if stated_ids != {doc_id}:
        raise UpstreamFailure("identity_mismatch", f"{doc_id}: document states docId {sorted(stated_ids)}")

    head = parse_head(data["head"])
    court_raw = squash(text_content(head["Gericht"])) if "Gericht" in head else ""
    date_raw = squash(text_content(head["Entscheidungsdatum"])) if "Entscheidungsdatum" in head else ""
    if not court_raw or not date_raw or "Aktenzeichen" not in head:
        raise UpstreamFailure("parse_error", f"{doc_id}: header lacks Gericht/Entscheidungsdatum/Aktenzeichen")
    az_raw = text_content(head["Aktenzeichen"]).strip()
    ecli_raw = squash(text_content(head["ECLI"])) if "ECLI" in head else ""
    doktyp = squash(text_content(head["Dokumenttyp"])) if "Dokumenttyp" in head else ""
    norms, norms_truncated = _norms(head.get("Normen"))
    court_name, chamber = split_court(court_raw)
    info = resolve(court_name, default_land=portal.land)

    root = parse_fragment(data["text"])
    _strip_invisible_rn_labels(root)
    events: list[tuple] = []
    _events(root, "text:", events)

    sections: list[Section] = []
    by_name: dict[str, Section] = {}
    procedural_text: list[str] = []
    apparatus: dict[str, str] = {}
    unmapped: list[str] = []
    covered = 0
    current: tuple[str, str, str, str | None] | None = None   # (name, authorship, basis, heading)
    current_kind = "pre"                                       # pre | section | procedural | apparatus
    current_label = ""

    def section_for(name: str, authorship: str, basis: str, heading: str | None) -> Section:
        if name not in by_name:   # repeated rubrics are merged (positional ids must stay unique)
            by_name[name] = Section(name=name, authorship=authorship, authorship_basis=basis, heading=heading)
            sections.append(by_name[name])
        return by_name[name]

    for kind, label, node, cpath in events:
        if kind == "heading":
            covered += _visible_len(node)
            key = label.lower()
            current_label = label
            if key in SECTION_LABELS:
                name, authorship, basis = SECTION_LABELS[key]
                current, current_kind = (name, authorship, basis.format(label=label), label), "section"
            elif key in PROCEDURAL_LABELS:
                current, current_kind = None, "procedural"
            elif key in APPARATUS_LABELS:
                current, current_kind = None, "apparatus"
            else:
                unmapped.append(label)
                current = ("sonstiger_langtext", "unknown", f"unmapped juris rubric {label!r}", label)
                current_kind = "section"
            continue
        if current_kind == "procedural":
            text = text_content(node)
            covered += _dense_len(text)
            if text:
                procedural_text.append(text)
            continue
        if current_kind == "apparatus":
            text = text_content(node)
            covered += _dense_len(text)
            if text:
                apparatus[current_label] = (apparatus.get(current_label, "") + "\n" + text).strip()
            continue
        blocks = _node_blocks(node, cpath)
        covered += sum(_dense_len(b.text) + _dense_len(b.label) for b in blocks)
        if not any(b.text.strip() for b in blocks):
            continue
        if current_kind == "pre":
            if "docLayoutTitel" in _classes(node) or any("docLayoutTitel" in _classes(d) for d in node.iter()):
                target = section_for("titel", "editorial", TITLE_BASIS, None)
            else:
                target = section_for("sonstiger_langtext", "unknown", "text before the first juris rubric", None)
        else:
            assert current is not None
            target = section_for(*current)
        target.blocks.extend(blocks)

    total = _visible_len(root)
    missing = total - covered
    if missing > 20:   # every visible character must land in a block, a rubric heading or the apparatus
        raise UpstreamFailure("parse_error", f"{doc_id}: {missing} of {total} visible characters not captured")
    if not any(s.name not in ("titel", "leitsatz", "orientierungssatz") for s in sections):
        raise UpstreamFailure("empty_document", f"{doc_id}: no decision text rubric")

    ecli = ecli_raw if ECLI_RE.match(ecli_raw) else None
    title = next((b.text for s in sections if s.name == "titel" for b in s.blocks if b.text), None)
    decision_date = iso_date(date_raw)
    case_numbers = split_case_numbers(az_raw)
    own_keys = {case_key(c) for c in case_numbers}
    procedural, self_refs = [], []
    for ref in parse_verfahrensgang("\n".join(procedural_text)):
        # LaReDa sometimes lists the decision itself in its Verfahrensgang ("vorgehend VG, <own date>, <own Az>").
        if ref["date"] == decision_date and own_keys & {case_key(c) for c in split_case_numbers(ref["case_number"])}:
            self_refs.append(ref["raw"])
        else:
            procedural.append(ref)
    quelle = head.get("Quelle")
    metadata = {
        "norms": norms,
        "norms_truncated": norms_truncated,
        "juris_doc_id": doc_id,
        "juris_portal": portal.code,
        "head": {k: squash(text_content(v)) for k, v in head.items() if k not in ("Normen", "Quelle")},
        "quelle": next((img.get("alt") for img in quelle.iter("img")), None) if quelle is not None else None,
        "verfahrensgang_raw": "\n".join(procedural_text) or None,
        "court_raw": court_raw,
    }
    if ecli_raw and not ecli:
        metadata["ecli_raw"] = ecli_raw
    if self_refs:
        metadata["verfahrensgang_self_references"] = self_refs
    if apparatus:
        metadata["apparatus"] = apparatus
    if unmapped:
        metadata["unmapped_rubrics"] = unmapped
    return ParsedDecision(
        source_id=portal.source_id, source_doc_id=doc_id, source_url=portal.permalink(doc_id),
        court_name=court_name, decision_date=decision_date, case_numbers_raw=az_raw,
        case_numbers=case_numbers, decision_type=doktyp or None, sections=sections, ecli=ecli,
        court_id=info.court_id, court_type=info.court_type,
        jurisdiction=info.jurisdiction if info.jurisdiction != "unknown" else None,
        land=info.land or portal.land, chamber=chamber, title=title, metadata=metadata, procedural=procedural)


# --- listing -------------------------------------------------------------------

def parse_result_list(data: dict, portal: JurisPortal, *, branch: str | None = None) -> tuple[list[dict], int]:
    """recherche3 ``search`` response -> (listing items, hits)."""
    if not isinstance(data, dict) or not isinstance(data.get("resultList"), list) \
            or not isinstance(data.get("hits"), int):
        raise UpstreamFailure("unexpected_representation", "search JSON without resultList/hits", url=portal.api)
    items = []
    for entry in data["resultList"]:
        if not isinstance(entry, dict) or entry.get("deleted") or entry.get("inaccessible"):
            continue
        if entry.get("categoryId") not in (None, "Rechtsprechung"):
            continue
        doc_id = entry.get("docId") or ""
        if not DOC_ID_RE.match(doc_id):
            raise UpstreamFailure("parse_error", f"implausible docId {doc_id!r}", url=portal.api)
        titles = [squash(t) for t in entry.get("titleList") or []]
        subtitles = [squash(t) for t in entry.get("subtitleList") or []]
        gericht = titles[0] if titles else ""
        court_name, _ = split_court(gericht)
        info = resolve(court_name, default_land=portal.land) if court_name else None
        try:
            listed_date = iso_date(entry["date"]) if entry.get("date") else None
        except UpstreamFailure:
            listed_date = None
        prio = 0 if branch in PRIORITY_BRANCHES else priority(info.jurisdiction if info else None)
        items.append({
            "source_doc_id": doc_id, "url": portal.permalink(doc_id), "listed_date": listed_date,
            "listed_court": info.court_id if info else None,
            "listed_case_numbers": titles[1] if len(titles) > 1 else None,
            "listed_modified": None, "priority": prio,
            "extra": {"docPart": entry.get("docPart") or "L", "gericht": gericht,
                      "doktyp": subtitles[0] if subtitles else None, "branch": branch}})
    return items, data["hits"]


def _german_date(iso: str) -> str:
    y, m, d = iso_date(iso).split("-")
    return f"{d}.{m}.{y}"


def _item_extra(item) -> dict:
    extra = item["extra"] if "extra" in item.keys() else {}
    if isinstance(extra, str):
        try:
            extra = json.loads(extra or "{}")
        except ValueError:
            extra = {}
    return extra or {}


# --- API client ------------------------------------------------------------------

@dataclass
class _Session:
    http_id: int
    r3id: str
    csrf: str


def _msg_id(body: bytes | None) -> str | None:
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if isinstance(data, dict) and isinstance(data.get("msgId"), str):
        return data["msgId"]
    return None


def _api_failure(msg_id: str, url: str, status: int | None) -> UpstreamFailure:
    if msg_id.startswith("security_"):
        return UpstreamFailure("login", f"juris API {msg_id}: anonymous session rejected", url=url, status=status)
    if status and status >= 400:
        return UpstreamFailure("http_error", f"juris API {msg_id} (HTTP {status})", url=url, status=status)
    return UpstreamFailure("error_page", f"juris API error {msg_id}", url=url, status=status)


class JurisPortalAdapter(Adapter):
    adapter_name = "juris-jportal"

    def __init__(self, portal: JurisPortal):
        self.portal = portal
        self.source_id = portal.source_id
        self.label = f"{portal.name} (juris Bürgerservice {portal.code}, {portal.host})"
        self.base_url = portal.base_url
        self.land = portal.land
        self._session: _Session | None = None

    # session ------------------------------------------------------------------
    def _clear_cookies(self, http: PoliteHttp) -> None:
        jar = http.session.cookies
        for cookie in list(jar):
            domain = (cookie.domain or "").lstrip(".").lower()
            if domain and (self.portal.host == domain or self.portal.host.endswith("." + domain)):
                jar.clear(cookie.domain, cookie.path, cookie.name)

    def _open_session(self, http: PoliteHttp) -> _Session:
        self._clear_cookies(http)
        # The portal page sets the anonymous session cookies; it redirects to the JS app shell,
        # which we do not load (it is not content and would read as a "JavaScript required" page).
        response = http.get(self.portal.portal_page, allow_redirects=False, detect=False, timeout=60)
        location = response.headers.get("location", "")
        if re.search(r"login|anmeld|sso|auth", location, re.I):
            raise UpstreamFailure("login", f"portal page redirects to {location}", url=self.portal.portal_page,
                                  status=response.status)
        if response.status not in (200, 301, 302, 303, 307, 308):
            raise UpstreamFailure("http_error", f"portal page HTTP {response.status}", url=self.portal.portal_page,
                                  status=response.status)
        verdict = classify_page(response.body, status=response.status) if response.body else None
        if verdict and verdict[1] != "javascript-only shell":   # the app shell itself is expected
            raise UpstreamFailure(verdict[0], f"portal page: {verdict[1]}", url=self.portal.portal_page,
                                  status=response.status)
        r3id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        session = _Session(http_id=id(http), r3id=r3id, csrf="")
        _, data = self._post(http, session, "init", {})
        csrf = data.get("csrfToken") if isinstance(data, dict) else None
        if not isinstance(csrf, str) or not csrf:
            raise UpstreamFailure("unexpected_representation", "init without csrfToken", url=self.portal.api + "init")
        session.csrf = csrf
        return session

    def _post(self, http: PoliteHttp, session: _Session, op: str, body: dict):
        url = self.portal.api + op
        payload = dict(body)
        payload.update({"clientID": self.portal.code, "clientVersion": f"{self.portal.code} - {self.portal.version}",
                        "r3ID": session.r3id})
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "JURIS-PORTALID": self.portal.code}
        if session.csrf:
            headers["X-CSRF-TOKEN"] = session.csrf
        seen: dict = {}

        def keep(response, *args, **kwargs):
            seen["body"] = response.content

        try:
            response = http.post(url, json=payload, headers=headers, hooks={"response": [keep]}, timeout=120)
        except UpstreamFailure as failure:
            msg_id = _msg_id(seen.get("body"))
            if msg_id:
                raise _api_failure(msg_id, url, failure.status) from failure
            raise
        if "json" not in response.content_type.lower() and not response.body.lstrip().startswith((b"{", b"[")):
            raise UpstreamFailure("unexpected_representation",
                                  f"expected JSON, got {response.content_type or 'unknown'}", url=url,
                                  status=response.status)
        try:
            data = json.loads(response.body)
        except ValueError as exc:
            raise UpstreamFailure("unexpected_representation", f"invalid JSON: {exc}", url=url) from exc
        if isinstance(data, dict) and isinstance(data.get("msgId"), str) and "resultList" not in data \
                and "text" not in data and "csrfToken" not in data:
            raise _api_failure(data["msgId"], url, response.status)
        return response, data

    def _api(self, http: PoliteHttp, op: str, body: dict):
        """POST with the anonymous session; on security_* re-open the session once."""
        for attempt in (1, 2):
            if self._session is None or self._session.http_id != id(http):
                self._session = self._open_session(http)
            try:
                return self._post(http, self._session, op, body)
            except UpstreamFailure as failure:
                if failure.kind != "login" or attempt == 2:
                    raise
                self._session = None
        raise AssertionError("unreachable")

    def search(self, http: PoliteHttp, searches: list[dict], *, start: int, size: int):
        body = {"searchTasks": {"RESULT_LIST": {"start": start, "size": size, "sort": "date",
                                                "addToHistory": False, "addCategory": True}},
                "filters": {"CATEGORY": ["Rechtsprechung"]}, "searches": searches}
        return self._api(http, "search", body)

    # listing --------------------------------------------------------------------
    def _done_ids(self, conn: sqlite3.Connection | None) -> set[str]:
        if conn is None:
            return set()
        try:
            rows = conn.execute("SELECT source_doc_id FROM source_items WHERE source_id=? AND state<>'pending'",
                                (self.source_id,)).fetchall()
        except sqlite3.Error:
            return set()
        return {r[0] for r in rows}

    def _last_full_listing(self, conn: sqlite3.Connection | None) -> str | None:
        """Start date of the last run that listed the whole source (no since/limit) successfully."""
        if conn is None:
            return None
        try:
            row = conn.execute("""SELECT max(started_at) FROM sync_runs WHERE source_id=? AND since IS NULL
                                  AND limit_n IS NULL AND status IN ('ok','partial')
                                  AND coalesce(notes, '') NOT LIKE 'listing incomplete%'""",
                               (self.source_id,)).fetchone()
        except sqlite3.Error:
            return None
        return row[0][:10] if row and row[0] else None

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since: str | None,
                   limit: int | None, courts: list[str] | None) -> Listing:
        filters: list[dict] = []
        if since:
            filters.append({"id": "Datum", "value": f"ab {_german_date(since)}"})
        last_full = self._last_full_listing(conn)
        if last_full:
            # Incremental: only documents added (Erstabgabe) since shortly before the last full listing.
            window = date.fromisoformat(last_full) - timedelta(days=INCREMENTAL_OVERLAP_DAYS)
            filters.append({"id": "Erstabgabe", "value": f"ab {window.strftime('%d.%m.%Y')}"})
        done = self._done_ids(conn)
        prefixes = [c.lower() for c in courts or []]
        items: dict[str, dict] = {}
        complete = True

        def counts(item: dict, want_priority: int | None) -> bool:
            if item["source_doc_id"] in done:
                return False
            if prefixes and not any((item["listed_court"] or "").lower().startswith(p) for p in prefixes):
                return False
            return want_priority is None or item["priority"] == want_priority

        def page_through(searches: list[dict], branch: str | None, need: int | None,
                         want_priority: int | None) -> tuple[int, bool, int]:
            """Page newest-first until ``need`` countable items are found; (found, exhausted, hits)."""
            found, start, seen, hits = 0, 1, 0, 0
            size = PAGE_SIZE if need is None else max(1, min(PAGE_SIZE, need))
            while True:
                _, data = self.search(http, searches, start=start, size=size)
                page, hits = parse_result_list(data, self.portal, branch=branch)
                returned = len(data["resultList"])
                seen += returned
                for item in page:
                    if item["source_doc_id"] in items:
                        continue
                    items[item["source_doc_id"]] = item
                    if counts(item, want_priority):
                        found += 1
                start += size
                if start > hits or returned == 0:
                    if seen < hits:
                        print(f"[juris {self.source_id}] listing {searches} ended after {seen} of {hits} hits",
                              file=sys.stderr, flush=True)
                    return found, seen >= hits, hits
                if need is not None and found >= need:
                    return found, False, hits
                size = PAGE_SIZE   # later pages: large pages (most newest items may be stored already)

        # With a court filter, skip priority branches that cannot contain the requested court types.
        wanted = {next((b for _, t, b in COURT_TYPES if t == p.split("-")[0]), None) for p in prefixes}
        pending_p0 = 0
        for branch in PRIORITY_BRANCHES:
            if prefixes and None not in wanted and BRANCH_JURISDICTION[branch] not in wanted:
                continue
            found, exhausted, _ = page_through([{"id": "Gerichtsbarkeit", "value": branch}] + filters, branch,
                                               limit, None)
            pending_p0 += found
            complete = complete and exhausted
        if limit is not None and pending_p0 >= limit:
            return Listing(items=list(items.values()), upstream_total=None, complete=False)
        need = None if limit is None else limit - pending_p0
        found, exhausted, hits = page_through(filters, None, need, 1)
        complete = complete and exhausted
        upstream_total = hits if not filters else None
        return Listing(items=list(items.values()), upstream_total=upstream_total, complete=complete)

    # documents ------------------------------------------------------------------
    def fetch(self, http: PoliteHttp, item) -> Capture:
        doc_id = item["source_doc_id"]
        if not DOC_ID_RE.match(doc_id or ""):
            raise UpstreamFailure("parse_error", f"implausible docId {doc_id!r}")
        part = _item_extra(item).get("docPart") or "L"
        body = {"docId": doc_id, "format": "xsl", "docPart": part, "keyword": None,
                "sourceParams": {"source": "TL", "category": "Rechtsprechung"}}
        response, data = self._api(http, "document", body)
        if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not data.get("text").strip():
            raise UpstreamFailure("empty_document" if isinstance(data, dict) and "text" in data
                                  else "unexpected_representation",
                                  f"{doc_id}: document JSON without text", url=self.portal.api + "document")
        return Capture(body=response.body, url=self.portal.permalink(doc_id), media_type="application/json",
                       extension="json", headers=response.headers)

    def parse(self, capture: Capture, item) -> ParsedDecision:
        doc_id = item["source_doc_id"]
        try:
            data = json.loads(capture.body)
        except ValueError as exc:
            raise UpstreamFailure("unexpected_representation", f"{doc_id}: invalid JSON: {exc}") from exc
        if isinstance(data, dict) and isinstance(data.get("msgId"), str) and "text" not in data:
            raise _api_failure(data["msgId"], capture.url, None)
        parsed = parse_document(data, doc_id, self.portal)
        listed_date = item["listed_date"]
        if listed_date and listed_date != parsed.decision_date:
            raise UpstreamFailure("identity_mismatch",
                                  f"{doc_id}: listed date {listed_date} != document date {parsed.decision_date}")
        listed_az = item["listed_case_numbers"]
        if listed_az:
            listed_keys = {case_key(c) for c in split_case_numbers(listed_az)}
            if listed_keys and not listed_keys & {case_key(c) for c in parsed.case_numbers}:
                raise UpstreamFailure("identity_mismatch",
                                      f"{doc_id}: listed Az {listed_az!r} != document Az {parsed.case_numbers_raw!r}")
        listed_court = item["listed_court"]
        if listed_court and listed_court != parsed.court_id:
            raise UpstreamFailure("identity_mismatch",
                                  f"{doc_id}: listed court {listed_court} != document court {parsed.court_id}")
        parsed.metadata["capture"] = {"api": self.portal.api + "document", "docId": doc_id,
                                      "docPart": _item_extra(item).get("docPart") or "L",
                                      "host": urlsplit(self.portal.base_url).netloc}
        return parsed


def adapters() -> dict[str, Adapter]:
    return {p.source_id: JurisPortalAdapter(p) for p in PORTALS}
