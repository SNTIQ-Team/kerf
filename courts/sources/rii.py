"""Rechtsprechung im Internet (RII, BMJ/BfJ): federal courts, official XML.

Listing: the complete table of contents ``rii-toc.xml`` (one item per decision
with court, date, Aktenzeichen, ZIP link and a ``modified`` timestamp used for
incremental sync).  Document: one ZIP per decision holding one XML file
(DTD ``rii-dok.dtd``).  Randnummern are ``<dl><dt><a name="rd_N">N</a></dt>``;
an empty ``<dt/>`` continues the previous Rn.
"""
from __future__ import annotations

import io
import re
import sqlite3
import xml.etree.ElementTree as ET
import zipfile

from ..casenumbers import split_case_numbers
from ..courtids import FEDERAL, priority
from ..errors import UpstreamFailure
from ..http import PoliteHttp
from ..model import Block, ParsedDecision, Section
from .base import OPERATOR_DECISION_BASIS, OPERATOR_DECISION_DATE, Adapter, Capture, Listing, iso_date, squash

BASE = "https://www.rechtsprechung-im-internet.de"
TOC_URL = BASE + "/rii-toc.xml"
ZIP_URL = BASE + "/jportal/docs/bsjrs/{doc}.zip"
PERMALINK = BASE + "/jportal/?quelle=jlink&docid={doc}&psml=bsjrsprod.psml&max=true"
DOC_RE = re.compile(r"(jb-[A-Za-z0-9]+)\.zip$")
MAX_ZIP = 64 * 1024 * 1024
MAX_XML = 128 * 1024 * 1024
COURT_KEYS = {"BVerfG": "bverfg", "BGH": "bgh", "BVerwG": "bverwg", "BFH": "bfh", "BAG": "bag",
              "BSG": "bsg", "BPatG": "bpatg"}
# RII section element -> (canonical section, authorship, basis)
SECTIONS = [
    ("titelzeile", "titel", "editorial", "RII <titelzeile>: documentation title line"),
    ("leitsatz", "leitsatz", "court", "RII <leitsatz>: official headnote of the court"),
    ("sonstosatz", "orientierungssatz", "editorial", "RII <sonstosatz>: Orientierungssatz (documentation)"),
    ("tenor", "tenor", "court", "RII <tenor>"),
    ("tatbestand", "tatbestand", "court", "RII <tatbestand>"),
    ("entscheidungsgruende", "entscheidungsgruende", "court", "RII <entscheidungsgruende>"),
    ("gruende", "gruende", "court", "RII <gruende>"),
    ("abwmeinung", "abweichende_meinung", "court", "RII <abwmeinung>: separate opinion"),
    ("sonstlt", "sonstiger_langtext", "unknown", "RII <sonstlt>: other long text"),
]
BLOCK_TAGS = {"p", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}
BREAK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "br", "li", "dd", "dl", "blockquote"}


def safe_xml(data: bytes, label: str) -> ET.Element:
    """Parse without DTD/entity expansion (the DTD declaration is removed)."""
    if re.search(rb"<!ENTITY\b", data, re.I):
        raise UpstreamFailure("unexpected_representation", f"{label}: entity declarations not allowed")
    # Remove the declaration including an internal subset ("<!DOCTYPE items [ ... ]>").
    data = re.sub(rb"<!DOCTYPE\b[^\[>]*(?:\[.*?\])?\s*>", b"", data, count=1, flags=re.I | re.S)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise UpstreamFailure("parse_error", f"{label}: invalid XML: {exc}") from exc


def text_content(node: ET.Element) -> str:
    """Inline text with newline block boundaries and tab-separated table cells."""
    def walk(el: ET.Element) -> str:
        text = el.text or ""
        for child in el:
            text += walk(child) + (child.tail or "")
        if el.tag in BREAK_TAGS:
            text += "\n"
        elif el.tag in {"td", "th"}:
            text += "\t"
        return text
    lines = []
    for line in walk(node).splitlines():
        cells = ["".join(" ".join(cell.split())) for cell in line.split("\t")]
        line = "\t".join(cells).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def section_blocks(node: ET.Element, xml_path: str) -> list[Block]:
    blocks: list[Block] = []

    def visit(el: ET.Element, path: str) -> None:
        counts: dict[str, int] = {}
        for child in el:
            counts[child.tag] = counts.get(child.tag, 0) + 1
            cpath = f"{path}/{child.tag}[{counts[child.tag]}]"
            if child.tag == "dl":
                dt, dd = child.find("dt"), child.find("dd")
                if dd is None:
                    raise UpstreamFailure("parse_error", f"<dl> without <dd> at {cpath}")
                label = squash("".join(dt.itertext())) if dt is not None else ""
                anchors = [a.get("name") for a in dt.iter("a") if a.get("name")] if dt is not None else []
                heading = len(dd) > 0 and all(c.tag in {"h1", "h2", "h3", "h4", "h5", "h6"} for c in dd)
                blocks.append(Block(text=text_content(dd), label=label or None,
                                    kind="heading" if heading else "text",
                                    locator=cpath + "/dd[1]" + (f"#{anchors[0]}" if anchors else "")))
            elif child.tag in BLOCK_TAGS:
                blocks.append(Block(text=text_content(child), locator=cpath))
            else:
                visit(child, cpath)
    visit(node, xml_path)
    return blocks


def parse_vorinstanz(node: ET.Element | None) -> list[dict]:
    """'vorgehend VG Stuttgart, 3. Februar 2015, Az: A 6 K 3840/14' -> procedural references."""
    if node is None:
        return []
    raw = text_content(node)
    out = []
    for line in re.split(r"\n+", raw):
        line = squash(line)
        m = re.match(r"(?P<dir>vorgehend|nachgehend)\s+(?P<court>.+?),\s*(?P<date>\d{1,2}\.\s*\w+\s+\d{4}),"
                     r"\s*Az:?\s*(?P<az>[^,]+?)(?:,\s*(?P<type>.+))?$", line)
        if not m:
            continue
        try:
            when = iso_date(m.group("date"))
        except UpstreamFailure:
            when = None
        out.append({"direction": "previous" if m.group("dir") == "vorgehend" else "subsequent",
                    "court": m.group("court"), "date": when, "case_number": squash(m.group("az")),
                    "decision_type": m.group("type"), "raw": line, "field": "rii:vorinstanz",
                    "origin": "editorial_metadata"})
    return out


def read_zip_xml(blob: bytes, doc: str) -> bytes:
    if not blob.startswith(b"PK") or len(blob) > MAX_ZIP:
        raise UpstreamFailure("unexpected_representation", f"{doc}: not a ZIP ({len(blob)} bytes)")
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            members = [i for i in archive.infolist() if not i.is_dir() and i.filename.lower().endswith(".xml")]
            if len(members) != 1:
                raise UpstreamFailure("parse_error", f"{doc}: expected one XML member, found {len(members)}")
            if members[0].file_size > MAX_XML:
                raise UpstreamFailure("parse_error", f"{doc}: XML too large")
            return archive.read(members[0])
    except zipfile.BadZipFile as exc:
        raise UpstreamFailure("unexpected_representation", f"{doc}: bad ZIP: {exc}") from exc


def parse_xml_decision(xml: bytes, doc: str, source_url: str) -> ParsedDecision:
    root = safe_xml(xml, doc)
    if root.tag != "dokument":
        raise UpstreamFailure("unexpected_representation", f"{doc}: root <{root.tag}> is not <dokument>")
    doknr = squash(root.findtext("doknr"))
    if doc.lower() != f"jb-{doknr}".lower():
        raise UpstreamFailure("identity_mismatch", f"{doc}: XML doknr {doknr!r}")
    court_short = squash(root.findtext("gertyp"))
    court_key = COURT_KEYS.get(court_short)
    if court_key is None:
        if court_short.lower().startswith("gemeinsamer") or court_short.startswith("GmS"):
            court_key = "gmsogb"
        else:
            raise UpstreamFailure("parse_error", f"{doc}: unknown court {court_short!r}")
    court_name, branch = FEDERAL[court_key]
    raw_az = squash(root.findtext("aktenzeichen"))
    sections = []
    for tag, name, authorship, basis in SECTIONS:
        node = root.find(tag)
        if node is None or len(node) == 0 and not (node.text or "").strip():
            continue
        blocks = section_blocks(node, f"/dokument/{tag}[1]")
        if not blocks and (node.text or "").strip():
            blocks = [Block(text=squash(node.text), locator=f"/dokument/{tag}[1]")]
        if blocks:
            sections.append(Section(name=name, authorship=authorship, authorship_basis=basis, blocks=blocks))
    norms = [squash("".join(n.itertext())) for n in root.findall("norm")]
    title = next((b.text for s in sections if s.name == "titel" for b in s.blocks), None)
    return ParsedDecision(
        source_id="rii", source_doc_id=doc, source_url=source_url, court_name=court_name,
        decision_date=iso_date(squash(root.findtext("entsch-datum"))), case_numbers_raw=raw_az,
        case_numbers=split_case_numbers(raw_az), decision_type=squash(root.findtext("doktyp")) or None,
        sections=sections, ecli=squash(root.findtext("ecli")) or None, court_id=court_key,
        court_type=court_key, jurisdiction=branch, land="de",
        chamber=squash(root.findtext("spruchkoerper")) or None, title=title,
        metadata={"norms": [n for n in norms if n], "rii_doknr": doknr,
                  "rii_identifier": squash(root.findtext("identifier")) or None,
                  "publisher": squash(root.findtext("publisher")) or None,
                  "vorinstanz_raw": squash(text_content(root.find("vorinstanz"))) if root.find("vorinstanz") is not None else None},
        procedural=parse_vorinstanz(root.find("vorinstanz")))


def parse_toc(data: bytes) -> list[dict]:
    items = []
    root = safe_xml(data, "rii-toc")
    for item in root.iter("item"):
        link = squash(item.findtext("link"))
        m = DOC_RE.search(link)
        if not m:
            continue
        court_raw = squash(item.findtext("gericht"))
        court_short = court_raw.split(" ")[0] if court_raw else ""
        court_key = COURT_KEYS.get(court_short, "gmsogb" if court_short.startswith("Gemeinsamer") else None)
        branch = FEDERAL[court_key][1] if court_key else None
        raw_date = squash(item.findtext("entsch-datum"))
        try:
            listed_date = iso_date(raw_date) if raw_date else None
        except UpstreamFailure:
            listed_date = None
        items.append({"source_doc_id": m.group(1), "url": ZIP_URL.format(doc=m.group(1)),
                      "listed_date": listed_date, "listed_court": court_key or court_short.lower(),
                      "listed_case_numbers": squash(item.findtext("aktenzeichen")),
                      "listed_modified": squash(item.findtext("modified")) or None,
                      "priority": priority(branch), "extra": {"gericht": court_raw}})
    if not items:
        raise UpstreamFailure("unexpected_representation", "RII TOC without items", url=TOC_URL)
    return items


class RiiAdapter(Adapter):
    source_id = "rii"
    label = "Rechtsprechung im Internet (BMJ/BfJ): BVerfG, BGH, BVerwG, BFH, BAG, BSG, BPatG"
    base_url = BASE
    land = "de"
    adapter_name = "rii-xml"
    robots_policy = "ignore_by_operator_decision"
    policy_decided_at = OPERATOR_DECISION_DATE
    policy_basis = OPERATOR_DECISION_BASIS
    min_interval = 1.1

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        response = http.get(TOC_URL, timeout=600)
        if b"<items" not in response.body[:4096]:
            raise UpstreamFailure("unexpected_representation", "TOC is not an <items> document", url=TOC_URL)
        items = parse_toc(response.body)
        return Listing(items=items, upstream_total=len(items), complete=True)

    def fetch(self, http: PoliteHttp, item) -> Capture:
        response = http.get(item["url"], timeout=120)
        if not response.body.startswith(b"PK"):
            raise UpstreamFailure("unexpected_representation",
                                  f"expected ZIP, got {response.content_type or 'unknown'}", url=item["url"])
        return Capture(body=response.body, url=item["url"], media_type="application/zip", extension="zip",
                       headers=response.headers)

    def parse(self, capture: Capture, item) -> ParsedDecision:
        doc = item["source_doc_id"]
        parsed = parse_xml_decision(read_zip_xml(capture.body, doc), doc, PERMALINK.format(doc=doc))
        listed_date = item["listed_date"]
        if listed_date and listed_date != parsed.decision_date:
            raise UpstreamFailure("identity_mismatch",
                                  f"{doc}: TOC date {listed_date} != XML date {parsed.decision_date}")
        listed_court = item["listed_court"]
        if listed_court in COURT_KEYS.values() and listed_court != parsed.court_id:
            raise UpstreamFailure("identity_mismatch", f"{doc}: TOC court {listed_court} != {parsed.court_id}")
        parsed.metadata["capture"] = {"zip_member": f"{doc}.xml"}
        return parsed


def adapters() -> dict[str, Adapter]:
    return {"rii": RiiAdapter()}
