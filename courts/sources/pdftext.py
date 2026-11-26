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
from .base import iso_date

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


_COVER_FORMS = {form.casefold(): form for form in (
    "Beschluss", "Urteil", "Gerichtsbescheid", "Teilurteil", "Schlussurteil", "Zwischenurteil",
    "Endurteil", "Anerkenntnisurteil", "Versäumnisurteil", "Verfügung",
)}
_COVER_END = re.compile(r"^(?:In der |In dem Verfahren|In Sachen|hat (?:der|das|die) )", re.I)
_COVER_DATE = r"\d{1,2}\.\s*(?:\d{1,2}\.\s*|[A-Za-zÄÖÜäöü]+\s+)\d{4}"
_PRONOUNCED = re.compile(r"\bverkündet\s+am\s*:?\s*(" + _COVER_DATE + r")", re.I)
_DECIDED = re.compile(r"\bam\s+(" + _COVER_DATE + r")\s+(?:für\s+Recht\s+erkannt|beschlossen|entschieden|verfügt)\s*:", re.I)


_HEARD_AND_DECIDED = re.compile(r"\baufgrund der mündlichen Verhandlung vom (" + _COVER_DATE +
                                r") für Recht erkannt\s*:", re.I)


def decision_form_from_cover(text: str, docket: str, day: str, listed: str | None,
                             metadata: dict, *, court_abbr: str = "") -> str | None:
    """Read a standalone cover heading once the source's date and docket are corroborated."""
    pages = text.split("\f", 2)
    labels = set()
    for line in pages[0].splitlines()[:80]:
        line = " ".join(line.split())
        if _COVER_END.match(line):
            break
        form = _COVER_FORMS.get(re.sub(r"\s+", "", line).rstrip(":").casefold())
        if form:
            labels.add(form)
    if len(labels) != 1:
        return listed
    form = labels.pop()
    if form == listed:
        return listed
    # A generic cover can accompany a more specific catalogue label, for example Streitwertbeschluss.
    if listed and listed.casefold().endswith(form.casefold()):
        return listed

    docket_pattern = r"\s*".join(re.escape(token) for token in docket.split())
    prefixes = r"Az\.?|Aktenzeichen|Geschäftszeichen"
    if court_abbr:
        prefixes += "|" + re.escape(court_abbr) + r"\.?"
    prefix = re.compile(r"^(?:" + prefixes + r")(?=[:\s])\s*:?\s*", re.I)
    docket_on_cover = False
    for line in pages[0].splitlines()[:25]:
        line = line.strip()
        if not re.search(r"\d\s*/\s*\d{2,4}\b", line):
            continue
        value = prefix.sub("", line, count=1)
        if value != line or re.match(r"^(?:[A-Z]\s+)?\d+\s+[A-Z]", line):
            primary = value.partition("=")[0].strip()
            docket_on_cover = bool(docket_pattern and re.match(docket_pattern + r"(?![\w/])", primary, re.I))
            break
    lines = pages[0].splitlines()
    if len(pages) > 1:
        following = pages[1].splitlines()
        lines += [line for i, line in enumerate(following) if i >= 4 or not EDGE_PAGE_MARKER.fullmatch(line)]
    front = join_lines(lines)
    dates = set()
    patterns = (_PRONOUNCED, _DECIDED) if _PRONOUNCED.search(front) or _DECIDED.search(front) else (_HEARD_AND_DECIDED,)
    for pattern in patterns:
        for candidate in pattern.finditer(front):
            try:
                dates.add(iso_date(candidate.group(1)))
            except UpstreamFailure:
                continue
    # A hearing date can differ from the date when the court pronounced its decision.
    date_on_front = dates == {day}
    metadata["decision_type_evidence"] = {"catalog": listed, "pdf_cover": form, "docket_on_cover": docket_on_cover,
                                          "date_on_front": date_on_front, "operative_dates": sorted(dates)}
    if docket_on_cover and len(dates) == 1 and day and day not in dates:
        raise UpstreamFailure("identity_mismatch",
                              f"catalogue {listed!r} dated {day} links to {form!r} dated {next(iter(dates))} for {docket}")
    return form if docket_on_cover and date_on_front else listed


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


XHTML = "{http://www.w3.org/1999/xhtml}"


def pdf_word_lines(pdf: bytes, *, url: str | None = None) -> list[dict]:
    """Per page: ``{"width", "height", "lines": [{"y0", "y1", "words": [(x0, x1, text)]}]}`` from
    ``pdftotext -bbox-layout``.  Positions let a reader find a margin column exactly, which the
    space-padded ``-layout`` text cannot do reliably (a justified line may leave one space before
    a right-margin number, or none)."""
    import xml.etree.ElementTree as ET
    if not pdf.startswith(b"%PDF"):
        raise UpstreamFailure("unexpected_representation", "not a PDF", url=url)
    if shutil.which("pdftotext") is None:
        raise UpstreamFailure("parse_error", "pdftotext (poppler) not installed", url=url)
    proc = subprocess.run(["pdftotext", "-bbox-layout", "-enc", "UTF-8", "-", "-"], input=pdf,
                          capture_output=True, timeout=180)
    if proc.returncode != 0:
        raise UpstreamFailure("parse_error", f"pdftotext -bbox-layout failed: {proc.stderr[:200]!r}", url=url)
    try:
        root = ET.fromstring(proc.stdout)
    except ET.ParseError as exc:
        raise UpstreamFailure("parse_error", f"pdftotext bbox output: {exc}", url=url) from exc
    pages = []
    for page in root.iter(f"{XHTML}page"):
        lines = []
        for line in page.iter(f"{XHTML}line"):
            words = [(float(w.get("xMin")), float(w.get("xMax")), w.text or "") for w in line.iter(f"{XHTML}word")]
            if words:
                lines.append({"y0": float(line.get("yMin")), "y1": float(line.get("yMax")), "words": words})
        pages.append({"width": float(page.get("width")), "height": float(page.get("height")), "lines": lines})
    return pages


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
