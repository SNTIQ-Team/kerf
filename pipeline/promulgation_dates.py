"""Adoption (Ausfertigung) and promulgation (Verkündung) dates of one act.

Both dates are read from the printed Bundesgesetzblatt only:

* **adopted** -- the ``Vom <d>. <Monat> <yyyy>`` line directly under the act's
  title.  The signature block (``Berlin, den …`` / ``Bonn, den …``) after the
  act's last article corroborates it.
* **promulgated** -- the issue's ``Ausgegeben zu Bonn am <date>``: the
  masthead of an electronic BGBl document (2023+), or, for the printed BGBl,
  the running header of the page on which the act starts.  A printed issue
  contains several acts; the PDF of one act may begin with the tail of the
  previous act and end with the head of the next one, so the header is bound
  by page number and issue number, never by "first date in the file".

DIP (``execution_date``/``publication_date``) and recht.bund.de metadata
(``official_fields``) are *cross-checks*: they produce agreement or
disagreement findings and are never the source of the date.  GII
``G v. …`` strings are not consulted at all.

Result shape (per date)::

    {"date": "2023-08-16" | None, "status": "official" | "unknown",
     "basis": "BGBl document heading 'Vom 16. August 2023'",
     "reason": None | "…",           # only when unknown
     "evidence": [{"kind", "snippet", "start", "end", "page_index",
                   "text_sha256", "pdf_sha256", "document_id"}],
     "cross_checks": [{"source", "field", "value", "status"}]}
"""
from __future__ import annotations

import hashlib
import re
from typing import Any

from bgbl_text import (
    DATE_RE, MONTH_RE, SIGNATURE_RE, TITLE_DATE_RE, compact, german_date,
    page_index,
)

SCHEMA = "kerf-promulgation-dates/1"

# Electronic BGBl masthead: "2023 Ausgegeben zu Bonn am 18. August 2023 Nr. 217"
MASTHEAD_RE = re.compile(
    r"Ausgegeben\s+zu\s+(?P<place>Bonn|Berlin)\s+am\s+"
    rf"(?P<day>\d{{1,2}})\s?\.\s*(?P<month>{MONTH_RE})\s+(?P<year>\d{{4}})"
    r"\s+Nr\.\s*(?P<issue>\d+)")
# Running header of the printed BGBl, page number before (even page) or
# after (odd page); the text layer may break "a\nm", "T eil", "am 9.MON".
RUNNING_HEADER_RE = re.compile(
    r"(?:(?P<page_before>\d{1,5})[ \t]*\n?[ \t]*)?"
    r"Bundesgesetzblatt\s+Jahrgang\s+(?P<volume>\d{4})\s+T\s?eil\s+I\s+"
    r"Nr\.\s*(?P<issue>\d+)\s*,?\s*ausgegeben\s+zu\s+(?P<place>Bonn|Berlin)"
    rf"\s+a\s*m\s+(?P<day>\d{{1,2}})\s?\.\s*(?P<month>{MONTH_RE})\s+"
    r"(?P<year>\d{4})(?:[ \t]*(?P<page_after>\d{1,5})(?=[ \t]*(?:\n|$)))?")
# Electronic running footer: "Seite 3 von 25 Bundesgesetzblatt Jahrgang …"
FOOTER_RE = re.compile(
    r"Seite\s+(?P<page>\d+)\s+von\s+(?P<pages>\d+)\s+Bundesgesetzblatt\s+"
    r"Jahrgang\s+(?P<volume>\d{4})\s+Teil\s+I\s+Nr\.\s*(?P<issue>\d+)\s*,?\s*"
    rf"ausgegeben\s+zu\s+(?:Bonn|Berlin)\s+am\s+{DATE_RE}")
ENACTING_RE = re.compile(
    r"(?:Der\s+Bundestag\s+hat|Auf\s+Grund\s+(?:des|der|von)|Aufgrund\s+"
    r"(?:des|der|von)|verordnet|beschlossen\s*:)")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _evidence(kind: str, text: str, start: int, end: int,
              meta: dict) -> dict:
    return {
        "kind": kind,
        "snippet": text[start:end],
        "start": start,
        "end": end,
        "page_index": page_index(text, start),
        "document_id": meta.get("document_id"),
        "text_sha256": meta.get("text_sha256") or sha256_text(text),
        "pdf_sha256": meta.get("pdf_sha256"),
    }


def _unknown(reason: str, **extra: Any) -> dict:
    return {"date": None, "status": "unknown", "basis": None,
            "reason": reason, "evidence": [], "cross_checks": [], **extra}


def _cross(source: str, field: str, value: object,
           printed: str | None) -> dict:
    iso = _iso(value)
    if iso is None:
        status = "not_available"
    elif printed is None:
        status = "printed_date_unknown"
    else:
        status = "agree" if iso == printed else "disagree"
    return {"source": source, "field": field, "value": iso or value,
            "status": status}


def _iso(value: object) -> str | None:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    match = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if match:
        try:
            from datetime import date
            return date(int(match.group(3)), int(match.group(2)),
                        int(match.group(1))).isoformat()
        except ValueError:
            return None
    return None


# ------------------------------------------------------------- title/Vom


def _title_block(text: str, vom_start: int) -> tuple[int, str]:
    """Title lines directly above a ``Vom`` line (at most six lines)."""
    lines_start = vom_start
    collected: list[str] = []
    cursor = vom_start
    for _ in range(6):
        if cursor <= 0:
            break
        previous = text.rfind("\n", 0, cursor - 1)
        line = text[previous + 1:cursor].strip()
        if not line or line.endswith((".", ":", ";", "“", "“.")) or \
                re.search(r"Bundesgesetzblatt|www\.|^\d{1,5}$", line) or \
                SIGNATURE_RE.match(line):
            break
        collected.insert(0, line)
        lines_start = previous + 1
        cursor = previous + 1
    title = re.sub(r"(?<=\w)\s?[-­]\s*\n\s*(?=[a-zäöüß])", "",
                   "\n".join(collected))
    return lines_start, compact(title)


def _title_words(value: str) -> set[str]:
    value = re.sub(r"\(G-SIG:[^)]*\)", " ", str(value or ""))
    value = value.replace("­", "")
    return {word for word in re.findall(r"[a-zäöüß0-9]{3,}", value.casefold())
            if word not in {"und", "der", "die", "das", "des", "zur", "zum",
                            "vom", "von", "für", "über", "mit", "anderer"}}


def title_similarity(printed: str, expected: str) -> float | None:
    """Share of the expected (DIP/metadata) title words that were printed."""
    wanted = _title_words(expected)
    if not wanted:
        return None
    seen = _title_words(re.sub(r"\s+", "", printed)) | _title_words(printed)
    compact_printed = re.sub(r"\s+", "", printed).casefold()
    hits = sum(word in seen or word in compact_printed for word in wanted)
    return round(hits / len(wanted), 3)


def title_dates(text: str) -> list[dict]:
    rows = []
    for match in TITLE_DATE_RE.finditer(text):
        iso = german_date(match.group("day"), match.group("month"),
                          match.group("year"))
        if iso is None:
            continue
        block_start, title = _title_block(text, match.start())
        rows.append({"date": iso, "start": match.start(), "end": match.end(),
                     "title_start": block_start, "title": title,
                     "page_index": page_index(text, match.start())})
    return rows


def signatures(text: str, start: int = 0) -> list[dict]:
    rows = []
    for match in SIGNATURE_RE.finditer(text, start):
        iso = german_date(match.group("day"), match.group("month"),
                          match.group("year"))
        if iso:
            rows.append({"date": iso, "start": match.start(),
                         "end": match.end(), "place": match.group("place")})
    return rows


def act_title_date(text: str, *, title: str | None = None) -> dict | None:
    """The ``Vom`` line of the act this document was retrieved for.

    The retrieved document (electronic BGBl document, or the printed act's
    PDF) starts on the act's first page, so its title is on page 0.  When
    page 0 carries several titles (a short preceding act ends on it), the
    DIP/metadata title decides; ambiguity stays unresolved.
    """
    rows = title_dates(text)
    first_page = [row for row in rows if row["page_index"] == 0]
    if not first_page:
        # A title printed at the very bottom of page 0 can be extracted
        # after the page break; accept page 1 only if page 0 had none.
        first_page = [row for row in rows if row["page_index"] == 1][:1]
    for row in first_page:
        row["title_similarity"] = title_similarity(row["title"], title) \
            if title else None
    if len(first_page) == 1:
        return first_page[0]
    if len(first_page) > 1 and title:
        ranked = sorted(first_page, key=lambda row: -(
            row["title_similarity"] or 0))
        best, second = ranked[0], ranked[1]
        if (best["title_similarity"] or 0) >= 0.6 and \
                (best["title_similarity"] or 0) > (
                    second["title_similarity"] or 0):
            return best
    return None


def extract_adopted(text: str, meta: dict | None = None, *,
                    commencement_end: int | None = None) -> dict:
    """Ausfertigungsdatum from the ``Vom`` heading line of the act."""
    meta = meta or {}
    row = act_title_date(text, title=meta.get("title"))
    if row is None:
        result = _unknown("no_unique_title_date_line_on_first_page")
        result["cross_checks"] = _adopted_cross_checks(None, meta)
        return result
    printed = text[row["start"]:row["end"]].strip()
    evidence = [_evidence("title_date_line", text, row["start"], row["end"],
                          meta)]
    if row.get("title"):
        evidence[0]["title"] = row["title"]
        evidence[0]["title_similarity"] = row.get("title_similarity")
    signature_check = {"source": "BGBl signature block",
                       "field": "Berlin/Bonn, den", "value": None,
                       "status": "not_found"}
    after = commencement_end if commencement_end is not None else row["end"]
    blocks = signatures(text, after)
    if not blocks and commencement_end is not None:
        blocks = signatures(text, row["end"])
    if blocks:
        # Printed issues interleave neighbouring acts in text order: the
        # act's own signature is the first one carrying its heading date.
        block = next((candidate for candidate in blocks
                      if candidate["date"] == row["date"]), blocks[0])
        evidence.append(_evidence("signature_date", text, block["start"],
                                  block["end"], meta))
        signature_check.update({
            "value": block["date"],
            "status": "agree" if block["date"] == row["date"] else "disagree"})
    return {
        "date": row["date"],
        "status": "official",
        "basis": f"BGBl document heading '{compact(printed)}'",
        "reason": None,
        "evidence": evidence,
        "cross_checks": [signature_check, *_adopted_cross_checks(
            row["date"], meta)],
    }


def _adopted_cross_checks(printed: str | None, meta: dict) -> list[dict]:
    checks = [_cross("DIP", "ausfertigungsdatum", meta.get("execution_date"),
                     printed)]
    official = meta.get("official_fields") or {}
    if official.get("Ausfertigungsdatum"):
        checks.append(_cross("recht.bund.de", "Ausfertigungsdatum",
                             official.get("Ausfertigungsdatum"), printed))
    return checks


# ---------------------------------------------------------- promulgation


def issue_headers(text: str) -> list[dict]:
    """Every issue date statement in the text, with page binding."""
    rows = []
    for match in MASTHEAD_RE.finditer(text):
        iso = german_date(match.group("day"), match.group("month"),
                          match.group("year"))
        if iso:
            rows.append({"kind": "masthead", "date": iso,
                         "issue": match.group("issue"), "page": None,
                         "start": match.start(), "end": match.end(),
                         "page_index": page_index(text, match.start())})
    for match in FOOTER_RE.finditer(text):
        iso = german_date(match.group("day"), match.group("month"),
                          match.group("year"))
        if iso:
            rows.append({"kind": "running_footer", "date": iso,
                         "issue": match.group("issue"),
                         "volume": match.group("volume"),
                         "page": None, "start": match.start(),
                         "end": match.end(),
                         "page_index": page_index(text, match.start())})
    for match in RUNNING_HEADER_RE.finditer(text):
        if any(row["start"] <= match.start() < row["end"] for row in rows):
            continue
        iso = german_date(match.group("day"), match.group("month"),
                          match.group("year"))
        if not iso:
            continue
        page = match.group("page_before") or match.group("page_after")
        rows.append({"kind": "running_header", "date": iso,
                     "issue": match.group("issue"),
                     "volume": match.group("volume"),
                     "page": page, "start": match.start(),
                     "end": match.end(),
                     "page_index": page_index(text, match.start())})
    return sorted(rows, key=lambda row: row["start"])


def extract_promulgated(text: str, meta: dict | None = None) -> dict:
    """Verkündungsdatum: the issue date of the page on which the act starts.

    ``meta`` may carry ``issue`` (Nr.), ``page`` (legacy start page) and
    ``year``.  For a printed issue the header of the act's start page is
    preferred; otherwise the header on the PDF's first page.  Headers of a
    different issue or date in the same file are reported, never silently
    merged.
    """
    meta = meta or {}
    headers = issue_headers(text)
    expected_issue = str(meta.get("issue") or "").strip() or None
    expected_page = str(meta.get("page") or "").strip() or None
    masthead = [row for row in headers if row["kind"] == "masthead"
                and row["page_index"] == 0]
    chosen = None
    basis_kind = None
    if masthead:
        chosen = masthead[0]
        basis_kind = "masthead"
    else:
        running = [row for row in headers if row["kind"] == "running_header"]
        if expected_page:
            by_page = [row for row in running if row["page"] == expected_page]
            if by_page:
                chosen = by_page[0]
                basis_kind = "start_page_header"
        if chosen is None:
            first = [row for row in running if row["page_index"] == 0]
            if first:
                chosen = first[0]
                basis_kind = "first_page_header"
    if chosen is None:
        result = _unknown("no_issue_header_found")
        result["cross_checks"] = _promulgated_cross_checks(None, meta)
        return result
    if expected_issue and chosen["issue"] != expected_issue:
        result = _unknown("issue_number_mismatch",
                          observed_issue=chosen["issue"],
                          expected_issue=expected_issue)
        result["cross_checks"] = _promulgated_cross_checks(None, meta)
        return result
    snippet = compact(text[chosen["start"]:chosen["end"]])
    evidence = [_evidence(chosen["kind"], text, chosen["start"],
                          chosen["end"], meta)]
    same_issue = [row for row in headers if row["issue"] == chosen["issue"]
                  and row is not chosen]
    conflicting = [row for row in same_issue if row["date"] != chosen["date"]]
    other_issues = sorted({row["issue"] for row in headers
                           if row["issue"] != chosen["issue"]})
    checks = [{"source": "BGBl running headers/footers of the same issue",
               "field": "ausgegeben zu Bonn am",
               "value": len(same_issue),
               "status": ("disagree" if conflicting else
                          "agree" if same_issue else "not_available")}]
    if conflicting:
        result = _unknown("issue_headers_disagree",
                          conflicting_dates=sorted({row["date"]
                                                    for row in conflicting}))
        result["evidence"] = evidence
        result["cross_checks"] = checks
        return result
    if basis_kind == "masthead":
        place = re.search(r"zu\s+(\w+)", snippet)
        basis = (f"BGBl issue masthead 'Ausgegeben zu "
                 f"{place.group(1) if place else 'Bonn'} am "
                 f"{_german(chosen['date'])} Nr. {chosen['issue']}'")
    else:
        where = (f"page {chosen['page']}" if chosen.get("page") else
                 "first page")
        basis = (f"BGBl running header of the act's {where}: Teil I "
                 f"Nr. {chosen['issue']}, ausgegeben am "
                 f"{_german(chosen['date'])}")
    return {
        "date": chosen["date"],
        "status": "official",
        "basis": basis,
        "reason": None,
        "issue": chosen["issue"],
        "evidence": evidence,
        "other_issue_headers_in_file": other_issues,
        "cross_checks": checks + _promulgated_cross_checks(chosen["date"],
                                                           meta),
    }


def _german(iso: str) -> str:
    from bgbl_text import format_german_date
    return format_german_date(iso)


def _promulgated_cross_checks(printed: str | None, meta: dict) -> list[dict]:
    checks = [_cross("DIP", "verkuendungsdatum", meta.get("publication_date"),
                     printed)]
    official = meta.get("official_fields") or {}
    if official.get("Veröffentlichungsdatum"):
        checks.append(_cross("recht.bund.de", "Veröffentlichungsdatum",
                             official.get("Veröffentlichungsdatum"), printed))
    return checks


def findings(result: dict, field: str) -> list[dict]:
    """Agreement/disagreement findings for one date result."""
    rows = []
    for check in result.get("cross_checks") or []:
        if check["status"] in {"agree", "disagree"}:
            rows.append({"kind": f"{field}_cross_check",
                         "field": field, "status": check["status"],
                         "source": check["source"],
                         "source_field": check["field"],
                         "source_value": check["value"],
                         "printed_value": result.get("date")})
    return rows


def extract_promulgation_dates(text: str, meta: dict | None = None, *,
                               commencement_end: int | None = None) -> dict:
    """Both dates plus cross-check findings for one BGBl document text."""
    meta = dict(meta or {})
    meta.setdefault("text_sha256", sha256_text(text))
    adopted = extract_adopted(text, meta, commencement_end=commencement_end)
    promulgated = extract_promulgated(text, meta)
    rows = findings(adopted, "adopted") + findings(promulgated, "promulgated")
    if adopted.get("date") and promulgated.get("date") and \
            adopted["date"] > promulgated["date"]:
        rows.append({"kind": "adopted_after_promulgated", "status": "disagree",
                     "field": "adopted", "printed_value": adopted["date"],
                     "source_value": promulgated["date"]})
    return {"schema": SCHEMA, "adopted": adopted, "promulgated": promulgated,
            "findings": rows}
