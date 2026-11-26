"""Evidence-only dates of one amendment item: adopted, promulgated, in force.

Entry point::

    resolve_change_dates(document_row_or_text, article, item=None)
    -> {"adopted": {...}, "promulgated": {...}, "in_force": {...},
        "applies_from": {...}, "findings": [...], ...}

``document_row_or_text`` is either a BGBl document row (as stored in
``data/snapshots/bgbl_history_backfill/*/documents.jsonl``,
``bgbl_documents`` or the legacy ``bgbl_legacy_history_backfill`` store and
checkpoints; its ``text_object`` is read and verified against
``text_sha256``) or the extracted document text itself.  ``item`` is the
position inside the amending article, e.g. ``{"nummer": "18", "buchstabe":
"b"}``; ``absatz`` addresses a collective "Folgeänderungen" subsection.

All three dates come from the printed BGBl document; DIP and recht.bund.de
metadata only produce agreement/disagreement findings.
"""
from __future__ import annotations

import hashlib
import os
import re
from functools import lru_cache
from pathlib import Path

from commencement import (
    dip_cross_check, item_path, parse_document_commencement, path_label,
    resolve_applies_from, resolve_in_force,
)
from promulgation_dates import extract_promulgation_dates

SCHEMA = "kerf-change-dates/1"
ROOT = Path(__file__).resolve().parents[1]
LEGACY_ID_RE = re.compile(r"bgbl-1-\d{4}-s\d+")


class DocumentTextError(ValueError):
    """The document text is missing or differs from its recorded digest."""


def default_text_roots() -> list[Path]:
    """Where ``text_object`` paths are resolved, in order."""
    roots = []
    for key in ("KERF_DATA_ROOT",):
        if os.environ.get(key):
            roots.append(Path(os.environ[key]))
    roots.append(ROOT)
    roots.append(ROOT / "data" / "legacy_bgbl_history")
    archive = os.environ.get("KERF_LEGACY_ARCHIVE") or str(
        Path.home() / ".local/share/sntiq-legal/kerf-archive/data/"
        "legacy_bgbl_history")
    roots.append(Path(archive))
    return roots


def load_document_text(row: dict, roots: list[Path] | None = None) -> str:
    """Read a row's ``text_object`` from the first root holding it.

    The bytes must hash to ``text_sha256``; a mismatch is an error, never a
    fallback to another copy with different content.
    """
    relative = str(row.get("text_object") or "")
    digest = str(row.get("text_sha256") or "")
    if not relative or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise DocumentTextError("document row has no verifiable text object")
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise DocumentTextError("text object path escapes its store")
    for root in roots or default_text_roots():
        path = Path(root) / relative
        if not path.is_file():
            continue
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != digest:
            raise DocumentTextError(f"text digest mismatch: {path}")
        return payload.decode("utf-8")
    raise DocumentTextError(f"text object not found: {relative}")


def is_legacy(row: dict | None, document_id: str | None = None) -> bool:
    row = row or {}
    identifier = str(document_id or row.get("document_id") or "")
    return bool(LEGACY_ID_RE.fullmatch(identifier) or row.get(
        "source_filename") or (row.get("page") and not row.get("eli")))


def document_meta(row: dict) -> dict:
    return {
        "document_id": row.get("document_id"),
        "title": row.get("title"),
        "issue": row.get("issue"),
        "page": row.get("page"),
        "year": row.get("year"),
        "execution_date": row.get("execution_date"),
        "publication_date": row.get("publication_date"),
        "official_fields": row.get("official_fields") or {},
        "pdf_sha256": row.get("sha256"),
        "text_sha256": row.get("text_sha256"),
    }


def dip_rows_of(row: dict) -> list[dict]:
    """DIP ``inkrafttreten`` rows attached to a document (deduplicated)."""
    rows: list[dict] = []
    sources = [row.get("dip_entry_into_force") or []]
    for procedure in row.get("retrospective_procedures") or []:
        sources.append(procedure.get("dip_entry_into_force") or [])
    for source in sources:
        for entry in source:
            if isinstance(entry, dict) and entry not in rows:
                rows.append(entry)
    return rows


@lru_cache(maxsize=64)
def _analyse(text: str, legacy: bool, meta_key: tuple) -> dict:
    meta = {key: dict(value) if key == "official_fields" and
            isinstance(value, tuple) else value for key, value in meta_key}
    commencement = parse_document_commencement(text, legacy=legacy)
    located = commencement.get("located") or {}
    dates = extract_promulgation_dates(
        text, meta, commencement_end=located.get("end"))
    return {"commencement": commencement, "dates": dates}


def _freeze(meta: dict) -> tuple:
    frozen = []
    for key, value in sorted(meta.items()):
        if isinstance(value, dict):
            value = tuple(sorted((k, str(v)) for k, v in value.items()))
        frozen.append((key, value))
    return tuple(frozen)


def analyse_document(document_row_or_text: dict | str, *,
                     meta: dict | None = None, legacy: bool | None = None,
                     roots: list[Path] | None = None) -> dict:
    """Commencement rules and adoption/promulgation dates of one document."""
    if isinstance(document_row_or_text, str):
        text = document_row_or_text
        row: dict = {}
    else:
        row = document_row_or_text
        text = load_document_text(row, roots)
    merged = {**document_meta(row), **(meta or {})} if row else dict(
        meta or {})
    merged["text_sha256"] = merged.get("text_sha256") or hashlib.sha256(
        text.encode("utf-8")).hexdigest()
    if legacy is None:
        legacy = is_legacy(row, merged.get("document_id"))
    result = _analyse(text, bool(legacy), _freeze(merged))
    return {"text": text, "meta": merged, "legacy": bool(legacy),
            "dip_rows": dip_rows_of(row) if row else [], **result}


def _item_query(article: str | None, item: dict | None) -> list:
    item = item or {}
    return item_path(article, absatz=item.get("absatz"),
                     nummer=item.get("nummer"),
                     buchstabe=item.get("buchstabe"),
                     doppelbuchstabe=item.get("doppelbuchstabe"),
                     dreifachbuchstabe=item.get("dreifachbuchstabe"))


def resolve_change_dates(document_row_or_text: dict | str,
                         article: str | None, item: dict | None = None, *,
                         dip_rows: list[dict] | None = None,
                         meta: dict | None = None,
                         legacy: bool | None = None,
                         roots: list[Path] | None = None,
                         target: dict | None = None) -> dict:
    """Adopted, promulgated, in-force and application date of one item.

    ``target`` (e.g. ``{"para": "7"}``, the amended norm from the command's
    ``ref``) only matters for clauses that name amended-law norms instead
    of amending-act items; without it such clauses keep the item unknown.
    """
    analysis = analyse_document(document_row_or_text, meta=meta,
                                legacy=legacy, roots=roots)
    dates = analysis["dates"]
    commencement = analysis["commencement"]
    query = _item_query(article, item)
    promulgated = dates["promulgated"]
    promulgated_day = promulgated.get("date")
    in_force = resolve_in_force(commencement, query,
                                promulgated=promulgated_day,
                                meta=analysis["meta"], target=target)
    if in_force["status"] == "computed_from_commencement_clause":
        in_force["evidence"] = in_force["evidence"] + promulgated["evidence"]
        disputed = [check for check in promulgated.get("cross_checks") or []
                    if check.get("status") == "disagree"]
        if disputed:
            # The computed day depends on a promulgation date that another
            # official source states differently: keep both, decide nothing.
            in_force = {**in_force, "date": None, "status": "unknown",
                        "reason": "promulgation_date_disputed",
                        "computed_from_printed_promulgation": in_force["date"],
                        "promulgation_disagreements": disputed}
    applies_from = resolve_applies_from(commencement, query,
                                        promulgated=promulgated_day,
                                        meta=analysis["meta"], target=target)
    rows = dip_rows if dip_rows is not None else analysis["dip_rows"]
    dip = dip_cross_check(rows, query, in_force, target=target)
    in_force["dip_cross_check"] = dip
    findings = list(dates["findings"])
    if dip["status"] in {"agree", "disagree", "dip_inconclusive_truncated",
                         "printed_unknown"}:
        findings.append({"kind": "in_force_cross_check", "field": "in_force",
                         "status": dip["status"], "source": dip["source"],
                         "source_value": dip.get("dip_date"),
                         "printed_value": in_force.get("date")})
    located = commencement.get("located") or {}
    return {
        "schema": SCHEMA,
        "document_id": analysis["meta"].get("document_id"),
        "article": article,
        "item": {key: (item or {}).get(key) for key in (
            "absatz", "nummer", "buchstabe", "doppelbuchstabe",
            "dreifachbuchstabe")},
        "query": path_label(query),
        "adopted": dates["adopted"],
        "promulgated": promulgated,
        "in_force": in_force,
        "applies_from": applies_from,
        "findings": findings,
        "commencement_article": {
            "article": commencement.get("article"),
            "heading": commencement.get("heading"),
            "start": located.get("start"), "end": located.get("end"),
            "located_by": located.get("located_by"),
            "rule_count": len(commencement.get("rules") or []),
        },
    }


def compact_dates(result: dict) -> dict:
    """The four date objects without bulky evidence, for summaries."""
    def slim(value: dict) -> dict:
        return {key: value.get(key) for key in (
            "date", "status", "basis", "reason", "retroactive")
            if key in value}
    return {key: slim(result[key]) for key in (
        "adopted", "promulgated", "in_force", "applies_from")}


__all__ = ["resolve_change_dates", "analyse_document", "load_document_text",
           "dip_rows_of", "document_meta", "compact_dates",
           "DocumentTextError", "SCHEMA"]
