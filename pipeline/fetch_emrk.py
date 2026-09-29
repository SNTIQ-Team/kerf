"""EMRK (European Convention on Human Rights), German text, from archived official PDFs.

The Council of Europe publishes the German translation of the Convention
(``echr.coe.int/documents/d/echr/convention_deu``, formerly
``/Documents/Convention_DEU.pdf``).  echr.coe.int answers automated requests with
a Cloudflare challenge; it is never bypassed.  The Internet Archive's raw captures
(``id_``) of that PDF are used instead, one per edition, each validated
(``pdfinfo``, articles 1-59 present) and recorded with its archive URL and SHA-256.

The document says itself: only the English and French texts are authentic; the
German text is a translation (recorded as ``authentic: false``).  Each edition's
start is the latest protocol entry into force named in the document's own
preface ("… das am 1. August 2021 in Kraft getretene Protokoll Nr. 15").  Only the
Convention's articles are kept (``int/emrk/art/<n>``); the protocols are separate
treaties and not part of this identity.

Output: ``data/snapshots/emrk/<date>/acts.jsonl`` (one row, the shape of
``pipeline/fetch_eu_texts.py``).  Needs ``pdftotext``/``pdfinfo`` (poppler).
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))

from fetch_eu_texts import render_blocks  # noqa: E402

UA = "SNTIQ-lexgraph/0.1 (research; contact@sntiq.com)"
CACHE = ROOT / "data" / "cache" / "emrk"
ORIGINAL = "https://www.echr.coe.int/documents/d/echr/convention_deu"
# Chosen captures: the newest intact capture of each edition (CDX, 2026-09-29).
EDITIONS = [
    {"edition": "protocols-11-14", "timestamp": "20210611211834",
     "original": "https://www.echr.coe.int/Documents/Convention_DEU.pdf"},
    {"edition": "protocols-11-14-15", "timestamp": "20250303220019",
     "original": ORIGINAL},
]
MONTHS = {m: i for i, m in enumerate(("januar", "februar", "märz", "april", "mai", "juni", "juli",
                                      "august", "september", "oktober", "november", "dezember"), 1)}


def _get(url: str) -> bytes:
    import requests
    for attempt in range(4):
        time.sleep(2)
        response = requests.get(url, headers={"User-Agent": UA}, timeout=120)
        if response.status_code == 200:
            return response.content
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"{url}: HTTP {response.status_code}")


def pdf_lines(data: bytes) -> list[str]:
    """Text lines in reading order.

    The PDF is imposed for booklet printing: every page is a spread of two
    printed pages, not in reading order.  Each half is extracted on its own and
    the halves are ordered by their printed page number; unnumbered halves
    (cover, blank pages) keep their place before the numbered ones.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "doc.pdf"
        path.write_bytes(data)
        pages = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True)
        count = re.search(r"Pages:\s+(\d+)", pages.stdout)
        if pages.returncode != 0 or not count:
            raise RuntimeError("not a readable PDF")
        info = subprocess.run(["pdfinfo", "-box", "-f", "1", "-l", count.group(1), str(path)],
                              capture_output=True, text=True)
        boxes = {int(m.group(1)): (float(m.group(2)), float(m.group(3))) for m in re.finditer(
            r"Page\s+(\d+) MediaBox:\s+[\d.]+\s+[\d.]+\s+([\d.]+)\s+([\d.]+)", info.stdout)}
        parts = []
        for page in range(1, int(count.group(1)) + 1):
            width, height = boxes.get(page, (0.0, 0.0))
            if not width:
                raise RuntimeError(f"page {page}: no MediaBox")
            half = width / 2
            # a landscape page is a spread of two printed pages
            halves = [(0, int(width) + 1)] if width < height else \
                [(0, int(half)), (int(half) + 1, int(half))]
            for index, (x, w) in enumerate(halves):
                out = subprocess.run(
                    ["pdftotext", "-layout", "-f", str(page), "-l", str(page), "-x", str(x),
                     "-y", "0", "-W", str(w), "-H", str(int(height) + 1), str(path), "-"],
                    capture_output=True, text=True)
                if out.returncode != 0:
                    raise RuntimeError("pdftotext failed")
                lines = [line.strip() for line in out.stdout.splitlines()]
                numbers = [int(line) for line in lines if re.fullmatch(r"\d{1,3}", line)]
                parts.append((numbers[-1] if numbers else None, page, index, lines))
    numbered = sorted((p for p in parts if p[0] is not None), key=lambda p: p[0])
    front = [p for p in parts if p[0] is None and (not numbered or p[1] <= numbered[0][1])]
    return [line for part in front + numbered for line in part[3]]


def preface_start(lines: list[str]) -> tuple[str | None, str | None]:
    """Latest entry into force among the protocols the heading names.

    "in der Fassung der Protokolle Nr. 11, 14 und 15" + the preface's own dates
    ("das am 1. August 2021 in Kraft getretene Protokoll Nr. 15", "Protokoll Nr. 11
    … ab dessen Inkrafttreten am 1. November 1998").
    """
    text = " ".join(line for line in lines[:80] if line)
    heading = re.search(r"in der Fassung der Protokolle Nr\. ([\d, und]+)", text)
    if not heading:
        return None, None
    named = [int(n) for n in re.findall(r"\d+", heading.group(1))]
    date_re = r"(\d{1,2})\. (\w+) (\d{4})"
    found = []
    for number in named:
        patterns = (rf"am {date_re} in Kraft getretene Protokoll (?:Nr\. )?{number}\b",
                    rf"Protokoll (?:Nr\. )?{number}\b[^.]*?(?:in Kraft getreten am|"
                    rf"ab dessen Inkrafttreten am) {date_re}")
        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                day, month, year = match.groups()[-3:]
                if MONTHS.get(month.casefold()):
                    found.append((f"{year}-{MONTHS[month.casefold()]:02d}-{int(day):02d}", number))
                break
    if not found:
        return None, None
    day, number = max(found)
    return day, (f"Vorbemerkung des Textes (Fassung der Protokolle Nr. "
                 f"{', '.join(map(str, named))}): Protokoll Nr. {number} am {day} in Kraft "
                 "getreten (Europarat)")


_ART = re.compile(r"^ARTIKEL (\d+)$")
_SECTION = re.compile(r"^ABSCHNITT [IVX]+$")
_PARA = re.compile(r"^(\d+)\.(?:\s+(.*))?$")
_ITEM = re.compile(r"^\(([a-z])\)\s+(.*)$")
_CONJ = re.compile(r"^(?:oder|und|bzw\.|sowie)\b")


def parse_convention(lines: list[str]) -> list[dict]:
    articles: list[dict] = []
    current = None
    expect_title = skip_heading = False
    started = False
    for raw in lines:
        line = raw.strip()
        if not line or re.fullmatch(r"\d{1,3}", line):
            continue
        if started and line.startswith("Zusatzprotokoll"):
            break
        match = _ART.match(line)
        if match:
            started = True
            number = match.group(1)
            if articles and int(number) != int(articles[-1]["number"]) + 1:
                break                  # a protocol's own ARTIKEL 1: the Convention ended
            current = {"number": number, "title": "", "paragraphs": []}
            articles.append(current)
            expect_title = True
            continue
        if not started or current is None:
            continue
        if _SECTION.match(line):
            skip_heading = True
            continue
        if skip_heading and line.isupper():
            skip_heading = False
            continue
        skip_heading = False
        if expect_title:
            current["title"] = line
            expect_title = False
            continue
        para, item = _PARA.match(line), _ITEM.match(line)
        if para and (not current["paragraphs"] or _closed(current)):
            current["paragraphs"].append([f"({para.group(1)})", [["text", para.group(2) or ""]]])
        elif item:
            if not current["paragraphs"]:
                current["paragraphs"].append([None, []])
            current["paragraphs"][-1][1].append(["list", [[f"{item.group(1)})", item.group(2)]]])
        else:
            if not current["paragraphs"]:
                current["paragraphs"].append([None, [["text", line]]])
                continue
            segments = current["paragraphs"][-1][1]
            if not segments:
                segments.append(["text", line])
            elif segments[-1][0] == "list":
                last = segments[-1][1][-1]
                if _ended(last[1]) and not line[:1].islower():
                    segments.append(["text", line])      # text after the list
                else:
                    last[1] = _join(last[1], line)
            else:
                segments[-1][1] = _join(segments[-1][1], line)
    out = []
    for article in articles:
        paragraphs = []
        for marker, segments in article["paragraphs"]:
            merged: list = []
            for kind, value in segments:
                if kind == "list" and merged and merged[-1][0] == "list":
                    merged[-1] = ("list", merged[-1][1] + [tuple(v) for v in value])
                else:
                    merged.append((kind, [tuple(v) for v in value] if kind == "list" else value))
            paragraphs.append((marker, merged))
        text = render_blocks(paragraphs)
        if text:
            out.append({"enbez": f"Art. {article['number']}", "titel": article["title"],
                        "text": text})
    return out


def _ended(value: str) -> bool:
    return value.rstrip().endswith((".", ";", ":", "–"))


def _closed(article: dict) -> bool:
    segments = article["paragraphs"][-1][1]
    if not segments:
        return True
    last = segments[-1][1][-1][1] if segments[-1][0] == "list" else segments[-1][1]
    return _ended(last) or not last.strip()


def _join(left: str, right: str) -> str:
    left = left.rstrip()
    if not left:
        return right
    if left.endswith("-") and not left.endswith(" -"):
        # compound ellipsis ("Annahme- oder") keeps its hyphen and space
        return f"{left} {right}" if _CONJ.match(right) else f"{left}{right}"
    return f"{left} {right}"


def fetch(*, offline: bool = False) -> dict:
    editions = []
    for spec in EDITIONS:
        url = f"https://web.archive.org/web/{spec['timestamp']}id_/{spec['original']}"
        path = CACHE / f"convention_deu-{spec['timestamp']}.pdf"
        if path.is_file():
            data = path.read_bytes()
        elif offline:
            raise RuntimeError(f"offline: {path.name} not cached")
        else:
            data = _get(url)
            CACHE.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        lines = pdf_lines(data)
        articles = parse_convention(lines)
        numbers = [int(a["enbez"].split()[-1]) for a in articles]
        if numbers[:1] != [1] or numbers[-1] < 59:
            raise RuntimeError(f"{spec['timestamp']}: Convention articles incomplete ({len(numbers)})")
        start, basis = preface_start(lines)
        day = f"{spec['timestamp'][:4]}-{spec['timestamp'][4:6]}-{spec['timestamp'][6:8]}"
        editions.append({
            "kind": "coe_official", "celex": None, "label": spec["edition"],
            "format": "pdf", "date": day, "published": None,
            "valid_from": start, "valid_from_basis": basis,
            "items": [{"url": url, "original_url": spec["original"],
                       "sha256": hashlib.sha256(data).hexdigest(), "bytes_len": len(data),
                       "archived_at": day}],
            "articles": articles, "annexes_skipped": 0, "corrigenda": [],
        })
    return {
        "act_id": "int_emrk", "celex": None, "jurabk": "EMRK", "family": "treaty",
        "aliases": ["MRK", "Europäische Menschenrechtskonvention",
                    "Konvention zum Schutz der Menschenrechte und Grundfreiheiten"],
        "title": "Konvention zum Schutz der Menschenrechte und Grundfreiheiten",
        "source_url": ORIGINAL,
        "dates": {"adopted": "1950-11-04", "signed": "1950-11-04", "published": None,
                  "oj_ref": "SEV Nr. 005", "in_force": None, "applies_from": None,
                  "end_of_validity": None,
                  "basis": "the document's heading (Rom, 4.XI.1950); entry into force for "
                           "Germany is not stated in the source"},
        "relations": {}, "editions": editions,
        "warnings": ["German translation of the Council of Europe; only the English and "
                     "French texts are authentic (the document's own note)"],
        "authentic": False,
        "source": {"name": "Council of Europe (Internet Archive captures)",
                   "reason": "echr.coe.int answers with a Cloudflare challenge; not bypassed"},
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    from common import snapshot_dir, write_jsonl
    row = fetch(offline=args.offline)
    out = snapshot_dir("emrk")
    write_jsonl(out / "acts.jsonl", [row])
    print(f"EMRK: {[len(e['articles']) for e in row['editions']]} articles -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
