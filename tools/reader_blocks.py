"""Export optional current-reader structure without changing canonical text.

Run explicitly, for example:
  python -m tools.reader_blocks --data web/data --out data/reader \
      fed_asylvfg_1992 fed_aufenthg_2004 fed_sgb_12 fed_sgb_9_2018

Only matching current indexed norms are exported. API reads never fetch sources.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.common import Http

TAGS = frozenset({
    "p", "div", "ul", "ol", "li", "dl", "dt", "dd", "table", "thead",
    "tbody", "tr", "td", "th", "blockquote", "h1", "h2", "h3", "h4",
    "h5", "h6", "br", "sup", "sub", "strong", "em", "u", "s", "caption", "small",
    "img", "source-file", "pre",
})
INLINE_TAGS = frozenset({"br", "sup", "sub", "strong", "em", "u", "s", "small", "img"})
DROP = frozenset({"script", "style", "iframe", "object", "embed"})
MAX_BYTES = 40 * 1024 * 1024
MEDIA_URL = re.compile(r"https://www\.gesetze-im-internet\.de/normengrafiken/bgbl[12]_\d{4}/[A-Za-z0-9_-]+\.(?:jpg|png|gif|pdf)")


class ReaderBlocksError(ValueError):
    pass


def _media_url(source: str, *, pdf: bool = False) -> str:
    extension = "pdf" if pdf else "(?:jpg|png|gif)"
    match = re.fullmatch(rf"(bgbl[12]_\d{{4}})_([A-Za-z0-9_-]+\.{extension})", source)
    if match is None:
        raise ReaderBlocksError("unsupported official media source")
    return f"https://www.gesetze-im-internet.de/normengrafiken/{match[1]}/{match[2]}"


def source_url(act_id: str, official_url: str | None = None) -> str:
    if not isinstance(act_id, str) or not re.fullmatch(r"(?:fed|by)_[a-z0-9][a-z0-9_-]*", act_id):
        raise ReaderBlocksError("unsupported act id")
    if act_id.startswith("by_"):
        if not isinstance(official_url, str) or not re.fullmatch(
                r"https://www\.gesetze-bayern\.de/Content/Document/[A-Za-z0-9][A-Za-z0-9_-]*", official_url):
            raise ReaderBlocksError("invalid Bavarian official source URL")
        return official_url.replace("/Document/", "/Zip/")
    if official_url is not None:
        if not isinstance(official_url, str) or not re.fullmatch(
                r"https://www\.gesetze-im-internet\.de/[a-z0-9][a-z0-9_-]*/", official_url):
            raise ReaderBlocksError("invalid official source URL")
        return official_url + "xml.zip"
    slug = {"rustag": "stag", "azrg_dv": "azrg-dv", "vwdg_dv": "vwdg-dv"}.get(
        act_id[4:], act_id[4:])
    return f"https://www.gesetze-im-internet.de/{slug}/xml.zip"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical_text(text: str, act_id: str = "fed_") -> str:
    # Match pipeline.fetch_gii.strip_tags exactly, including retained newlines.
    if act_id.startswith("by_"):
        return " ".join(text.split())
    return re.sub(r"[ \t]+", " ", text).strip()


def _paragraph_nodes(children: list[dict]) -> list[dict]:
    # GII permits lists/tables inside P; HTML does not. Split at block borders
    # without changing a single text character or its original order.
    result, pending = [], []
    for node in children:
        if node.get("type") == "element" and node.get("tag") not in INLINE_TAGS:
            if pending:
                result.append({"type": "element", "tag": "p", "children": pending})
                pending = []
            result.append(node)
        else:
            pending.append(node)
    if pending:
        result.append({"type": "element", "tag": "p", "children": pending})
    return result


def _children(element: ET.Element, depth: int = 0, *, columns: dict | None = None,
              heading: bool = False) -> list[dict]:
    if depth > 64:
        raise ReaderBlocksError("XML nesting too deep")
    nodes = []
    if element.text:
        nodes.append({"type": "text", "text": element.text})
    for child in element:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag not in DROP:
            child_columns = columns
            if tag == "tgroup":
                child_columns = {entry.get("colname"): index for index, entry
                                 in enumerate(child.findall("colspec"), 1)}
            children = _children(child, depth + 1, columns=child_columns,
                                 heading=heading or tag == "thead")
            # GII Title is a heading except when it labels its direct table.
            # Titles inside cells still head their own prose, and a nested
            # table gets its own caption rather than inheriting the outer one.
            if tag == "title" and element.tag.rsplit("}", 1)[-1].lower() == "table":
                tag = "caption"
            tag = {"row": "tr", "entry": "th" if heading else "td", "b": "strong",
                   "i": "em", "strike": "s", "toc": "div", "title": "h4",
                   "subtitle": "h5", "ident": "h4", "file": "source-file"}.get(tag, tag)
            if tag in TAGS:
                if tag == "p":
                    nodes.extend(_paragraph_nodes(children))
                    if child.tail:
                        nodes.append({"type": "text", "text": child.tail})
                    continue
                node = {"type": "element", "tag": tag, "children": children}
                if (tag == "sup" and child.get("class") == "Rec"
                        and re.fullmatch(r"[0-9]+", "".join(child.itertext()))):
                    node["attrs"] = {"role": "sentence"}
                if tag in {"img", "source-file"}:
                    attrs = {"src": _media_url(child.get("SRC", ""), pdf=tag == "source-file")}
                    if tag == "img":
                        attrs["alt"] = child.get("alt", "")[:2000]
                        for name in ("width", "height"):
                            raw = child.get(name.title(), "")
                            if raw.isascii() and raw.isdecimal() and 1 <= int(raw) <= 10000:
                                attrs[name] = int(raw)
                    node["attrs"] = attrs
                if tag in {"td", "th"}:
                    attrs = {}
                    for name in ("colspan", "rowspan"):
                        raw = child.get(name, "")
                        if raw.isascii() and raw.isdecimal() and 1 <= int(raw) <= 1000:
                            attrs[name] = int(raw)
                    if child.tag.lower() == "entry":
                        start = (columns or {}).get(child.get("namest"))
                        end = (columns or {}).get(child.get("nameend"))
                        if start is not None and end is not None and 1 <= end - start + 1 <= 1000:
                            attrs["colspan"] = end - start + 1
                        more = child.get("morerows", "")
                        if more.isascii() and more.isdecimal() and 0 <= int(more) < 1000:
                            attrs["rowspan"] = int(more) + 1
                    if attrs:
                        node["attrs"] = attrs
                nodes.append(node)
            else:
                nodes.extend(children)
        if child.tail:
            nodes.append({"type": "text", "text": child.tail})
    return nodes


def flatten_nodes(nodes: list[dict], depth: int = 0) -> str:
    if depth > 64 or not isinstance(nodes, list):
        raise ReaderBlocksError("invalid reader nodes")
    result = []
    for node in nodes:
        if not isinstance(node, dict):
            raise ReaderBlocksError("invalid reader node")
        if node.get("type") == "text":
            if set(node) != {"type", "text"} or not isinstance(node["text"], str):
                raise ReaderBlocksError("invalid text node")
            result.append(node["text"])
        elif node.get("type") == "element":
            if set(node) - {"type", "tag", "children", "attrs"} or not isinstance(node.get("tag"), str) or node["tag"] not in TAGS:
                raise ReaderBlocksError("unsafe reader element")
            attrs = node.get("attrs", {})
            if not isinstance(attrs, dict) or (attrs and node["tag"] not in {"td", "th", "sup", "small", "img", "source-file"}):
                raise ReaderBlocksError("unsafe reader attributes")
            if node["tag"] in {"img", "source-file"}:
                url = attrs.get("src")
                if not isinstance(url, str) or MEDIA_URL.fullmatch(url) is None:
                    raise ReaderBlocksError("unsafe reader media source")
                if node["tag"] == "img" and url.endswith(".pdf") or node["tag"] == "source-file" and not url.endswith(".pdf"):
                    raise ReaderBlocksError("reader media type mismatch")
            for name, value in attrs.items():
                if node["tag"] in {"img", "source-file"} and name == "src":
                    continue
                if node["tag"] == "img" and name == "alt" and isinstance(value, str) and len(value) <= 2000:
                    continue
                if node["tag"] == "img" and name in {"width", "height"} and type(value) is int and 1 <= value <= 10000:
                    continue
                if name == "role" and isinstance(value, str) and ((node["tag"] == "sup" and value in {"sentence", "footnote"}) or
                                       (node["tag"] == "small" and value == "footnote")):
                    continue
                if node["tag"] not in {"td", "th"} or name not in {"colspan", "rowspan"} or type(value) is not int or not 1 <= value <= 1000:
                    raise ReaderBlocksError("unsafe reader attribute")
            result.append(flatten_nodes(node.get("children"), depth + 1))
            if node["tag"] in {"br", "img"} and node["children"]:
                raise ReaderBlocksError("void reader element has children")
        else:
            raise ReaderBlocksError("invalid reader node type")
    return "".join(result)


def _current_norms(act: dict) -> dict[str, str]:
    source_url(act.get("id"), act.get("permalink") if act.get("id", "").startswith("by_") else act.get("source_url"))
    result, ambiguous = {}, set()
    for norm in act.get("norms", []):
        name, text = norm.get("enbez"), norm.get("text")
        if not isinstance(name, str) or not isinstance(text, str):
            raise ReaderBlocksError("invalid current norm")
        if not name:
            continue
        if name in result:
            ambiguous.add(name)
        result[name] = text
    for name in ambiguous:
        del result[name]
    if not act.get("norms"):
        raise ReaderBlocksError("no current norms")
    return result


def _matching_index(act: dict, unique: dict, name: str, source_text: str,
                    *, html: bool = False) -> tuple[str, str] | None:
    """Find one exact record when its label cannot safely be used as a key."""
    if name in unique:
        return None
    normalize = (lambda value: re.sub(r"\s", "", value)) if html else (lambda value: value)
    matches = [(str(index), norm["text"]) for index, norm in enumerate(act["norms"])
               if norm["enbez"] == name and normalize(norm["text"]) == normalize(source_text)]
    return matches[0] if len(matches) == 1 else None


def _annotation_fields(norm: dict) -> dict:
    """Validate the optional source notes independently from canonical body text."""
    fields = ("source_annotations", "source_annotations_text", "source_annotations_sha256")
    if not any(key in norm for key in fields):
        return {}
    nodes = norm.get("source_annotations")
    text = norm.get("source_annotations_text")
    if not isinstance(text, str) or len(text) > 4_000_000:
        raise ReaderBlocksError("invalid source annotation text")
    if flatten_nodes(nodes) != text or norm.get("source_annotations_sha256") != text_hash(text):
        raise ReaderBlocksError("source annotation integrity mismatch")
    pending = list(nodes)
    count = 0
    while pending:
        node = pending.pop()
        count += 1
        if count > 50_000:
            raise ReaderBlocksError("source annotations contain too many nodes")
        if node["type"] == "element":
            pending.extend(node["children"])
    return {key: norm[key] for key in fields}


def _gii_annotations(norm: ET.Element) -> dict:
    """Keep both GII note representations without changing the norm's Content."""
    blocks, expected = [], []
    for note in norm.findall("textdaten/text/Footnotes/Footnote"):
        children = []
        label = (note.get("FnZ") or "") + (note.get("Postfix") or "")
        if label:
            marker = {"type": "text", "text": label}
            if note.get("Pos") == "exp":
                marker = {"type": "element", "tag": "sup", "attrs": {"role": "footnote"},
                          "children": [marker]}
            children.extend([marker, {"type": "text", "text": " "}])
        children.extend(_children(note))
        blocks.append({"type": "element", "tag": "div", "children": _paragraph_nodes(children)})
        expected.append((label + " " if label else "") + "".join(note.itertext()))
    # GII keeps application and constitutional notes separately from Footnotes.
    # Only a labelled source record may carry these norm-specific notes; act
    # and section header metadata must not migrate onto a neighbouring norm.
    if (norm.findtext("metadaten/enbez") or "").strip():
        for content in norm.findall("textdaten/fussnoten/Content"):
            text = "".join(content.itertext())
            if not text.strip():
                continue
            blocks.append({"type": "element", "tag": "div", "children": _paragraph_nodes(_children(content))})
            expected.append(text)
    if not blocks:
        return {}
    text = "".join(expected)
    return _annotation_fields({"source_annotations": blocks, "source_annotations_text": text,
                               "source_annotations_sha256": text_hash(text)})


def build_sidecar(xml_bytes: bytes, act: dict, *, retrieved_at: str) -> dict:
    current = _current_norms(act)
    if len(xml_bytes) > MAX_BYTES or b"<!ENTITY" in xml_bytes.upper():
        raise ReaderBlocksError("unsupported XML document")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ReaderBlocksError("invalid XML document") from exc
    if root.tag != "dokumente":
        raise ReaderBlocksError("unexpected GII document root")
    norms, seen, indexed, seen_indices = {}, set(), {}, set()
    for norm in root.findall("norm"):
        name = (norm.findtext("metadaten/enbez") or "").strip()
        if name in seen:
            norms.pop(name, None)
        duplicate = name in seen
        seen.add(name)
        content = norm.find("textdaten/text/Content")
        if content is None:
            continue
        original = _canonical_text(ET.tostring(content, encoding="unicode", method="text"))
        indexed_target = _matching_index(act, current, name, original)
        unique_match = name in current and original == current[name] and not duplicate
        if not unique_match and indexed_target is None:
            continue
        nodes = _children(content)
        if _canonical_text(flatten_nodes(nodes)) != original:
            continue
        value = {"text_sha256": text_hash(original), "blocks": nodes, **_gii_annotations(norm)}
        if unique_match:
            norms[name] = value
        if indexed_target is not None:
            index, _ = indexed_target
            if index in seen_indices:
                indexed.pop(index, None)
            else:
                indexed[index] = {"enbez": name, **value}
            seen_indices.add(index)
    return {
        "schema_version": 1, "act_id": act["id"],
        "source_url": source_url(act["id"], act.get("source_url")), "retrieved_at": retrieved_at,
        "norms": norms, "norms_by_index": indexed,
    }


def _exact_whitespace(nodes: list[dict], target: str) -> list[dict]:
    """Put the pipeline's whitespace into text leaves without moving letters.

    The Bavarian pipeline collapses each top-level norm part, then joins those
    parts with newlines. This normalizes only text leaves and retains their
    position in the source tree, including inline superscripts.
    """
    result = copy.deepcopy(nodes)
    cursor = 0
    leaves = []

    def visit(items):
        nonlocal cursor
        for node in items:
            if node["type"] == "element":
                visit(node["children"])
                continue
            leaves.append(node)
            text = []
            for character in node["text"]:
                if character.isspace():
                    continue
                while cursor < len(target) and target[cursor].isspace():
                    text.append(target[cursor])
                    cursor += 1
                if cursor >= len(target) or target[cursor] != character:
                    raise ReaderBlocksError("source text changed during normalization")
                text.append(character)
                cursor += 1
            node["text"] = "".join(text)

    visit(result)
    if target[cursor:].strip():
        raise ReaderBlocksError("source text is incomplete")
    if leaves:
        leaves[-1]["text"] += target[cursor:]
    if flatten_nodes(result) != target:
        raise ReaderBlocksError("source text differs after normalization")
    def compact(items):
        output = []
        for node in items:
            if node["type"] == "element":
                node["children"] = compact(node["children"])
            elif not node["text"]:
                continue
            output.append(node)
        return output
    return compact(result)


def _bayern_tree(element: ET.Element) -> ET.Element:
    """Translate explicit BY XML block/list structure into safe HTML semantics.

    Unknown source wrappers are retained until the common safe converter strips
    the wrapper while preserving its text. No sentence numbering is inferred.
    """
    result = copy.deepcopy(element)

    def visit(node, depth=0):
        if depth > 64:
            raise ReaderBlocksError("XML nesting too deep")
        original = node.tag.rsplit("}", 1)[-1].lower()
        labelled_list = any(child.tag.lower() == "li" and child.find("symbol") is not None
                            for child in node)
        for child in list(node):
            visit(child, depth + 1)
        node.tag = {"jurabsatz": "div", "absatz": "p", "liste": "dl",
                    "hoch": "sup", "tief": "sub", "fett": "strong",
                    "kursiv": "em", "unterstrichen": "u"}.get(original, original)
        if labelled_list:
            node.tag = "dl"
            for item in node:
                symbols = [child for child in item if child.tag == "symbol"]
                if item.tag != "li" or len(symbols) != 1:
                    continue
                symbol = symbols[0]
                if list(item).index(symbol) != 0:
                    continue
                item.tag = "list-item-wrapper"
                symbol.tag = "dt"
                definition = ET.Element("dd")
                definition.text, symbol.tail = symbol.tail, None
                for child in list(item)[1:]:
                    item.remove(child)
                    definition.append(child)
                item.append(definition)

    visit(result)
    return result


def build_bayern_sidecar(xml_bytes: bytes, act: dict, *, retrieved_at: str) -> dict:
    """Build BY structure only for text identical to the indexed source edition."""
    current = _current_norms(act)
    if not act["id"].startswith("by_") or len(xml_bytes) > MAX_BYTES or b"<!ENTITY" in xml_bytes.upper():
        raise ReaderBlocksError("unsupported Bavarian XML document")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ReaderBlocksError("invalid Bavarian XML document") from exc
    if root.tag != "byrecht-norm":
        raise ReaderBlocksError("unexpected Bavarian document root")
    norms, seen, indexed, seen_indices = {}, set(), {}, set()
    for norm in root.iter("einzelnorm"):
        name = " ".join((norm.findtext("para.nr") or "").split())
        if name in seen:
            norms.pop(name, None)
        duplicate = name in seen
        seen.add(name)
        parts = [child for child in norm if child.tag not in {"para.nr", "para.titel"}]
        original = "\n".join(text for child in parts
                             if (text := " ".join(ET.tostring(child, encoding="unicode", method="text").split())))
        indexed_target = _matching_index(act, current, name, original)
        unique_match = name in current and original == current[name] and not duplicate
        if not unique_match and indexed_target is None:
            continue
        container = ET.Element("Content")
        for child in parts:
            container.append(_bayern_tree(child))
        nodes = _exact_whitespace(_children(container), original)
        value = {"text_sha256": text_hash(original), "blocks": nodes}
        if unique_match:
            norms[name] = value
        if indexed_target is not None:
            index, _ = indexed_target
            if index in seen_indices:
                indexed.pop(index, None)
            else:
                indexed[index] = {"enbez": name, **value}
            seen_indices.add(index)
    return {"schema_version": 1, "act_id": act["id"],
            "source_url": source_url(act["id"], act.get("permalink")),
            "retrieved_at": retrieved_at, "norms": norms, "norms_by_index": indexed}


def _html_children(element, footnotes: dict, depth: int = 0) -> list[dict]:
    from bs4 import Comment, NavigableString
    if depth > 64:
        raise ReaderBlocksError("HTML nesting too deep")
    result = []
    for child in element.children:
        if isinstance(child, Comment):
            continue
        if isinstance(child, NavigableString):
            result.append({"type": "text", "text": str(child)})
            continue
        tag = child.name.lower()
        if tag in DROP or set(child.get("class", [])) & {"paraheading", "fn-container", "clearer"}:
            continue
        children = _html_children(child, footnotes, depth + 1)
        tag = {"b": "strong", "i": "em", "strike": "s"}.get(tag, tag)
        if tag == "span":
            if "b" in child.get("class", []):
                tag = "strong"
            elif "i" in child.get("class", []):
                tag = "em"
        if tag not in TAGS:
            result.extend(children)
            continue
        if tag == "p":
            result.extend(_paragraph_nodes(children))
            continue
        node = {"type": "element", "tag": tag, "children": children}
        if tag == "sup" and "satznr" in child.get("class", []):
            node["attrs"] = {"role": "sentence"}
        if tag in {"td", "th"}:
            attrs = {key: int(raw) for key in ("colspan", "rowspan")
                     if (raw := str(child.get(key, ""))).isascii() and raw.isdecimal()
                     and 1 <= int(raw) <= 1000}
            if attrs:
                node["attrs"] = attrs
        link = child.select_one("a.footnotelink-down[href]") if tag == "sup" else None
        if link is not None:
            node["attrs"] = {"role": "footnote"}
        # 'automatisch' marks an HTML-generated display number absent from
        # the source XML's text. Its referenced definition is still retained.
        if link is None or "automatisch" not in link.get("class", []):
            result.append(node)
        if link is not None:
            target = link.get("href", "").removeprefix("#")
            if target in footnotes:
                result.append({"type": "element", "tag": "small", "attrs": {"role": "footnote"},
                               "children": _html_children(footnotes[target], {}, depth + 1)})
    return result


def build_bayern_html_sidecar(html: str, act: dict, *, retrieved_at: str) -> dict:
    """Use official HTML semantics with exact indexed characters and whitespace.

    BAYERN.RECHT XML puts footnote definitions inline, whereas its HTML moves
    them below the norm. Reinsert only explicitly linked definitions beside
    their call, preserving the canonical text order and marking their role.
    """
    from bs4 import BeautifulSoup
    current = _current_norms(act)
    if not act["id"].startswith("by_") or len(html.encode("utf-8")) > MAX_BYTES:
        raise ReaderBlocksError("unsupported Bavarian HTML document")
    soup = BeautifulSoup(html, "html.parser")
    norms, seen, indexed, seen_indices = {}, set(), {}, set()
    for container in soup.select("#content .cont"):
        if container.select_one(".paraheading") is None:
            continue
        label = container.select_one(".paraheading .paranr")
        name = ""
        if label is not None:
            clean_label = copy.deepcopy(label)
            for marker in clean_label.select("sup, .footnoteanchor"):
                marker.decompose()
            name = " ".join(clean_label.get_text().split())
        if name in seen:
            norms.pop(name, None)
        duplicate = name in seen
        seen.add(name)
        footnotes = {}
        for note in container.select(".fn-container .fn"):
            anchor = note.select_one("a.footnoteanchor[id]")
            if anchor is None:
                continue
            target = anchor["id"]
            body = copy.deepcopy(note)
            for link in body.select(".footnotelink-up"):
                marker = link.parent if link.parent.name == "sup" else link
                marker.decompose()
            for marker in body.select("a.footnoteanchor"):
                marker.decompose()
            for marker in body.find_all("b"):
                if marker.get_text().strip() == "[Amtl. Anm.:]":
                    marker.decompose()
            footnotes[target] = body
        nodes = _html_children(container, footnotes)
        original = flatten_nodes(nodes)
        indexed_target = _matching_index(act, current, name, original, html=True)
        unique_match = name in current and not duplicate and re.sub(r"\s", "", original) == re.sub(r"\s", "", current[name])
        if not unique_match and indexed_target is None:
            continue
        target_text = current[name] if unique_match else indexed_target[1]
        nodes = _exact_whitespace(nodes, target_text)
        value = {"text_sha256": text_hash(target_text), "blocks": nodes}
        if unique_match:
            norms[name] = value
        if indexed_target is not None:
            index, _ = indexed_target
            if index in seen_indices:
                indexed.pop(index, None)
            else:
                indexed[index] = {"enbez": name, **value}
            seen_indices.add(index)
    return {"schema_version": 1, "act_id": act["id"],
            "source_url": act["permalink"] + "/true", "retrieved_at": retrieved_at,
            "norms": norms, "norms_by_index": indexed}


def load_sidecar(directory: Path, act: dict) -> dict:
    current = _current_norms(act)
    path = directory / f"{act['id']}.json"
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ReaderBlocksError("reader sidecar too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReaderBlocksError("reader sidecar unavailable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("act_id") != act["id"]:
        raise ReaderBlocksError("invalid reader sidecar")
    valid_sources = {source_url(act["id"], act.get("permalink") if act["id"].startswith("by_") else act.get("source_url"))}
    if act["id"].startswith("by_"):
        valid_sources.add(act["permalink"] + "/true")
    if not isinstance(payload.get("source_url"), str) or payload["source_url"] not in valid_sources or not isinstance(payload.get("retrieved_at"), str):
        raise ReaderBlocksError("invalid reader source")
    if not isinstance(payload.get("norms"), dict):
        raise ReaderBlocksError("invalid reader norms")
    norms = {}
    for name, norm in payload["norms"].items():
        if name not in current or not isinstance(norm, dict):
            continue
        if norm.get("text_sha256") != text_hash(current[name]):
            continue
        nodes = norm.get("blocks")
        if _canonical_text(flatten_nodes(nodes), act["id"]) == _canonical_text(current[name], act["id"]):
            norms[name] = {"text_sha256": norm["text_sha256"], "blocks": nodes, **_annotation_fields(norm)}
            if isinstance(norm.get("source_retrieved_at"), str):
                norms[name]["source_retrieved_at"] = norm["source_retrieved_at"]
            if isinstance(norm.get("source_url"), str) and norm["source_url"] in valid_sources:
                norms[name]["source_url"] = norm["source_url"]
    indexed = {}
    if not isinstance(payload.get("norms_by_index", {}), dict):
        raise ReaderBlocksError("invalid indexed reader norms")
    for index, norm in payload.get("norms_by_index", {}).items():
        if not isinstance(index, str) or not index.isascii() or not index.isdecimal() or str(int(index)) != index:
            continue
        if int(index) >= len(act["norms"]) or not isinstance(norm, dict):
            continue
        canonical = act["norms"][int(index)]
        if norm.get("enbez") != canonical["enbez"] or norm.get("text_sha256") != text_hash(canonical["text"]):
            continue
        nodes = norm.get("blocks")
        if _canonical_text(flatten_nodes(nodes), act["id"]) == _canonical_text(canonical["text"], act["id"]):
            indexed[index] = {"enbez": norm["enbez"], "text_sha256": norm["text_sha256"], "blocks": nodes,
                              **_annotation_fields(norm)}
            if isinstance(norm.get("source_retrieved_at"), str):
                indexed[index]["source_retrieved_at"] = norm["source_retrieved_at"]
            if isinstance(norm.get("source_url"), str) and norm["source_url"] in valid_sources:
                indexed[index]["source_url"] = norm["source_url"]
    if not norms and not indexed:
        raise ReaderBlocksError("no matching reader norms")
    return {"schema_version": 1, "act_id": act["id"],
            "source_url": payload["source_url"], "retrieved_at": payload["retrieved_at"],
            "norms": norms, "norms_by_index": indexed}


def _write_sidecar(directory: Path, value: dict) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{value['act_id']}.", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o644)
        os.replace(temporary, directory / f"{value['act_id']}.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


def _retain_matching(value: dict, previous: dict) -> list[str]:
    retained = []
    for field in ("norms", "norms_by_index"):
        for key, norm in previous.get(field, {}).items():
            if key not in value.setdefault(field, {}):
                value[field][key] = {**norm,
                    "source_retrieved_at": norm.get("source_retrieved_at", previous["retrieved_at"]),
                    "source_url": norm.get("source_url", previous["source_url"])}
                retained.append(key if field == "norms" else f"index:{key}")
    return retained


def refresh_cached_sidecars(acts, outdir: Path, sourcedir: Path,
                            htmldir: Path | None = None, *,
                            previous_directory: Path | None = None) -> dict:
    """Refresh a release's structure from saved official sources, without HTTP.

    Process one act at a time. Every source candidate and previous sidecar is
    checked against the newly built canonical act. Missing or changed source
    text cannot replace a previously verified layout with unavailable output.
    """
    outdir, sourcedir = Path(outdir), Path(sourcedir)
    htmldir = Path(htmldir) if htmldir is not None else sourcedir / "html"
    report = {"updated": 0, "unavailable": [], "errors": [], "accepted_norms": 0}
    for act in acts:
        aid = act.get("id", "")
        if not aid.startswith(("fed_", "by_")):
            continue
        _current_norms(act)
        candidates = []
        xml_path = sourcedir / f"{aid}.xml"
        paths = [(xml_path, "xml")]
        if aid.startswith("by_"):
            paths.append((htmldir / (act["permalink"].rsplit("/", 1)[-1] + ".html"), "html"))
        for path, kind in paths:
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > MAX_BYTES:
                    raise ReaderBlocksError("cached source too large")
                stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                if kind == "html":
                    candidate = build_bayern_html_sidecar(path.read_text(encoding="utf-8"), act, retrieved_at=stamp)
                else:
                    builder = build_bayern_sidecar if aid.startswith("by_") else build_sidecar
                    candidate = builder(path.read_bytes(), act, retrieved_at=stamp)
                candidates.append(candidate)
            except (ReaderBlocksError, OSError, ValueError) as exc:
                report["errors"].append({"act_id": aid, "source": str(path), "error": str(exc)})
        candidates.sort(key=lambda value: value["retrieved_at"], reverse=True)
        value = candidates[0] if candidates else None
        for candidate in candidates[1:]:
            _retain_matching(value, candidate)
        for directory in dict.fromkeys([outdir, previous_directory]):
            if directory is None:
                continue
            try:
                previous = load_sidecar(Path(directory), act)
            except ReaderBlocksError:
                continue
            if value is None:
                value = previous
            else:
                _retain_matching(value, previous)
        if value is None or not (value["norms"] or value.get("norms_by_index")):
            report["unavailable"].append(aid)
            continue
        _write_sidecar(outdir, value)
        report["updated"] += 1
        report["accepted_norms"] += len(value["norms"]) + len(value.get("norms_by_index", {}))
    return report


def export_act(data: Path, output: Path, act_id: str, http: Http,
               *, source_cache: Path | None = None,
               html_directory: Path | None = None,
               previous_reader_directory: Path | None = None) -> dict:
    if not isinstance(act_id, str) or not re.fullmatch(r"(?:fed|by)_[a-z0-9][a-z0-9_-]*", act_id):
        raise ReaderBlocksError("unsupported act id")
    path = data / "acts" / f"{act_id}.json"
    if not path.is_file():
        raise ReaderBlocksError("act is not in the curated current data plane")
    act = json.loads(path.read_text(encoding="utf-8"))
    if act.get("id") != act_id:
        raise ReaderBlocksError("current act identity mismatch")
    url = source_url(act_id, act.get("permalink") if act_id.startswith("by_") else act.get("source_url"))
    _current_norms(act)
    cached = source_cache / f"{act_id}.xml" if source_cache is not None else None
    retrieved_at = datetime.now(timezone.utc).isoformat()
    if act_id.startswith("by_") and html_directory is not None:
        html_path = html_directory / (act["permalink"].rsplit("/", 1)[-1] + ".html")
        if html_path.stat().st_size > MAX_BYTES:
            raise ReaderBlocksError("cached HTML too large")
        retrieved_at = datetime.fromtimestamp(html_path.stat().st_mtime, timezone.utc).isoformat()
        value = build_bayern_html_sidecar(html_path.read_text(encoding="utf-8"), act,
                                          retrieved_at=retrieved_at)
        xml_bytes = None
    elif cached is not None and cached.is_file():
        if cached.stat().st_size > MAX_BYTES:
            raise ReaderBlocksError("cached XML too large")
        xml_bytes = cached.read_bytes()
        retrieved_at = datetime.fromtimestamp(cached.stat().st_mtime, timezone.utc).isoformat()
    else:
        response = http.get(url, timeout=30)
        response.raise_for_status()
        if response.url != url:
            raise ReaderBlocksError("unexpected official source redirect")
        if len(response.content) > MAX_BYTES:
            raise ReaderBlocksError("source archive too large")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            members = [info for info in archive.infolist() if info.filename.lower().endswith(".xml")]
            if len(members) != 1 or members[0].file_size > MAX_BYTES:
                raise ReaderBlocksError("unexpected XML archive")
            xml_bytes = archive.read(members[0])
    if xml_bytes is not None:
        builder = build_bayern_sidecar if act_id.startswith("by_") else build_sidecar
        value = builder(xml_bytes, act, retrieved_at=retrieved_at)
    retained = []
    if previous_reader_directory is not None:
        try:
            previous = load_sidecar(previous_reader_directory, act)
        except ReaderBlocksError:
            previous = None
        if previous is not None:
            retained = _retain_matching(value, previous)
    if not value["norms"] and not value.get("norms_by_index"):
        raise ReaderBlocksError("source has no exact current text matches")
    # Re-read immediately before publishing so a concurrent canonical rebuild
    # cannot certify a newer reader sidecar against the earlier data plane.
    latest = json.loads(path.read_text(encoding="utf-8"))
    if latest != act:
        raise ReaderBlocksError("current act changed during export")
    if xml_bytes is not None and cached is not None and not cached.exists():
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(xml_bytes)
    _write_sidecar(output, value)
    return {"act_id": act_id, "accepted": len(value["norms"]),
            "accepted_by_index": len(value.get("norms_by_index", {})),
            "indexed": len(act["norms"]), "source_url": value["source_url"],
            "retained_previous_norms": retained}


def main() -> int:
    from pipeline.common import Http
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("web/data"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-cache", type=Path,
                        help="reuse/persist official XML for offline structure rebuilds")
    parser.add_argument("--html-dir", type=Path,
                        help="official Bavarian full-law HTML captures named <document-key>.html")
    parser.add_argument("--previous-reader", type=Path,
                        help="retain older hash-verified structure when current source wording changed")
    parser.add_argument("act_ids", nargs="+")
    args = parser.parse_args()
    http, failures = Http(), 0
    for act_id in dict.fromkeys(args.act_ids):
        try:
            print(json.dumps(export_act(args.data, args.out, act_id, http, source_cache=args.source_cache,
                                        html_directory=args.html_dir,
                                        previous_reader_directory=args.previous_reader)))
        except (ReaderBlocksError, OSError, ValueError, zipfile.BadZipFile) as exc:
            print(json.dumps({"act_id": act_id, "error": str(exc)}))
            failures += 1
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
