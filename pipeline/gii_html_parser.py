"""Parse archived gesetze-im-internet.de (GII) full-act pages into norms.

The goal is to reproduce, from an HTML rendering, the norm texts that
``fetch_gii.parse_law`` derives from the official GII XML:

* ``enbez`` / ``titel`` from the norm heading,
* ``text`` = the flattened norm body (``textdaten/text/Content`` in XML):
  all text nodes concatenated in document order, ``[ \\t]+ -> " "``, stripped;
  footnotes, footnote references and the unofficial sentence numbers that
  some layouts inject are *not* part of the body;
* norms without a designator are skipped, pseudo-norms with a designator
  (``Inhaltsübersicht``) are kept, exactly like ``parse_law``.

Layouts handled (fingerprinted on real Wayback captures 2006-2026):

``juris-table-2006``   pre-``jnnorm`` pages (bundesrecht.juris.de and
                       early gesetze-im-internet.de): heading ``<div>`` with
                       an ``<a name=..BJNE..>`` anchor and bold ``<p>`` lines,
                       body in ``<table>`` cells, ``(+++ Stand: … +++)``.
``jnnorm-2006``        ``div.jnnorm`` blocks, body in ``div.jnhtml`` with
                       ``<p>``; stand in ``div.kommentar_Stand``.
``jnnorm-2008``        adds ``div.jurAbsatz`` and ``table.standangaben``.
``jnnorm-2022``        same markup inside ``#content_2022``.

Uncertain decisions are reported in ``warnings`` and per norm in ``flags``;
a truncated page or a page without norms yields ``complete: false``.

``parse_gii_xml_zip`` handles archived ``xml.zip`` packages by delegating to
``fetch_gii.parse_law`` so XML-derived texts are byte-identical to the daily
GII states.
"""
from __future__ import annotations

import io
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
from html.parser import HTMLParser
from pathlib import Path

PARSER_VERSION = "gii-html/1"
XML_PARSER_VERSION = "gii-xml/1"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}
SKIP_TAGS = {"script", "style", "noscript", "head", "title"}
# Classes whose subtree is never norm body text.
SKIP_CLASSES = {"jnfussnote", "footnotes", "FnR", "Footnote", "Rec",
                "nichtamtlich", "jnheader"}
TOC_LABELS = {"Inhaltsübersicht", "Inhaltsverzeichnis", "Inhalt"}
RANGE_ENBEZ = re.compile(r"^(?:§§|Art\.?|Artikel)\s.*\b(?:bis|und)\b")
KNOWN_STAND_TYPES = {"Stand", "Neuf", "Hinweis", "Sonst", "Ersetzt",
                     "Aufh", "Aufhebung", "Änderung", "Fussnote"}


# ---------------------------------------------------------------- tiny DOM

class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None,
                 parent: "Node | None" = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []
        self.parent = parent

    @property
    def classes(self) -> set[str]:
        return set((self.attrs.get("class") or "").split())

    def get(self, key: str, default: str = "") -> str:
        return self.attrs.get(key) or default

    def elements(self) -> list["Node"]:
        return [c for c in self.children if isinstance(c, Node)]

    def iter(self, tag: str | None = None):
        stack = [self]
        while stack:
            node = stack.pop()
            if tag is None or node.tag == tag:
                yield node
            stack.extend(reversed(node.elements()))

    def find(self, tag: str | None = None, cls: str | None = None,
             **attrs) -> "Node | None":
        for node in self.iter(tag):
            if node is self:
                continue
            if cls and cls not in node.classes:
                continue
            if any(node.attrs.get(k) != v for k, v in attrs.items()):
                continue
            return node
        return None

    def find_all(self, tag: str | None = None, cls: str | None = None):
        for node in self.iter(tag):
            if node is self:
                continue
            if cls and cls not in node.classes:
                continue
            yield node

    def raw_text(self) -> str:
        parts: list[str] = []
        stack: list[Node | str] = [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            elif item.tag not in SKIP_TAGS:
                stack.extend(reversed(item.children))
        return "".join(parts)


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        node = Node(tag, {k: (v or "") for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return
        # stray end tag: ignore (browsers do the same)

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def build_tree(html: str) -> tuple[Node, bool]:
    """Return ``(root, closed)``; ``closed`` is False for truncated markup."""
    builder = _TreeBuilder()
    builder.feed(html)
    builder.close()
    closed = bool(re.search(r"</html\s*>\s*$", html[-4096:], re.I))
    return builder.root, closed


def decode_html(data: bytes) -> str:
    head = data[:2048].decode("ascii", "replace").lower()
    match = re.search(r"charset=[\"']?([a-z0-9_\-]+)", head)
    charset = match.group(1) if match else "iso-8859-1"
    if charset in {"iso-8859-1", "latin-1", "latin1", "us-ascii", "ascii"}:
        charset = "cp1252"          # WHATWG: the iso-8859-1 label is cp1252
    try:
        return data.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return data.decode("cp1252", "replace")


# ------------------------------------------------------------ text flatten

def _skip(node: Node) -> bool:
    if node.tag in SKIP_TAGS:
        return True
    classes = node.classes
    return bool(classes & SKIP_CLASSES)


def flatten(nodes, *, in_pre: bool = False) -> str:
    """Concatenate body text like ``ET.tostring(method="text")``."""
    out: list[str] = []

    def walk(item, pre: bool) -> None:
        if isinstance(item, str):
            text = item.replace("\r\n", "\n").replace("\r", "\n")
            if not pre:
                text = re.sub(r"\s*\n\s*", " ", text)
            out.append(text)
            return
        if _skip(item):
            return
        pre = pre or item.tag == "pre"
        for child in item.children:
            walk(child, pre)

    for node in nodes:
        walk(node, in_pre)
    return re.sub(r"[ \t]+", " ", "".join(out)).strip()


def clean_label(text: str) -> str:
    text = text.replace("\r", " ").replace("\n", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


# ------------------------------------------------------------------ stand

def _stand_type(label: str, text: str) -> str:
    base = label.strip().rstrip(":").strip()
    if base == "Stand" and re.match(r"(?i)neugefasst\b", text):
        return "Neuf"
    if base in {"Sonstiges", "Sonstige"}:
        return "Sonst"
    return base or "Stand"


def stand_record(entries: list[dict]) -> dict:
    raw = " | ".join(f"{e['type']}: {e['text']}" for e in entries)
    return {"raw": raw, "entries": entries}


def _stand_from_table(table: Node) -> list[dict]:
    entries: list[dict] = []
    label = ""
    for row in table.find_all("tr"):
        cells = [c for c in row.elements() if c.tag == "td"]
        if len(cells) == 1:
            # colspan row without a label: GII's "Sonst" standangabe
            text = clean_label(cells[0].raw_text()).replace("\xa0", " ").strip()
            if text:
                entries.append({"type": "Sonst", "text": text})
            continue
        if len(cells) < 2:
            continue
        head = clean_label(cells[0].raw_text()).replace("\xa0", " ").strip()
        text = clean_label(cells[1].raw_text()).replace("\xa0", " ").strip()
        css = next((c[len("Standangabe"):] for c in cells[0].classes
                    if c.startswith("Standangabe")), "")
        if head:
            label = head
        elif css:
            label = css
        if not text:
            continue
        entries.append({"type": _stand_type(label or css, text), "text": text})
    return entries


def text_with_breaks(node: Node) -> str:
    """Raw text with ``<br>`` as a line break (for Stand blocks only)."""
    parts: list[str] = []

    def walk(item) -> None:
        if isinstance(item, str):
            parts.append(item)
        elif item.tag == "br":
            parts.append("\n")
        elif item.tag not in SKIP_TAGS:
            for child in item.children:
                walk(child)

    walk(node)
    return "".join(parts)


def _typed_entries(label: str, value: str) -> list[dict]:
    entries = []
    for part in re.split(r"\s*\n\s*|\s{3,}", value.strip()):
        text = clean_label(part.replace("\xa0", " "))
        if text:
            entries.append({"type": _stand_type(label, text), "text": text})
    return entries


STAND_PLUS = re.compile(r"\(\+\+\+\s*([^:+]+?)\s*:\s*(.*?)\s*\+\+\+\)", re.S)


def _stand_from_text(text: str) -> list[dict]:
    entries = []
    for match in STAND_PLUS.finditer(text):
        label = match.group(1).strip()
        if label not in KNOWN_STAND_TYPES:
            continue
        # continuation lines of a pre block are indented: one entry each
        entries.extend(_typed_entries(label, match.group(2)))
    return entries


# ---------------------------------------------------------------- layouts

def _jnnorm_blocks(root: Node) -> list[Node]:
    return [n for n in root.iter("div") if "jnnorm" in n.classes]


def leading_text(node: Node) -> str:
    """Text before the first child element, like ``Element.findtext``.

    ``parse_law`` reads ``<titel>`` with ``findtext`` which stops at the
    first child element (typically ``<BR/>``); GII HTML renders the whole
    title, so the HTML side has to cut at the same place."""
    parts = []
    for child in node.children:
        if not isinstance(child, str):
            break
        parts.append(child)
    return clean_label("".join(parts))


def _heading_parts(header: Node) -> tuple[str, str, list[str]]:
    flags: list[str] = []
    enbez = header.find("span", cls="jnenbez")
    titel = header.find("span", cls="jnentitel")
    enbez_text = leading_text(enbez) if enbez else ""
    titel_text = ""
    if titel is not None:
        titel_text = leading_text(titel)
        if titel.elements() and clean_label(titel.raw_text()) != titel_text:
            flags.append("titel_cut_at_child_element")
    if titel_text == "----":           # GII placeholder for a missing <titel>
        titel_text = ""
        flags.append("titel_placeholder_dropped")
    return enbez_text, titel_text, flags


def _body_nodes(block: Node) -> list[Node | str]:
    """Children of a ``div.jnnorm`` that make up the norm body."""
    body = []
    for child in block.children:
        if isinstance(child, Node) and ("jnheader" in child.classes
                                        or "jnfussnote" in child.classes):
            continue
        body.append(child)
    return body


def _first_content_element(block: Node) -> Node | None:
    html = block.find("div", cls="jnhtml")
    node = html
    while node is not None:
        elements = [e for e in node.elements() if e.tag not in {"br"}]
        if not elements:
            return None
        first = elements[0]
        if first.tag == "div" and not first.classes and len(elements) == 1:
            node = first
            continue
        return first
    return None


def _looks_like_toc_element(block: Node) -> bool:
    """GII renders an XML ``<TOC>`` (text-less in ``parse_law``) with
    centred ``Ident``/``Title`` divs that hold plain text; a TOC typed as
    ordinary ``Content`` starts with a ``jurAbsatz`` table instead."""
    first = _first_content_element(block)
    if first is None or first.tag != "div":
        return False
    style = first.get("style").replace(" ", "")
    ident_class = any(re.fullmatch(r"S\d", c) for c in first.classes)
    if "text-align:center" not in style and not ident_class:
        return False
    return all(isinstance(c, str) or c.tag == "br" for c in first.children)


def _is_empty_body(nodes) -> bool:
    return not flatten(nodes)


def _xxxx(enbez: str, flags: list[str]) -> str:
    """GII XML prefixes range pseudo-norms ("§§ 3 bis 6") with "(XXXX) "."""
    if RANGE_ENBEZ.match(enbez):
        flags.append("range_enbez_xxxx_prefixed")
        return f"(XXXX) {enbez}"
    return enbez


def _parse_jnnorm(root: Node, record: dict) -> None:
    gliederung = ""
    blocks = _jnnorm_blocks(root)
    for block in blocks:
        kind = block.get("title")
        header = next((c for c in block.elements() if "jnheader" in c.classes),
                      None)
        if kind == "Rahmen":
            record["doknr"] = block.get("id") or record.get("doknr")
            if header is not None:
                langue = header.find("span", cls="jnlangue")
                if langue is not None:
                    record["title"] = clean_label(langue.raw_text())
                for p in header.find_all("p"):
                    value = clean_label(p.raw_text())
                    if value and not re.match(
                            r"(?i)(ausfertigungsdatum|geltung ab|vollzitat)",
                            value):
                        record["jurabk"] = value
                        break
            entries: list[dict] = []
            for child in block.elements():
                if "jnfussnote" in child.classes or "jnheader" in child.classes:
                    continue
                for table in child.find_all("table", cls="standangaben"):
                    entries.extend(_stand_from_table(table))
                for div in child.iter("div"):
                    for cls in div.classes:
                        if cls.startswith("kommentar_"):
                            text = text_with_breaks(div).replace("\xa0", " ")
                            match = re.match(r"\s*([^:\n]+):\s*(.*)", text, re.S)
                            if match and match.group(1).strip() in KNOWN_STAND_TYPES:
                                entries.extend(_typed_entries(
                                    match.group(1).strip(), match.group(2)))
            if entries:
                record["stand"] = stand_record(entries)
            continue
        if kind == "Gliederung":
            h2 = header.find("h2") if header is not None else None
            if h2 is not None:
                spans = [clean_label(s.raw_text()) for s in h2.find_all("span")]
                spans = [s for s in spans if s]
                gliederung = " ".join(spans) if spans else clean_label(h2.raw_text())
            continue
        if header is None:
            record["warnings"].append(
                f"jnnorm block {block.get('id')} without header skipped")
            continue
        enbez, titel, flags = _heading_parts(header)
        if not enbez:
            continue                         # like parse_law: no enbez, no norm
        body = _body_nodes(block)
        if enbez in TOC_LABELS and _looks_like_toc_element(block):
            text = ""
            flags.append("toc_element_text_suppressed")
        else:
            text = flatten(body)
        if not text and _is_empty_body(body):
            enbez = _xxxx(enbez, flags)
        elif RANGE_ENBEZ.match(enbez):
            enbez = _xxxx(enbez, flags)
        norm = {"enbez": enbez, "titel": titel, "text": text,
                "gliederung": gliederung, "doknr": block.get("id")}
        if flags:
            norm["flags"] = flags
        record["norms"].append(norm)


def _bold_lines(div: Node) -> list[str]:
    lines = []
    for p in div.find_all("p"):
        style = p.get("style").replace(" ", "").lower()
        if "font-weight:bold" in style:
            lines.append(clean_label(p.raw_text()))
    return lines


def _parse_table_layout(root: Node, record: dict) -> None:
    container = (root.find("div", id="paddingLR12")
                 or root.find("div", id="container") or root.find("body"))
    if container is None:
        record["warnings"].append("no content container found")
        return
    gliederung = ""
    current: dict | None = None
    current_nodes: list[Node] = []
    stand_text: list[str] = []
    seen_first_heading = False

    def finish() -> None:
        nonlocal current, current_nodes
        if current is not None:
            flags = current.setdefault("flags", [])
            text = flatten(current_nodes)
            # list numbers sit in their own cell followed by two nbsp
            text = re.sub(r"(\S)\xa0\xa0(?=\S)", r"\1", text)
            if not text:
                current["enbez"] = _xxxx(current["enbez"], flags)
            elif RANGE_ENBEZ.match(current["enbez"]):
                current["enbez"] = _xxxx(current["enbez"], flags)
            current["text"] = text
            flags.append("legacy_table_layout")
            record["norms"].append(current)
        current, current_nodes = None, []

    for child in container.elements():
        if child.tag in SKIP_TAGS or child.tag in {"a", "br", "hr"}:
            continue
        if "jnzitat" in child.classes:
            continue
        if child.tag != "div":
            # bare tables/paragraphs: Stand block before the first heading,
            # norm body after it
            if not seen_first_heading:
                stand_text.append(text_with_breaks(child))
            elif current is not None:
                current_nodes.append(child)
            continue
        anchor = next((a for a in child.find_all("a") if a.get("name")), None)
        name = anchor.get("name") if anchor is not None else ""
        if "BJNE" in name or "BJNG" in name:
            seen_first_heading = True
            lines = [l for l in _bold_lines(child)]
            if "BJNG" in name:
                finish()
                gliederung = " ".join(l for l in lines if l)
                continue
            finish()
            enbez = lines[0] if lines else ""
            titel = lines[1] if len(lines) > 1 else ""
            if not enbez:
                current = None
                continue
            current = {"enbez": enbez, "titel": titel, "text": "",
                       "gliederung": gliederung, "doknr": name}
            continue
        if not seen_first_heading:
            h4 = child.find("h4")
            if h4 is not None:
                record["jurabk"] = clean_label(h4.raw_text())
                lines = _bold_lines(child)
                if lines:
                    record["title"] = lines[0]
            stand_text.append(text_with_breaks(child))
            continue
        if current is not None:
            current_nodes.append(child)
    finish()
    entries = _stand_from_text("\n".join(stand_text))
    if entries:
        record["stand"] = stand_record(entries)


def detect_layout(html: str) -> str:
    low = html.lower()
    if 'class="jnnorm"' in low:
        if "content_2022" in low:
            return "jnnorm-2022"
        if 'class="jurabsatz"' in low or "standangaben" in low:
            return "jnnorm-2008"
        return "jnnorm-2006"
    if "nichtamtliches inhaltsverzeichnis" in low and "bjne" in low:
        return "juris-table-2006"
    return "unknown"


def parse_gii_html(data: bytes) -> dict:
    """Parse one archived GII full-act HTML page (bytes as captured)."""
    html = decode_html(data)
    root, closed = build_tree(html)
    layout = detect_layout(html)
    record = {"parser": PARSER_VERSION, "layout": layout, "jurabk": "",
              "title": "", "builddate": None, "doknr": "",
              "stand": {"raw": "", "entries": []}, "complete": True,
              "norms": [], "warnings": []}
    title = root.find("title")
    if title is not None:
        record["page_title"] = clean_label("".join(
            c for c in title.children if isinstance(c, str)))
    if layout.startswith("jnnorm"):
        _parse_jnnorm(root, record)
    elif layout == "juris-table-2006":
        _parse_table_layout(root, record)
    else:
        record["warnings"].append("unrecognised page layout")
    if not closed:
        record["complete"] = False
        record["warnings"].append("page truncated: no closing </html>")
    if not record["norms"]:
        record["complete"] = False
        record["warnings"].append("no norms parsed")
    if not record["stand"]["entries"]:
        record["warnings"].append("no Stand block found")
    if not record["jurabk"] and record.get("page_title"):
        record["jurabk"] = record["page_title"].split(" - ")[0].strip()
    labels = [n["enbez"] for n in record["norms"]]
    dupes = sorted({l for l in labels if labels.count(l) > 1})
    if dupes:
        record["warnings"].append(
            "duplicate designators: " + ", ".join(dupes[:10]))
    record["norm_count"] = len(record["norms"])
    return record


# --------------------------------------------------------------- xml.zip

def _import_parse_law():
    here = Path(__file__).resolve().parent
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    from fetch_gii import parse_law  # noqa: WPS433 — heavy imports on demand
    return parse_law


def parse_gii_xml_zip(data: bytes, slug: str) -> dict:
    """Parse an archived GII ``xml.zip`` with ``fetch_gii.parse_law``."""
    parse_law = _import_parse_law()
    record = {"parser": XML_PARSER_VERSION, "layout": "gii-xml",
              "jurabk": "", "title": "", "builddate": None, "doknr": "",
              "stand": {"raw": "", "entries": []}, "complete": True,
              "norms": [], "warnings": []}
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [m for m in archive.infolist()
                       if m.filename.lower().endswith(".xml")]
            if not members:
                raise ValueError("no XML member in package")
            if len(members) > 1:
                record["warnings"].append(
                    f"{len(members)} XML members; parsed {members[0].filename}")
            xml_bytes = archive.read(members[0])
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        record["complete"] = False
        record["warnings"].append(f"unreadable package: {exc}")
        record["norm_count"] = 0
        return record
    try:
        act, norms = parse_law(xml_bytes, slug)
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        record["complete"] = False
        record["warnings"].append(f"XML parse error: {exc}")
        record["norm_count"] = 0
        return record
    entries = []
    for norm in root.findall("norm"):
        meta = norm.find("metadaten")
        if meta is not None and (meta.findtext("jurabk") or "").strip():
            for st in meta.findall(".//standangabe"):
                k = (st.findtext("standtyp") or "").strip()
                v = (st.findtext("standkommentar") or "").strip()
                if k and v:
                    entries.append({"type": k, "text": v})
            break
    record.update({
        "jurabk": act["jurabk"], "title": act["long_title"],
        "builddate": act["builddate"] or None, "doknr": act["doknr"],
        "stand": {"raw": act["stand"], "entries": entries},
    })
    record["norms"] = [{"enbez": n["enbez"], "titel": n["titel"],
                        "text": n["text"], "gliederung": n["gliederung"],
                        "doknr": n["doknr"]} for n in norms]
    record["norm_count"] = len(record["norms"])
    if not record["norms"]:
        record["complete"] = False
        record["warnings"].append("no norms parsed")
    return record
