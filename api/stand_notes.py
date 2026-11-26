"""Typed notes from the GII ``Stand`` block (standangabe entries).

GII publishes each consolidation with typed status entries, flattened by
Kerf's fetcher into ``"Neuf: … | Stand: … | Hinweis: … | Sonst: …"``.
The wording carries legally relevant, *source-declared* facts that were so
far only exposed as free text:

``Stand: zuletzt geändert durch Art. 8 G v. 22.12.2025 I Nr. 363``
    the last amendment of the **act** (not of any particular norm).
``Hinweis: Änderung durch Art. 1 G v. 16.4.2026 I Nr. 107 textlich
nachgewiesen, dokumentarisch noch nicht abschließend bearbeitet``
    the change is already incorporated in the served text, but GII's
    documentation (Stand line, footnotes) is not finished.
``Hinweis: Änderung durch Art. 4 G v 22.7.2026 I Nr. 222 mWv 29.7.2026
noch nicht berücksichtigt``
    a promulgated change is *not* yet in the served text.

Every note keeps its verbatim text and the amending reference exactly as GII
wrote it (``citation_text``).  The reference may be used to *find* an
official document, never as the source of a date: the ``mWv`` dates below
are GII's secondary statement (``announced_effective_dates``) and the
``G v.`` date is not an adoption date until the promulgation document itself
confirms it.
"""
from __future__ import annotations

import re
from typing import Any

NOTE_SCHEMA = "kerf-gii-stand-note/1"

_ENTRY = re.compile(r"(?:^|\s\|\s)(Stand|Neuf|Hinweis|Sonst|Aufh|Ausfertigung)"
                    r"\s*:\s*")
# Several changes in one entry are written back to back.
_SPLIT = re.compile(r"(?<=[a-zäöüß.)\d])(?<!Mittelbare)\s*;?\s+"
                    r"(?=(?:Mittelbare\s+)?Änderung(?:en)?\s+durch\s)")
_REFERENCE = re.compile(
    r"(?P<ref>(?:Art\.|§)\s*\d+[a-z]?(?:\s+(?:Abs|Nr|Buchst|Satz)\.?\s*"
    r"\d+[a-z]?(?:\s+(?:bis|u\.)\s+\d+[a-z]?)*)*"
    r"(?:\s+(?:bis|u\.)\s+\d+[a-z]?)*"
    r"\s+(?:G|V|Bek|VO|Gesetz|Verordnung)\.?\s+v\.?\s*\d{1,2}\.\d{1,2}\.\d{4}"
    r"\s+(?:I|II)(?:\s+\d{4})?\s+(?:Nr\.\s*\d+|\d+(?:,\s*\d+)*)"
    r"(?:\s*\(Nr\.\s*\d+\))?)")
_BARE_REFERENCE = re.compile(
    r"(?P<ref>(?:G|V|Bek)\.?\s+v\.?\s*\d{1,2}\.\d{1,2}\.\d{4}\s+(?:I|II)"
    r"(?:\s+\d{4})?\s+(?:Nr\.\s*\d+|\d+(?:,\s*\d+)*))")
_ARTICLE = re.compile(r"^(?:Art\.|§)\s*(\d+[a-z]?)")
_ISSUE = re.compile(r"\bv\.?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(I|II)"
                    r"(?:\s+(\d{4}))?\s+Nr\.\s*(\d+)")
_PAGE = re.compile(r"\bv\.?\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(I|II)\s+(\d+)")
_MWV = re.compile(r"mWv\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")


def split_entries(stand: str | None) -> list[dict[str, str]]:
    """``"Stand: … | Hinweis: …"`` → ``[{"type", "text"}]`` in source order."""
    raw = " ".join(str(stand or "").split())
    if not raw:
        return []
    marks = list(_ENTRY.finditer(raw))
    if not marks:
        return [{"type": "unknown", "text": raw}]
    entries = []
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(raw)
        text = raw[mark.end():end].strip().rstrip(";").strip()
        if text:
            entries.append({"type": mark.group(1), "text": text})
    return entries


def _day(day: str, month: str, year: str) -> str | None:
    try:
        d, m, y = int(day), int(month), int(year)
    except ValueError:
        return None
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def amending_reference(text: str) -> dict[str, Any] | None:
    """The amending reference exactly as GII wrote it (lookup key only)."""
    match = _REFERENCE.search(text) or _BARE_REFERENCE.search(text)
    if not match:
        return None
    ref = " ".join(match.group("ref").split())
    result: dict[str, Any] = {"citation_text": ref, "article": None,
                              "gazette_lookup": None}
    if article := _ARTICLE.match(ref):
        result["article"] = article.group(1)
    if issue := _ISSUE.search(ref):
        year = issue.group(5) or issue.group(3)
        result["gazette_lookup"] = {
            "part": issue.group(4), "year": int(year),
            "number": int(issue.group(6)), "page": None,
            # GII's wording only; used to pick the document, never shown
            # or used as an adoption date (the document's own heading is).
            "date_in_gii_wording": _day(issue.group(1), issue.group(2),
                                        issue.group(3)),
            "document_id": (f"bgbl-{1 if issue.group(4) == 'I' else 2}-"
                            f"{year}-{int(issue.group(6))}")}
    elif page := _PAGE.search(ref):
        result["gazette_lookup"] = {
            "part": page.group(4), "year": int(page.group(3)),
            "number": None, "page": int(page.group(5)),
            "date_in_gii_wording": _day(page.group(1), page.group(2),
                                        page.group(3)),
            "document_id": (f"bgbl-{1 if page.group(4) == 'I' else 2}-"
                            f"{page.group(3)}-s{int(page.group(5))}")}
    return result


def classify(text: str) -> dict[str, Any]:
    folded = text.casefold()
    indirect = folded.startswith("mittelbare")
    if "textlich nachgewiesen" in folded:
        kind = "textually_incorporated_documentation_pending"
        incorporated, documented = True, False
    elif "noch nicht berücksichtigt" in folded:
        kind = "not_yet_incorporated"
        incorporated, documented = False, None
    elif re.search(r"\b(?:ist|sind)\s+berücksichtigt\b", folded):
        kind = ("indirect_change_incorporated" if indirect else
                "announcement_incorporated" if folded.startswith("bek")
                or "bek." in folded[:12] else "incorporated")
        incorporated, documented = True, None
    elif "nicht ausführbar" in folded or "nicht mehr ausführbar" in folded:
        kind = "change_not_executable"
        incorporated, documented = False, None
    elif "aufgeh" in folded[:40]:
        kind = "repeal_announced"
        incorporated, documented = None, None
    elif re.search(r"\b(?:tritt|treten)\b.*\bin kraft\b", folded) or \
            "in kraft getreten" in folded:
        kind = "commencement_note"
        incorporated, documented = None, None
    elif folded.startswith(("dieses g ersetzt", "ersetzt g", "ersetzt v")):
        kind = "replaces_act"
        incorporated, documented = None, None
    elif re.match(r"(?:zuletzt\s+)?geändert\s+durch\b", folded):
        kind = "amended_by_note"
        incorporated, documented = True, None
    else:
        kind = "other"
        incorporated, documented = None, None
    return {"kind": kind, "indirect": indirect,
            "text_incorporated": incorporated,
            "documentation_complete": documented}


def parse_notes(stand: str | None) -> dict[str, Any]:
    """Typed view of one consolidation's Stand block.

    Returns ``{"act_stand_note", "consolidation_note", "documentation_notes",
    "entries"}``.  ``act_stand_note`` is the act-level "zuletzt geändert"
    statement and never a claim about an individual norm.
    """
    entries = split_entries(stand)
    act_note = None
    consolidation = None
    notes: list[dict[str, Any]] = []
    for entry in entries:
        kind, text = entry["type"], entry["text"]
        if kind == "Stand":
            match = re.search(r"zuletzt\s+geändert\s+durch\s+(.+)$", text,
                              flags=re.IGNORECASE)
            act_note = {
                "schema": NOTE_SCHEMA, "scope": "act", "norm_specific": False,
                "source": "GII Stand", "text": text,
                "last_amended_by_text": (match.group(1).strip().rstrip(";")
                                         if match else None),
                "amending_act": amending_reference(text),
                "note": ("GII's act-level 'zuletzt geändert' statement; it "
                         "does not say which norms that amendment touched"),
            }
            continue
        if kind == "Neuf":
            consolidation = {"source": "GII Stand (Neuf)", "text": text,
                             "amending_act": amending_reference(text)}
            continue
        parts = [part.strip() for part in _SPLIT.split(text) if part.strip()]
        for part in parts or [text]:
            typed = classify(part)
            note = {
                "schema": NOTE_SCHEMA, **typed,
                "text": part, "source": f"GII Stand ({kind})",
                "amending_act": amending_reference(part),
                "announced_effective_dates": [
                    day for day in (_day(*m.groups()) for m in
                                    _MWV.finditer(part)) if day],
            }
            if note["announced_effective_dates"]:
                note["announced_effective_dates_basis"] = (
                    "GII 'mWv' wording (secondary; the commencement clause "
                    "of the amending act is authoritative)")
            if "mit zukünftiger wirkung" in part.casefold():
                note["announced_future_effect_undated"] = True
            notes.append(note)
    return {"act_stand_note": act_note, "consolidation_note": consolidation,
            "documentation_notes": notes, "entries": entries}
