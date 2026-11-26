"""Read source-bound Rn. and positional blocks from an official NeuRIS judgment."""
from collections import Counter
from hashlib import sha256
import json
import re
import unicodedata
import xml.etree.ElementTree as ET

PROFILE = "neuris-akn/1"
BASE = "https://testphase.rechtsinformationen.bund.de/v1/rechtsprechung/"
NS = {"akn": "http://docs.oasis-open.org/legaldocml/ns/akn/3.0",
      "ris": "http://rechtsinformationen.bund.de/schema/ris/0.1"}
AKN = "{" + NS["akn"] + "}"
RIS = "{" + NS["ris"] + "}"
SECTIONS = {
    "Tenor": ("tenor", "court_disposition"), "Leitsatz": ("leitsatz", "published_headnote"),
    "Orientierungssatz": ("sonstosatz", "published_headnote"), "Tatbestand": ("tatbestand", "court_reported_facts"),
    "Entscheidungsgründe": ("entscheidungsgruende", "court_reasoning_section"),
    "Gründe": ("gruende", "court_reasons_section"), "Abweichende Meinung": ("abwmeinung", "separate_opinion"),
    "Gliederung": ("sonstlt", "published_other"), "Sonstiger Langtext": ("sonstlt", "published_other"),
    "Rechtsmittelbelehrung": ("sonstlt", "published_other"),
}
BLOCK_TAGS = {AKN + name for name in ("p", "block", "heading", "table", "tr", "td", "th", "br", "item",
                                     "li", "list", "blockList", "container", "hcontainer", "content", "num")}


def source_url(metadata):
    number = metadata["document_number"]
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]{1,99}", number) or metadata["id"] != "neuris-" + number.lower():
        raise ValueError("invalid NeuRIS document number or public identifier")
    return BASE + number + ".xml"


def one(element, path):
    found = element.findall(path, NS)
    if len(found) != 1:
        raise ValueError(f"expected one NeuRIS source element at {path}")
    return found[0]


def contents(node):
    chunks = [node.text or ""]
    for child in node:
        if child.tag in BLOCK_TAGS:
            chunks.extend((" ", contents(child), " "))
        else:
            chunks.append(contents(child))
        chunks.append(child.tail or "")
    return "".join(chunks)


def compact(text):
    return "".join(unicodedata.normalize("NFC", text).split())


def document(blob, metadata):
    if len(blob) > 8 * 1024 * 1024 or re.search(br"<!DOCTYPE|<!ENTITY", blob, re.I):
        raise ValueError("unsupported NeuRIS XML declaration or size")
    if b"\x00" in blob:
        raise ValueError("expected UTF-8 NeuRIS XML")
    try:
        root = ET.fromstring(blob.decode("utf-8"))
    except (ET.ParseError, UnicodeError) as exc:
        raise ValueError("invalid NeuRIS XML") from exc
    if root.tag != AKN + "akomaNtoso":
        raise ValueError("unexpected NeuRIS document root")
    judgment = one(root, "akn:judgment")
    work = one(judgment, "akn:meta/akn:identification/akn:FRBRWork")
    ris = one(judgment, "akn:meta/akn:proprietary/ris:meta")
    date = one(work, "akn:FRBRdate")
    if date.get("name") not in {"entscheidungsdatum", "datumDerZustellungAnVerkuendungsStatt"}:
        raise ValueError("unsupported official decision date role")
    aliases = work.findall('akn:FRBRalias[@name="ecli"]', NS)
    if len(aliases) > 1:
        raise ValueError("multiple Work ECLI aliases")
    ecli = (aliases[0].get("value") or None) if aliases else None
    dockets = [n.text.strip() for n in ris.findall("ris:aktenzeichenListe/ris:aktenzeichen", NS) if n.text]
    kind = " ".join(one(ris, "ris:dokumenttyp").itertext()).strip()
    court = one(ris, "ris:gericht/ris:gerichtstyp").text
    source_url(metadata)
    expected = (metadata["document_number"], metadata.get("ecli"), metadata["court_short"], metadata["date"],
                sorted(metadata["case_numbers"]), metadata["kind"])
    actual = (one(work, "akn:FRBRthis").get("value"), ecli, court, date.get("date"), sorted(dockets), kind)
    if expected != actual or not dockets or metadata["az"] != ", ".join(metadata["case_numbers"]):
        raise ValueError("NeuRIS metadata does not match the retained publication")
    return one(judgment, "akn:judgmentBody"), ecli, kind


def child_paths(node, path):
    positions = Counter()
    for child in node:
        if not child.tag.startswith(AKN):
            raise ValueError("unexpected namespace in the judgment body")
        name = child.tag[len(AKN):]
        positions[name] += 1
        yield child, f"{path}/akn:{name}[{positions[name]}]"


def extract(blob, metadata):
    body, ecli, _ = document(blob, metadata)
    if any(node.get("src") for node in body.iter()):
        raise ValueError("NeuRIS judgment requires accompanying source files")
    if any(n.tag == AKN + "p" and "Vorabveröffentlichung. Ergänzungen erfolgen in Kürze." in contents(n) for n in body.iter()):
        if not any(n.tag in (AKN+"decision", AKN+"background", AKN+"motivation") for n in body):
            raise ValueError("NeuRIS judgment text is awaiting publication")
    rows, ordinals, ids, numbered_in_section = [], Counter(), set(), set()
    number_tag = AKN + "hcontainer"

    def numbered(node):
        return node.tag == number_tag and node.get("name") == "Randnummer"

    def blocks(node, path):
        if numbered(node):
            marker = one(node, "akn:num")
            label = "".join(marker.itertext()).strip()
            if not re.fullmatch(r"[0-9]{1,4}[a-z]?", label) or any(numbered(c) for c in node.iter() if c is not node):
                raise ValueError("unsupported or nested NeuRIS paragraph label")
            if (node.text or "").strip() or any((c.tail or "").strip() for c in node):
                raise ValueError("unexpected text beside a numbered NeuRIS block")
            text = " ".join(" ".join(contents(c) for c in node if c is not marker).split())
            if not text:
                raise ValueError("explicit NeuRIS paragraph has no text")
            yield node, path, label, text
        elif any(numbered(c) for c in node.iter()):
            if (node.text or "").strip() or any((c.tail or "").strip() for c in node):
                raise ValueError("unexpected text around numbered NeuRIS blocks")
            for child, child_path in child_paths(node, path):
                yield from blocks(child, child_path)
        else:
            text = " ".join(contents(node).split())
            if text:
                yield node, path, None, text

    if (body.text or "").strip() or any((c.tail or "").strip() for c in body):
        raise ValueError("unexpected text between NeuRIS sections")
    for section_node, section_path in child_paths(body, "/akn:akomaNtoso/akn:judgment[1]/akn:judgmentBody[1]"):
        domain = section_node.get(RIS + "domainTerm")
        if domain not in SECTIONS:
            raise ValueError(f"unsupported NeuRIS judgment section {domain!r}")
        section, role = SECTIONS[domain]
        parts = []
        if (section_node.text or "").strip() or any((c.tail or "").strip() for c in section_node):
            nodes = [(section_node, section_path)]
        else:
            nodes = child_paths(section_node, section_path)
        for node, path in nodes:
            for element, locator, label, text in blocks(node, path):
                if label and (section, label) in numbered_in_section:
                    raise ValueError(f"duplicate NeuRIS Rn. {label} in {section}")
                if label:
                    numbered_in_section.add((section, label))
                anchor = element.get("eId")
                if anchor and not re.fullmatch(r"[a-zA-Z0-9._-]{1,160}", anchor):
                    raise ValueError("unexpected source anchor")
                ordinals[section] += 1
                pid = f"{section}:{anchor or ('block-' + str(ordinals[section]).zfill(4))}"
                if pid in ids:
                    raise ValueError("duplicate NeuRIS passage identity")
                ids.add(pid)
                rows.append({"decision_id": metadata["id"], "passage_id": pid, "section": section,
                             "ordinal": ordinals[section], "document_position": len(rows) + 1,
                             "source_role": role, "source_anchor": anchor, "source_label": label,
                             "paragraph_label": label, "xml_path": locator, "text": text,
                             "text_sha256": sha256(text.encode()).hexdigest()})
                parts.append((label or "") + text)
        if compact("".join(section_node.itertext())) != compact("".join(parts)):
            raise ValueError("NeuRIS section text differs after extraction")
    if not rows:
        raise ValueError("NeuRIS judgment contains no retained body passages")
    return rows, ecli


def add_selected(existing, selected):
    """Keep reviewed web identifiers stable and hold any ambiguous cross-source match."""
    def identities(row):
        from courts.casenumbers import case_key
        court, date = row["court_short"].casefold(), row["date"]
        keys = {("id", row["id"])}
        if row.get("ecli"):
            keys.add(("ecli", row["ecli"].casefold()))
        for number in row.get("case_numbers") or [row.get("az")]:
            if number:
                keys.add(("docket", court, date, case_key(number)))
        return keys

    if not selected:
        return existing
    known = set()
    for row in existing:
        known.update(identities(row))
    for row in selected:
        source_url(row)
        keys = identities(row)
        if keys & known:
            raise ValueError(f"review conflicting identifiers before adding {row['id']} to web release")
        known.update(keys)
    return existing + selected


def selected_snapshot(snapshot):
    """Check that the complete catalog and its source receipts match the reviewed selection."""
    if snapshot is None:
        return []
    expected = json.loads((snapshot / "selection.json").read_bytes())
    rows = [json.loads(line) for line in (snapshot / "decisions.jsonl").read_text(encoding="utf-8").splitlines() if line]
    if not expected or len(expected) != len(rows) or set(expected) != {r["document_number"] for r in rows}:
        raise ValueError("incomplete or duplicate reviewed NeuRIS snapshot")
    for row in rows:
        source_url(row)
        receipt = json.loads((snapshot / "official_decisions" / (row["id"] + ".json")).read_bytes())
        if receipt.get("sha256") != expected[row["document_number"]] or receipt.get("profile") != PROFILE:
            raise ValueError(f"receipt differs from the reviewed source selection for {row['id']}")
    return rows
