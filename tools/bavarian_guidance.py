"""Build the separate public collection of two captured Bavarian migration directives."""
from __future__ import annotations

import hashlib
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

SOURCE = "https://www.gesetze-bayern.de/Content"
LEGAL_NOTICE_EN = (
    "THIS DOCUMENT IS NOT A STATUTE. ADMINISTRATIVE GUIDANCE MAY CONFLICT WITH APPLICABLE LAW. "
    "THE LAW PREVAILS."
)
AGENT_GUIDANCE_EN = (
    "Use this directive to understand the issuing authority's intended practice. "
    "The text alone cannot establish what officials did, what a person is entitled to, "
    "or whether a particular decision was lawful. Courts independently apply the law. "
    "Lawful internal instructions can bind officials. Equal treatment may also require "
    "an authority to follow its established lawful practice in comparable cases. "
    "Check the governing law, the case record and evidence of actual practice."
)
CAPTURES = (
    ("2026-11-18", "BayVV_2126_2_G_087"),
    ("2026-11-18", "BayVV_2176_I_14046"),
)
SUPERSCRIPT = str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹")


def _document(directory: Path, day: str, key: str) -> dict:
    archive = directory / day / (key + ".zip")
    with ZipFile(archive) as package:
        xml = package.read(f"bayportalvv/{key}.xml")
    root = ET.fromstring(xml)
    identity = root.find("verwaltungsdaten")
    body = root.find("textdaten/vv1.0/rumpf")
    meta = root.find("metadaten/bayernrecht")
    if (root.tag != "byrecht-vv" or identity is None or identity.get("doknr") != key
            or body is None or meta is None):
        raise ValueError(f"Unexpected BAYERN.RECHT directive: {key}")

    # Retain the order of paragraphs, list bullets, cross-references and sentence numbers.
    for marker in body.iter("sup"):
        if marker.text:
            marker.text = marker.text.translate(SUPERSCRIPT)
    for br in body.iter("br"):
        br.text = " "
    lines = []
    for node in body.iter():
        if node.tag in ("p", "symbol"):
            line = " ".join("".join(node.itertext()).split())
            if line:
                lines.append(line)
    if not lines or not meta.findtext("bayernrecht_langtitel"):
        raise ValueError(f"Missing directive text or title: {key}")

    return {
        "id": key,
        "document_type": "Verwaltungsvorschrift",
        "is_statute": False,
        "legal_notice_en": LEGAL_NOTICE_EN,
        "conflict_with_statute": "not_assessed",
        "agent_guidance_en": AGENT_GUIDANCE_EN,
        "title": meta.findtext("bayernrecht_langtitel"),
        "abbreviation": meta.findtext("bayernrecht_abkuerzung"),
        "jurisdiction": "DE-BY",
        "language": "de",
        "valid_from": meta.findtext("bayernrecht_inkraft"),
        "valid_until": meta.findtext("bayernrecht_ausserkraft"),
        "source": {
            "publisher": "BAYERN.RECHT",
            "url": f"{SOURCE}/Document/{key}/true",
            "zip_url": f"{SOURCE}/Zip/{key}",
            "captured_on": day,
            "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "xml_sha256": hashlib.sha256(xml).hexdigest(),
        },
        "text": "\n\n".join(lines),
    }


def migration_guidance(directory: Path) -> list[dict]:
    return [_document(directory, day, key) for day, key in CAPTURES]
