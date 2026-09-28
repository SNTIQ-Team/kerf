"""Historical Bavarian norm versions and change history (BAYERN.RECHT, GVBl).

Offline library: content-addressed archive access, parsers for archived
BAYERN.RECHT captures and GVBl documents, per-norm version chains, per-norm
change records and cross-source findings, plus loaders for the JSONL outputs.
Network capture lives in ``pipeline/fetch_bavarian_history.py``.

Archive layout (archive worker only; see the phase-4 spec)::

    R = $LEXGRAPH_HISTORY_ARCHIVE or ~/.local/share/sntiq-legal/lexgraph-archive/history
    R/raw/<source>/sha256/<xx>/<sha256>.<ext>.gz   gzip of the raw bytes
    R/ledger/<source>.jsonl                        one row per capture attempt
    R/parsed/<source>/<act_id>/<sha256>.json.gz    one parse per raw object

Sources: ``wayback_bayern`` (Wayback copies of gesetze-bayern.de),
``bayern_recht`` (live BAYERN.RECHT), ``gvbl`` (verkuendung-bayern.de).

Date discipline: capture timestamps are observations (``known_at``) and are
never promoted to legal dates.  BAYERN.RECHT's ``Text gilt ab`` is an
act-level consolidation date (every norm page of one act shows the same value
at one time), so it is carried as such and never as a norm's own start date.
Adoption, promulgation and commencement dates come only from the GVBl
document (signature line, issue date, Inkrafttreten clause).
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import tempfile
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # the shared helper lands with the hist-wayback workstream
    from api.history_identity import norm_identity  # type: ignore
except ImportError:  # pragma: no cover - exercised until that merge
    # Byte-identical copy of api/history_identity.py on branch hist-wayback
    # (commit 92ab93d), which implements the phase-4 spec identity.
    _WS = re.compile(r"\s+")

    def normalize_text_for_identity(value: str | None) -> str:
        """Norm body: NFC, nbsp folded, soft hyphens dropped, all whitespace removed."""
        text = unicodedata.normalize("NFC", str(value or ""))
        text = text.replace("\xa0", " ").replace("\xad", "")
        return _WS.sub("", text)

    def normalize_label_for_identity(value: str | None) -> str:
        """Designator/title: NFC, nbsp as space, whitespace runs collapsed."""
        text = unicodedata.normalize("NFC", str(value or ""))
        text = text.replace("\xa0", " ").replace("\xad", "")
        return _WS.sub(" ", text).strip()

    def norm_identity(enbez: str | None, titel: str | None, text: str | None) -> str:
        """sha256 identity of one norm version (hex)."""
        payload = "\x1f".join((normalize_label_for_identity(enbez),
                               normalize_label_for_identity(titel),
                               normalize_text_for_identity(text)))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

ROOT = Path(__file__).resolve().parent.parent
MAIN_CHECKOUT = Path(os.environ.get(
    "LEXGRAPH_MAIN_CHECKOUT",
    str(Path(__file__).resolve().parents[1])))
UA = "SNTIQ-lexgraph/0.2 (research; contact@sntiq.com)"
LEDGER_SCHEMA = "lexgraph-history-capture/1"
PARSED_SCHEMA = "lexgraph-history-parsed/1"
SOURCES = ("wayback_bayern", "bayern_recht", "gvbl")
SHA_RE = re.compile(r"[0-9a-f]{64}")

# act_id -> BAYERN.RECHT document key and official abbreviation
ACTS: dict[str, dict[str, str]] = {
    "by_agsg": {"key": "BayAGSG", "jurabk": "AGSG"},
    "by_agvwgo": {"key": "BayAGVwGO", "jurabk": "AGVwGO"},
    "by_dvasyl": {"key": "BayDVAsyl", "jurabk": "DVAsyl"},
}


class HistoryArchiveError(ValueError):
    """A raw object, ledger row or parse failed verification."""


# ------------------------------------------------------------------ archive

def archive_root(root: str | os.PathLike | None = None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get("LEXGRAPH_HISTORY_ARCHIVE")
    if env:
        return Path(env)
    return Path.home() / ".local/share/sntiq-legal/lexgraph-archive/history"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_source(source: str) -> None:
    if source not in SOURCES:
        raise HistoryArchiveError(f"unknown history source {source!r}")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def _gzip(data: bytes) -> bytes:
    return gzip.compress(data, compresslevel=9, mtime=0)


def raw_path(source: str, digest: str, ext: str,
             root: str | os.PathLike | None = None) -> Path:
    _check_source(source)
    if not SHA_RE.fullmatch(digest or "") or not re.fullmatch(r"[a-z0-9]{1,8}", ext):
        raise HistoryArchiveError("invalid raw object name")
    return (archive_root(root) / "raw" / source / "sha256" / digest[:2]
            / f"{digest}.{ext}.gz")


def put_raw(source: str, data: bytes, ext: str,
            root: str | os.PathLike | None = None) -> str:
    """Store raw bytes gzip-compressed under their (uncompressed) sha256."""
    digest = sha256_hex(data)
    path = raw_path(source, digest, ext, root)
    if path.exists():
        if get_raw(source, digest, ext, root) != data:  # pragma: no cover
            raise HistoryArchiveError("raw object collision")
        return digest
    _atomic_write(path, _gzip(data))
    return digest


def get_raw(source: str, digest: str, ext: str,
            root: str | os.PathLike | None = None) -> bytes:
    """Read and verify one raw object."""
    path = raw_path(source, digest, ext, root)
    try:
        with gzip.open(path, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise HistoryArchiveError(f"raw object unavailable: {path}") from exc
    if sha256_hex(data) != digest:
        raise HistoryArchiveError(f"raw object hash mismatch: {path}")
    return data


def ledger_path(source: str, root: str | os.PathLike | None = None) -> Path:
    _check_source(source)
    return archive_root(root) / "ledger" / f"{source}.jsonl"


def append_ledger(source: str, row: dict,
                  root: str | os.PathLike | None = None) -> dict:
    row = {"schema": LEDGER_SCHEMA, "source": source, **row}
    path = ledger_path(source, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return row


def read_ledger(source: str, root: str | os.PathLike | None = None
                ) -> list[dict]:
    path = ledger_path(source, root)
    if not path.is_file():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:  # a torn final line from an interrupted run
                continue
    return rows


def parsed_path(source: str, act_id: str, digest: str,
                root: str | os.PathLike | None = None) -> Path:
    _check_source(source)
    if not re.fullmatch(r"[a-z0-9_]+", act_id) or not SHA_RE.fullmatch(digest):
        raise HistoryArchiveError("invalid parsed object name")
    return (archive_root(root) / "parsed" / source / act_id
            / f"{digest}.json.gz")


def put_parsed(record: dict, root: str | os.PathLike | None = None) -> Path:
    path = parsed_path(record["source"], record["act_id"], record["sha256"], root)
    data = json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8")
    _atomic_write(path, _gzip(data))
    return path


def get_parsed(source: str, act_id: str, digest: str,
               root: str | os.PathLike | None = None) -> dict | None:
    path = parsed_path(source, act_id, digest, root)
    if not path.is_file():
        return None
    with gzip.open(path, "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def wayback_captured_at(timestamp: str) -> str:
    stamp = datetime.strptime(timestamp, "%Y%m%d%H%M%S")
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------ BAYERN.RECHT

def _bayern_recht_module():
    """``pipeline/fetch_bayern_recht.py`` builds today's norm texts; reuse it
    so archived XML is flattened exactly like the current consolidation."""
    import sys
    path = str(ROOT / "pipeline")
    if path not in sys.path:
        sys.path.insert(0, path)
    import fetch_bayern_recht  # noqa: PLC0415
    return fetch_bayern_recht


def _xml_gliederung(root: ET.Element) -> list[str]:
    """Ancestor gliederung titles for each einzelnorm, in document order."""
    br = _bayern_recht_module()
    out: list[str] = []

    def walk(node: ET.Element, trail: list[str]) -> None:
        for child in node:
            if child.tag == "gliederung":
                title = child.find("gliederung.titel")
                label = br.flat(title) if title is not None else ""
                walk(child, trail + ([label] if label else []))
            elif child.tag == "einzelnorm":
                out.append(" / ".join(trail))
            else:
                walk(child, trail)
    walk(root, [])
    return out


def parse_zip(data: bytes, key: str, jurabk: str) -> dict:
    """Parse one /Content/Zip/<KEY> archive (bayportalnorm XML)."""
    br = _bayern_recht_module()
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".xml"))
        xml_bytes = archive.read(name)
    act, norms, versions, _refs = br.parse_act_xml(xml_bytes, key, jurabk)
    root = ET.fromstring(xml_bytes)
    glied = _xml_gliederung(root)
    warnings = []
    # older exports carry an empty <para.titel/> before <para.nr> and the real
    # title after it; parse_act_xml reads the first one
    for norm, element in zip(norms, root.iter("einzelnorm")):
        if norm["titel"]:
            continue
        titles = [br.flat(t) for t in element.findall("para.titel")]
        title = next((t for t in titles if t), "")
        if title:
            norm["titel"] = title
            if "empty first para.titel" not in warnings:
                warnings.append("empty first para.titel")
    if len(glied) != len(norms):
        warnings.append("gliederung/norm count mismatch; gliederung dropped")
        glied = [""] * len(norms)
    title_el = root.find("kopf/angaben.versabh/titelangaben")
    title = br.flat(title_el) if title_el is not None else ""
    fassung = (root.findtext(".//fassung/fassungsdatum") or "").strip()
    zitat = root.find(".//normzitat")
    normzitat = br.flat(zitat) if zitat is not None else ""
    return {
        "jurabk": act["jurabk"], "title": title,
        "builddate": act["builddate"] or None, "doknr": key,
        "inkraft": act["inkraft"] or None, "fassung": fassung or None,
        "stand": {"raw": normzitat, "entries": ([{"type": "Vollzitat", "text": normzitat}]
                                                if normzitat else []) + [
            {"type": "Aenderung", "seq": v["seq"], "text": v["description"],
             "gvbl_citation": v["gvbl_citation"]} for v in versions]},
        "aenderungsverlauf": versions,
        "norms": [{"enbez": n["enbez"], "titel": n["titel"], "text": n["text"],
                   "gliederung": g} for n, g in zip(norms, glied)],
        "complete": True, "warnings": warnings,
    }


def _collapse(value: str) -> str:
    return " ".join(value.split())


BLOCK_TAGS = {"div", "p", "dl", "dt", "dd", "li", "ul", "ol", "table", "tr",
              "td", "th", "thead", "tbody", "h1", "h2", "h3", "h4", "h5", "h6",
              "br"}


def _render(node) -> str:
    """Text like ``ET.tostring(method='text')`` of the source XML: all text
    nodes in order, with a separator around block elements (the XML always
    has whitespace between list symbols, paragraphs and list items)."""
    from bs4 import Comment, NavigableString, Tag
    if isinstance(node, Comment):
        return ""
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag) or node.name in ("script", "style"):
        return ""
    classes = node.get("class") or []
    if "clearer" in classes:
        return " "
    if node.name == "sup" and "satznr" in classes:
        # XML: "… Bezirke. <satz.nr>2</satz.nr>Die …" -> "Bezirke. 2Die"
        return " " + _collapse(node.get_text()) + GLUE
    inner = "".join(_render(child) for child in node.children)
    return f" {inner} " if node.name in BLOCK_TAGS else inner


PARA_START = re.compile(r"^\(\d+[a-z]?\)")
GLUE = "\x00"


def html_norm_text(blocks: list) -> str:
    """Join the top-level body blocks of one norm like the XML flattening:
    a jurAbsatz starts a new line only where the page starts a numbered
    paragraph ``(n)``; list blocks and continuations stay on the line."""
    lines: list[str] = []
    for block in blocks:
        # the page keeps the XML's own blank after a sentence number
        # ("1 Art. 32" vs "2Art. 33"); only the marker goes
        text = _collapse(_render(block).replace(GLUE, ""))
        if not text:
            continue
        classes = block.get("class") or [] if hasattr(block, "get") else []
        new_para = (not lines) or ("paratext" in classes and PARA_START.match(text))
        if new_para:
            lines.append(text)
        else:
            lines[-1] = f"{lines[-1]} {text}"
    return "\n".join(lines)


META_RE = re.compile(r"^(Text gilt ab|in Kraft ab|gültig ab|gültig bis|Fassung|"
                     r"außer Kraft(?: ab| seit)?|Stand)\s*:\s*(.+)$", re.I)


def _iso(day: str) -> str | None:
    match = re.fullmatch(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})", day.strip())
    if not match:
        return None
    d, m, y = (int(g) for g in match.groups())
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def html_metadata(soup) -> dict:
    meta: dict[str, Any] = {"jurabk": None, "fields": {}}
    box = soup.select_one("#doc-metadata")
    if box is None:
        return meta
    bold = box.find("b")
    meta["jurabk"] = _collapse(bold.get_text()) if bold else None
    for cell in box.find_all("div"):
        if cell.find("div"):
            continue
        text = _collapse(cell.get_text(" "))
        match = META_RE.match(text)
        if match:
            meta["fields"][match.group(1)] = {"raw": match.group(2),
                                              "date": _iso(match.group(2))}
    return meta


def parse_bayern_html(data: bytes, key: str, jurabk: str, kind: str) -> dict:
    """Parse an archived BAYERN.RECHT page (full view, per-norm or landing)."""
    from bs4 import BeautifulSoup
    warnings: list[str] = []
    text = data.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text, "html.parser")
    title_tag = soup.find("title")
    if title_tag is not None and "Security Check" in title_tag.get_text():
        return {"jurabk": None, "title": "", "norms": [], "complete": False,
                "metadata": {}, "stand": {"raw": "", "entries": []},
                "toc": [], "warnings": ["archived page is a bot security check"]}
    content = soup.select_one("#content")
    meta = html_metadata(soup)
    if content is None:
        return {"jurabk": meta["jurabk"], "title": "", "norms": [],
                "complete": False, "metadata": meta, "toc": [],
                "stand": {"raw": "", "entries": []},
                "warnings": ["no #content element"]}
    if meta["jurabk"] and meta["jurabk"] != jurabk:
        warnings.append(f"page abbreviation {meta['jurabk']!r} != {jurabk!r}")
    _inline_footnotes(content)
    title_el = content.select_one(".titelzeile")
    stand_el = content.select_one(".standangabe")
    stand_raw = _collapse(stand_el.get_text(" ")) if stand_el else ""
    toc = []
    prefix = f"/Content/Document/{key}-"
    for link in content.select(".subdoc a[href]"):
        href = link["href"]
        if href.startswith(prefix):
            toc.append({"document": href.rsplit("/", 1)[-1],
                        "label": _collapse(link.get_text(" "))})
    norms, trail = [], {}
    for container in content.select(".cont"):
        for heading in container.select(".gltitel"):
            level = int(heading.name[1]) if re.fullmatch(r"h\d", heading.name or "") else 9
            trail = {k: v for k, v in trail.items() if k < level}
            trail[level] = _collapse(heading.get_text(" "))
        head = container.find("div", class_="paraheading")
        if head is None:
            continue
        nr = head.select_one(".paranr")
        tit = head.select_one(".paratitel")
        blocks = [child for child in container.children
                  if getattr(child, "name", None) and child is not head
                  and "clearer" not in (child.get("class") or [])]
        body = html_norm_text(blocks)
        enbez = _collapse(nr.get_text(" ")) if nr else ""
        titel = _collapse(tit.get_text(" ")) if tit else ""
        if not enbez and not titel:
            if body.startswith("München, den"):
                titel = "Schlussformel"     # the XML's para.titel for this block
            else:
                warnings.append("unlabelled norm block skipped")
                continue
        norms.append({"enbez": enbez, "titel": titel, "text": body,
                      "gliederung": " / ".join(trail[k] for k in sorted(trail))})
    labelled = [n["enbez"] for n in norms if n["enbez"]]
    complete = False
    if kind == "full":
        toc_labels = [t["label"].split(" ", 2)[:2] for t in toc
                      if re.fullmatch(re.escape(key) + r"-\d+[a-z]?", t["document"])]
        toc_labels = [" ".join(parts) for parts in toc_labels]
        if toc_labels:
            complete = bool(labelled) and toc_labels == labelled
            if not complete:
                warnings.append(f"full view TOC ({len(toc_labels)}) does not match "
                                f"norm bodies ({len(labelled)})")
        else:   # acts without Gliederung list the bodies directly
            complete = bool(labelled)
            warnings.append("no TOC on the full view (act without Gliederung); "
                            "completeness not cross-checked")
    if kind == "norm" and len(norms) != 1:
        warnings.append(f"per-norm page with {len(norms)} norm bodies")
    if kind == "landing":
        norms = []
    return {"jurabk": meta["jurabk"],
            "title": _collapse(title_el.get_text(" ")) if title_el else "",
            "metadata": meta, "stand": {"raw": stand_raw,
                                        "entries": parse_standangabe(stand_raw)},
            "toc": toc, "norms": norms, "complete": complete,
            "warnings": warnings}


def _inline_footnotes(content) -> None:
    """The XML carries a footnote inline (``<fn.call><fn.text>*)</fn.text>
    <fn.def>…</fn.def></fn.call>``); the page moves it into a trailing
    ``fn-container`` and adds a ``[Amtl. Anm.:]`` label.  Put the definition
    text back behind its marker, as the XML flattening reads it."""
    from bs4 import NavigableString
    for link in content.select("a.footnotelink-down[href^='#']"):
        target = content.find(id=link["href"][1:])
        box = target.find_parent("div", class_="fn") if target else None
        if box is None:
            continue
        parts = []
        for node in box.children:
            name = getattr(node, "name", None)
            if name == "sup" and node.select_one("a.footnotelink-up"):
                continue
            if name == "b" and re.fullmatch(r"\s*\[[^\]]*\]:?\s*", node.get_text()):
                continue
            parts.append(node.get_text() if name else str(node))
        anchor = link.find_parent("sup") or link
        anchor.insert_after(NavigableString(_collapse("".join(parts))))
    for node in content.select("div.fn-container, div.fn, hr.fn"):
        if node.parent is not None:
            node.decompose()


MWV_RE = re.compile(r"mWv\.?\s*(\d{1,2}\.\s?\d{1,2}\.\s?\d{4})")


def parse_standangabe(raw: str) -> list[dict]:
    """Split BAYERN.RECHT's Stand/Vollzitat line into typed entries."""
    if not raw:
        return []
    entries = []
    if raw.startswith("Vollzitat"):
        entries.append({"type": "Vollzitat", "text": raw})
    elif raw.lower().startswith("zuletzt geändert"):
        entry = {"type": "Stand", "text": raw}
        match = MWV_RE.search(raw)
        if match:
            entry["mwv"] = _iso(match.group(1).replace(" ", ""))
            entry["mwv_raw"] = match.group(0)
        entries.append(entry)
    else:
        entries.append({"type": "Sonst", "text": raw})
    return entries


def build_parsed(row: dict, data: bytes) -> dict:
    """SPEC ``lexgraph-history-parsed/1`` record for one ledger row + bytes."""
    act = ACTS[row["act_id"]]
    kind = row["kind"]
    if kind == "zip":
        parser, body = "byrecht-xml/1", parse_zip(data, act["key"], act["jurabk"])
    else:
        parser = "bayern-recht-html/1"
        body = parse_bayern_html(data, act["key"], act["jurabk"], kind)
    record = {
        "schema": PARSED_SCHEMA, "parser": parser, "source": row["source"],
        "act_id": row["act_id"], "kind": kind,
        "document": row.get("document"), "sha256": row["sha256"],
        "captured_at": row.get("captured_at"),
        "original_url": row.get("original_url"),
        "archive_url": row.get("archive_url"),
        "jurabk": body.get("jurabk") or act["jurabk"],
        "title": body.get("title", ""), "builddate": body.get("builddate"),
        "doknr": act["key"], "stand": body.get("stand"),
        "metadata": body.get("metadata"), "inkraft": body.get("inkraft"),
        "fassung": body.get("fassung"),
        "aenderungsverlauf": body.get("aenderungsverlauf"),
        "toc": body.get("toc"),
        "complete": body["complete"], "norm_count": len(body["norms"]),
        "norms": body["norms"], "warnings": body["warnings"],
    }
    return record


# --------------------------------------------------------------------- GVBl

MONTHS = {"januar": 1, "februar": 2, "märz": 3, "april": 4, "mai": 5,
          "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
          "november": 11, "dezember": 12}
LONG_DATE = r"(\d{1,2})\.\s*(Januar|Februar|März|April|Mai|Juni|Juli|August|" \
            r"September|Oktober|November|Dezember)\s+(\d{4})"
LONG_DATE_RE = re.compile(LONG_DATE)


def long_date(text: str) -> str | None:
    match = LONG_DATE_RE.search(text or "")
    if not match:
        return None
    day, month, year = match.groups()
    try:
        return date(int(year), MONTHS[month.lower()], int(day)).isoformat()
    except ValueError:
        return None


def parse_gvbl_volume(data: bytes) -> list[dict]:
    """Issues of one GVBl volume: issue, promulgation date, page range, PDF."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(data.decode("utf-8", errors="replace"), "html.parser")
    issues = []
    for row in soup.find_all("tr"):
        cells = [_collapse(td.get_text(" ")) for td in row.find_all("td")]
        link = row.find("a", href=True)
        if len(cells) < 4 or link is None:
            continue
        pages = re.fullmatch(r"(\d+)\s*-\s*(\d+)", cells[3])
        if not pages or not cells[1].isdigit():
            continue
        issues.append({"year": int(cells[0]), "issue": int(cells[1]),
                       "date": _iso(cells[2]), "first_page": int(pages.group(1)),
                       "last_page": int(pages.group(2)), "pdf": link["href"]})
    return issues


HEADING_ART_RE = re.compile(r"^(§|Art\.)\s*(\d+[a-z]?)(?![\d.])\b\s*(.*)$")


def _gvbl_lines(text_html) -> list[dict]:
    """Leaf block lines of the GVBl text (both page generations)."""
    from bs4 import Tag
    leaf_tags = ("p", "h1", "h2", "h3", "h4", "h5", "dl", "li", "tr", "div")
    lines = []
    for el in text_html.find_all(leaf_tags):
        if any(isinstance(d, Tag) and d.name in leaf_tags for d in el.descendants):
            continue
        text = _collapse(el.get_text(" "))
        if not text:
            continue
        classes = el.get("class") or []
        heading = (el.name in ("h1", "h2", "h3", "h4", "h5") or "center" in classes
                   or any(c.startswith("berschrift") for c in classes))
        lines.append({"text": text, "heading": heading, "tag": el.name,
                      "classes": classes})
    return lines


def _follows(previous: str | None, number: str) -> bool:
    """Amending acts number their own articles 1, 2, 3 … (maybe 1a): a heading
    that breaks the sequence belongs to quoted new wording."""
    if previous is None:
        return True
    p = re.fullmatch(r"(\d+)([a-z]?)", previous)
    n = re.fullmatch(r"(\d+)([a-z]?)", number)
    if not p or not n:
        return False
    pn, nn = int(p.group(1)), int(n.group(1))
    return nn == pn + 1 and not n.group(2) or (nn == pn and n.group(2) > p.group(2))


def gvbl_sections(lines: list[dict]) -> list[dict]:
    """Split GVBl text lines into article sections (``§ 1 …``/``Art. 1 …``).

    Only headings in the document's own unit and numbering sequence open a
    section; quoted new provisions ("Art. 66a Inklusionsamt" inside § 1)
    stay inside the article that inserts them."""
    sections, current, preamble = [], None, []
    own_prefix = None
    i = 0
    while i < len(lines):
        line = lines[i]
        match = HEADING_ART_RE.match(line["text"]) if line["heading"] else None
        if match and own_prefix is None:
            own_prefix = match.group(1)
        if match and (match.group(1) != own_prefix or not _follows(
                current["number"] if current else None, match.group(2))):
            match = None
        if match:
            title = match.group(3)
            # old pages put "§ 1" and "Änderung …" into two centred lines
            if not title and i + 1 < len(lines) and lines[i + 1]["heading"] \
                    and not HEADING_ART_RE.match(lines[i + 1]["text"]):
                title = lines[i + 1]["text"]
                i += 1
            current = {"prefix": match.group(1), "number": match.group(2),
                       "heading": f"{match.group(1)} {match.group(2)} {title}".strip(),
                       "title": title, "lines": []}
            sections.append(current)
        elif current is not None:
            current["lines"].append(line)
        else:
            preamble.append(line)
        i += 1
    return sections if sections else [{"prefix": None, "number": None,
                                       "heading": "", "title": "",
                                       "lines": preamble}]


def parse_gvbl_detail(data: bytes) -> dict:
    """Parse a www.verkuendung-bayern.de /gvbl/<year>-<page>/ detail page."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(data.decode("utf-8", errors="replace"), "html.parser")
    box = soup.select_one(".tx-gvbl") or soup
    out: dict[str, Any] = {"parser": "gvbl-detail-html/1", "warnings": []}
    h1 = box.find("h1")
    fund = re.search(r"GVBl\.\s*(\d{4})\s*S\.\s*(\d+)", h1.get_text(" ") if h1 else "")
    out["gazette"] = ({"name": "GVBl.", "year": int(fund.group(1)),
                       "page": int(fund.group(2)),
                       "reference": f"GVBl. {fund.group(1)} S. {fund.group(2)}"}
                      if fund else None)
    link = box.select_one("a.pdf_file")
    issue = re.search(r"(\d{4})/(\d+)\s*vom\s*(\d{2}\.\d{2}\.\d{4})",
                      _collapse(link.get_text(" ")) if link else "")
    out["issue"] = int(issue.group(2)) if issue else None
    out["issue_date"] = _iso(issue.group(3)) if issue else None
    out["pdf_url"] = link["href"] if link else None
    label = link.get("aria-label", "") if link else ""
    meta = re.search(r":\s*(.*?)\s+vom\s+(\d{2}\.\d{2}\.\d{4})\s*\(PDF", label)
    out["platform_title"] = meta.group(1).strip() if meta else None
    out["platform_date"] = _iso(meta.group(2)) if meta else None
    sha = box.select_one(".hashpdf strong")
    out["pdf_sha256"] = _collapse(sha.get_text()) if sha else None
    kind = box.select_one("#head-right h2")
    out["document_type"] = _collapse(kind.get_text()) if kind else None
    text_html = box.select_one(".text_html")
    if text_html is None:
        out["warnings"].append("no document text on detail page")
        out.update({"title": None, "title_date": None, "lines": [],
                    "sections": [], "signature": None, "text": ""})
        return out
    lines = _gvbl_lines(text_html)
    return {**out, **_gvbl_body(lines)}


def _gvbl_body(lines: list[dict]) -> dict:
    title, title_date = None, None
    for idx, line in enumerate(lines[:8]):
        if title is None and line["heading"] and not HEADING_ART_RE.match(line["text"]) \
                and len(line["text"]) > 12:
            title = line["text"]
            # new pages: title line, then "vom <date>"; old: "Vom <date>"
            for follow in lines[idx:idx + 3]:
                match = re.match(r"^(?:[Vv]om|Bekanntmachung vom)\s+" + LONG_DATE,
                                 follow["text"]) or re.search(r"\b[Vv]om\s+" + LONG_DATE + r"\s*$",
                                                              follow["text"])
                if match:
                    title_date = long_date(match.group(0))
                    break
    decision = re.search(r"Entscheidung\b.*?\bvom\s+" + LONG_DATE, title or "")
    decision_date = long_date(decision.group(0)[decision.group(0).rfind("vom"):]) \
        if decision else None
    signature = None
    for line in lines:
        match = re.match(r"^München,\s*(?:den\s*)?" + LONG_DATE, line["text"])
        if match:
            signature = {"raw": match.group(0), "date": long_date(match.group(0))}
    sections = gvbl_sections(lines)
    return {"title": title, "title_date": title_date, "decision_date": decision_date,
            "lines": lines,
            "sections": [{k: v for k, v in s.items() if k != "lines"}
                         | {"text": "\n".join(l["text"] for l in s["lines"]),
                            "items": [l["text"] for l in s["lines"]]}
                         for s in sections],
            "signature": signature,
            "text": "\n".join(l["text"] for l in lines)}


def pdf_text_pages(data: bytes) -> list[str]:
    """Text per PDF page.  poppler's ``pdftotext`` keeps the two-column GVBl
    reading order; pypdf (column-interleaving) is only the fallback."""
    import shutil
    import subprocess
    if shutil.which("pdftotext"):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as handle:
            handle.write(data)
            handle.flush()
            done = subprocess.run(["pdftotext", "-enc", "UTF-8", handle.name, "-"],
                                  capture_output=True, timeout=600, check=False)
        if done.returncode == 0:
            pages = done.stdout.decode("utf-8", errors="replace").split("\f")
            return pages[:-1] if pages and not pages[-1].strip() else pages
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    return [(page.extract_text() or "") for page in reader.pages]


def parse_gvbl_pdf_act(data: bytes, first_page: int, page: int,
                       title_date_hint: str | None) -> dict:
    return parse_gvbl_pdf_pages(pdf_text_pages(data), first_page, page,
                                title_date_hint)


def parse_gvbl_pdf_pages(pages: list[str], first_page: int, page: int,
                         title_date_hint: str | None) -> dict:
    """Cut one act out of a GVBl issue PDF: from its heading on ``page`` to
    the first signature line after it.  ``title_date_hint`` (the act date from
    the official version log, ISO) only locates the printed heading; the
    adoption date reported is the one printed in the signature line."""
    index = page - first_page
    out: dict[str, Any] = {"parser": "gvbl-issue-pdf/1", "warnings": [],
                           "pdf_page_index": index}
    if index < 0 or index >= len(pages):
        out["warnings"].append("page outside the issue PDF")
        return {**out, "title": None, "title_date": None, "sections": [],
                "signature": None, "text": ""}
    text = "\n".join(pages[index:index + 40])
    text = re.sub(r"(?<=[a-zäöüß])-\n(?=[a-zäöüß])", "", text)   # de-hyphenate
    raw_lines = [_collapse(line) for line in text.split("\n")]
    heading_at, title, title_date = None, None, None
    if title_date_hint:
        y, m, d = (int(x) for x in title_date_hint.split("-"))
        month = [k for k, v in MONTHS.items() if v == m][0]
        pattern = re.compile(rf"^[Vv]om\s+{d}\.\s*{month}\s+{y}\b", re.I)
        heading_at = next((i for i, line in enumerate(raw_lines) if pattern.match(line)), None)
    if heading_at is None:
        out["warnings"].append("act heading date not found on its page")
        return {**out, "title": None, "title_date": None, "sections": [],
                "signature": None, "text": ""}
    title_date = long_date(raw_lines[heading_at])
    back = []
    for line in reversed(raw_lines[max(0, heading_at - 5):heading_at]):
        if not line or re.search(r"[.:;]$", line) or re.fullmatch(r"[\d–\-A-Z/, ]+", line):
            break
        back.insert(0, line)
    title = " ".join(back) or None
    sig = re.compile(r"^München,\s*(?:den\s*)?" + LONG_DATE)
    end = next((i for i in range(heading_at + 1, len(raw_lines)) if sig.match(raw_lines[i])), None)
    body = raw_lines[heading_at + 1:(end + 1) if end is not None else len(raw_lines)]
    if end is None:
        out["warnings"].append("no signature line found")
    lines = [{"text": line, "heading": bool(re.fullmatch(r"(?:§|Art\.)\s*\d+[a-z]?", line)),
              "tag": "pdf", "classes": []} for line in body if line]
    # a PDF heading is "§ 1" alone on its line, its title on the next line
    for i, line in enumerate(lines[:-1]):
        if line["heading"]:
            follow = lines[i + 1]["text"]
            lines[i + 1]["heading"] = bool(
                len(follow) < 100 and not HEADING_ART_RE.match(follow)
                and not _level(follow) and not follow.startswith(("(", "„"))
                and not follow.endswith(":")
                and not re.search(r"\b(?:wird|werden|tritt|treten|ist|sind|erhält|"
                                  r"erhalten|gilt|hat|kann|verordnet)\b", follow))
    parsed = _gvbl_body(lines)
    parsed["title"], parsed["title_date"] = title, title_date
    parsed["signature"] = ({"raw": raw_lines[end], "date": long_date(raw_lines[end])}
                           if end is not None else None)
    parsed["sections"] = [_merge_pdf_items(sec) for sec in parsed["sections"]]
    return {**out, **parsed}


def _merge_pdf_items(section: dict) -> dict:
    """PDF layout lines -> logical items (a new item starts at a marker)."""
    items: list[str] = []
    for line in section.get("items") or []:
        if not items or _level(line) or ABSATZ_MARKER.match(line) or line.startswith("„"):
            items.append(line)
        else:
            items[-1] = f"{items[-1]} {line}"
    return {**section, "items": items, "text": "\n".join(items)}


# ---------------------------------------------------------- commencement

REL_PATTERNS = [
    (re.compile(r"am\s+Tag\s+nach\s+(?:ihrer|seiner|der)\s+(?:Verkündung|Bekanntmachung)"),
     "day_after_promulgation"),
    (re.compile(r"am\s+ersten\s+Tag\s+des\s+auf\s+(?:ihre|seine|die)\s+(?:Verkündung|Bekanntmachung)\s+folgenden\s+(?:Kalender)?[Mm]onats"),
     "first_day_of_following_month"),
    (re.compile(r"am\s+Tag\s+(?:ihrer|seiner|der)\s+(?:Verkündung|Bekanntmachung)"),
     "day_of_promulgation"),
]


SENTENCE_SPLIT = re.compile(
    r"(?<=[a-zäöüß)][.;])(?<!Abs\.)(?<!Nr\.)(?<!Nrn\.)(?<!Art\.)(?<! S\.)"
    r"(?<!Buchst\.)(?<!bzw\.)(?<!vgl\.)(?<!ff\.)\s+"
    r"(?=(?:\(\d+\)\s*)?(?:\d+\s*)?[A-ZÄÖÜ§])")


def _sentences(clause: str) -> list[str]:
    text = _collapse(clause)
    text = re.split(r"\bMünchen,\s*(?:den\s*)?\d", text)[0]   # drop signature
    parts = SENTENCE_SPLIT.split(text)
    return [p.strip() for p in parts if p.strip()]


REF_RE = re.compile(r"(§§?|Art\.)\s*(\d+[a-z]?)((?:\s+(?:Abs\.|Nr\.|Buchst\.|Satz)\s*\d*[a-z]?)*)")


def _split_enumerated(sentence: str) -> list[str]:
    """``Abweichend von Satz 1 tritt 1. § 2 mit Wirkung vom … und 2. § 1 Nr. 7
    mit Wirkung vom … in Kraft.`` -> one pseudo-sentence per list item."""
    items = re.split(r"\s(?=\d+\.\s+(?:§|Art\.)\s*\d)", sentence)
    if len(items) < 3 or len(re.findall(LONG_DATE, sentence)) < 2:
        return [sentence]
    head, tail = items[0], "in Kraft"
    out = []
    for item in items[1:]:
        item = re.sub(r"^\d+\.\s+", "", item)
        item = re.sub(r"\s+(?:und|sowie)\s*$|[,;]\s*$|\s*in Kraft\.?$", "", item.strip())
        out.append(f"{head} {item} {tail}.")
    return out


def parse_commencement(clause: str, promulgated: str | None) -> dict:
    """Commencement rules of one Inkrafttreten clause (verbatim sentences)."""
    rules = []
    sentences = [part for sentence in _sentences(clause)
                 for part in _split_enumerated(sentence)]
    for sentence in sentences:
        if "in Kraft" not in sentence:
            continue
        core = re.sub(r"^(?:\(\d+\)\s*)?(?:\d+\s*(?=[A-ZÄÖÜ]))?", "", sentence)
        core = re.sub(r"^Abweichend\s+von\s+(?:Satz|Abs\.)\s*\d+\s*", "", core)
        opening = re.search(r"\b(?:Dieses|Diese)\s+(?:Gesetz|Verordnung|Bekanntmachung|"
                            r"Statut|Satzung)\s+tr(?:itt|eten)\b", core)
        # a heading fragment may precede the clause ("Übergangsregelungen (1) 1Dieses …")
        general = bool(opening and len(core[:opening.start()]) < 40
                       and not REF_RE.search(core[:opening.start()]))
        refs = []
        if not general:
            verb = re.search(r"\btr(?:itt|eten)\b", core)
            if verb and not core[:verb.start()].strip():
                # inverted order: "… tritt § 3 mit Wirkung vom … in Kraft"
                subject = re.split(r"\s(?:am|mit Wirkung vom|mit Ablauf|in Kraft)\b",
                                   core[verb.end():], maxsplit=1)[0]
            else:
                subject = core[:verb.start()] if verb else core
            previous_end = None
            for match in REF_RE.finditer(subject):
                prefix = "Art." if match.group(1) == "Art." else "§"
                gap = subject[previous_end:match.start()] if previous_end is not None else None
                if refs and gap is not None and not gap.strip() and prefix != refs[-1]["prefix"]:
                    # "§ 3 Art. 22 Abs. 1": a provision inside § 3, not § 3 as a whole
                    inner = _collapse(match.group(0))
                    refs[-1]["sub"] = f"{refs[-1]['sub']} {inner}".strip() \
                        if refs[-1]["sub"] else inner
                    previous_end = match.end()
                    continue
                refs.append({"prefix": prefix, "number": match.group(2),
                             "sub": _collapse(match.group(3)) or None})
                previous_end = match.end()
            # "§§ 1 und 2" / "Art. 3 und 4"
            for extra in re.finditer(r"(?:,|und)\s*(\d+[a-z]?)\b(?!\s*(?:Abs|Nr|Satz))", subject):
                if refs and not any(r["number"] == extra.group(1) for r in refs):
                    refs.append({"prefix": refs[-1]["prefix"], "number": extra.group(1),
                                 "sub": None})
        in_force_part = sentence.split("außer Kraft")[0] if "außer Kraft" in sentence \
            and sentence.find("außer Kraft") < sentence.find("in Kraft") else sentence
        rule = {"sentence": sentence, "general": general, "refs": refs,
                "date": None, "status": "unknown", "retroactive": False,
                "relative": None}
        match = re.search(r"(?:am|mit Wirkung vom)\s+" + LONG_DATE, in_force_part)
        if match:
            rule["date"] = long_date(match.group(0))
            rule["status"] = "official_commencement_clause"
            rule["retroactive"] = match.group(0).startswith("mit Wirkung")
        else:
            for pattern, kind in REL_PATTERNS:
                if pattern.search(in_force_part):
                    rule["relative"] = kind
                    if promulgated:
                        base = date.fromisoformat(promulgated)
                        if kind == "day_after_promulgation":
                            value = base + timedelta(days=1)
                        elif kind == "day_of_promulgation":
                            value = base
                        else:
                            value = (base.replace(day=1) + timedelta(days=32)).replace(day=1)
                        rule["date"] = value.isoformat()
                        rule["status"] = "computed_from_commencement_clause"
                    break
        rules.append(rule)
    return {"clause": _collapse(clause), "rules": rules}


def in_force_for(commencement: dict | None, article: str | None,
                 nummer: str | None, prefix: str | None = None) -> dict:
    """Resolve the in-force date of one amending article (+ Nummer).
    ``prefix`` ("§"/"Art.") restricts matches to the amending act's unit."""
    if not commencement or not commencement.get("rules"):
        return {"date": None, "status": "unknown",
                "basis": "no Inkrafttreten clause found in the GVBl document",
                "clause": commencement.get("clause") if commencement else None}
    rules = commencement["rules"]
    general = [r for r in rules if r["general"]]
    specific = [r for r in rules if not r["general"] and r["refs"]]
    chosen, reason = None, None
    def same(ref):
        return ref["number"] == article and (prefix is None or ref["prefix"] == prefix)
    if article is not None:
        whole = [r for r in specific if any(same(ref) and not ref["sub"] for ref in r["refs"])]
        partial = [r for r in specific if any(same(ref) and ref["sub"] for ref in r["refs"])]
        if whole:
            chosen = whole[0]
        elif partial:
            wanted = f"Nr. {nummer}" if nummer else None
            hit = [r for r in partial if wanted and any(
                same(ref) and ref["sub"] and
                re.search(rf"\bNr\.\s*{re.escape(nummer)}\b", ref["sub"])
                for ref in r["refs"])]
            if hit:
                chosen = hit[0]
            elif nummer:
                chosen = general[0] if general else None
            else:
                reason = ("commencement differs within the amending article "
                          f"({'; '.join(r['sentence'] for r in partial)}) and the "
                          "Nummer of this change is not known")
    if chosen is None and reason is None:
        if general:
            chosen = general[0]
        elif len(rules) == 1:
            chosen = rules[0]
    if chosen is None:
        return {"date": None, "status": "unknown",
                "basis": reason or "commencement clause not resolvable for this article",
                "clause": commencement["clause"]}
    if chosen["date"] is None:
        return {"date": None, "status": "unknown",
                "basis": ("relative commencement but promulgation date unknown"
                          if chosen["relative"] else "commencement date not parseable"),
                "clause": chosen["sentence"]}
    basis = ("GVBl Inkrafttreten clause"
             + (" (retroactive: mit Wirkung vom)" if chosen["retroactive"] else "")
             + (f"; computed: {chosen['relative']} from the GVBl issue date"
                if chosen["relative"] else ""))
    return {"date": chosen["date"], "status": chosen["status"], "basis": basis,
            "clause": chosen["sentence"]}


# ------------------------------------------------------------ version log

def _open_snapshot_rows(path: Path) -> Iterator[dict]:
    import sys
    pipeline = str(ROOT / "pipeline")
    if pipeline not in sys.path:
        sys.path.insert(0, pipeline)
    from snapshot_storage import open_snapshot_text  # noqa: PLC0415
    with open_snapshot_text(path) as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def snapshot_root() -> Path:
    env = os.environ.get("LEXGRAPH_SNAPSHOTS")
    return Path(env) if env else MAIN_CHECKOUT / "data" / "snapshots"


def bayern_recht_snapshots() -> list[Path]:
    base = snapshot_root() / "bayern_recht"
    return sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


ART_PART = (r"(?:§§?|Art\.)\s*\d+[a-z]?(?:\s*(?:,|und)\s*\d+[a-z]?)*"
            r"(?:\s*(?:Abs\.|Nr\.|Buchst\.)\s*\d+[a-z]?)*")
CIT_RE = re.compile(
    rf"^(?P<art>{ART_PART}|Art\.)?\s*(?P<type>G|V|Urteil|Beschluss|Bek\.?|Entsch\.?)"
    r"\s+v\.\s*(?P<date>\d{1,2}\.\d{1,2}\.\d{4}),\s*(?P<pages>.*)$")


def parse_amending_article(text: str) -> dict:
    """``§ 1 Nr. 51`` / ``Art. 10 Abs. 2 Nr. 16`` / ``§§ 1, 2`` -> parts."""
    raw = _collapse(text or "")
    out = {"raw": raw or None, "prefix": None, "articles": [], "absatz": None,
           "nummer": None, "buchstabe": None}
    if not raw:
        return out
    match = re.match(r"^(§§?|Art\.)\s*(.*)$", raw)
    if not match:
        return out
    out["prefix"] = "Art." if match.group(1) == "Art." else "§"
    rest = match.group(2)
    head = re.split(r"\s+(?:Abs\.|Nr\.|Buchst\.)", rest, maxsplit=1)[0]
    out["articles"] = re.findall(r"\d+[a-z]?", head)
    for key, label in (("absatz", "Abs."), ("nummer", "Nr."), ("buchstabe", "Buchst.")):
        found = re.search(rf"{re.escape(label)}\s*(\d+[a-z]?|[a-z]{{1,3}}\))", rest)
        if found:
            out[key] = found.group(1).rstrip(")")
    return out


def parse_ffn_citation(text: str) -> dict:
    """``§ 1 Nr. 51 G v. 08.04.2013, S. 174, 547; 2014 S. 96`` -> fields."""
    raw = _collapse(text or "")
    match = CIT_RE.match(raw)
    out = {"raw": raw, "type": None, "act_date_text": None, "page": None,
           "further": None, "article": parse_amending_article("")}
    if not match:
        return out
    out["type"] = match.group("type")
    out["act_date_text"] = match.group("date")
    out["article"] = parse_amending_article(match.group("art") or "")
    pages = match.group("pages")
    first = re.match(r"S\.\s*(\d+)(.*)$", pages)
    if first:
        out["page"] = int(first.group(1))
        out["further"] = first.group(2).strip(" ,;") or None
    return out


XML_DESC_RE = re.compile(rf"^(?:(?P<art>{ART_PART})\s+)?(?P<title>.*?)\s+"
                         r"vom\s+(?P<date>\d{1,2}\.\d{1,2}\.\d{4})\s*$")


def parse_xml_entry(description: str, citation: str) -> dict:
    """aenderungsverlauf entry: ``§ 1 Nr. 51 LandesrechtsbereinigungsG vom 8.4.2013``."""
    desc = _collapse(description)
    match = XML_DESC_RE.match(desc)
    art = parse_amending_article(match.group("art") if match else "")
    title = match.group("title") if match else desc
    cit = _collapse(citation)
    gvbl = re.match(r"GVBl\.\s*(?:(\d{4})\s*)?S\.\s*(\d+)(.*)$", cit)
    return {"raw": desc, "short_title": title or None,
            "act_date_text": match.group("date") if match else None,
            "article": art, "gvbl_citation": cit or None,
            "gvbl_year": int(gvbl.group(1)) if gvbl and gvbl.group(1) else None,
            "page": int(gvbl.group(2)) if gvbl else None,
            "further": (gvbl.group(3).strip(" ,;") or None) if gvbl else None,
            "type": ("Entsch." if title.startswith("Entsch") else
                     "V" if re.match(r"^(V\b|Verordnung)", title) else
                     "G" if re.match(r"^(G\b|Gesetz)|G\b|gesetz", title) else None)}


def _dmy(text: str | None) -> date | None:
    if not text:
        return None
    match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if not match:
        return None
    d, m, y = (int(g) for g in match.groups())
    try:
        return date(y, m, d)
    except ValueError:
        return None


def load_version_log(act_id: str, snapshot: Path | None = None) -> list[dict]:
    """Official version log of one act: the aenderungsverlauf (XML) joined to
    the Fortführungsnachweis (ffn) rows by (act date, first GVBl page).

    ``act_date_text`` is the date printed in the log ("G v. 10.09.2007"); it
    is used only to match documents and is never reported as adopted."""
    act = ACTS[act_id]
    snapshot = snapshot or bayern_recht_snapshots()[-1]
    xml_rows, ffn_rows = [], []
    for row in _open_snapshot_rows(snapshot / "versions.jsonl"):
        if row.get("jurabk") != act["jurabk"]:
            continue
        (xml_rows if row.get("source") == "xml" else ffn_rows).append(row)
    entries = []
    used_ffn: set[int] = set()
    ffn_parsed = [(i, row, parse_ffn_citation(row.get("gvbl_citation", "")))
                  for i, row in enumerate(ffn_rows)]
    for row in xml_rows:
        xml = parse_xml_entry(row.get("description", ""), row.get("gvbl_citation", ""))
        xdate = _dmy(xml["act_date_text"])
        exact = [(i, r, c) for i, r, c in ffn_parsed if i not in used_ffn
                 and c["page"] == xml["page"] and _dmy(c["act_date_text"]) == xdate]
        near = [(i, r, c) for i, r, c in ffn_parsed if i not in used_ffn
                and c["page"] == xml["page"] and xdate and _dmy(c["act_date_text"])
                and _dmy(c["act_date_text"]).year == xdate.year]
        matched = exact or (near if len(near) == 1 else [])
        findings = []
        if matched and not exact:
            findings.append({"kind": "version_log_date_disagreement",
                             "detail": f"aenderungsverlauf date {xml['act_date_text']} vs "
                                       f"ffn date {matched[0][2]['act_date_text']} for "
                                       f"GVBl. S. {xml['page']}"})
        used_ffn.update(i for i, _, _ in matched)
        entries.append(_log_entry(act_id, xml, row, matched, findings))
    for i, row, cit in ffn_parsed:
        if i in used_ffn:
            continue
        entries.append(_log_entry(act_id, None, None, [(i, row, cit)], [{
            "kind": "ffn_row_without_aenderungsverlauf_entry",
            "detail": row.get("gvbl_citation")}]))
    entries.sort(key=lambda e: (_dmy(e["act_date_text"]) or date.min, e["page"] or 0))
    return entries


def _log_entry(act_id: str, xml: dict | None, xml_row: dict | None,
               ffn: list[tuple], findings: list[dict]) -> dict:
    cits = [c for _, _, c in ffn]
    first = cits[0] if cits else {}
    page = (xml or {}).get("page") or first.get("page")
    date_text = (xml or {}).get("act_date_text") or first.get("act_date_text")
    act_day = _dmy(date_text)
    candidates = []
    explicit = (xml or {}).get("gvbl_year")
    if page:
        if explicit:
            candidates.append({"year": explicit, "page": page,
                               "basis": "explicit year in citation"})
        elif act_day:
            candidates.append({"year": act_day.year, "page": page,
                               "basis": "year of the act date (verified against the GVBl document)"})
            if act_day.month == 12:
                candidates.append({"year": act_day.year + 1, "page": page,
                                   "basis": "following year (December act; verified against the GVBl document)"})
    article = xml["article"] if xml and xml["article"]["raw"] else \
        (first.get("article") if len(cits) == 1 else None) or parse_amending_article("")
    ffn_rows = []
    for (_, row, cit) in ffn:
        ffn_rows.append({"seq": row.get("seq"), "citation": cit["raw"],
                         "description": row.get("description"),
                         "article": cit["article"], "type": cit["type"],
                         "act_date_text": cit["act_date_text"], "page": cit["page"],
                         "further": cit["further"]})
        if xml and xml["article"]["raw"] and cit["article"]["articles"] and (
                not set(cit["article"]["articles"]) <= set(xml["article"]["articles"])
                or any(cit["article"][k] and xml["article"][k]
                       and cit["article"][k] != xml["article"][k]
                       for k in ("absatz", "nummer"))):
            findings.append({"kind": "version_log_article_disagreement",
                             "detail": f"aenderungsverlauf {xml['article']['raw']!r} vs "
                                       f"ffn {cit['article']['raw']!r}"})
    doc_type = first.get("type") or (xml or {}).get("type")
    return {
        "act_id": act_id,
        "xml_seq": xml_row.get("seq") if xml_row else None,
        "xml_description": xml["raw"] if xml else None,
        "short_title": xml["short_title"] if xml else None,
        "xml_citation": xml["gvbl_citation"] if xml else None,
        "ffn": ffn_rows,
        "act_date_text": date_text, "page": page,
        "further_pages": (xml or {}).get("further") or first.get("further"),
        "type": doc_type, "article": article,
        "gvbl_candidates": candidates, "findings": findings,
    }


# ------------------------------------------------------ ffn descriptors

OPS = [
    (re.compile(r"\bneu\s+eingef(?:ügt)?\.?"), "eingefügt"),
    (re.compile(r"\bneu\s+gef(?:asst|aßt)?\.?"), "neugefasst"),
    (re.compile(r"\beingef(?:ügt)?\.?"), "eingefügt"),
    (re.compile(r"\bangef(?:ügt)?\.?"), "angefügt"),
    (re.compile(r"\bgeänd(?:ert)?\.?"), "geändert"),
    (re.compile(r"\baufgeh(?:oben)?\.?"), "aufgehoben"),
    (re.compile(r"\bersetzt\b"), "ersetzt"),
    (re.compile(r"\bgestrichen\b"), "aufgehoben"),
]
SUB_RE = re.compile(r"^\s*(?:Abs\.|Absatz|Sätze|Satz|Nrn?\.|Buchst\.|bisherige[rs]?\s|"
                    r"UAbs\.|Halbsatz)")
NUM = r"\d+[a-z]?"


def _op_spans(text: str) -> list[tuple[int, int, str]]:
    spans = []
    for pattern, op in OPS:
        for match in pattern.finditer(text):
            if any(s <= match.start() < e for s, e, _ in spans):
                continue
            spans.append((match.start(), match.end(), op))
    return sorted(spans)


def _expand(prefix: str, start: str, end: str) -> list[str]:
    if start.isdigit() and end.isdigit() and int(start) <= int(end) <= int(start) + 300:
        return [str(n) for n in range(int(start), int(end) + 1)]
    return [start, end]


def parse_ffn_descriptor(description: str, prefix: str) -> dict:
    """Per-norm operations of one Fortführungsnachweis descriptor.

    A qualifier (``Abs. 3 aufgeh.``) makes the norm-level change ``geändert``;
    only an unqualified target takes the operation itself.  ``mehrfach
    geänd.`` names no norm and is reported as ``unspecified``."""
    text = _collapse(description or "")
    result = {"raw": text, "unspecified": "mehrfach" in text.casefold(),
              "targets": [], "non_norm_targets": [], "no_operation": False}
    if not text or result["unspecified"]:
        return result
    spans = _op_spans(text)
    segments = []
    last = 0
    for start, end, op in spans:
        segments.append((text[last:start], op))
        last = end
    tail = text[last:].strip(" ,.;")
    if tail:
        segments.append((tail, None))
    if not spans:
        result["no_operation"] = True
    targets: dict[str, dict] = {}
    order: list[str] = []
    current_prefix = None
    last_norm = None
    for segment, op in segments:
        seg = segment.strip(" ,;")
        # structural containers: "Teil 15 (Art. 117)" -> the bracketed norms
        seg = re.sub(r"Teil\s+\d+[a-z]?\s*\(([^)]*)\)", r"\1", seg)
        if re.match(r"^(?:IÜ|Inhaltsübersicht)\b", seg):
            result["non_norm_targets"].append({"target": "Inhaltsübersicht", "operation": op})
            seg = re.sub(r"^(?:IÜ|Inhaltsübersicht)\s*(?:,|und)?\s*", "", seg)
        pieces = re.split(r"(?=(?:§§?|Art\.)\s*\d)", seg)
        for piece in pieces:
            piece = piece.strip(" ,;")
            if not piece:
                continue
            match = re.match(r"^(§§?|Art\.)\s*(.*)$", piece)
            if match:
                current_prefix = "Art." if match.group(1) == "Art." else "§"
                body = match.group(2)
            elif re.match(rf"^{NUM}\b", piece) and current_prefix:
                body = piece
            elif SUB_RE.match(piece) and last_norm:
                targets[last_norm]["qualified"].append({"text": piece, "operation": op})
                continue
            else:
                result["non_norm_targets"].append({"target": piece, "operation": op})
                continue
            # numbers until a qualifier starts
            qual = SUB_RE.search(" " + body) if False else re.search(
                r"\s(?:Abs\.|Absatz|Sätze|Satz|Nrn?\.|Buchst\.|UAbs\.)", " " + body)
            nums_part = body[:qual.start() - 1] if qual else body
            qualifier = body[qual.start() - 1:].strip() if qual else ""
            numbers: list[str] = []
            for rng in re.finditer(rf"({NUM})(?:\s*(?:bis|-|–)\s*({NUM}))?", nums_part):
                numbers.extend(_expand(current_prefix, rng.group(1), rng.group(2))
                               if rng.group(2) else [rng.group(1)])
            for index, number in enumerate(numbers):
                label = f"{current_prefix if current_prefix == 'Art.' else '§'} {number}"
                if label not in targets:
                    targets[label] = {"norm": label, "whole": [], "qualified": []}
                    order.append(label)
                is_last = index == len(numbers) - 1
                if qualifier and is_last:
                    targets[label]["qualified"].append({"text": qualifier, "operation": op})
                else:
                    targets[label]["whole"].append(op)
                last_norm = label
    for label in order:
        target = targets[label]
        whole = [op for op in target["whole"] if op]
        if whole:
            change = {"angefügt": "eingefügt"}.get(whole[-1], whole[-1])
        elif target["qualified"] and any(q["operation"] for q in target["qualified"]):
            change = "geändert"
        else:
            change = "unbekannt"
        result["targets"].append({"norm": label, "change_type": change,
                                  "whole_operations": target["whole"],
                                  "qualified": target["qualified"]})
    return result


# ----------------------------------------------------- GVBl commands

LEVEL_MARKERS = [
    (1, re.compile(r"^(\d{1,3}[a-z]?)\.\s+(?=\S)")),
    (4, re.compile(r"^([a-z])\1\1\)\s+")),
    (3, re.compile(r"^([a-z])\1\)\s+")),
    (2, re.compile(r"^([a-z])\)\s+")),
]
ABSATZ_MARKER = re.compile(r"^\((\d+[a-z]?)\)\s+")
QUOTE_RE = re.compile(r"„[^“]*“|\"[^\"]*\"")
VERB_RE = re.compile(r"\b(?:wird|werden|erhält|erhalten|tritt|treten)\b")
OTHER_ACT_RE = re.compile(r"\s(?:des|der)\s+(?:Gesetzes|Verordnung|[A-ZÄÖÜ][\wäöüß-]*"
                          r"(?:gesetzes|verordnung|ordnung))\b")
TARGET_REF_RE = re.compile(r"(Art\.|§§?)\s*(\d+[a-z]?)((?:\s*(?:,|und|bis)\s*\d+[a-z]?)*)"
                           r"(\s+(?:Abs\.|Absatz|Satz|Sätze|Nr\.|Nrn\.|Buchst\.|Halbsatz|UAbs\.))?")
CITED_BY_RE = re.compile(r"\b(?:zuletzt\s+)?geändert\s+durch\b|\b(?:die|das|der)\s+"
                         r"(?:zuletzt\s+)?durch\b|\(")
INSERT_RE = re.compile(r"(?:folgende[rs]?|die\s+folgenden)\s+(Art\.|§§?)\s*(\d+[a-z]?)"
                       r"((?:\s*(?:,|und|bis)\s*\d+[a-z]?)*)\s+(?:wird|werden)?\s*"
                       r"(?:eingefügt|angefügt|vorangestellt)")
RENUMBER_RE = re.compile(r"(?:bisherige[rn]?\s+)?(Art\.|§)\s*(\d+[a-z]?)\s+wird\s+(?:Art\.|§)\s*(\d+[a-z]?)")


def _level(line: str) -> tuple[int, str] | None:
    for level, pattern in LEVEL_MARKERS:
        match = pattern.match(line)
        if match:
            return level, match.group(1)
    return None


def _unquoted(text: str) -> str:
    return QUOTE_RE.sub("„…“", text)


def _command_lines(lines: list[str]) -> list[str]:
    """Drop quoted new wording (lines inside „…“) from a command container."""
    out, depth = [], 0
    for line in lines:
        if depth == 0:
            out.append(line)
        opens, closes = line.count("„"), line.count("“")
        depth = max(0, depth + opens - closes)
    return out


def _numbers(prefix: str, first: str, rest: str) -> list[str]:
    numbers = [first]
    for sep, number in re.findall(r"\s*(,|und|bis)\s*(\d+[a-z]?)", rest or ""):
        if sep == "bis" and numbers[-1].isdigit() and number.isdigit():
            numbers.extend(str(n) for n in range(int(numbers[-1]) + 1, int(number) + 1))
        else:
            numbers.append(number)
    return numbers


def command_targets(head: str, prefix: str, default_predicate: str = "") -> list[dict]:
    """Norm targets of one amending command (its first sentence).
    ``default_predicate`` supplies the verb of list items such as
    "Folgende Vorschriften werden aufgehoben: … 51. Art. 117 und 118 …"."""
    text = _unquoted(_collapse(head))
    targets: list[dict] = []
    renumber = RENUMBER_RE.search(text)
    if renumber and not INSERT_RE.search(text):
        old, new = renumber.group(2), renumber.group(3)
        return [{"norm": f"{prefix} {old}", "change_type": "geändert",
                 "operation": f"umnummeriert (wird {prefix} {new})"},
                {"norm": f"{prefix} {new}", "change_type": "geändert",
                 "operation": f"umnummeriert (bisher {prefix} {old})"}]
    for match in INSERT_RE.finditer(text):
        for number in _numbers(prefix, match.group(2), match.group(3)):
            targets.append({"norm": f"{prefix} {number}", "change_type": "eingefügt",
                            "operation": "eingefügt"})
    if targets:
        return targets
    verb = VERB_RE.search(text)
    subject = text[:verb.start()] if verb else text
    if re.search(r"Inhaltsübersicht|Inhaltsverzeichnis", subject):
        return []                       # table of contents, not a norm
    for cut in (OTHER_ACT_RE.search(subject), CITED_BY_RE.search(subject)):
        if cut:
            subject = subject[:cut.start()]
    predicate = text[verb.start():] if verb else default_predicate
    repeal = bool(re.match(r"^(?:wird|werden)\s+aufgehoben\b", predicate))
    rewrite = bool(re.match(r"^(?:erhält|erhalten)\s+folgende\s+Fassung|"
                            r"^(?:wird|werden)\s+wie\s+folgt\s+gefasst", predicate))
    replace = bool(re.match(r"^(?:wird|werden)\s+durch\s+(?:den|die)?\s*folgenden?\s+"
                            r"(?:Art\.|§)", predicate))
    for match in TARGET_REF_RE.finditer(subject):
        qualified = bool(match.group(4))
        numbers = _numbers(prefix, match.group(2), match.group(3))
        for index, number in enumerate(numbers):
            is_last = index == len(numbers) - 1
            whole = not (qualified and is_last)
            change = ("aufgehoben" if repeal and whole else
                      "neugefasst" if rewrite and whole else
                      "ersetzt" if replace and whole else "geändert")
            targets.append({"norm": f"{prefix} {number}", "change_type": change,
                            "operation": change if whole else f"{change} (Teil)"})
    return targets


STRUCTURE_RE = re.compile(r"^(?:Der\s+|Die\s+|Das\s+|Nach\s+|Vor\s+|Dem\s+)?(?:bisherige[rn]?\s+)?"
                          r"(?:Teil|Abschnitt|Kapitel|Unterabschnitt|Gliederung)\b")


def structural_targets(head: str, lines: list[str], prefix: str) -> list[dict]:
    """"Teil 7a wird wie folgt gefasst: „… Art. 66a Inklusionsamt …“" names
    no norm itself; the norms are the headings of the quoted new wording."""
    text = _unquoted(_collapse(head))
    if not STRUCTURE_RE.match(text):
        return []
    insert = bool(re.search(r"\b(?:eingefügt|angefügt|vorangestellt)\b", text))
    rewrite = bool(re.search(r"wie\s+folgt\s+gefasst|folgende\s+Fassung|durch\s+folgende", text))
    if not (insert or rewrite):
        return []
    targets, seen = [], set()
    for line in lines[1:]:
        match = re.match(rf"^„?\s*({re.escape(prefix)})\s*(\d+[a-z]?)(?:\s+[A-ZÄÖÜ(][^.]*)?$",
                         line)
        if match and match.group(2) not in seen:
            seen.add(match.group(2))
            change = "eingefügt" if insert else "neugefasst"
            targets.append({"norm": f"{prefix} {match.group(2)}", "change_type": change,
                            "operation": f"{change} (als Teil der neu gefassten Gliederungseinheit)"})
    return targets


def _container(section_lines: list[str], nummer: str | None,
               absatz: str | None) -> tuple[list[str], int] | None:
    """Lines amending the target act inside one GVBl article section and the
    item level of its commands."""
    lines = section_lines
    if absatz:
        start = next((i for i, l in enumerate(lines)
                      if (m := ABSATZ_MARKER.match(l)) and m.group(1) == absatz), None)
        if start is None:
            return None
        end = next((i for i in range(start + 1, len(lines))
                    if ABSATZ_MARKER.match(lines[i])), len(lines))
        lines = [ABSATZ_MARKER.sub("", lines[start], count=1)] + lines[start + 1:end]
    if nummer:
        start = next((i for i, l in enumerate(lines)
                      if (lv := _level(l)) and lv == (1, nummer)), None)
        if start is None:
            return None
        end = next((i for i in range(start + 1, len(lines))
                    if (lv := _level(lines[i])) and lv[0] == 1), len(lines))
        head = LEVEL_MARKERS[0][1].sub("", lines[start], count=1)
        return [head] + lines[start + 1:end], 2
    return lines, 1


def extract_commands(section: dict, prefix: str, nummer: str | None = None,
                     absatz: str | None = None) -> dict:
    """Commands (Nummer/Buchstabe + target norms) for one amending article."""
    # locate the container first: an unbalanced quote elsewhere in a long
    # omnibus act must not hide it; quoted new wording is dropped inside it
    found = _container(section.get("items") or [], nummer, absatz)
    if found is None:
        return {"status": "container_not_found", "commands": []}
    lines, level = found
    items, intro = [], []
    depth = 0
    for line in lines:
        # an item starts only outside quoted new wording
        lv = _level(line) if depth == 0 else None
        if lv and lv[0] == level:
            items.append({"marker": lv[1], "lines": [line]})
        elif items:
            items[-1]["lines"].append(line)
        else:
            intro.append(line)
        depth = max(0, depth + line.count("„") - line.count("“"))
    # "Art. 5 des Gesetzes … wird wie folgt geändert: 1. Abs. 1a …": the
    # intro names the norm, the items only its sub-units
    intro_targets = (command_targets(intro[0], prefix)
                     if intro and items and re.match(r"^(?:In\s+|Dem\s+|Der\s+|Die\s+)?"
                                                     r"(?:Art\.|§)\s*\d", intro[0])
                     else [])
    context = " ".join([section.get("title") or ""] + (section.get("items") or [])[:1])
    default = ("werden aufgehoben" if re.search(r"\bAufhebung\b|werden\s+aufgehoben:|"
                                                r"treten\s+außer\s+Kraft:", context)
               else "")
    if not items:  # a single command, e.g. "(276) In § 2 Satz 1 der …"
        head = lines[0] if lines else ""
        return {"status": "single_command", "commands": [{
            "nummer": nummer, "buchstabe": None, "raw": _collapse(" ".join(lines))[:2000],
            "targets": command_targets(head, prefix, default)}]}
    commands = []
    expected = 1
    for item in items:
        # PDF layout can interleave columns: only an unbroken 1, 2, 3 …
        # sequence (letters a, b, c … on level 2) keeps its marker
        marker = item["marker"]
        want = str(expected) if level == 1 else chr(ord("a") + expected - 1)
        in_sequence = marker == want
        if in_sequence:
            expected += 1
        head = re.sub(r"^\S+\s+", "", item["lines"][0], count=1)
        targets = command_targets(head, prefix) or structural_targets(head, item["lines"], prefix)
        if not targets and intro_targets:
            targets = [{"norm": t["norm"], "change_type": "geändert",
                        "operation": "geändert (Teil)", "inherited": True}
                       for t in intro_targets]
        commands.append({
            "nummer": (marker if in_sequence else None) if level == 1 else nummer,
            "buchstabe": (marker if in_sequence else None) if level == 2 else None,
            "in_sequence": in_sequence,
            "raw": _collapse(" ".join(item["lines"]))[:2000],
            "targets": targets})
    return {"status": "items", "commands": commands}


def decision_targets(doc: dict, prefix: str) -> list[dict]:
    """Norms a published Normenkontrolle decision declares invalid."""
    text = _collapse(doc.get("text") or "")
    formula = text.split("Entscheidungsformel:", 1)
    if len(formula) < 2:
        return []
    body = formula[1].split("München, den")[0]
    end = re.search(r"\berklärt\b", body)
    sentence = body[:end.end()] if end else body
    if not re.search(r"unwirksam|nichtig|ungültig", sentence):
        return []
    subject = OTHER_ACT_RE.split(sentence, maxsplit=1)[0]
    targets = []
    for match in TARGET_REF_RE.finditer(subject):
        qualified = bool(match.group(4))
        numbers = _numbers(prefix, match.group(2), match.group(3))
        for index, number in enumerate(numbers):
            whole = not (qualified and index == len(numbers) - 1)
            label = f"{prefix} {number}"
            if any(t["norm"] == label for t in targets):
                continue
            targets.append({"norm": label,
                            "change_type": "aufgehoben" if whole else "geändert",
                            "operation": "für unwirksam erklärt" + ("" if whole else " (Teil)")})
    return targets


def section_for(doc: dict, act_id: str, article: str | None) -> dict | None:
    """The GVBl article that amends ``act_id`` (by number, else by name)."""
    sections = [s for s in doc.get("sections") or [] if s.get("number")]
    if article:
        hit = [s for s in sections if s["number"] == article]
        return hit[0] if hit else None
    names = ACT_NAMES[act_id]
    named = [s for s in sections if any(n in (s.get("title") or "") or
                                        n in " ".join((s.get("items") or [])[:2])
                                        for n in names)]
    if len(named) == 1:
        return named[0]
    amending = [s for s in sections if "Inkrafttreten" not in (s.get("title") or "")
                and "Inkraft" not in (s.get("heading") or "")]
    if len(amending) == 1:
        return amending[0]
    return None


ACT_NAMES = {
    "by_agsg": ("Ausführung der Sozialgesetze", "(AGSG)"),
    "by_agvwgo": ("Ausführung der Verwaltungsgerichtsordnung", "(AGVwGO)"),
    "by_dvasyl": ("Asyldurchführungsverordnung", "(DVAsyl)", "(DVAsyI)"),
}


def commencement_section(doc: dict) -> dict | None:
    for section in reversed(doc.get("sections") or []):
        if re.search(r"Inkraft|In-Kraft|Geltungsdauer", section.get("heading") or "") \
                and "in Kraft" in (section.get("text") or ""):
            return section
    # single-article documents: the clause may close the last article
    for section in reversed(doc.get("sections") or []):
        if "in Kraft" in (section.get("text") or ""):
            return section
    return None


# ------------------------------------------------------------- building

AS_OF = os.environ.get("LEXGRAPH_AS_OF") or date.today().isoformat()
ROLE_RANK = {"official_consolidation": 0, "official_promulgation": 1,
             "archived_capture": 2, "reconstruction": 3}
CONSOLIDATION_BASIS = (
    "BAYERN.RECHT act-level consolidation date ('Text gilt ab'/'in Kraft ab' on "
    "the page, <inkraft> in the XML): the consolidated state that showed this "
    "text applied from this date; it is not this norm's own start date")


def _act_row(act_id: str) -> dict:
    return {"id": act_id, "juris": "DE-BY"}


def norm_id_for(act_id: str, enbez: str, titel: str = "") -> str:
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from api.norm_identity import act_prefix, designator_unit  # noqa: PLC0415
    unit, number = designator_unit(enbez or titel)
    return f"{act_prefix(_act_row(act_id))}/{unit}/{number}"


def norm_ids(act_id: str, norms: list[dict]) -> list[tuple[str, dict]]:
    import sys
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from api.norm_identity import norm_rows_with_ids  # noqa: PLC0415
    return norm_rows_with_ids(_act_row(act_id), norms)


def content_hash(enbez: str, titel: str, text: str) -> str:
    """Same payload hash as ``api.norm_history.norm_content_hash``."""
    payload = {"enbez": enbez or "", "titel": titel or "", "text": text or ""}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _builddate(value: str | None) -> str | None:
    match = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", value or "")
    return f"{match.group(3)}-{match.group(2)}-{match.group(1)}" if match else None


def parsed_capture(row: dict, root=None, refresh: bool = False) -> dict:
    """Parse (and cache) the raw object behind one ok/skipped ledger row."""
    source, act_id, digest = row["source"], row["act_id"], row["sha256"]
    if not refresh:
        cached = get_parsed(source, act_id, digest, root)
        if cached is not None and cached.get("parser_version") == PARSER_VERSION:
            return cached
    data = get_raw(source, digest, row.get("ext") or
                   ("zip" if row["kind"] == "zip" else "html"), root)
    record = build_parsed(row, data)
    record["parser_version"] = PARSER_VERSION
    put_parsed(record, root)
    return record


PARSER_VERSION = "2026-09-28.16"


def wayback_observations(act_id: str, root=None) -> tuple[list[dict], list[dict]]:
    """Per-norm observations from every archived capture of ``act_id``, plus
    act-level stand observations (landing pages, Vollzitat, Text gilt ab)."""
    rows = [r for r in read_ledger("wayback_bayern", root)
            if r.get("act_id") == act_id and r.get("status") in ("ok", "skipped")
            and r.get("kind") in ("zip", "full", "norm", "landing", "anlage")
            and r.get("sha256")]
    reps = {}
    for row in rows:
        if row["status"] == "ok":
            reps.setdefault(row["sha256"], row)
    observations, stands = [], []
    for row in rows:
        rep = reps.get(row["sha256"])
        if rep is None:
            continue
        parsed = parsed_capture(rep, root)
        known_at = row["captured_at"][:10]
        meta = (parsed.get("metadata") or {}).get("fields") or {}
        tga = meta.get("Text gilt ab") or meta.get("in Kraft ab") or {}
        cons_date = tga.get("date") if tga else parsed.get("inkraft")
        cons_raw = (f"Text gilt ab: {tga['raw']}" if meta.get("Text gilt ab") else
                    f"in Kraft ab: {tga['raw']}" if tga else
                    f"<inkraft>{parsed['inkraft']}</inkraft>" if parsed.get("inkraft") else None)
        base = {"act_id": act_id, "source": "Wayback/BAYERN.RECHT",
                "role": "archived_capture", "kind": row["kind"],
                "url": row["original_url"], "archive_url": row["archive_url"],
                "known_at": known_at, "captured_at": row["captured_at"],
                "sha256": row["sha256"], "consolidation_date": cons_date,
                "consolidation_raw": cons_raw,
                "builddate": _builddate(parsed.get("builddate")),
                "fassung": ((meta.get("Fassung") or {}).get("date") or parsed.get("fassung")),
                "parser": parsed["parser"]}
        stand = (parsed.get("stand") or {})
        stands.append({**base, "stand": stand.get("raw") or None,
                       "stand_entries": stand.get("entries") or [],
                       "complete": parsed["complete"], "norm_count": parsed["norm_count"],
                       "warnings": parsed.get("warnings") or []})
        if row["kind"] == "landing" or not parsed["norms"]:
            continue
        if row["kind"] in ("zip", "full"):
            pairs = norm_ids(act_id, parsed["norms"])
        else:
            pairs = [(norm_id_for(act_id, n["enbez"], n["titel"]), n)
                     for n in parsed["norms"][:1]]
        for norm_id, norm in pairs:
            observations.append({**base, "norm_id": norm_id, "enbez": norm["enbez"],
                                 "titel": norm["titel"], "text": norm["text"],
                                 "identity": norm_identity(norm["enbez"], norm["titel"],
                                                           norm["text"]),
                                 "complete_state": parsed["complete"]})
    return observations, stands


def official_observations(act_id: str) -> tuple[list[dict], list[dict]]:
    """Lexgraph's own daily BAYERN.RECHT snapshots (official consolidation;
    derived text, raw bytes not retained) plus the published act JSON."""
    act = ACTS[act_id]
    observations, stands = [], []
    zip_url = f"https://www.gesetze-bayern.de/Content/Zip/{act['key']}"
    for snap in bayern_recht_snapshots():
        acts = [r for r in _open_snapshot_rows(snap / "acts.jsonl")
                if r.get("key") == act["key"]]
        if not acts:
            continue
        meta = acts[0]
        known_at = _builddate(meta.get("builddate")) or snap.name
        norms = [r for r in _open_snapshot_rows(snap / "norms.jsonl")
                 if r.get("key") == act["key"]]
        base = {"act_id": act_id, "source": "BAYERN.RECHT",
                "role": "official_consolidation", "kind": "daily_snapshot",
                "url": zip_url, "archive_url": None, "known_at": known_at,
                "captured_at": None, "sha256": None,
                "consolidation_date": meta.get("inkraft") or None,
                "consolidation_raw": (f"<inkraft>{meta['inkraft']}</inkraft>"
                                      if meta.get("inkraft") else None),
                "builddate": known_at, "parser": "byrecht-xml/1",
                "snapshot": f"bayern_recht/{snap.name}"}
        stands.append({**base, "stand": None, "stand_entries": [],
                       "complete": True, "norm_count": len(norms), "warnings": []})
        for norm_id, norm in norm_ids(act_id, norms):
            observations.append({**base, "norm_id": norm_id, "enbez": norm["enbez"],
                                 "titel": norm["titel"], "text": norm["text"],
                                 "identity": norm_identity(norm["enbez"], norm["titel"],
                                                           norm["text"]),
                                 "complete_state": True})
    return observations, stands


def current_act(act_id: str) -> dict:
    path = MAIN_CHECKOUT / "web" / "data" / "acts" / f"{act_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate_against_current(act_id: str, observations: list[dict],
                             changes: list[dict] | None = None) -> dict:
    """Match rate of the newest archived capture of each norm vs today's text.

    A mismatch is "explained" when the version log dates a change of that
    norm after what the capture showed (the capture predates the change)."""
    act = current_act(act_id)
    current = {nid: row for nid, row in norm_ids(act_id, act["norms"])}
    latest: dict[str, dict] = {}
    for obs in observations:
        if obs["role"] != "archived_capture":
            continue
        prev = latest.get(obs["norm_id"])
        if prev is None or obs["captured_at"] > prev["captured_at"]:
            latest[obs["norm_id"]] = obs
    exact = identity = compared = 0
    mismatches = []
    for norm_id, obs in latest.items():
        row = current.get(norm_id)
        if row is None:
            continue
        compared += 1
        same_identity = obs["identity"] == norm_identity(row["enbez"], row["titel"], row["text"])
        identity += same_identity
        exact += (obs["text"] == row["text"] and obs["titel"] == row["titel"]
                  and obs["enbez"] == row["enbez"])
        if not same_identity:
            shown = obs.get("consolidation_date") or obs["known_at"]
            later = [c["amending_act"]["citation"] for c in changes or []
                     if c["norm_id"] == norm_id and any(
                         d and d > shown for d in (c["dates"]["in_force"]["date"],
                                                   c["dates"]["promulgated"]["date"]))]
            mismatches.append({"norm_id": norm_id, "captured_at": obs["captured_at"],
                               "archive_url": obs["archive_url"],
                               "explained_by_later_change": later})
    explained = sum(1 for m in mismatches if m["explained_by_later_change"])
    unchanged = compared - explained
    return {"act_id": act_id, "compared": compared, "exact": exact,
            "identity": identity, "current_norms": len(current),
            "exact_rate": round(exact / compared, 4) if compared else None,
            "identity_rate": round(identity / compared, 4) if compared else None,
            "explained_by_later_change": explained,
            "identity_rate_unchanged_norms": round(identity / unchanged, 4) if unchanged else None,
            "exact_rate_unchanged_norms": round(exact / unchanged, 4) if unchanged else None,
            "mismatches": mismatches}


# ------------------------------------------------------ GVBl resolution

VB_BASE = "https://www.verkuendung-bayern.de"


def gvbl_index(root=None) -> dict:
    detail, pdfs = {}, []
    for row in read_ledger("gvbl", root):
        if row.get("status") != "ok":
            continue
        if row.get("kind") == "gvbl_detail":
            detail[(row["gvbl_year"], row["gvbl_page"])] = row
        elif row.get("kind") == "gvbl_issue_pdf":
            pdfs.append(row)
    return {"detail": detail, "pdfs": pdfs}


def _pdf_pages_cached(row: dict, root=None) -> list[str]:
    act_id = row.get("act_id") or "gvbl"
    cached = get_parsed("gvbl", act_id, row["sha256"], root)
    if cached and cached.get("parser_version") == PARSER_VERSION:
        return cached["pages"]
    pages = pdf_text_pages(get_raw("gvbl", row["sha256"], "pdf", root))
    put_parsed({"schema": PARSED_SCHEMA, "parser": "gvbl-issue-pdf-text/1",
                "parser_version": PARSER_VERSION, "source": "gvbl",
                "act_id": act_id, "sha256": row["sha256"],
                "captured_at": row.get("captured_at"),
                "original_url": row["original_url"], "archive_url": None,
                "complete": True, "norm_count": 0, "norms": [], "pages": pages,
                "warnings": []}, root)
    return pages


def _gvbl_detail_cached(row: dict, root=None) -> dict:
    """Parse (and keep, per raw object) one GVBl detail page."""
    act_id = row.get("act_id") or "gvbl"
    cached = get_parsed("gvbl", act_id, row["sha256"], root)
    if cached and cached.get("parser_version") == PARSER_VERSION:
        return cached["document"]
    doc = parse_gvbl_detail(get_raw("gvbl", row["sha256"], row.get("ext") or "html", root))
    doc.pop("lines", None)
    put_parsed({"schema": PARSED_SCHEMA, "parser": doc["parser"],
                "parser_version": PARSER_VERSION, "source": "gvbl", "act_id": act_id,
                "sha256": row["sha256"], "captured_at": row.get("captured_at"),
                "original_url": row["original_url"], "archive_url": None,
                "title": doc.get("title"), "complete": bool(doc.get("sections")),
                "norm_count": 0, "norms": [], "document": doc,
                "warnings": doc.get("warnings") or []}, root)
    return doc


def _linked_pdf(detail: dict, index: dict, hint_iso: str | None, root=None) -> dict | None:
    """The issue PDF a detail page links to (``…/gvbl-2018-13.pdf#page=5``)."""
    link = detail.get("pdf_url") or ""
    match = re.search(r"#page=(\d+)", link)
    url = VB_BASE + link.split("#")[0] if link.startswith("/") else link.split("#")[0]
    prow = next((r for r in index["pdfs"] if r["original_url"] == url), None)
    if prow is None or not match:
        return None
    pages = _pdf_pages_cached(prow, root)
    first_page = detail["page"] - int(match.group(1)) + 1
    doc = parse_gvbl_pdf_pages(pages, first_page, detail["page"], hint_iso)
    if any("heading date not found" in w for w in doc["warnings"]):
        return None
    return {**doc, "year": detail["year"], "page": detail["page"],
            "url": url + f"#page={match.group(1)}", "sha256": prow["sha256"],
            "fetched_at": prow.get("fetched_at"), "issue": detail.get("issue"),
            "issue_date": detail.get("issue_date"), "gazette": detail.get("gazette"),
            "document_type": detail.get("document_type"),
            "platform_date": detail.get("platform_date"), "form": "issue_pdf",
            "year_basis": detail.get("year_basis")}


def resolve_gvbl(entry: dict, index: dict, root=None) -> dict | None:
    """The verified GVBl document of one version-log entry, or None."""
    hint = _dmy(entry.get("act_date_text"))
    hint_iso = hint.isoformat() if hint else None
    hints = {hint_iso} | {d.isoformat() for f in entry.get("ffn") or []
                          if (d := _dmy(f.get("act_date_text")))}
    hints.discard(None)
    for cand in entry.get("gvbl_candidates") or []:
        year, page = cand["year"], cand["page"]
        row = index["detail"].get((year, page))
        if row:
            doc = _gvbl_detail_cached(row, root)
            printed = {doc.get("platform_date"), doc.get("title_date"),
                       doc.get("decision_date"), (doc.get("signature") or {}).get("date")}
            if hints & printed:
                found = {**doc, "year": year, "page": page, "url": row["original_url"],
                         "sha256": row["sha256"], "fetched_at": row.get("fetched_at"),
                         "form": "detail_html", "year_basis": cand["basis"]}
                if commencement_section(found) is None and \
                        found.get("document_type") != "Gerichtsentscheidung":
                    pdf = _linked_pdf(found, index, hint_iso, root)
                    if pdf is not None:
                        pdf["detail_page"] = {"url": row["original_url"], "sha256": row["sha256"]}
                        pdf["warnings"] = pdf["warnings"] + [
                            "detail page text incomplete (no Inkrafttreten); issue PDF used"]
                        return pdf
                return found
            continue
        for prow in index["pdfs"]:
            if prow.get("gvbl_year") == year and prow.get("gvbl_first_page") is not None \
                    and prow["gvbl_first_page"] <= page <= prow["gvbl_last_page"]:
                pages = _pdf_pages_cached(prow, root)
                doc = parse_gvbl_pdf_pages(pages, prow["gvbl_first_page"], page, hint_iso)
                if any("heading date not found" in w for w in doc["warnings"]):
                    continue
                return {**doc, "year": year, "page": page,
                        "url": prow["original_url"] + f"#page={page - prow['gvbl_first_page'] + 1}",
                        "sha256": prow["sha256"], "fetched_at": prow.get("fetched_at"),
                        "issue": prow.get("gvbl_issue"), "issue_date": prow.get("gvbl_issue_date"),
                        "gazette": {"name": "GVBl.", "year": year, "page": page,
                                    "reference": f"GVBl. {year} S. {page}"},
                        "form": "issue_pdf", "year_basis": cand["basis"]}
    return None


# ------------------------------------------------------------ changes

def _prefix(act_id: str) -> str:
    return "§" if act_id == "by_dvasyl" else "Art."


def _date_field(date_value, status, basis, evidence=None, **extra) -> dict:
    return {"date": date_value, "status": status, "basis": basis,
            "evidence": evidence or [], **extra}


def standangabe_mwv(stands: list[dict]) -> dict[tuple, dict]:
    """``mWv`` statements of the old BAYERN.RECHT Stand line, by GVBl page."""
    out = {}
    for stand in stands:
        for entry in stand.get("stand_entries") or []:
            if entry.get("type") != "Stand" or not entry.get("mwv"):
                continue
            page = re.search(r"GVBl\.?\s*S\.\s*(\d+)", entry["text"])
            if page:
                key = (int(page.group(1)), entry["mwv"][:4])
                out.setdefault(key, {"mwv": entry["mwv"], "text": entry["text"],
                                     "archive_url": stand.get("archive_url"),
                                     "known_at": stand.get("known_at")})
    return out


def amending_act_record(act_id: str, entry: dict, doc: dict | None) -> dict:
    art = entry.get("article") or parse_amending_article("")
    year = doc["year"] if doc else None
    page = entry.get("page")
    gazette = ({"name": "GVBl.", "part": None, "year": year,
                "number": doc.get("issue"), "page": page,
                "reference": f"GVBl. {year} S. {page}"} if doc else
               {"name": "GVBl.", "part": None, "year": None, "number": None,
                "page": page, "reference": f"GVBl. S. {page}" if page else None})
    type_label = entry.get("type")
    art_label = art.get("raw")
    date_label = None
    ffn = (entry.get("ffn") or [{}])[0]
    if ffn.get("act_date_text"):
        date_label = ffn["act_date_text"]
    elif entry.get("act_date_text"):
        date_label = entry["act_date_text"]
    parts = [p for p in (art_label, type_label if type_label not in ("Entsch.", "Urteil") else type_label,
                         f"v. {date_label}" if date_label else None) if p]
    citation = " ".join(parts) + (f" ({gazette['reference']})" if gazette["reference"] else "")
    return {"document_id": f"gvbl-{year}-{page}" if doc and page else None,
            "title": (doc or {}).get("title") or entry.get("short_title"),
            "short_title": entry.get("short_title"),
            "type": type_label, "citation": citation,
            "article": ",".join(art.get("articles") or []) or None,
            "article_prefix": art.get("prefix"), "absatz": art.get("absatz"),
            "nummer": art.get("nummer"), "buchstabe": art.get("buchstabe"),
            "gazette": gazette, "url": (doc or {}).get("url"),
            "procedure_id": None,
            "version_log": {"xml_seq": entry.get("xml_seq"),
                            "ffn_seq": [f.get("seq") for f in entry.get("ffn") or []],
                            "xml_description": entry.get("xml_description"),
                            "xml_citation": entry.get("xml_citation"),
                            "ffn_citation": [f.get("citation") for f in entry.get("ffn") or []]}}


def act_dates(entry: dict, doc: dict | None, mwv: dict | None) -> dict:
    """adopted / promulgated from the GVBl document (never from the log)."""
    evidence = [{"source": "GVBl", "url": doc["url"], "sha256": doc["sha256"],
                 "fetched_at": doc.get("fetched_at"), "form": doc["form"]}] if doc else []
    is_decision = (entry.get("type") in ("Entsch.", "Urteil")
                   or (doc or {}).get("document_type") == "Gerichtsentscheidung")
    if doc is None:
        adopted = _date_field(None, "unknown", "GVBl document not captured/verified")
        promulgated = _date_field(None, "unknown", "GVBl document not captured/verified")
    else:
        sig = (doc.get("signature") or {})
        if is_decision:
            adopted = _date_field(None, "unknown", "court decision published in the GVBl; "
                                  "no Ausfertigung", evidence,
                                  decision_date=doc.get("decision_date"),
                                  decision_date_basis="date of the decision as printed in "
                                                      "the GVBl Bekanntmachung title")
        elif sig.get("date"):
            adopted = _date_field(sig["date"], "official",
                                  f"GVBl document signature line ({sig['raw']})", evidence,
                                  heading_date=doc.get("title_date"),
                                  platform_date=doc.get("platform_date"))
        elif doc.get("title_date"):
            adopted = _date_field(doc["title_date"], "official",
                                  "GVBl document heading date (vom …)", evidence,
                                  platform_date=doc.get("platform_date"))
        else:
            adopted = _date_field(None, "unknown", "no signature/heading date in the GVBl text",
                                  evidence)
        if doc.get("issue_date"):
            promulgated = _date_field(doc["issue_date"], "official",
                                      f"GVBl issue date (Ausgabe {doc.get('issue')}/{doc['year']}"
                                      f" vom {doc['issue_date']}, verkuendung-bayern.de)", evidence)
        else:
            promulgated = _date_field(None, "unknown", "GVBl issue date not found", evidence)
    return {"adopted": adopted, "promulgated": promulgated, "is_decision": is_decision}


def change_in_force(doc: dict | None, article: str | None, nummer: str | None,
                    promulgated: str | None, mwv: dict | None,
                    is_decision: bool, prefix: str | None = None) -> tuple[dict, dict]:
    evidence = [{"source": "GVBl", "url": doc["url"], "sha256": doc["sha256"]}] if doc else []
    applies = {"date": None, "status": "not_stated", "basis": None}
    if is_decision:
        result = {"date": None, "status": "unknown",
                  "basis": "court decision (Normenkontrolle) published in the GVBl; "
                           "no commencement clause", "clause": None}
    elif doc is None:
        result = {"date": None, "status": "unknown",
                  "basis": "GVBl document not captured/verified", "clause": None}
    else:
        section = commencement_section(doc)
        commencement = parse_commencement(section["text"], promulgated) if section else None
        result = in_force_for(commencement, article, nummer, prefix)
        if section:
            applies_sentences = [s for s in _sentences(section["text"]) if "anzuwenden" in s]
            if applies_sentences:
                applies = {"date": None, "status": "stated_see_clause",
                           "basis": "GVBl Inkrafttreten section contains an application rule",
                           "clause": " ".join(applies_sentences)}
    result["evidence"] = evidence
    if mwv:
        result["consolidation_statement"] = {"source": "BAYERN.RECHT Stand line",
                                             "mwv": mwv["mwv"], "text": mwv["text"],
                                             "archive_url": mwv["archive_url"]}
        if result["date"] is None:
            result.update({"date": mwv["mwv"], "status": "official_consolidation_statement",
                           "basis": "BAYERN.RECHT Stand line 'mWv' (mit Wirkung vom) for "
                                    "this amending act; no GVBl clause resolved"})
    return result, applies


def build_changes(act_id: str, root=None, stands: list[dict] | None = None,
                  as_of: str = AS_OF) -> tuple[list[dict], list[dict], list[dict]]:
    """Change records, amending-act records and log findings for one act."""
    index = gvbl_index(root)
    prefix = _prefix(act_id)
    mwv_index = standangabe_mwv(stands or [])
    changes, acts, findings = [], [], []
    for entry in load_version_log(act_id):
        for finding in entry["findings"]:
            findings.append({"act_id": act_id, "norm_id": None, **finding,
                             "version_log": entry.get("xml_description")})
        doc = resolve_gvbl(entry, index, root)
        amending = amending_act_record(act_id, entry, doc)
        year = str(doc["year"]) if doc else (entry.get("act_date_text") or "")[-4:]
        mwv = mwv_index.get((entry.get("page"), year))
        dates = act_dates(entry, doc, mwv)
        if doc:
            printed = ({doc.get("title_date"), (doc.get("signature") or {}).get("date"),
                        doc.get("platform_date")} - {None}) if not dates["is_decision"] else set()
            if len(printed) > 1:
                findings.append({"act_id": act_id, "norm_id": None,
                                 "kind": "gvbl_adoption_date_disagreement",
                                 "detail": f"{amending['citation']}: heading {doc.get('title_date')}, "
                                           f"signature {(doc.get('signature') or {}).get('date')}, "
                                           f"platform {doc.get('platform_date')}"})
        elif entry.get("gvbl_candidates"):
            findings.append({"act_id": act_id, "norm_id": None, "kind": "gvbl_document_missing",
                             "detail": f"{amending['citation']}: no verified GVBl document"})
        # ---- targets: ffn descriptors + GVBl commands
        per_norm: dict[str, dict] = {}
        unresolved = []
        articles = (entry.get("article") or {}).get("articles") or []
        ffn_rows = entry.get("ffn") or [{"article": entry.get("article"), "description": None}]
        for ffn in ffn_rows:
            ffn_articles = (ffn.get("article") or {}).get("articles") or articles or [None]
            descriptor = parse_ffn_descriptor(ffn.get("description") or "", prefix)
            gvbl_cmds = []
            for article in ffn_articles:
                section = section_for(doc, act_id, article) if doc else None
                if section is None:
                    continue
                art = ffn.get("article") or entry.get("article") or {}
                extracted = extract_commands(section, prefix, art.get("nummer"), art.get("absatz"))
                for cmd in extracted["commands"]:
                    gvbl_cmds.append({**cmd, "article": section["number"],
                                      "article_prefix": section["prefix"]})
            for target in descriptor["targets"]:
                slot = per_norm.setdefault(target["norm"], {"ffn": [], "gvbl": [],
                                                            "article": ffn_articles[0],
                                                            "target_basis": "ffn"})
                slot["ffn"].append({**target, "raw": descriptor["raw"], "seq": ffn.get("seq")})
            # The Fortführungsnachweis names the changed norms where it can; the
            # GVBl commands then only add Nummer/Buchstabe and wording.  Only for
            # "mehrfach geänd." (or no descriptor) do the commands name targets.
            named = {t["norm"] for t in descriptor["targets"]}
            for cmd in gvbl_cmds:
                for target in cmd["targets"]:
                    if named and target["norm"] not in named:
                        if doc["form"] == "detail_html":
                            findings.append({
                                "act_id": act_id, "norm_id": norm_id_for(act_id, target["norm"]),
                                "kind": "gvbl_command_target_not_in_ffn",
                                "detail": f"{amending['citation']}: GVBl command "
                                          f"{cmd['nummer'] or ''} names {target['norm']} "
                                          f"({target['change_type']}); ffn: {descriptor['raw']!r}"})
                        continue
                    slot = per_norm.setdefault(target["norm"], {
                        "ffn": [], "gvbl": [], "article": cmd["article"],
                        "target_basis": ("gvbl_commands_html" if doc["form"] == "detail_html"
                                         else "gvbl_commands_pdf_heuristic")})
                    slot["gvbl"].append({**target, "nummer": cmd["nummer"],
                                         "buchstabe": cmd["buchstabe"], "raw": cmd["raw"],
                                         "article": cmd["article"], "form": doc["form"],
                                         "in_sequence": cmd.get("in_sequence", True)})
            if doc and dates["is_decision"] and not descriptor["targets"]:
                for target in decision_targets(doc, prefix):
                    slot = per_norm.setdefault(target["norm"], {
                        "ffn": [], "gvbl": [], "article": None,
                        "target_basis": "gvbl_decision_formula"})
                    slot["gvbl"].append({**target, "nummer": None, "buchstabe": None,
                                         "raw": _collapse(doc["text"]).split(
                                             "Entscheidungsformel:", 1)[-1][:1500],
                                         "article": None, "form": doc["form"],
                                         "in_sequence": True})
                gvbl_cmds = gvbl_cmds or [{"targets": []}] if per_norm else gvbl_cmds
            if not descriptor["targets"] and not gvbl_cmds and not per_norm:
                unresolved.append({"ffn": ffn, "descriptor": descriptor})
        acts.append({"act_id": act_id, **amending, "dates": {
            "adopted": dates["adopted"], "promulgated": dates["promulgated"]},
            "gvbl_form": (doc or {}).get("form"), "norm_targets": sorted(per_norm)})
        for label, slot in sorted(per_norm.items(), key=lambda kv: _natural(kv[0])):
            changes.append(_change_record(act_id, label, slot, amending, dates, doc,
                                          mwv, as_of, findings))
        if not per_norm:
            reason = ("version log says 'mehrfach geänd.' and the GVBl commands were not "
                      "resolved" if any(u["descriptor"]["unspecified"] for u in unresolved)
                      else "no norm named in the version log or the GVBl commands")
            changes.append(_change_record(act_id, None, {"ffn": [], "gvbl": [],
                                                         "article": articles[0] if articles else None},
                                          amending, dates, doc, mwv, as_of, findings,
                                          scope_reason=reason,
                                          ffn_raw=[u["descriptor"]["raw"] for u in unresolved]))
    return changes, acts, findings


def _natural(label: str) -> tuple:
    match = re.search(r"(\d+)([a-z]*)", label or "")
    return (int(match.group(1)), match.group(2)) if match else (10**9, label or "")


def _combine_type(ops: list[str]) -> str:
    strong = [op for op in ops if op in ("neugefasst", "aufgehoben", "eingefügt", "ersetzt")]
    if strong:
        return strong[-1]
    if "geändert" in ops:
        return "geändert"
    return "unbekannt"


def _change_record(act_id, label, slot, amending, dates, doc, mwv, as_of, findings,
                   scope_reason=None, ffn_raw=None) -> dict:
    norm_id = norm_id_for(act_id, label) if label else None
    ffn_types = [t["change_type"] for t in slot["ffn"]]
    gvbl_types = [t["change_type"] for t in slot["gvbl"]]
    change_type = _combine_type(ffn_types) if ffn_types and _combine_type(ffn_types) != "unbekannt" \
        else _combine_type(gvbl_types)
    html_types = [t["change_type"] for t in slot["gvbl"] if t.get("form") == "detail_html"]
    compatible = {("neugefasst", "ersetzt"), ("aufgehoben", "ersetzt")}
    if ffn_types and html_types and _combine_type(ffn_types) not in ("unbekannt",) \
            and _combine_type(html_types) != _combine_type(ffn_types) \
            and (_combine_type(ffn_types), _combine_type(html_types)) not in compatible:
        findings.append({"act_id": act_id, "norm_id": norm_id,
                         "kind": "change_type_disagreement",
                         "detail": f"{amending['citation']}: ffn says {_combine_type(ffn_types)}, "
                                   f"GVBl commands say {_combine_type(gvbl_types)}"})
    # a Nummer/Buchstabe is norm-specific only if exactly one command names
    # this norm directly (not merely inheriting it from the article's intro)
    own = [t for t in slot["gvbl"] if not t.get("inherited")]
    nummern = sorted({t["nummer"] for t in own if t.get("nummer")}, key=_natural)
    buchstaben = sorted({t["buchstabe"] for t in own if t.get("buchstabe")})
    nummer = (nummern[0] if len(nummern) == 1 and len(own) == len(slot["gvbl"])
              else None) or amending.get("nummer")
    buchstabe = (buchstaben[0] if len(buchstaben) == 1 and len(own) == len(slot["gvbl"])
                 else None) or amending.get("buchstabe")
    article = slot.get("article") or amending.get("article")
    if article and "," in str(article):
        article = None
    in_force, applies = change_in_force(doc, article, nummer if slot["gvbl"] else amending.get("nummer"),
                                        dates["promulgated"]["date"], mwv, dates["is_decision"],
                                        amending.get("article_prefix"))
    if in_force.get("consolidation_statement") and in_force["status"] != \
            "official_consolidation_statement" and in_force["date"] and \
            in_force["consolidation_statement"]["mwv"] != in_force["date"]:
        findings.append({"act_id": act_id, "norm_id": norm_id,
                         "kind": "in_force_disagreement",
                         "detail": f"{amending['citation']}: GVBl clause {in_force['date']} vs "
                                   f"BAYERN.RECHT mWv {in_force['consolidation_statement']['mwv']}"})
    date_value = in_force["date"]
    pending = bool(date_value and date_value > as_of)
    state = "date_unknown" if not date_value else ("pending" if pending else "in_force")
    commands = [{"operation": t["operation"], "ref": {"norm": t["norm"], "nummer": t.get("nummer"),
                                                      "buchstabe": t.get("buchstabe"),
                                                      "article": t.get("article")},
                 "raw": t["raw"], "source": "GVBl"} for t in slot["gvbl"]]
    commands += [{"operation": t["change_type"], "ref": {"norm": t["norm"]},
                  "raw": t["raw"], "source": "BAYERN.RECHT Fortführungsnachweis"}
                 for t in slot["ffn"]]
    art_part = amending.get("article_prefix") and article and \
        f"{amending['article_prefix']} {article}"
    cit_parts = [art_part or None, f"Nr. {nummer}" if nummer else None,
                 f"Buchst. {buchstabe}" if buchstabe else None]
    base_cit = amending["citation"]
    if art_part and amending.get("article") and "," in str(amending.get("article")):
        base_cit = re.sub(r"^(?:§§?|Art\.)\s*[\d\s,a-zund]+", "", base_cit).strip()
        citation = " ".join(p for p in cit_parts if p) + " " + base_cit
    elif nummer and not amending.get("nummer") and art_part:
        citation = re.sub(r"^((?:§|Art\.)\s*\d+[a-z]?(?:\s+Abs\.\s*\d+[a-z]?)?)",
                          rf"\1 Nr. {nummer}", base_cit, count=1)
    else:
        citation = base_cit
    evidence = []
    vl = amending["version_log"]
    if vl.get("xml_description"):
        evidence.append({"source": "BAYERN.RECHT", "kind": "aenderungsverlauf",
                         "text": f"{vl['xml_description']} ({vl['xml_citation']})",
                         "url": f"https://www.gesetze-bayern.de/Content/Zip/{ACTS[act_id]['key']}"})
    for text in vl.get("ffn_citation") or []:
        evidence.append({"source": "BAYERN.RECHT", "kind": "fortfuehrungsnachweis",
                         "text": text, "url": "https://www.gesetze-bayern.de/Content/Document/ffn"})
    if doc:
        evidence.append({"source": "GVBl", "kind": doc["form"], "url": doc["url"],
                         "sha256": doc["sha256"], "fetched_at": doc.get("fetched_at")})
    change_id = "chg:" + hashlib.sha256(
        f"{act_id}|{amending.get('document_id') or amending['citation']}|{article}|"
        f"{nummer}|{norm_id}".encode("utf-8")).hexdigest()[:20]
    record = {
        "change_id": change_id, "act_id": act_id, "norm_id": norm_id,
        "norm_label": label, "scope": "norm" if label else "act",
        "target_basis": slot.get("target_basis"),
        "amending_act": {**{k: amending[k] for k in (
            "document_id", "title", "short_title", "type", "gazette", "url",
            "procedure_id", "absatz")}, "citation": citation,
            "article": article, "nummer": nummer, "buchstabe": buchstabe,
            "nummern": nummern if len(nummern) > 1 else None},
        "dates": {"adopted": dates["adopted"], "promulgated": dates["promulgated"],
                  "in_force": in_force, "applies_from": applies},
        "change_type": change_type if label else "unbekannt",
        "pending": pending, "state": state, "commands": commands,
        "text_effect": {"before_version_id": None, "after_version_id": None,
                        "verification": "not_verified"},
        "diff_url": None, "evidence": evidence,
        "version_log": vl,
    }
    if scope_reason:
        record["scope_reason"] = scope_reason
        record["commands"] += [{"operation": "unbekannt", "ref": None, "raw": raw,
                                "source": "BAYERN.RECHT Fortführungsnachweis"}
                               for raw in (ffn_raw or []) if raw]
    return record


# ------------------------------------------------------------ versions

def _obs_order(obs: dict) -> tuple:
    return (obs["known_at"], obs.get("captured_at") or "", ROLE_RANK[obs["role"]])


def build_versions(act_id: str, observations: list[dict]) -> dict[str, list[dict]]:
    """Distinct texts (by identity) per norm, ordered by first observation."""
    by_norm: dict[str, list[dict]] = defaultdict(list)
    for obs in observations:
        by_norm[obs["norm_id"]].append(obs)
    out: dict[str, list[dict]] = {}
    for norm_id, items in by_norm.items():
        items.sort(key=_obs_order)
        groups: dict[str, list[dict]] = {}
        for obs in items:
            groups.setdefault(obs["identity"], []).append(obs)
        versions = []
        for identity, group in groups.items():
            # display text: the official consolidation's; else an archived
            # page's (same flattening as today's text, validated); else the
            # archived XML's (older exports can be minified, gluing words)
            official = [o for o in group if o["role"] == "official_consolidation"]
            pages = [o for o in group if o["kind"] in ("full", "norm")]
            rep = (official or pages or group)[-1]
            variants = Counter((o["enbez"], o["titel"], o["text"]) for o in group)
            versions.append({"norm_id": norm_id, "identity": identity, "rep": rep,
                             "observations": group, "text_variants": len(variants)})
        versions.sort(key=lambda v: _obs_order(v["observations"][0]))
        # runs in observation order: a text that returns after another one
        runs, last = [], None
        for obs in items:
            if obs["identity"] != last:
                runs.append(obs["identity"])
                last = obs["identity"]
        for version in versions:
            version["runs"] = runs.count(version["identity"])
        out[norm_id] = versions
    return out


def _attestations(group: list[dict]) -> list[dict]:
    bucket: dict[tuple, dict] = {}
    for obs in group:
        key = (obs["source"], obs["kind"], obs["url"])
        att = bucket.get(key)
        entry = {"known_at": obs["known_at"], "archive_url": obs.get("archive_url"),
                 "sha256": obs.get("sha256"), "consolidation": obs.get("consolidation_raw"),
                 "snapshot": obs.get("snapshot")}
        if att is None:
            bucket[key] = att = {
                "source": obs["source"], "role": obs["role"], "kind": obs["kind"],
                "url": obs["url"], "archive_url": obs.get("archive_url"),
                "known_at": obs["known_at"], "last_seen": obs["known_at"],
                "stand": obs.get("consolidation_raw"), "sha256": obs.get("sha256"),
                "count": 0, "observations": []}
        att["count"] += 1
        att["last_seen"] = max(att["last_seen"], obs["known_at"])
        if obs["role"] == "archived_capture":
            att["observations"].append(entry)
        elif not att["observations"] or att["observations"][-1]["known_at"] != obs["known_at"]:
            att["observations"].append(entry)
    for att in bucket.values():
        if att["role"] == "official_consolidation":
            # daily snapshots: keep first/last only, the count says the rest
            att["observations"] = att["observations"][:1] + att["observations"][-1:] \
                if len(att["observations"]) > 1 else att["observations"]
    return sorted(bucket.values(), key=lambda a: (a["known_at"], ROLE_RANK[a["role"]]))


def _cons(obs: dict) -> str:
    """Temporal position of what an observation showed: the act-level
    consolidation date if stated, else the capture/observation date."""
    return obs.get("consolidation_date") or obs["known_at"]


def link_changes(changes: list[dict], versions: dict[str, list[dict]],
                 findings: list[dict], as_of: str = AS_OF) -> None:
    """Attach before/after versions to change records (bracketing captures)."""
    by_norm = defaultdict(list)
    for change in changes:
        if change["norm_id"]:
            by_norm[change["norm_id"]].append(change)
    for norm_id, norm_changes in by_norm.items():
        norm_versions = versions.get(norm_id, [])
        obs = sorted((o for v in norm_versions for o in v["observations"]), key=_obs_order)
        vid = {o["identity"]: v for v in norm_versions for o in v["observations"]}
        dated = [c for c in norm_changes if c["dates"]["in_force"]["date"]]
        for change in norm_changes:
            day = change["dates"]["in_force"]["date"]
            effect = change["text_effect"]
            if not day:
                effect["reason"] = "in-force date unknown"
                continue
            if day > as_of:
                effect["reason"] = "pending change (not yet in force)"
                continue
            promulgated = change["dates"]["promulgated"]["date"]
            retroactive = bool(promulgated and day < promulgated)
            if retroactive:
                # a retroactive change reaches the consolidation only after its
                # promulgation: order observations by when they were served
                pos = lambda o: o["known_at"]  # noqa: E731
                before = [o for o in obs if o["known_at"] < promulgated]
                after = [o for o in obs if o["known_at"] >= promulgated]
                weak = True
            else:
                pos = _cons
                before = [o for o in obs if _cons(o) < day]
                after = [o for o in obs if o.get("consolidation_date")
                         and o["consolidation_date"] >= day]
                weak = False
                if not after:
                    after = [o for o in obs if not o.get("consolidation_date")
                             and o["known_at"] >= day]
                    weak = bool(after)
            last_before = max(before, key=lambda o: (pos(o), o["known_at"])) if before else None
            first_after = min(after, key=lambda o: (pos(o), o["known_at"])) if after else None
            if first_after is None:
                effect["reason"] = "no observation of the consolidation after the in-force date"
                if last_before:
                    effect["before_version_id"] = vid[last_before["identity"]].get("version_id")
                continue
            lower = pos(last_before) if last_before else "0000-00-00"
            upper = pos(first_after)

            def effective(c):
                d, p = c["dates"]["in_force"]["date"], c["dates"]["promulgated"]["date"]
                return p if retroactive and p else d
            rivals = [c for c in dated if c is not change
                      and lower < effective(c) <= upper]
            effect["window"] = {"after_last_observation_before": lower,
                                "first_observation_after": upper,
                                "basis": ("capture dates around the promulgation of a "
                                          "retroactive change" if retroactive else
                                          "consolidation dates of the bracketing observations"
                                          + (" (capture dates, no consolidation date)"
                                             if weak else ""))}
            if rivals:
                # several changes share the window: no single one owns the texts
                effect["reason"] = (f"{len(rivals)} other dated change(s) to this norm fall "
                                    "between the bracketing observations")
                continue
            effect["after_version_id"] = vid[first_after["identity"]].get("version_id")
            if last_before:
                effect["before_version_id"] = vid[last_before["identity"]].get("version_id")
            if last_before is None:
                effect["reason"] = "no observation before the change"
                continue
            if last_before["identity"] == first_after["identity"]:
                effect["reason"] = "no text difference between the bracketing observations"
                if change["change_type"] not in ("unbekannt",):
                    findings.append({
                        "act_id": change["act_id"], "norm_id": norm_id,
                        "kind": "version_log_change_without_text_difference",
                        "change_id": change["change_id"],
                        "detail": (f"{change['amending_act']['citation']} "
                                   f"({change['change_type']}, in force {day}) but the text is "
                                   f"identical in the observations bracketing that date "
                                   f"({last_before['known_at']} {last_before['source']} / "
                                   f"{first_after['known_at']} {first_after['source']})"),
                        "evidence": [last_before.get("archive_url") or last_before.get("snapshot"),
                                     first_after.get("archive_url") or first_after.get("snapshot")]})
                continue
            effect["verification"] = "bracketed_by_captures"
            change["diff_url"] = (f"/norms/{norm_id}/diff?from={effect['before_version_id']}"
                                  f"&to={effect['after_version_id']}")


def version_records(act_id: str, versions: dict[str, list[dict]], changes: list[dict],
                    findings: list[dict]) -> list[dict]:
    """SPEC version records; assigns version ids (in place) before linking."""
    records = []
    produced = defaultdict(list)
    for change in changes:
        after = change["text_effect"].get("after_version_id")
        if after and change["text_effect"]["verification"] == "bracketed_by_captures":
            produced[after].append(change)
    for norm_id, norm_versions in versions.items():
        for index, version in enumerate(norm_versions):
            rep = version["rep"]
            group = version["observations"]
            atts = _attestations(group)
            roles = {o["role"] for o in group}
            role = min(roles, key=ROLE_RANK.get)
            source_kinds = {(o["source"], o["kind"]) for o in group}
            cons_dates = sorted({o["consolidation_date"] for o in group if o.get("consolidation_date")})
            made_by = produced.get(version.get("version_id"), [])
            valid_from = valid_status = valid_basis = None
            if len(made_by) == 1:
                c = made_by[0]
                valid_from = c["dates"]["in_force"]["date"]
                valid_status = c["dates"]["in_force"]["status"]
                valid_basis = (f"in force date of {c['amending_act']['citation']} "
                               f"({c['dates']['in_force']['basis']}); text change bracketed "
                               f"by observations")
            version_findings = [f["kind"] for f in findings if f.get("norm_id") == norm_id
                                and f.get("version_id") == version.get("version_id")]
            if version["runs"] > 1:
                version_findings.append("text_reappears_after_other_text")
            unit, number = norm_id.split("/")[-2:]
            records.append({
                "id": version["version_id"], "canonical_id": norm_id,
                "version_id": version["version_id"], "act_id": act_id,
                "pinpoint": {"label": rep["enbez"], "title": rep["titel"],
                             "unit": unit, "number": number.split("~")[0]},
                "content_hash": {"algorithm": "sha256",
                                 "value": content_hash(rep["enbez"], rep["titel"], rep["text"]),
                                 "identity": version["identity"],
                                 "text_sha256": sha256_hex(rep["text"].encode("utf-8"))},
                "text": rep["text"],
                "source": {"url": rep["url"], "archive_url": rep.get("archive_url"),
                           "role": role, "observed_at": group[0]["known_at"]},
                "source_role": role,
                "attestations": atts,
                "completeness": {"status": "complete",
                                 "reason": "full norm text as served by the source"},
                "verification": {
                    "cross_source": "agree" if len(source_kinds) > 1 else "single_source",
                    "sources": sorted(f"{a}:{b}" for a, b in source_kinds),
                    "text_variants": version["text_variants"],
                    "findings": version_findings},
                "quality": {"source_exact": True, "complete": True,
                            "reconstruction_status": "not_reconstructed",
                            "text_history_complete": False},
                "temporal": {
                    "known_at": group[0]["known_at"],
                    "observed_from": group[0]["known_at"],
                    "observed_until": group[-1]["known_at"],
                    "valid_from": valid_from, "valid_from_status": valid_status or "unknown",
                    "valid_from_basis": valid_basis or (
                        "no single dated change bracketed by observations produced this text"),
                    "valid_until_exclusive": None, "valid_until_status": "unknown",
                    "consolidation_dates": cons_dates,
                    "consolidation_basis": CONSOLIDATION_BASIS,
                    "date_axes_note": ("valid_* is legal validity; observed_*/known_at is when a "
                                       "source served this text; they are never substituted")},
                "amendment": None, "relations": [],
                "change": _compact_change(made_by[0]) if len(made_by) == 1 else None,
                "sequence": index + 1,
            })
    # valid_until = next version's valid_from (same norm), when both are known
    by_norm = defaultdict(list)
    for record in records:
        by_norm[record["canonical_id"]].append(record)
    for items in by_norm.values():
        for current, following in zip(items, items[1:]):
            if following["temporal"]["valid_from"]:
                current["temporal"]["valid_until_exclusive"] = following["temporal"]["valid_from"]
                current["temporal"]["valid_until_status"] = following["temporal"]["valid_from_status"]
    return records


def _compact_change(change: dict) -> dict:
    act = change["amending_act"]
    return {"change_id": change["change_id"], "change_type": change["change_type"],
            "amending_act": {k: act.get(k) for k in ("citation", "document_id", "gazette",
                                                      "article", "nummer", "title", "url")},
            "dates": {k: {"date": v.get("date"), "status": v.get("status")}
                      for k, v in change["dates"].items()},
            "source_role": "official_promulgation"}


def assign_version_ids(versions: dict[str, list[dict]]) -> None:
    for norm_id, items in versions.items():
        for version in items:
            rep = version["rep"]
            digest = content_hash(rep["enbez"], rep["titel"], rep["text"])
            anchor = f"observed-{version['observations'][0]['known_at']}"
            version["version_id"] = f"{norm_id}@{anchor}#{digest[:12]}"


def text_difference_findings(act_id: str, versions: dict[str, list[dict]],
                             changes: list[dict]) -> list[dict]:
    """Consecutive distinct texts with no version-log change in between."""
    findings = []
    by_norm = defaultdict(list)
    for change in changes:
        if change["norm_id"]:
            by_norm[change["norm_id"]].append(change)
    act_level = [c for c in changes if not c["norm_id"]]
    for norm_id, items in versions.items():
        obs = sorted((o for v in items for o in v["observations"]), key=_obs_order)
        for prev, cur in zip(obs, obs[1:]):
            if prev["identity"] == cur["identity"]:
                continue
            lower, upper = _cons(prev), _cons(cur)
            if upper < lower:
                lower, upper = upper, lower

            def within(c):
                days = [c["dates"]["in_force"]["date"], c["dates"]["promulgated"]["date"]]
                top = max(upper, cur["known_at"])
                return any(d is not None and lower < d <= top for d in days)
            explained = [c for c in by_norm.get(norm_id, []) if within(c)]
            if explained:
                continue
            maybe = [c["amending_act"]["citation"] for c in act_level if within(c)]
            notes = editorial_notes_only(prev["text"], cur["text"])
            if notes is not None:
                findings.append({
                    "act_id": act_id, "norm_id": norm_id, "kind": "editorial_note_change",
                    "detail": (f"between {prev['known_at']} ({prev.get('consolidation_raw')}) and "
                               f"{cur['known_at']} ({cur.get('consolidation_raw')}) BAYERN.RECHT "
                               f"changed only editorial notes: {notes}"),
                    "notes": notes,
                    "evidence": [prev.get("archive_url") or prev.get("snapshot"),
                                 cur.get("archive_url") or cur.get("snapshot")]})
                continue
            findings.append({
                "act_id": act_id, "norm_id": norm_id,
                "kind": "text_difference_without_version_log_change",
                "detail": (f"text differs between {prev['known_at']} ({prev['source']}, "
                           f"{prev.get('consolidation_raw')}) and {cur['known_at']} "
                           f"({cur['source']}, {cur.get('consolidation_raw')}) but no change "
                           f"of this norm is dated in that window"
                           + (f"; possibly one of the unspecified entries {maybe}" if maybe else "")),
                "evidence": [prev.get("archive_url") or prev.get("snapshot"),
                             cur.get("archive_url") or cur.get("snapshot")]})
    return findings


def split_editions(act_id: str, wayback: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep captures of the act edition BAYERN.RECHT serves today.

    One document key can serve a replaced act (DVAsyl of 4 June 2002 until
    31 Aug 2016, then the new DVAsyl of 16 Aug 2016).  The page's "Fassung"
    date (XML <fassungsdatum>) tells the editions apart."""
    dated = sorted((o for o in wayback if o.get("fassung")), key=lambda o: o["captured_at"])
    if not dated:
        return wayback, []
    current = dated[-1]["fassung"]
    keep = [o for o in wayback if not o.get("fassung") or o["fassung"] == current]
    other = [o for o in wayback if o.get("fassung") and o["fassung"] != current]
    return keep, other


EDITORIAL_START = re.compile(r"^\[(?:Redaktioneller Hinweis|Red\. Anm\.|Amtl\. Anm\.)")


def editorial_notes_only(old: str, new: str) -> list[str] | None:
    """The editorial notes (``[Redaktioneller Hinweis: …``) inserted or
    removed, if the two texts differ in nothing else; else None."""
    import difflib
    a, b = old.split(), new.split()
    notes = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op == "replace":
            return None
        chunk = " ".join(b[j1:j2] if op == "insert" else a[i1:i2])
        if not EDITORIAL_START.match(chunk):
            return None
        notes.append(chunk if op == "insert" else f"(entfernt) {chunk}")
    return notes or None


def build_act_history(act_id: str, root=None, as_of: str = AS_OF) -> dict:
    wayback, stands = wayback_observations(act_id, root)
    wayback, predecessor = split_editions(act_id, wayback)
    official, official_stands = official_observations(act_id)
    observations = wayback + official
    versions = build_versions(act_id, observations)
    assign_version_ids(versions)
    changes, amending_acts, findings = build_changes(act_id, root, stands, as_of)
    link_changes(changes, versions, findings, as_of)
    findings += text_difference_findings(act_id, versions, changes)
    if predecessor:
        editions = Counter(o["fassung"] for o in predecessor)
        findings.append({
            "act_id": act_id, "norm_id": None, "kind": "predecessor_edition_captures_excluded",
            "detail": (f"{len(predecessor)} norm observations of another edition of this "
                       f"document key (Fassung {', '.join(sorted(editions))}; captured "
                       f"{min(o['known_at'] for o in predecessor)} to "
                       f"{max(o['known_at'] for o in predecessor)}) are not part of this "
                       f"act's version chains"),
            "evidence": sorted({o["archive_url"] for o in predecessor})[:5]})
    records = version_records(act_id, versions, changes, findings)
    validation = validate_against_current(act_id, wayback, changes)
    return {"act_id": act_id, "versions": records, "changes": changes,
            "amending_acts": amending_acts, "findings": findings,
            "validation": validation,
            "stands": [s for s in stands if s.get("stand")],
            "counts": _counts(act_id, records, changes)}


def _counts(act_id: str, records: list[dict], changes: list[dict]) -> dict:
    per_norm = Counter(r["canonical_id"] for r in records)
    dated = {field: Counter(c["dates"][field]["status"] for c in changes)
             for field in ("adopted", "promulgated", "in_force", "applies_from")}
    return {"act_id": act_id, "norms": len(per_norm), "versions": len(records),
            "norms_with_history": sum(1 for n in per_norm.values() if n > 1),
            "versions_by_role": dict(Counter(r["source_role"] for r in records)),
            "changes": len(changes),
            "changes_norm_level": sum(1 for c in changes if c["norm_id"]),
            "changes_by_type": dict(Counter(c["change_type"] for c in changes)),
            "text_effect": dict(Counter(c["text_effect"]["verification"] for c in changes)),
            "date_status": {k: dict(v) for k, v in dated.items()},
            "dates_resolved": {k: sum(1 for c in changes if c["dates"][k]["date"])
                               for k in ("adopted", "promulgated", "in_force", "applies_from")}}


# ------------------------------------------------------------- outputs

OUTPUT_FILES = ("versions", "changes", "amending_acts", "findings")


def default_output_dir() -> Path:
    env = os.environ.get("LEXGRAPH_BY_HISTORY_OUT")
    return Path(env) if env else archive_root() / "derived" / "bavaria"


def write_outputs(results: list[dict], out_dir: Path) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"schema": "lexgraph-bavarian-history/1", "built_at": utc_now(),
               "as_of": AS_OF, "acts": {}}
    for name in OUTPUT_FILES:
        rows = [row for result in results for row in result[name]]
        data = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)
        _atomic_write(out_dir / f"{name}.jsonl", data.encode("utf-8"))
    for result in results:
        summary["acts"][result["act_id"]] = {"counts": result["counts"],
                                             "validation": result["validation"]}
    _atomic_write(out_dir / "summary.json",
                  json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"))
    return summary


def _read_jsonl(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                yield json.loads(line)


def _load(name: str, out_dir=None, act_id=None, norm_id=None) -> list[dict]:
    path = Path(out_dir or default_output_dir()) / f"{name}.jsonl"
    rows = []
    for row in _read_jsonl(path):
        if act_id and row.get("act_id") != act_id:
            continue
        if norm_id and (row.get("norm_id") or row.get("canonical_id")) != norm_id:
            continue
        rows.append(row)
    return rows


def load_versions(out_dir=None, act_id: str | None = None,
                  norm_id: str | None = None) -> list[dict]:
    """Version records (SPEC "Version record"), in chain order per norm."""
    return _load("versions", out_dir, act_id, norm_id)


def load_changes(out_dir=None, act_id: str | None = None,
                 norm_id: str | None = None) -> list[dict]:
    """Change records (SPEC "Change record")."""
    return _load("changes", out_dir, act_id, norm_id)


def load_amending_acts(out_dir=None, act_id: str | None = None) -> list[dict]:
    return _load("amending_acts", out_dir, act_id)


def load_findings(out_dir=None, act_id: str | None = None,
                  norm_id: str | None = None) -> list[dict]:
    return _load("findings", out_dir, act_id, norm_id)


def load_summary(out_dir=None) -> dict:
    return json.loads((Path(out_dir or default_output_dir()) / "summary.json").read_text())


def main(argv: list[str] | None = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Build Bavarian norm history JSONL "
                                                 "from the history archive (offline).")
    parser.add_argument("command", choices=("build",))
    parser.add_argument("--acts", default=",".join(ACTS))
    parser.add_argument("--out", default=None)
    parser.add_argument("--root", default=None)
    args = parser.parse_args(argv)
    results = [build_act_history(a.strip(), args.root) for a in args.acts.split(",") if a.strip()]
    summary = write_outputs(results, Path(args.out) if args.out else default_output_dir())
    print(json.dumps({k: v["counts"] for k, v in summary["acts"].items()},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    raise SystemExit(main())
