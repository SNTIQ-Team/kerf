"""Detect and remove printed page furniture from extracted official text.

Running headers and footers of the Bundesgesetzblatt and of Bundestag or
Bundesrat Drucksachen are evidence about the *page*, not wording of the
command, norm or explanation that happens to cross that page boundary.
``pdftotext`` emits them inline, sometimes in the middle of a line, e.g.::

    ... „29. Dezember 2023“ ersetzt. Seite 3 von 4 Bundesgesetzblatt Jahrgang
    2023 Teil I Nr. 406, ausgegeben zu Bonn am 28. Dezember 2023

The helpers below keep the removal narrow and auditable: every removed span
is reported with its offsets in the input, so a consumer can always
reproduce the original extraction byte for byte.
"""
from __future__ import annotations

import re
from typing import Any

_MONTH = (r"(?:Januar|Februar|März|April|Mai|Juni|Juli|August|September|"
          r"Oktober|November|Dezember)")
_ISSUED = (rf",?\s*ausgegeben\s+zu\s+(?:Bonn|Berlin|Köln)\s+am\s+\d{{1,2}}\.\s*"
           rf"{_MONTH}\s+\d{{4}}")

# ``T eil`` occurs in older BGBl text layers (letter-spaced typesetting).
_BGBL_CORE = (r"Bundesgesetzblatt\s+Jahrgang\s+\d{4}\s+T\s?eil\s+[IVX]+\s+"
              r"Nr\.\s*\d+")

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Electronic BGBl (2023+): "Seite 3 von 4 Bundesgesetzblatt ... Nr. 406,
    # ausgegeben zu Bonn am 28. Dezember 2023"
    ("bgbl_running_header", re.compile(
        rf"Seite\s+\d+\s+von\s+\d+\s+{_BGBL_CORE}(?:{_ISSUED})?")),
    # Printed BGBl (until 2022): page number before or after the header.
    ("bgbl_running_header", re.compile(
        rf"(?:(?<=\n)|^)[ \t]*\d{{1,5}}[ \t]+{_BGBL_CORE}{_ISSUED}", re.M)),
    ("bgbl_running_header", re.compile(
        rf"{_BGBL_CORE}{_ISSUED}(?:[ \t]+\d{{1,5}}(?=[ \t]*(?:\n|$)))?")),
    ("bgbl_footer", re.compile(
        r"Das\s+Bundesgesetzblatt\s+im\s+Internet:[^\n]*")),
    # Bundestag Drucksache running header, both page orientations.
    ("drucksache_running_header", re.compile(
        r"Drucksache\s+\d{1,2}/\d{1,6}\s+[–-]\s*\d+\s*[–-]\s+Deutscher\s+"
        r"Bundestag\s+[–-]\s+\d{1,2}\.\s+Wahlperiode")),
    ("drucksache_running_header", re.compile(
        r"Deutscher\s+Bundestag\s+[–-]\s+\d{1,2}\.\s+Wahlperiode\s+[–-]\s*"
        r"\d+\s*[–-]\s+Drucksache\s+\d{1,2}/\d{1,6}")),
    # Bundesrat Drucksache running header.
    ("drucksache_running_header", re.compile(
        r"Drucksache\s+\d{1,4}/\d{2}\s+[–-]\s*\d+\s*[–-]\s*Bundesrat")),
    ("drucksache_running_header", re.compile(
        r"Bundesrat\s+[–-]\s*\d+\s*[–-]\s*Drucksache\s+\d{1,4}/\d{2}")),
)


def find_page_furniture(text: str) -> list[dict[str, Any]]:
    """Return non-overlapping furniture spans ``{kind, start, end, text}``."""
    spans: list[dict[str, Any]] = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if start == end:
                continue
            if any(start < row["end"] and row["start"] < end for row in spans):
                continue
            spans.append({"kind": kind, "start": start, "end": end,
                          "text": match.group(0)})
    spans.sort(key=lambda row: row["start"])
    return spans


def strip_page_furniture(text: str) -> tuple[str, list[dict[str, Any]]]:
    """Remove furniture; return ``(clean_text, removed_spans)``.

    Removed spans keep offsets into the *input*.  Whitespace around a removed
    span collapses to one separator so words on both sides of a page break do
    not fuse and no empty line is invented inside a sentence.
    """
    spans = find_page_furniture(text)
    if not spans:
        return text, []
    out: list[str] = []
    cursor = 0
    for row in spans:
        before = text[cursor:row["start"]].rstrip(" \t")
        rest = text[row["end"]:]
        after = rest.lstrip(" \t")
        skip = len(rest) - len(after)
        line_before = not before or before.endswith("\n") or (
            not out and not before)
        if line_before and after.startswith("\n"):
            # The furniture occupied a whole line: drop that line entirely.
            skip += 1
        elif before and not before.endswith("\n") and after and \
                not after.startswith("\n"):
            before += " "  # keep the words on both sides apart
        out.append(before)
        cursor = row["end"] + skip
    out.append(text[cursor:])
    clean = "".join(out)
    return clean, spans
