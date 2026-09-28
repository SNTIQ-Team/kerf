"""Decision text from court PDFs (``pdftotext -layout``).

* Page numbers/markers ("-3-", "Seite 3 von 9") and running headers (the
  Aktenzeichen repeated on each page) are dropped; each page boundary emits a
  ``pagebreak`` block, which never creates or ends a Randnummer.
* A Randnummer is a number in the left margin followed by at least two spaces
  (``^12   Text``).  Justified text can produce the same shape by accident, so a
  candidate is only accepted when it continues the sequence (first 1, then
  previous + 1).
* Blank lines separate blocks.  Text continuing after a page break is a new
  unlabeled block and therefore continues the current Randnummer.
"""
from __future__ import annotations

import re
import shutil
import subprocess

from ..errors import UpstreamFailure
from ..model import Block

RN_LINE = re.compile(r"^(\d{1,3})\s{2,}(\S.*)$")
PAGE_MARKER = re.compile(r"^\s*(?:-\s*\d{1,3}\s*-|Seite\s+\d+(?:\s+von\s+\d+)?|\d{1,3}\s*/\s*\d{1,3})\s*$")
RULE = re.compile(r"^\s*[-_=]{10,}\s*$")
NO_JOIN = {"und", "oder", "bzw", "bzw.", "sowie"}


def pdf_text(pdf: bytes, *, url: str | None = None) -> str:
    if not pdf.startswith(b"%PDF"):
        raise UpstreamFailure("unexpected_representation", "not a PDF", url=url)
    if shutil.which("pdftotext") is None:
        raise UpstreamFailure("parse_error", "pdftotext (poppler) not installed", url=url)
    proc = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", "-", "-"], input=pdf,
                          capture_output=True, timeout=120)
    if proc.returncode != 0:
        raise UpstreamFailure("parse_error", f"pdftotext failed: {proc.stderr[:200]!r}", url=url)
    text = proc.stdout.decode("utf-8", errors="replace")
    if len(re.sub(r"\s", "", text)) < 200:
        raise UpstreamFailure("empty_document", "PDF has no extractable text (scanned?)", url=url)
    return text


def spaced_word(line: str) -> str:
    """'G r ü n d e :' -> 'Gründe:' ; normal words unchanged."""
    stripped = line.strip()
    if re.fullmatch(r"(?:\S\s)+\S:?", stripped):
        return stripped.replace(" ", "")
    return " ".join(stripped.split())


def join_lines(lines: list[str]) -> str:
    out = ""
    for raw in lines:
        line = " ".join(raw.split())
        if not line:
            continue
        if not out:
            out = line
            continue
        first = line.split(" ", 1)[0]
        if re.search(r"[a-zäöüß]-$", out) and first[:1].islower() and first.rstrip(",.;") not in NO_JOIN:
            out = out[:-1] + line
        else:
            out += " " + line
    return out


def layout_blocks(text: str, *, running_headers: set[str] = frozenset(),
                  headings: dict[str, str] | None = None, detect_rn: bool = True) -> list[Block]:
    """Blocks with explicit Rn labels, heading blocks and page-break markers."""
    headings = headings or {}
    headers = {" ".join(h.split()) for h in running_headers if h}
    blocks: list[Block] = []
    current: list[str] = []
    label: str | None = None
    expected = 1
    locator = ""

    def flush() -> None:
        nonlocal current, label
        joined = join_lines(current)
        if joined:
            blocks.append(Block(text=joined, label=label, locator=locator))
        current, label = [], None

    pages = text.split("\f")
    for page_no, page in enumerate(pages, 1):
        if page_no > 1:
            flush()
            blocks.append(Block(text="", kind="pagebreak", locator=f"page[{page_no}]"))
        for line_no, line in enumerate(page.splitlines(), 1):
            compact = " ".join(line.split())
            if not compact:
                flush()
                continue
            if PAGE_MARKER.match(line) or compact in headers or RULE.match(line):
                flush()
                continue
            heading = headings.get(spaced_word(line).rstrip(":").lower()) if len(compact) <= 40 else None
            if heading and (line.rstrip().endswith(":") or spaced_word(line).endswith(":")
                            or spaced_word(line).rstrip(":").lower() in ("tenor",)):
                flush()
                blocks.append(Block(text=spaced_word(line), kind="heading", label=None,
                                    locator=f"page[{page_no}]/line[{line_no}]#{heading}"))
                continue
            m = RN_LINE.match(line) if detect_rn else None
            if m and int(m.group(1)) == expected:
                flush()
                label = m.group(1)
                expected += 1
                current = [m.group(2)]
                locator = f"page[{page_no}]/line[{line_no}]"
                continue
            if not current:
                locator = f"page[{page_no}]/line[{line_no}]"
            current.append(line)
    flush()
    return blocks
