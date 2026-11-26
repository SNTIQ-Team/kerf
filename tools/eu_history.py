"""EU/treaty acts: text editions -> Kerf act detail + norm versions + change records.

Input: one row of ``data/snapshots/eu_texts/<date>/acts.jsonl`` (pipeline/fetch_eu_texts.py)
or of the EMRK snapshot.  Output: the act detail written to ``web/data/acts/<id>.json``
with a ``history`` block in the record shape of the Bavarian archive worker bundle
(``version``/``change``/``finding`` records, see tools/build_law_history.py
``build_records_act``), so log/at/diff/blame answer for EU articles exactly as for
German norms.

Dates (never invented, each with its basis):

* ``adopted`` = CELLAR ``work_date_document``; ``promulgated`` = Official Journal date;
  entry into force and general application use the date roles annotated by CELLAR.
* A consolidated version is valid from its EUR-Lex consolidation date (the first one
  from the entry into force) until the next consolidation or the end of validity.
* The Official Journal wording of an article that the first consolidation corrects
  (corrigenda correct ex tunc) keeps no validity interval of its own: it is the
  promulgated wording (``official_promulgation``), superseded by the corrected text.
* Treaty articles: the Lisbon consolidation (12008E/12008M) is valid from the Treaty of
  Lisbon's entry into force; a later republication whose wording differs keeps an
  unresolved start (only its publication is evidenced).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.history_identity import norm_identity  # noqa: E402
from api.norm_identity import act_prefix, designator_unit, norm_rows_with_ids  # noqa: E402

EURLEX = "https://eur-lex.europa.eu/legal-content/DE/TXT/?uri=CELEX:{celex}"
ROLE = {"oj": "official_promulgation", "consolidated": "official_consolidation",
        "treaty_consolidated": "official_consolidation",
        "coe_official": "official_consolidation"}
VALID_STATUS = "official_eu_consolidation"
# BAYERN.RECHT's "Text gilt ab" dates the act's consolidated version shown on an
# archived page: the wording is in force at the latest from then (it may be older)
AT_LATEST = "official_archived_in_force_at_latest"


def _day_after(value: str) -> str:
    return (date.fromisoformat(value) + timedelta(days=1)).isoformat()


def _date(value: str | None, basis: str, *, reason: str | None = None) -> dict[str, Any]:
    if value:
        return {"date": value, "status": "official", "basis": basis}
    return {"date": None, "status": "unknown", "reason": reason or "not in the source metadata"}


def _intervals(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Per edition: role, validity interval and observation interval."""
    dates = row.get("dates") or {}
    in_force = dates.get("in_force")
    end = dates.get("end_of_validity")
    end_exclusive = _day_after(end) if end else None
    editions = row.get("editions") or []
    cons = [e for e in editions if e.get("kind") == "consolidated"]
    fetched = str(row.get("fetched_at") or "")[:10] or None
    out = []
    for index, edition in enumerate(editions):
        kind = edition.get("kind")
        following = editions[index + 1] if index + 1 < len(editions) else None
        known_from = edition.get("published") or edition.get("date")
        boundary = (following.get("published") or following.get("date")) if following else end
        if fetched and known_from and known_from > fetched:
            known_from = fetched
        known_until = fetched
        if fetched and boundary and (not known_from or boundary >= known_from):
            known_until = min(boundary, fetched)
        valid_from = valid_until = None
        basis = until_basis = None
        if kind == "oj":
            if not cons:
                valid_from, basis = in_force, "CELLAR entry into force (Official Journal text)"
                valid_until = end_exclusive
                until_basis = "CELLAR end of validity" if end else None
            elif cons[0]["date"] > (dates.get("published") or "") and in_force:
                # an amendment, not a corrigendum, made the first consolidation
                valid_from, basis = in_force, "CELLAR entry into force"
                valid_until, until_basis = cons[0]["date"], "date of the first consolidated version"
        elif kind == "consolidated":
            position = cons.index(edition)
            start = edition["date"]
            if position == 0 and in_force and in_force > start:
                start = in_force
                basis = (f"CELLAR entry into force; EUR-Lex consolidated version "
                         f"{edition['celex']} (incorporates the corrigenda listed in its header)")
            else:
                basis = f"EUR-Lex consolidated version {edition['celex']} (consolidation date)"
            valid_from = start
            nxt = cons[position + 1] if position + 1 < len(cons) else None
            if nxt:
                valid_until, until_basis = nxt["date"], f"next consolidated version {nxt['celex']}"
            elif end_exclusive:
                valid_until, until_basis = end_exclusive, "CELLAR end of validity (day after)"
        else:  # treaty/charter/EMRK editions carry their own evidenced start
            valid_from, basis = edition.get("valid_from"), edition.get("valid_from_basis")
            if valid_from and following and following.get("valid_from"):
                valid_until = following["valid_from"]
                until_basis = "start of the following edition"
        out.append({"edition": edition, "kind": kind, "role": ROLE.get(kind, "official_consolidation"),
                    "valid_from": valid_from, "valid_from_basis": basis,
                    "valid_until": valid_until, "valid_until_basis": until_basis,
                    "known_from": known_from, "known_until": known_until})
    return out


def _attestation(interval: dict[str, Any]) -> dict[str, Any]:
    edition = interval["edition"]
    items = edition.get("items") or []
    return {
        "source": interval.get("source_name") or "CELLAR",
        "role": interval["role"], "known_from": interval["known_from"],
        "known_until": interval["known_until"], "count": 1,
        "kind": "norm_html" if interval.get("partial") else "full",
        "samples": [{k: v for k, v in {
            "url": (items[0].get("url") if items else None),
            "archive_url": (items[0].get("archive_url") if items else None),
            "sha256": (items[0].get("sha256") if items else None),
            "celex": edition.get("celex"), "format": edition.get("format"),
            "known_at": interval["known_from"],
            "stand": edition.get("oj_ref") or edition.get("celex")}.items() if v}],
    }


def build(row: dict[str, Any], *, juris: str = "EU",
          intervals: list[dict[str, Any]] | None = None,
          per_article_enactment: bool = True,
          version_change=None) -> dict[str, Any]:
    """The act detail (with ``history``) for one fetched act.

    ``intervals`` (default: the EU editions of ``row``) are the text sources in
    chronological order; an edition may hold every article or a single one
    (an archived per-article page).  ``version_change`` builds the change
    record between two versions (default: EU corrigenda/consolidations).
    """
    act_id = row["act_id"]
    celex = row.get("celex")
    dates = row.get("dates") or {}
    findings: list[dict[str, Any]] = []
    end = dates.get("end_of_validity")
    if juris == "EU" and end:
        # Older snapshots may name an expiry limited to one article.
        # Keep requiring evidence that the act as a whole was repealed.
        later_editions = [edition["celex"] for edition in row.get("editions") or []
                          if edition.get("kind") == "consolidated" and (edition.get("date") or "") > end]
        if later_editions or not (row.get("relations") or {}).get("repealed_by"):
            findings.append({"record_type": "finding", "kind": "act_end_of_validity_unresolved",
                             "reported_end_of_validity": end, "later_editions": later_editions,
                             "reason": ("A consolidated edition postdates the reported end."
                                        if later_editions else
                                        "The extracted date has no corroborating whole-act repeal relation.")})
            dates = {**dates, "end_of_validity": None}
            row = {**row, "dates": dates}
    act = {"id": act_id, "juris": juris, "jurabk": row["jurabk"], "celex": celex}
    prefix = act_prefix(act)
    intervals = _intervals(row) if intervals is None else intervals
    version_change = version_change or _version_change
    history_versions: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    per_norm: dict[str, list[tuple[int, dict[str, Any], dict[str, Any]]]] = {}
    order: list[str] = []
    for position, interval in enumerate(intervals):
        # the shared identity rule: a repeated designator gets ~2, ~3 in
        # document order (api.norm_identity.norm_rows_with_ids)
        for norm_id, article in norm_rows_with_ids(
                act, interval["edition"].get("articles") or []):
            if norm_id not in per_norm:
                per_norm[norm_id] = []
                order.append(norm_id)
            per_norm[norm_id].append((position, interval, article))
    full_intervals = [i for i in intervals if not i.get("partial")]
    last_interval = full_intervals[-1] if full_intervals else None
    corrigenda = sorted({(c.get("celex"), c.get("date"), c.get("oj_ref"))
                         for i in intervals for c in i["edition"].get("corrigenda") or []
                         if c.get("celex")}, key=lambda c: (c[1] or "", c[0]))
    base_change = _base_change(row, prefix)
    for norm_id in order:
        rows = per_norm[norm_id]
        groups: list[list[tuple[dict[str, Any], dict[str, Any], str, int]]] = []
        for position, interval, article in rows:
            identity = norm_identity(article.get("enbez"), article.get("titel"), article.get("text"))
            if groups and groups[-1][-1][2] == identity:
                groups[-1].append((interval, article, identity, position))
            else:
                groups.append([(interval, article, identity, position)])
        label = rows[-1][2].get("enbez")
        for vseq, group in enumerate(groups):
            carriers = [g for g in group if g[0]["valid_from"]]
            first = carriers[0][0] if carriers else None
            last = group[-1][0]
            article = group[-1][1]
            following = groups[vseq + 1] if vseq + 1 < len(groups) else None
            # an "in force at the latest from" start bounds nothing before it
            next_start = next((g[0]["valid_from"] for g in following or []
                               if g[0]["valid_from"] and g[0].get("status") != AT_LATEST),
                              None)
            own_until = carriers[-1][0]["valid_until"] if carriers else None
            temporal = {
                "valid_from": first["valid_from"] if first else None,
                "valid_from_status": ((first.get("status") or VALID_STATUS) if first
                                      else "unresolved"),
                "valid_from_basis": first["valid_from_basis"] if first else (
                    "Official Journal wording corrected ex tunc by the corrigenda "
                    "incorporated in the consolidated version"
                    if group[0][0]["kind"] == "oj" else
                    group[0][0].get("unresolved_basis") or
                    "only the publication of this wording is evidenced"),
                "attribution": None,
                "valid_until_exclusive": own_until or next_start,
                "valid_until_status": ((first or {}).get("status") or VALID_STATUS)
                if (own_until or next_start) else "unresolved",
                "valid_until_basis": (carriers[-1][0]["valid_until_basis"] if own_until
                                      else "start of the following version" if next_start
                                      else None),
                "observed_from": group[0][0]["known_from"],
                "observed_until": last["known_until"],
                "consolidation_dates": [g[0]["edition"].get("date") for g in group],
                "consolidation_basis": "CELLAR edition dates",
            }
            version_id = f"{norm_id}@v{vseq}#{group[-1][2][:12]}"
            change_id = None
            if vseq == 0 and group[0][3] > 0:
                change_id = f"{act_id}:{norm_id.rsplit('/', 2)[-2]}-{norm_id.rsplit('/', 1)[-1]}:inserted"
                inserted = group[0][0]
                changes.append({
                    "record_type": "change", "act_id": act_id, "change_id": change_id,
                    "norm_id": norm_id, "norm_label": label, "change_type": "eingefügt",
                    "amending_act": {"citation": inserted["edition"].get("celex"),
                                     "document_id": None, "title": None},
                    "dates": {"adopted": _date(None, "", reason="amending act not identified"),
                              "promulgated": _date(None, "", reason="amending act not identified"),
                              "in_force": _date(inserted["valid_from"],
                                                inserted["valid_from_basis"] or "",
                                                reason="the new article's start is not evidenced"),
                              "applies_from": _date(None, "", reason="not listed")},
                    "text_effect": {"before_version_id": None, "after_version_id": version_id,
                                    "observed_from": temporal["observed_from"]},
                    "verification": "first_edition_with_the_article"})
            elif vseq == 0 and base_change and per_article_enactment:
                change_id = f"{act_id}:{norm_id.rsplit('/', 2)[-2]}-{norm_id.rsplit('/', 1)[-1]}:enacted"
                changes.append({**base_change, "change_id": change_id, "norm_id": norm_id,
                                "norm_label": label, "change_type": "erlassen",
                                "text_effect": {"before_version_id": None,
                                                "after_version_id": version_id,
                                                "observed_from": temporal["observed_from"]}})
            elif vseq > 0:
                previous = groups[vseq - 1]
                change_id = f"{act_id}:{norm_id.rsplit('/', 2)[-2]}-{norm_id.rsplit('/', 1)[-1]}:v{vseq}"
                changes.append(version_change(row, prefix, norm_id, label, change_id, previous,
                                              group, vseq, version_id, corrigenda))
            history_versions.append({
                "record_type": "version", "act_id": act_id, "canonical_id": norm_id,
                "version_id": version_id, "sequence": vseq,
                "pinpoint": {"label": article.get("enbez"), "title": article.get("titel")},
                "text": article.get("text") or "",
                "content_hash": {"identity": group[-1][2],
                                 "text_sha256": hashlib.sha256(
                                     (article.get("text") or "").encode("utf-8")).hexdigest()},
                "temporal": temporal,
                "attestations": [_attestation(g[0]) for g in group],
                "change": {"change_id": change_id} if change_id else {},
                "source_role": ("official_consolidation" if any(
                    g[0]["role"] == "official_consolidation" for g in group)
                    else group[-1][0]["role"]),
                "completeness": {"status": "complete", "reason": None},
                "verification": {"cross_source": "single_source",
                                 "sources": sorted({g[0]["edition"].get("celex") or "" for g in group})},
            })
        seen_full = [r[1] for r in rows if not r[1].get("partial")]
        if last_interval is not None and seen_full and seen_full[-1] is not last_interval:
            findings.append({"record_type": "finding", "kind": "article_absent_from_latest_edition",
                             "norm_id": norm_id,
                             "latest_edition": last_interval["edition"].get("celex")
                             or last_interval.get("label")})
    changes.extend(_act_level_changes(row, prefix, corrigenda))
    for interval in intervals:
        for warning in interval["edition"].get("warnings") or []:
            findings.append({"record_type": "finding", "kind": "edition_warning", "detail": warning})
        if interval["edition"].get("annexes_skipped"):
            findings.append({"record_type": "finding", "kind": "annexes_not_included",
                             "edition": interval["edition"].get("celex"),
                             "count": interval["edition"]["annexes_skipped"]})
    if not any(i["kind"] == "consolidated" for i in intervals):
        for corrigendum in (row.get("relations") or {}).get("corrected_by") or []:
            if corrigendum.get("german"):
                findings.append({"record_type": "finding",
                                 "kind": "german_corrigendum_not_incorporated",
                                 "celex": corrigendum["celex"],
                                 "published": corrigendum.get("published"),
                                 "oj_ref": corrigendum.get("oj_ref"),
                                 "note": "no German consolidated version exists; the text is "
                                         "the Official Journal wording without this corrigendum"})
    for warning in row.get("warnings") or []:
        findings.append({"record_type": "finding", "kind": "fetch_warning", "detail": warning})
    full = [i for i in intervals if not i.get("partial")]
    latest_full = (full[-1]["edition"].get("articles") if full else []) or []
    newest = {norm: per_norm[norm][-1][2] for norm in order}
    head = []
    listed = set()
    for norm_id, article in norm_rows_with_ids(act, latest_full):
        head.append(newest.get(norm_id, article))
        listed.add(norm_id)
    head.extend(newest[n] for n in order if n not in listed
                and per_norm[n][-1][1].get("partial"))
    states = [{"source": i.get("source_key") or "cellar", "role": i["role"],
               **({"complete": False, "notes": {"capture_kind": "norm_html"}}
                  if i.get("partial") else {}),
               "known_from": i["known_from"], "known_until": i["known_until"],
               "url": ((i["edition"].get("items") or [{}])[0].get("url")),
               **({"archive_url": i["edition"]["items"][0]["archive_url"]}
                  if (i["edition"].get("items") or [{}])[0].get("archive_url") else {}),
               "sha256": ((i["edition"].get("items") or [{}])[0].get("sha256")),
               "stand": i["edition"].get("celex"), "valid_from": i["valid_from"],
               "valid_from_status": ((i.get("status") or VALID_STATUS) if i["valid_from"]
                                     else "unresolved"),
               "valid_from_basis": i["valid_from_basis"], "valid_until": i["valid_until"]}
              for i in intervals]
    display_versions = sorted(({"date": c["dates"]["in_force"]["date"] or
                                (c["dates"].get("promulgated") or {}).get("date"),
                                "text": _display(c)} for c in changes
                               if c.get("norm_id") is None and (
                                   c["dates"]["in_force"]["date"] or
                                   (c["dates"].get("promulgated") or {}).get("date"))),
                              key=lambda v: v["date"], reverse=True)
    source_url = row.get("source_url") or (EURLEX.format(celex=celex) if celex else None)
    extra = {k: row[k] for k in ("permalink", "bayrs", "unincorporated_gvbl_events")
             if row.get(k)}
    return {
        **extra,
        "id": act_id, "jurabk": row["jurabk"], "juris": juris, "celex": celex,
        "aliases": row.get("aliases") or [], "title": row.get("title") or row["jurabk"],
        "family": row.get("family"), "source_url": source_url, "eli": dates.get("eli"),
        "build": str(row.get("fetched_at") or "")[:10], "stand": (
            intervals[-1]["edition"].get("celex") if intervals else None),
        "dates": dates, "relations": row.get("relations") or {}, "extends": row.get("extends"),
        "source": row.get("source"), "norm_count": len(head),
        "norms": [{"enbez": a.get("enbez"), "titel": a.get("titel") or "",
                   "text": a.get("text") or "", "glied": ""} for a in head],
        "patches": [], "upcoming": [], "versions": display_versions,
        "history": {"states": states, "version": history_versions, "change": changes,
                    "finding": findings},
    }


def _display(change: dict[str, Any]) -> str:
    act = change.get("amending_act") or {}
    return f"{change.get('change_type')}: {act.get('citation') or act.get('title') or ''}"[:300]


def _base_change(row: dict[str, Any], prefix: str) -> dict[str, Any] | None:
    dates = row.get("dates") or {}
    if not (dates.get("adopted") or dates.get("published") or dates.get("in_force")):
        return None
    source = "CELLAR work metadata"
    clause = dates.get("application_clause")
    application_basis = (f"Official text, {clause['article']} Abs. {clause['paragraph']}"
                         if clause else f"{source} (general application date)")
    return {
        "record_type": "change", "act_id": row["act_id"],
        "amending_act": {"citation": dates.get("oj_ref") or row.get("celex"),
                         "document_id": row.get("celex"), "title": row.get("title"),
                         "gazette": {"reference": dates.get("oj_ref")},
                         "url": row.get("source_url") or EURLEX.format(celex=row.get("celex"))},
        "dates": {
            "adopted": _date(dates.get("adopted") or dates.get("signed"), f"{source} (work date)"),
            "promulgated": _date(dates.get("published"), f"{source} (Official Journal)"),
            "in_force": _date(dates.get("in_force"), f"{source} (entry into force)"),
            "applies_from": _date(dates.get("applies_from"),
                                  application_basis, reason=("application clause missing or inconsistent across editions"
                                                             if clause else "no separate date of application listed")),
        },
        "verification": "official_metadata_and_text" if clause and clause.get("date") else "official_metadata",
    }


def _version_change(row, prefix, norm_id, label, change_id, previous, group, vseq,
                    version_id, corrigenda) -> dict[str, Any]:
    before, after = previous[-1][0], group[0][0]
    dates = row.get("dates") or {}
    relations = row.get("relations") or {}
    record: dict[str, Any] = {
        "record_type": "change", "act_id": row["act_id"], "change_id": change_id,
        "norm_id": norm_id, "norm_label": label,
        "text_effect": {"before_version_id": f"{norm_id}@v{vseq - 1}#{previous[-1][2][:12]}",
                        "after_version_id": version_id,
                        "observed_from": after["known_from"]},
    }
    if before["kind"] == "oj" and after["kind"] == "consolidated" and not before["valid_from"]:
        cited = [c for c in corrigenda]
        record.update({
            "change_type": "berichtigt",
            "amending_act": {
                "citation": "; ".join(f"{c[2] or c[0]} ({c[0]})" for c in cited) or "Berichtigung",
                "document_id": cited[0][0] if len(cited) == 1 else None,
                "candidates": [{"celex": c[0], "published": c[1], "oj_ref": c[2]} for c in cited],
                "title": "Berichtigung (corrigendum)"},
            "dates": {
                "adopted": _date(None, "", reason="a corrigendum has no adoption date"),
                "promulgated": _date(cited[0][1] if len(cited) == 1 else None,
                                     "Official Journal date of the corrigendum",
                                     reason=f"{len(cited)} corrigenda; the consolidation does not "
                                            "say which one changed this article"),
                "in_force": _date(dates.get("in_force"),
                                  "corrigenda correct the text from its entry into force (ex tunc)"),
                "applies_from": _date(None, "", reason="not applicable"),
            },
            "verification": "text_difference_oj_vs_consolidation",
        })
        return record
    amending = [a for a in relations.get("amended_by") or []
                if after["valid_from"] and after["valid_from"] in {a.get("in_force"),
                                                                   a.get("applies_from")}]
    source = amending[0] if len(amending) == 1 else None
    record.update({
        "change_type": "geändert",
        "amending_act": {"citation": (source or {}).get("oj_ref") or (source or {}).get("celex")
                         or after["edition"].get("celex"),
                         "document_id": (source or {}).get("celex"),
                         "title": (source or {}).get("title"),
                         "candidates": [{"celex": a.get("celex"), "in_force": a.get("in_force")}
                                        for a in relations.get("amended_by") or []]},
        "dates": {
            "adopted": _date((source or {}).get("adopted"), "CELLAR work date of the amending act",
                             reason="amending act not identified"),
            "promulgated": _date((source or {}).get("published"),
                                 "Official Journal date of the amending act",
                                 reason="amending act not identified"),
            "in_force": _date(after["valid_from"], after["valid_from_basis"] or "",
                              reason="the new wording's start is not evidenced"),
            "applies_from": _date(None, "", reason="no separate date of application listed"),
        },
        "verification": ("amending_act_matched_by_date" if source else
                         "consolidation_difference_only"),
    })
    return record


def _act_level_changes(row, prefix, corrigenda) -> list[dict[str, Any]]:
    act_id = row["act_id"]
    out = []
    base = _base_change(row, prefix)
    if base:
        out.append({**base, "change_id": f"{act_id}:act:enacted", "norm_id": None,
                    "norm_label": None, "change_type": "erlassen", "text_effect": None})
    for celex, published, oj_ref in corrigenda:
        out.append({"record_type": "change", "act_id": act_id, "change_id": f"{act_id}:act:{celex}",
                    "norm_id": None, "norm_label": None, "change_type": "berichtigt",
                    "amending_act": {"citation": oj_ref or celex, "document_id": celex,
                                     "title": "Berichtigung (corrigendum)"},
                    "dates": {"adopted": _date(None, "", reason="a corrigendum has no adoption date"),
                              "promulgated": _date(published, "Official Journal date of the corrigendum"),
                              "in_force": _date((row.get("dates") or {}).get("in_force"),
                                                "corrigenda correct ex tunc"),
                              "applies_from": _date(None, "", reason="not applicable")},
                    "text_effect": None, "verification": "consolidation_header"})
    relations = row.get("relations") or {}
    end = (row.get("dates") or {}).get("end_of_validity")
    for key, kind in (("repealed_by", "aufgehoben"), ("implicitly_repealed_by", "implizite Aufhebungsbeziehung")):
        for rel in relations.get(key) or []:
            out.append({"record_type": "change", "act_id": act_id,
                        "change_id": f"{act_id}:act:{key}:{rel['celex']}", "norm_id": None,
                        "norm_label": None, "change_type": kind,
                        "amending_act": {"citation": rel.get("oj_ref") or rel["celex"],
                                         "document_id": rel["celex"], "title": rel.get("title"),
                                         "relation": key},
                        "dates": {"adopted": _date(rel.get("adopted"), "CELLAR work date"),
                                  "promulgated": _date(rel.get("published"), "Official Journal date"),
                                  "in_force": _date(_day_after(end) if end and key == "repealed_by" else None,
                                                    "day after the CELLAR end of validity of the repealed act",
                                                    reason="the repeal date is not in the source metadata"),
                                  "applies_from": _date(rel.get("applies_from"),
                                                        "CELLAR application date of the repealing act",
                                                        reason="no separate date of application listed")},
                        "text_effect": None, "verification": "official_metadata"})
    for rel in relations.get("extended_by") or []:
        out.append({"record_type": "change", "act_id": act_id,
                    "change_id": f"{act_id}:act:extended:{rel['celex']}", "norm_id": None,
                    "norm_label": None, "change_type": "verlängert",
                    "amending_act": {"citation": rel.get("oj_ref") or rel["celex"],
                                     "document_id": rel["celex"], "title": rel.get("title")},
                    "dates": {"adopted": _date(rel.get("adopted"), "CELLAR work date"),
                              "promulgated": _date(rel.get("published"), "Official Journal date"),
                              "in_force": _date(rel.get("in_force"), "CELLAR entry into force"),
                              "applies_from": _date(rel.get("applies_from"), "CELLAR application date",
                                                    reason="no separate date of application listed")},
                    "text_effect": None, "verification": "official_metadata",
                    "note": "extends the duration of temporary protection; the extended act's "
                            "text is unchanged"})
    return out


def index_row(detail: dict[str, Any]) -> dict[str, Any]:
    """The wiki.json row of an EU/treaty act."""
    last = max((v["date"] for v in detail.get("versions") or [] if v.get("date")), default=None)
    return {"id": detail["id"], "jurabk": detail["jurabk"], "juris": detail["juris"],
            "celex": detail.get("celex"), "aliases": detail.get("aliases") or [],
            "source_url": detail.get("source_url"), "title": detail.get("title"),
            "norms": detail.get("norm_count"), "build": detail.get("build"),
            "last_change": last, "next_change": None, "pending": 0, "decisions": 0}


def link_extensions(rows: list[dict[str, Any]]) -> None:
    """Record each extension decision on the act it extends (``relations.extended_by``)."""
    by_celex = {r.get("celex"): r for r in rows}
    for row in rows:
        target = by_celex.get(row.get("extends"))
        if target is None:
            continue
        rel = target.setdefault("relations", {}).setdefault("extended_by", [])
        if all(r.get("celex") != row["celex"] for r in rel):
            dates = row.get("dates") or {}
            rel.append({"celex": row["celex"], "title": row.get("title"),
                        "adopted": dates.get("adopted"), "published": dates.get("published"),
                        "oj_ref": dates.get("oj_ref"), "in_force": dates.get("in_force"),
                        "applies_from": dates.get("applies_from")})
            rel.sort(key=lambda r: r.get("published") or "")


def load_latest(snapshots: Path, family: str = "eu_texts") -> list[dict[str, Any]]:
    base = snapshots / family
    if not base.is_dir():
        return []
    dates = sorted(p for p in base.iterdir() if (p / "acts.jsonl").is_file())
    if not dates:
        return []
    with (dates[-1] / "acts.jsonl").open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


__all__ = ["build", "index_row", "link_extensions", "load_latest"]
