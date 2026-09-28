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
MARGIN_NUMBER = re.compile(r"^( *)(\d{1,3})( {2,})(?=\S)")
PAGE_MARKER = re.compile(r"^\s*(?:-\s*\d{1,3}\s*-|Seite\s+\d+(?:\s+von\s+\d+)?|\d{1,3}\s*/\s*\d{1,3})\s*$")
RULE = re.compile(r"^\s*[-_=]{10,}\s*$")
# Only at the top or bottom edge of a page: a bare page number ("2") or "Seite 2/7".
EDGE_PAGE_MARKER = re.compile(r"^\s*(?:\d{1,3}|Seite\s+\d+(?:\s*(?:/|von)\s*\d+)?)\s*$")
SENTENCE_END = re.compile(r"[.!?:;“”\"»)]$")
OUTLINE = re.compile(r"^(?:[IVX]+\.|\d{1,2}\.|[a-z]{1,2}\)|\([a-z0-9]{1,3}\)|[A-Z]\.)(?:\s|$)")
NO_JOIN = {"und", "oder", "bzw", "bzw.", "sowie"}
COLONLESS_HEADINGS = {"tenor", "gründe", "tatbestand", "entscheidungsgründe", "leitsatz", "leitsätze"}


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


def align_margin_numbers(text: str) -> tuple[str, int]:
    """Move right-aligned margin numbers to column 0.

    pdftotext -layout right-aligns the Randnummern column: on a page that also
    holds "10", Rn 9 is printed as " 9   Text", which pdftext.RN_LINE (number at
    column 0) does not accept, and the Rn sequence would stop there.  A number
    with one or two leading spaces is moved only when its text starts in the same
    column as the text of a column-0 margin number on the same page; only
    whitespace changes, the sequence check in layout_blocks still applies.
    """
    pages = text.split("\f")
    moved = 0
    for index, page in enumerate(pages):
        lines = page.split("\n")
        columns = {m.end() for m in map(MARGIN_NUMBER.match, lines) if m and not m.group(1)}
        if not columns:
            continue
        for i, line in enumerate(lines):
            m = MARGIN_NUMBER.match(line)
            if m and 1 <= len(m.group(1)) <= 2 and m.end() in columns:
                lines[i] = m.group(2) + " " * (len(m.group(1)) + len(m.group(3))) + line[m.end():]
                moved += 1
        pages[index] = "\n".join(lines)
    return "\f".join(pages), moved


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

    if detect_rn:
        text, _ = align_margin_numbers(text)
    pages = text.split("\f")
    for page_no, page in enumerate(pages, 1):
        lines = page.splitlines()
        filled = [i for i, raw in enumerate(lines) if raw.strip()]
        edges = {filled[0], filled[-1]} if filled else set()
        if page_no > 1:
            flush()
            blocks.append(Block(text="", kind="pagebreak", locator=f"page[{page_no}]"))
        for line_no, line in enumerate(lines, 1):
            compact = " ".join(line.split())
            if not compact:
                flush()
                continue
            if PAGE_MARKER.match(line) or compact in headers or RULE.match(line) or (
                    line_no - 1 in edges and EDGE_PAGE_MARKER.match(line)):
                flush()
                continue
            word = spaced_word(line)
            heading = headings.get(word.rstrip(":").lower()) if len(compact) <= 40 else None
            # "Beschluss"/"Urteil"/"Entscheidung" also occur as plain words and titles
            # ("URTEIL"), so they only count with a colon; reasoning headings stand alone.
            if heading and (word.endswith(":") or word.rstrip(":").lower() in COLONLESS_HEADINGS):
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
    return join_page_breaks(blocks)


def join_page_breaks(blocks: list[Block]) -> list[Block]:
    """Re-join a paragraph that a page break cut in two.

    The text after the break continues the block before it when that block does
    not end a sentence (or ends in a hyphenated word), unless the continuation
    starts a new outline item ("aa)", "2.").  Labeled blocks, headings and
    page-break markers are never merged into.
    """
    out: list[Block] = []
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if block.kind == "pagebreak" and out and i + 1 < len(blocks):
            prev, nxt = out[-1], blocks[i + 1]
            joinable = (prev.kind == "text" and nxt.kind == "text" and nxt.label is None and prev.text
                        and nxt.text and not OUTLINE.match(nxt.text)
                        and (re.search(r"[A-Za-zäöüß]-$", prev.text) or not SENTENCE_END.search(prev.text)
                             or nxt.text[:1].islower()))
            if joinable:
                merged = join_lines([prev.text, nxt.text])
                out[-1] = Block(text=merged, label=prev.label, kind="text",
                                locator=f"{prev.locator}+{nxt.locator}")
                i += 2
                continue
        out.append(block)
        i += 1
    return out
