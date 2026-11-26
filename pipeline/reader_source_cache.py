"""Atomic raw official-source captures used by the offline reader rebuild."""
from __future__ import annotations

import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path


def gii_designator(norm: ET.Element) -> str:
    """Use an article's structural label only when its own XML record has a body."""
    explicit = (norm.findtext("metadaten/enbez") or "").strip()
    if explicit:
        return explicit
    label = (norm.findtext("metadaten/gliederungseinheit/gliederungsbez") or "").strip()
    if not re.fullmatch(r"(?:Art(?:ikel)?\.?|§)\s*\d+[a-z]{0,3}", label, re.I):
        return ""
    content = norm.find("textdaten/text/Content")
    return label if content is not None and any(t.strip() for t in content.itertext()) else ""


def reader_act_id(family: str, jurabk: str) -> str:
    """Use the same public identity normalization as build_web_data.slug."""
    if family not in {"fed", "by"} or not isinstance(jurabk, str):
        raise ValueError("invalid reader source identity")
    slug = re.sub(r"[^a-z0-9]+", "_", jurabk.lower()).strip("_")
    if not slug:
        raise ValueError("empty reader source identity")
    return family + "_" + slug


def save_reader_source(directory: Path, act_id: str, xml_bytes: bytes) -> Path:
    """Store already-fetched, parsed XML; perform no network access."""
    if not isinstance(act_id, str) or not re.fullmatch(r"(?:fed|by)_[a-z0-9][a-z0-9_]*", act_id):
        raise ValueError("invalid reader source identity")
    if not isinstance(xml_bytes, bytes) or len(xml_bytes) > 40 * 1024**2 or b"<!ENTITY" in xml_bytes.upper():
        raise ValueError("unsupported reader source XML")
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise ValueError("invalid reader source XML") from exc
    expected = "dokumente" if act_id.startswith("fed_") else "byrecht-norm"
    if root.tag != expected:
        raise ValueError("reader source family mismatch")
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{act_id}.xml"
    fd, filename = tempfile.mkstemp(prefix=f".{act_id}.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(xml_bytes)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
        os.replace(filename, destination)
    finally:
        Path(filename).unlink(missing_ok=True)
    return destination
