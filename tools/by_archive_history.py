"""Bavarian acts from archived official copies -> act detail + versions + change records.

Input: one row of ``data/snapshots/bayern_recht_archive/<date>/acts.jsonl``
(pipeline/fetch_bayern_archive.py).  Text sources, oldest first:

* each intact archived BAYERN.RECHT ZIP export: a complete consolidation
  (``archived_capture``; the XML carries no per-article validity date, so such a
  version's start stays unresolved and its capture date is only ``known_at``);
* archived per-article pages newer than the newest ZIP: one article each, dated by
  the page's official "Text gilt ab" (``official_archived_version_date``).

Change records name the amending act only when the newer export's
``aenderungsverlauf`` lists exactly one entry for the article in the window
between the two captures; its "G v. <date>" is the adoption (Ausfertigung) date,
never an in-force date.  The shape is tools/eu_history.py's (one code path for
log/at/diff/blame).
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "pipeline"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from tools import eu_history  # noqa: E402
from common import read_jsonl  # noqa: E402

ARCHIVED = "archived_capture"
PAGE_STATUS = eu_history.AT_LATEST


def _day(ts: str) -> str:
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _title(page_title: str | None, export_title: str) -> str:
    """The page's title, but the export's spelling when they differ only in
    whitespace ("Aufsichtsbehörden(zu" vs "Aufsichtsbehörden (zu"): the export
    flattens inline markup without spaces, and a title is part of the identity."""
    if not page_title:
        return export_title
    same = re.sub(r"\s+", "", page_title) == re.sub(r"\s+", "", export_title or "")
    return export_title if same else page_title


def act_title(raw: str | None, jurabk: str) -> str:
    """The act's name from the export heading, without "Vom <date>", "in der
    Fassung der Bekanntmachung …" and the GVBl/BayRS tail (as the live feed's
    ``long_title``); the heading's inline markup is flattened without spaces."""
    text = " ".join(str(raw or "").split())
    text = re.sub(r"\(\s*([^()]*?)\s*\)", r"(\1)", text)
    text = re.sub(r"(?<=\))(?=in der Fassung|Vom|vom)", " ", text)
    text = re.sub(r"\s*(?:in der Fassung der Bekanntmachung\s+)?[Vv]om\s+\d{1,2}\.\s.*$", "",
                  text)
    text = re.sub(r"\s*in der Fassung\b.*$", "", text).strip()
    return text or jurabk


def intervals(row: dict[str, Any]) -> list[dict[str, Any]]:
    good = [c for c in row.get("zip_captures") or [] if c.get("status") == "ok"]
    out: list[dict[str, Any]] = []
    for capture in good:
        day = _day(capture["timestamp"])
        out.append({
            "edition": {"articles": capture["norms"], "label": f"ZIP {capture['timestamp']}",
                        "items": [{"url": capture.get("original_url") or capture["archive_url"],
                                   "archive_url": capture["archive_url"], "sha256": capture["sha256"]}],
                        "builddate": capture.get("builddate"), "date": capture.get("inkraft"),
                        "versions": capture.get("versions"),
                        "stand": f"BAYERN.RECHT export, builddate {capture.get('builddate')}"},
            "kind": "archived_zip", "role": ARCHIVED, "source_key": "wayback_bayern",
            "source_name": "Wayback/BAYERN.RECHT (ZIP export)",
            "valid_from": None, "valid_from_basis": None, "valid_until": None,
            "valid_until_basis": None, "status": None,
            "unresolved_basis": ("archived BAYERN.RECHT export captured "
                                 f"{day} (builddate {capture.get('builddate')}); the export "
                                 "has no per-article validity date"),
            "known_from": day,
            "known_until": day,
            "timestamp": capture["timestamp"],
        })
    titles: dict[str, str] = {}
    for norm in (good[-1]["norms"] if good else []):
        # the first occurrence carries the plain id (norm_rows_with_ids)
        titles.setdefault(norm["enbez"], norm.get("titel") or "")
    for page in sorted(row.get("pages") or [], key=lambda p: p["timestamp"]):
        if not page.get("enbez"):
            continue
        day = _day(page["timestamp"])
        out.append({
            "edition": {"articles": [{"enbez": page["enbez"],
                                      "titel": _title(page.get("titel"),
                                                      titles.get(page["enbez"], "")),
                                      "text": page["text"]}],
                        "label": f"page {page['doc']} {page['timestamp']}",
                        "items": [{"url": page["archive_url"].split("id_/", 1)[-1],
                                   "archive_url": page["archive_url"], "sha256": page.get("sha256")}]},
            "kind": "archived_page", "role": ARCHIVED, "partial": True,
            "source_key": "wayback_bayern", "source_name": "Wayback/BAYERN.RECHT (article page)",
            "valid_from": page.get("valid_from"),
            "valid_from_basis": (f"BAYERN.RECHT 'Text gilt ab' of the article page archived "
                                 f"{day}: in force at the latest from this date (the page "
                                 "dates the act's consolidated version; the wording may be "
                                 "older)" if page.get("valid_from") else None),
            "status": PAGE_STATUS if page.get("valid_from") else None,
            "valid_until": None, "valid_until_basis": None,
            "unresolved_basis": "archived article page without a 'Text gilt ab' date",
            "known_from": day, "known_until": day, "timestamp": page["timestamp"],
        })
    return sorted(out, key=lambda span: span["timestamp"])


def daily_editions(directory: Path, keys: set[str]) -> dict[str, list[dict]]:
    """Read dated official ZIP observations of acts with archived wording.

    The 2026-11-18 collector introduced source_observed_on and raw ZIP
    digests. Older directory names alone cannot establish an observation.
    """
    editions = defaultdict(list)
    if not keys:
        return editions
    for path in sorted(directory.glob("*/acts.jsonl")):
        if path.parent.name < "2026-11-18":
            continue
        acts = {a["key"]: a for a in read_jsonl(path)
                if a["key"] in keys and a.get("source_observed_on") == path.parent.name
                and a.get("source_url") and a.get("source_sha256")}
        if not acts:
            continue
        articles = defaultdict(list)
        for n in read_jsonl(path.with_name("norms.jsonl")):
            if n["key"] in acts:
                articles[n["key"]].append({k: n[k] for k in ("enbez", "titel", "text")})
        for key, act in acts.items():
            norms = articles[key]
            if not norms or len(norms) != act["norm_count"]:
                raise ValueError(f"incomplete daily Bavarian wording: {key} {path.parent.name}")
            day = act["source_observed_on"]
            editions[key].append({
                "edition": {"articles": norms, "date": act.get("inkraft"), "items": [{"url": act["source_url"],
                             "sha256": act["source_sha256"]}], "builddate": act["builddate"]},
                "kind": "official_daily", "role": "official_consolidation",
                "source_key": "bayern_recht", "source_name": "BAYERN.RECHT (ZIP export)",
                "known_from": day, "known_until": day,
                "timestamp": day.replace("-", ""), "status": None,
                "valid_from": None, "valid_from_basis": None, "valid_until": None,
                "valid_until_basis": None,
                "unresolved_basis": "Official consolidated text observed in the dated BAYERN.RECHT export."})
    return editions


def _entries_for(label: str, edition: dict[str, Any], after: str | None, until: str | None
                 ) -> list[dict[str, Any]]:
    import fetch_bayern_wayback as wayback
    match = re.match(r"(Art\.|§)\s*(\d+[a-z]?)", label or "")
    if not match:
        return []
    prefix, number = match.group(1), match.group(2)
    hits = []
    for entry in edition.get("versions") or []:
        day = entry.get("date") or ""
        if (after and day and day <= after) or (until and day and day > until):
            continue
        parsed = wayback.parse_descriptor(entry.get("description") or "")
        if any(norm.nr == number and (norm.prefix.startswith("Art") == prefix.startswith("Art"))
               for norm, _op in parsed.changes):
            hits.append(entry)
    return hits


def version_change(row, prefix, norm_id, label, change_id, previous, group, vseq,
                   version_id, _corrigenda) -> dict[str, Any]:
    before, after = previous[-1][0], group[0][0]
    newer_zip = next((g[0] for g in group if g[0]["kind"] == "archived_zip"), None)
    window_start = (before["edition"].get("builddate") or "")[:10]
    start = re.sub(r"^(\d{2})\.(\d{2})\.(\d{4}).*$", r"\3-\2-\1", window_start) or None
    entries = _entries_for(label, (newer_zip or {}).get("edition") or {}, start, None) \
        if newer_zip else []
    entry = entries[0] if len(entries) == 1 else None
    date = eu_history._date
    return {
        "record_type": "change", "act_id": row["act_id"], "change_id": change_id,
        "norm_id": norm_id, "norm_label": label, "change_type": "geändert",
        "amending_act": {
            "citation": (f"{entry['description']} ({entry['gvbl_citation']})".strip()
                         if entry else None),
            "document_id": None, "title": None,
            "candidates": [{"date": e.get("date"), "description": e.get("description"),
                            "gvbl": e.get("gvbl_citation")} for e in entries],
            "gvbl_events_after_captures": row.get("unincorporated_gvbl_events") or []},
        "dates": {
            "adopted": date(entry.get("date") if entry else None,
                            "BAYERN.RECHT Änderungsverlauf ('G v.' = Ausfertigung)",
                            reason="amending act not identified between the captures"),
            "promulgated": date(None, "", reason="GVBl issue date not in the archived export"),
            "in_force": {"date": None, "status": "unknown",
                         "reason": "between the archived captures",
                         "at_the_latest": after["valid_from"],
                         "at_the_latest_basis": after.get("valid_from_basis")},
            "applies_from": date(None, "", reason="not listed"),
        },
        "text_effect": {"before_version_id": f"{norm_id}@v{vseq - 1}#{previous[-1][2][:12]}",
                        "after_version_id": version_id, "observed_from": after["known_from"]},
        "verification": ("single_aenderungsverlauf_entry" if entry else
                         "archived_capture_difference_only"),
    }


def build(row: dict[str, Any], *, daily: list[dict] | None = None) -> dict[str, Any]:
    jurabk = row["jurabk"]
    act_id = "by_" + _slug(jurabk)
    spans = sorted(intervals(row) + (daily or []), key=lambda s: s["timestamp"])
    head_zip = next((i for i in reversed(spans) if i["kind"] == "archived_zip"), None)
    enacted = {
        "adopted": row.get("ausfertigung"), "published": None,
        "oj_ref": row.get("gvbl"), "in_force": None, "applies_from": None,
    }
    staged = {**row, "act_id": act_id, "celex": None, "dates": enacted,
              "source_url": row.get("permalink"), "fetched_at": row.get("fetched_at"),
              "permalink": row.get("permalink"), "bayrs": row.get("bayrs_nr"),
              "relations": {}}
    detail = eu_history.build(staged, juris="DE-BY", intervals=spans,
                              per_article_enactment=False, version_change=version_change)
    for version in detail["history"]["version"]:
        version["temporal"]["consolidation_basis"] = "BAYERN.RECHT act edition (angaben.versabh/inkraft)"
        version["verification"]["sources"] = sorted({a["source"] for a in version["attestations"]})
    for change in detail["history"]["change"]:
        if change.get("verification") == "first_edition_with_the_article":
            change["change_type"] = "erstmals beobachtet"
            change["dates"]["in_force"] = eu_history._date(
                None, "", reason="first observed text does not establish an insertion date")
        if change.get("change_type") == "erlassen":
            change["amending_act"].update({"citation": row.get("gvbl"), "document_id": None,
                                           "url": row.get("permalink")})
            change["dates"]["promulgated"] = eu_history._date(
                None, "", reason="GVBl issue date not in the archived export")
            change["dates"]["adopted"] = eu_history._date(
                row.get("ausfertigung"), "BAYERN.RECHT export (ausfertigungsdatum)")
            change["dates"]["in_force"] = eu_history._date(
                None, "", reason="the export dates the edition, not the original act's commencement")
    for event in row.get("unincorporated_gvbl_events") or []:
        detail["history"]["finding"].append({
            "record_type": "finding", "kind": "gvbl_event_after_newest_capture", **event})
    for capture in row.get("zip_captures") or []:
        if capture.get("status") != "ok":
            detail["history"]["finding"].append({
                "record_type": "finding", "kind": "archived_capture_rejected",
                "timestamp": capture["timestamp"], "archive_url": capture["archive_url"],
                "reason": capture.get("reason")})
    newest = max((s["known_until"] for s in spans), default=None)
    detail.update({
        "id": act_id, "juris": "DE-BY", "celex": None,
        "title": act_title(row.get("title"), jurabk),
        "bayrs": row.get("bayrs_nr"), "permalink": row.get("permalink"),
        "build": newest, "stand": (head_zip or {}).get("edition", {}).get("stand"),
        "text_source": "archived_official_copies",
        "bills": [], "gvbl_events": row.get("unincorporated_gvbl_events") or [],
        "source_url": None, "aliases": row.get("aliases") or [],
    })
    detail["versions"] = sorted(
        [{"date": e.get("date"), "text": f"{e.get('description')} ({e.get('gvbl_citation')})"[:300]}
         for e in ((head_zip or {}).get("edition", {}).get("versions") or []) if e.get("date")]
        + [{"date": e["date"], "text": f"GVBl: {e.get('title')}"[:300]}
           for e in row.get("unincorporated_gvbl_events") or [] if e.get("date")],
        key=lambda v: v["date"], reverse=True)
    return detail


def index_row(detail: dict[str, Any]) -> dict[str, Any]:
    return {"id": detail["id"], "jurabk": detail["jurabk"], "juris": "DE-BY",
            "aliases": detail.get("aliases") or [], "title": detail.get("title"),
            "norms": detail.get("norm_count"), "build": detail.get("build"),
            "last_change": max((v["date"] for v in detail.get("versions") or []), default=None),
            "next_change": None, "pending": 0, "decisions": 0,
            "text_source": "archived_official_copies"}


__all__ = ["build", "index_row", "intervals"]
