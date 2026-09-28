"""Parse NeuRIS LegalDocML.de expressions into the history "parsed" format.

A NeuRIS expression package (zip) holds ``regelungstext-*.xml`` (the act,
``akn:act``), optional ``anlage-regelungstext-*.xml`` attachments and a
``rechtsetzungsdokument-*.xml`` collection wrapper.  The output mirrors the
GII parsers (``gii_html_parser``) so norms de-duplicate against GII texts
with :func:`api.history_identity.norm_identity`:

* every ``akn:article`` becomes a norm: ``enbez`` = its ``akn:num``,
  ``titel`` = its ``akn:heading`` cut at the first child element (GII's
  ``parse_law`` reads ``<titel>`` with ``findtext``), ``text`` = the body
  text without footnotes (``akn:authorialNote``); a blank separates a
  paragraph number from its text as in GII (``"(1) Die ..."``);
* each attachment becomes an ``Anlage`` norm (``docTitle`` inlines give
  designator and "(zu § …)" reference);
* the preamble's table of contents becomes the ``Inhaltsübersicht``
  pseudo-norm with its text (GII has it either text-less or as a table).

FRBR dates (``aenderung`` point in time, ``generierung`` manifestation date,
Verkündungsdatum of the work) are recorded as *source identifiers* only and
are never legal dates of a version.
"""
from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
import zipfile

PARSER_VERSION = "neuris-ldml/1"
CONTAINERS = {"book", "part", "chapter", "section", "subsection", "title",
              "subtitle", "division", "subdivision"}
SKIP = {"authorialNote", "meta", "marker", "noteRef"}
RANGE_ENBEZ = re.compile(r"^(?:§§|Art\.?|Artikel)\s.*\b(?:bis|und)\b")
STAND_TYPES = {"neuf": "Neuf", "stand": "Stand", "hinweis": "Hinweis",
               "sonst": "Sonst"}


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if local(c.tag) == name]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def flatten(el: ET.Element | None, *, num_space: bool = True) -> str:
    if el is None:
        return ""
    out: list[str] = []

    def walk(node: ET.Element) -> None:
        name = local(node.tag)
        if name in SKIP:
            if node.tail:
                out.append(node.tail)
            return
        if node.text:
            out.append(node.text)
        for child in node:
            walk(child)
            if local(child.tag) == "num" and num_space and name == "paragraph":
                text = "".join(child.itertext()).strip()
                if text:
                    out.append(" ")
        if node.tail and node is not el:
            out.append(node.tail)

    walk(el)
    text = "".join(out).replace("\r", "").replace("\n", " ")
    return re.sub(r"[ \t]+", " ", text).strip()


def leading_text(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.text or "").strip()


def _label(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def _xxxx(enbez: str, flags: list[str]) -> str:
    if RANGE_ENBEZ.match(enbez):
        flags.append("range_enbez_xxxx_prefixed")
        return f"(XXXX) {enbez}"
    return enbez


def _frbr(root: ET.Element) -> dict:
    ident: dict = {}
    for level, key in (("FRBRWork", "work"), ("FRBRExpression", "expression"),
                       ("FRBRManifestation", "manifestation")):
        node = next((e for e in root.iter() if local(e.tag) == level), None)
        if node is None:
            continue
        this = _child(node, "FRBRthis")
        uri = _child(node, "FRBRuri")
        ident[f"{key}_eli"] = (uri.get("value") if uri is not None else None)
        ident[f"{key}_this"] = (this.get("value") if this is not None else None)
        for date in _children(node, "FRBRdate"):
            ident[f"{key}_date_{date.get('name')}"] = date.get("date")
        version = _child(node, "FRBRversionNumber")
        if version is not None:
            ident["expression_version"] = version.get("value")
        for alias in _children(node, "FRBRalias"):
            ident[f"{key}_alias_{alias.get('name')}"] = alias.get("value")
    return ident


def _parse_act(root: ET.Element, record: dict) -> None:
    for el in root.iter():
        name = local(el.tag)
        if name == "abkuerzung" and not record["jurabk"]:
            record["jurabk"] = _label(el)
        elif name == "standangabe":
            kind = STAND_TYPES.get((el.get("type") or "").lower(),
                                   (el.get("type") or "").capitalize())
            value = _label(el)
            if value:
                record["stand"]["entries"].append({"type": kind, "text": value})
    title = next((e for e in root.iter() if local(e.tag) == "docTitle"), None)
    if title is not None:
        record["title"] = flatten(title)
    preamble = next((e for e in root.iter() if local(e.tag) == "preamble"), None)
    if preamble is not None:
        for block in preamble:
            if local(block.tag) == "blockContainer" and (
                    block.get("refersTo") == "inhaltsuebersicht"):
                heading = _child(block, "heading")
                toc = _child(block, "toc")
                record["norms"].append({
                    "enbez": _label(heading) or "Inhaltsübersicht",
                    "titel": "", "text": flatten(toc), "gliederung": "",
                    "flags": ["toc_from_preamble"]})
            elif local(block.tag) in {"formula", "citations", "recitals"}:
                heading = _child(block, "heading")
                holder = ET.Element(local(block.tag))
                holder.extend(c for c in block if local(c.tag) != "heading")
                text = flatten(holder)
                default = ("Präambel" if local(block.tag) != "formula"
                           else "Eingangsformel")
                if text:
                    record["norms"].append({
                        "enbez": _label(heading) or default,
                        "titel": "", "text": text, "gliederung": "",
                        "flags": [f"preamble_{local(block.tag)}"]})
    body = next((e for e in root.iter() if local(e.tag) == "body"), None)
    if body is None:
        record["warnings"].append("no akn:body")
        return

    def walk(node: ET.Element, gliederung: str) -> None:
        for child in node:
            name = local(child.tag)
            if name in CONTAINERS:
                num = _label(_child(child, "num"))
                heading = _label(_child(child, "heading"))
                walk(child, " ".join(filter(None, (num, heading))))
            elif name == "article":
                flags: list[str] = []
                enbez = _label(_child(child, "num"))
                heading = _child(child, "heading")
                titel = leading_text(heading)
                if heading is not None and len(heading) and _label(heading) != titel:
                    flags.append("titel_cut_at_child_element")
                parts = [c for c in child
                         if local(c.tag) not in {"num", "heading"}]
                holder = ET.Element("article")
                holder.extend(parts)
                text = flatten(holder)
                if not enbez:
                    continue
                enbez = _xxxx(enbez, flags)
                norm = {"enbez": enbez, "titel": titel, "text": text,
                        "gliederung": gliederung, "eid": child.get("eId")}
                if child.get("period"):
                    norm["period_ref"] = child.get("period")
                if flags:
                    norm["flags"] = flags
                record["norms"].append(norm)
            else:
                walk(child, gliederung)

    walk(body, "")


def _parse_attachment(root: ET.Element) -> dict | None:
    title = next((e for e in root.iter() if local(e.tag) == "docTitle"), None)
    num = bezug = ""
    if title is not None:
        for inline in title.iter():
            ref = inline.get("refersTo") or ""
            if ref.endswith("-num"):
                num = _label(inline)
            elif ref.endswith("-bezug"):
                bezug = _label(inline)
        if not num:
            num = _label(title)
    main = next((e for e in root.iter() if local(e.tag) == "mainBody"), None)
    if not num:
        return None
    return {"enbez": num, "titel": bezug, "text": flatten(main),
            "gliederung": "", "flags": ["attachment_document"]}


def parse_neuris_zip(data: bytes) -> dict:
    record = {"parser": PARSER_VERSION, "layout": "legaldocml-de",
              "jurabk": "", "title": "", "builddate": None, "doknr": None,
              "stand": {"raw": "", "entries": []}, "complete": True,
              "norms": [], "warnings": [], "source_identifiers": {}}
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        record["complete"] = False
        record["warnings"].append(f"unreadable package: {exc}")
        record["norm_count"] = 0
        return record
    with archive:
        names = sorted(n for n in archive.namelist() if n.endswith(".xml"))
        acts = [n for n in names if re.search(r"/regelungstext-[^/]*\.xml$", n)]
        annexes = sorted((n for n in names if "/anlage-regelungstext-" in n),
                         key=lambda n: [int(x) if x.isdigit() else x
                                        for x in re.split(r"(\d+)", n)])
        if not acts:
            record["complete"] = False
            record["warnings"].append("no regelungstext document in package")
        for name in acts[:1]:
            root = ET.fromstring(archive.read(name))
            record["source_identifiers"] = _frbr(root)
            _parse_act(root, record)
        if len(acts) > 1:
            record["warnings"].append(f"{len(acts)} regelungstext documents; parsed first")
        for name in annexes:
            try:
                norm = _parse_attachment(ET.fromstring(archive.read(name)))
            except ET.ParseError as exc:
                record["warnings"].append(f"annex {name}: {exc}")
                record["complete"] = False
                continue
            if norm:
                record["norms"].append(norm)
    ident = record["source_identifiers"]
    ident["date_basis"] = "eli_frbr_identifiers_not_legal_effect"
    record["doknr"] = ident.get("expression_eli")
    record["stand"]["raw"] = " | ".join(
        f"{e['type']}: {e['text']}" for e in record["stand"]["entries"])
    record["norm_count"] = len(record["norms"])
    if not record["norms"]:
        record["complete"] = False
        record["warnings"].append("no norms parsed")
    return record
