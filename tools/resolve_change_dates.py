#!/usr/bin/env python3
"""Resolve evidence-only dates for every amendment item of the BGBl stores.

For each verified amendment candidate (an amending article of a BGBl
document that amends a corpus act) the article is itemised down to
Nummer/Buchstabe/Doppelbuchstabe and each item gets

    adopted      -- "Vom …" line under the act's title
    promulgated  -- "Ausgegeben zu Bonn am …" of the act's issue/page
    in_force     -- the act's own Inkrafttreten clause (explicit or computed)
    applies_from -- only where the clause states an application date

Inputs (read-only):
    <data-root>/data/snapshots/bgbl_history_backfill/<latest>/{documents,candidates}.jsonl
    <data-root>/data/snapshots/bgbl_documents/<latest>/documents.jsonl
    <data-root>/data/snapshots/bgbl_legacy_history_backfill/<latest non-empty>/…
    <legacy archive>/checkpoint.json (+ texts/, documents/) for pre-2023 PDFs
      captured after that snapshot

Outputs (<out-dir>):
    resolutions.jsonl  one row per (document_id, article, item)
    documents.jsonl    per document: adopted/promulgated + parsed rules
    unresolved.jsonl   items whose in-force date stays unknown, with reason
    summary.json       resolution rates per era/year/status, before/after

Usage:
    python3 tools/resolve_change_dates.py --data-root ../Gesetz --out-dir /tmp/x
    python3 tools/resolve_change_dates.py --all-acts …
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "pipeline"
for path in (PIPELINE, ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from amendment_items import itemise_amendment_commands  # noqa: E402
from backfill_bgbl_history import (  # noqa: E402
    act_aliases, parse_retrospective_article_sections, scope_section_to_act,
)
from backfill_legacy_bgbl_history import (  # noqa: E402
    LegacyArchive, build_legacy_inventory, legacy_article_sections,
)
from change_dates import (  # noqa: E402
    DocumentTextError, analyse_document, resolve_change_dates,
)
from common import read_jsonl  # noqa: E402

PRIORITY_SLUGS = {
    "asylblg", "sgb_2", "sgb_12", "sgb_10", "sgg", "vwgo", "vwvfg",
    "aufenthg_2004", "asylvfg_1992", "azrg", "bgb", "zpo", "gg",
}
RESOLUTION_SCHEMA = "kerf-change-date-resolution/1"
DEFAULT_LEGACY_ARCHIVE = (Path.home() / ".local/share/sntiq-legal/"
                          "kerf-archive/data/legacy_bgbl_history")


def latest_nonempty(base: Path, name: str) -> Path | None:
    if not base.is_dir():
        return None
    for day in sorted((p for p in base.iterdir() if p.is_dir()),
                      reverse=True):
        path = day / name
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def load_documents(data_root: Path, legacy_roots: list[Path]) -> dict[str, dict]:
    snapshots = data_root / "data" / "snapshots"
    documents: dict[str, dict] = {}
    for source in ("bgbl_history_backfill", "bgbl_documents"):
        path = latest_nonempty(snapshots / source, "documents.jsonl")
        for row in read_jsonl(path) if path else ():
            row["_era"] = "recht.bund.de"
            row["_store"] = source
            documents.setdefault(str(row["document_id"]), row)
    path = latest_nonempty(snapshots / "bgbl_legacy_history_backfill",
                           "documents.jsonl")
    for row in read_jsonl(path) if path else ():
        row["_era"] = "bgbl.de"
        row["_store"] = "bgbl_legacy_history_backfill"
        documents.setdefault(str(row["document_id"]), row)
    for root in legacy_roots:
        checkpoint = root / "checkpoint.json"
        if not checkpoint.is_file():
            continue
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        for key, row in (state.get("documents") or {}).items():
            if key in documents:
                continue
            row["_era"] = "bgbl.de"
            row["_store"] = f"checkpoint:{root}"
            row["_archive_root"] = str(root)
            documents[key] = row
    return documents


def load_candidates(data_root: Path, documents: dict[str, dict]) -> list[dict]:
    snapshots = data_root / "data" / "snapshots"
    candidates: list[dict] = []
    seen: set[str] = set()
    for source in ("bgbl_history_backfill", "bgbl_legacy_history_backfill"):
        path = latest_nonempty(snapshots / source, "candidates.jsonl")
        for row in read_jsonl(path) if path else ():
            if row["id"] not in seen:
                seen.add(row["id"])
                candidates.append(row)
    # Legacy PDFs captured after the latest published snapshot: derive their
    # candidates offline with the same verified inventory builder.
    covered = {row["document_id"] for row in candidates}
    extra: dict[str, list[dict]] = collections.defaultdict(list)
    for key, row in documents.items():
        if row.get("_archive_root") and key not in covered:
            extra[row["_archive_root"]].append(row)
    for root, rows in extra.items():
        archive = LegacyArchive(Path(root), max_bytes=None)
        for row in rows:
            clean = {k: v for k, v in row.items() if not k.startswith("_")}
            try:
                derived = build_legacy_inventory([clean], archive)
            except Exception as exc:  # noqa: BLE001 - report, keep going
                print(f"skip {row['document_id']}: {exc}", file=sys.stderr)
                continue
            for candidate in derived:
                if candidate["id"] not in seen:
                    candidate["_derived_offline"] = True
                    seen.add(candidate["id"])
                    candidates.append(candidate)
    return candidates


def _text_roots(data_root: Path, legacy_roots: list[Path],
                row: dict) -> list[Path]:
    roots = [data_root, data_root / "data" / "legacy_bgbl_history"]
    if row.get("_archive_root"):
        roots.insert(0, Path(row["_archive_root"]))
    roots.extend(legacy_roots)
    return roots


def _sections(text: str, legacy: bool) -> list[dict]:
    if legacy:
        return legacy_article_sections(text)
    return parse_retrospective_article_sections(text)[0]


def _matched_act(document: dict, candidate: dict) -> dict:
    for procedure in document.get("retrospective_procedures") or []:
        for act in procedure.get("matched_acts") or []:
            if act.get("slug") == candidate.get("slug"):
                return act
    return {"slug": candidate.get("slug"),
            "long_title": candidate.get("long_title"),
            "aliases": list(act_aliases({
                "slug": candidate.get("slug"),
                "long_title": candidate.get("long_title")}))}


def candidate_section(candidate: dict, sections: list[dict]) -> dict | None:
    """The exact final article the candidate was derived from."""
    wanted = candidate.get("article_text_sha256")
    for section in sections:
        if section.get("article") != candidate.get("amending_article"):
            continue
        digest = hashlib.sha256(str(section.get("text") or "").encode(
            "utf-8")).hexdigest()
        if not wanted or digest == wanted:
            return section
    return None


def leaf_target(leaf: dict) -> dict | None:
    """The amended norm(s) a leaf command changes, for norm-named scopes."""
    ref = leaf.get("ref") or {}
    if ref.get("toc"):
        return None
    paras = [ref["para"]] if ref.get("para") else []
    for unit in leaf.get("new_units") or []:
        if unit.get("kind", "").startswith("§"):
            paras.extend(value for value in (unit.get("first"),
                                             unit.get("last")) if value)
    if not paras:
        return None
    return {"para": paras[0], "paras": paras, "absatz": ref.get("absatz"),
            "satz": ref.get("satz"), "nummer": ref.get("nummer"),
            "buchstabe": ref.get("buchstabe")}


def _slim_date(value: dict) -> dict:
    keep = ("date", "status", "basis", "reason", "clause", "retroactive",
            "computation_rule", "alternative_reading",
            "retroactive_phrase", "rule_ids", "evidence", "cross_checks",
            "dip_cross_check", "timing", "condition", "parts", "excluded",
            "qualifier", "issue")
    return {key: value[key] for key in keep if key in value}


def _year(document: dict) -> str:
    return str(document.get("publication_date") or document.get("year")
               or "")[:4]


def run(args: argparse.Namespace) -> dict:
    data_root = Path(args.data_root).resolve()
    legacy_roots = [Path(p).expanduser().resolve() for p in args.legacy_archive]
    out = Path(args.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    documents = load_documents(data_root, legacy_roots)
    candidates = load_candidates(data_root, documents)
    if not args.all_acts:
        wanted = set(args.acts or PRIORITY_SLUGS)
        candidates = [row for row in candidates if row.get("slug") in wanted]
    candidates.sort(key=lambda row: (str(row.get("publication_date")),
                                     row["document_id"],
                                     str(row.get("amending_article")),
                                     row.get("slug")))
    counts = collections.Counter()
    by_year: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter)
    reasons = collections.Counter()
    dip = collections.Counter()
    before = collections.Counter()
    after_article = collections.Counter()
    change_types = collections.Counter()
    documents_out: dict[str, dict] = {}
    doc_errors: dict[str, str] = {}
    resolution_rows = 0
    with open(out / "resolutions.jsonl", "w", encoding="utf-8") as stream, \
            open(out / "unresolved.jsonl", "w", encoding="utf-8") as missing:
        for candidate in candidates:
            document = documents.get(candidate["document_id"])
            era = "bgbl.de" if candidate.get("legacy_document_object") or (
                document or {}).get("_era") == "bgbl.de" else "recht.bund.de"
            before[(era, candidate.get("effective_date_status"))] += 1
            if document is None:
                doc_errors[candidate["document_id"]] = "document row missing"
                continue
            roots = _text_roots(data_root, legacy_roots, document)
            try:
                analysis = analyse_document(document, roots=roots)
            except DocumentTextError as exc:
                doc_errors[candidate["document_id"]] = str(exc)
                continue
            legacy = analysis["legacy"]
            if candidate["document_id"] not in documents_out:
                documents_out[candidate["document_id"]] = {
                    "document_id": candidate["document_id"], "era": era,
                    "year": _year(document), "title": document.get("title"),
                    "adopted": _slim_date(analysis["dates"]["adopted"]),
                    "promulgated": _slim_date(
                        analysis["dates"]["promulgated"]),
                    "findings": analysis["dates"]["findings"],
                    "commencement": {
                        key: analysis["commencement"].get(key) for key in (
                            "article", "heading", "located", "reason",
                            "bekanntmachung")},
                    "rules": [{key: rule.get(key) for key in (
                        "rule_id", "paragraph", "event", "kind", "scope",
                        "timing", "clause", "clause_start", "clause_end",
                        "segment_normalized", "application_condition",
                        "ambiguity")}
                        for rule in analysis["commencement"].get("rules")
                        or []],
                    "notes": analysis["commencement"].get("notes") or [],
                }
            sections = _sections(analysis["text"], legacy)
            section = candidate_section(candidate, sections)
            absatz = candidate.get("collective_subsection")
            absatz = str(absatz) if absatz not in (None, "") else None
            leaves: list[dict] = []
            item_status = "itemised"
            if section is None:
                item_status = "article_section_not_found"
            else:
                scoped = scope_section_to_act(section,
                                              _matched_act(document, candidate))
                if scoped.get("command_scope_status", "").startswith((
                        "unresolved", "ambiguous")):
                    item_status = scoped["command_scope_status"]
                else:
                    leaves = itemise_amendment_commands(scoped, absatz=absatz)
            if not leaves:
                leaves = [{"item": {"absatz": absatz, "nummer": None,
                                    "buchstabe": None,
                                    "doppelbuchstabe": None,
                                    "dreifachbuchstabe": None},
                           "item_label": "", "change_type": "unbekannt",
                           "norm_change_type": "unbekannt",
                           "target_unit": None, "ref": {},
                           "raw_sha256": None, "raw": "",
                           "ordinal": 0}]
                if item_status == "itemised":
                    item_status = "no_commands_itemised"
            article = str(candidate.get("amending_article"))
            whole = resolve_change_dates(document, article,
                                         {"absatz": absatz}, roots=roots)
            after_article[(era, whole["in_force"]["status"])] += 1
            for leaf in leaves:
                result = resolve_change_dates(
                    document, article, leaf["item"], roots=roots,
                    target=leaf_target(leaf))
                in_force = result["in_force"]
                status = in_force["status"]
                counts[(era, status)] += 1
                by_year[f"{era}:{_year(document)}"][status] += 1
                change_types[leaf.get("change_type")] += 1
                dip[(era, in_force["dip_cross_check"]["status"])] += 1
                if status == "unknown":
                    reasons[(era, in_force.get("reason"))] += 1
                row = {
                    "schema": RESOLUTION_SCHEMA,
                    "document_id": candidate["document_id"],
                    "era": era,
                    "publication_year": _year(document),
                    "candidate_id": candidate["id"],
                    "act_id": candidate.get("act_id"),
                    "slug": candidate.get("slug"),
                    "amending_article": article,
                    "collective_subsection": absatz,
                    "item": leaf["item"],
                    "item_label": leaf.get("item_label"),
                    "itemisation_status": item_status,
                    "change_type": leaf.get("change_type"),
                    "norm_change_type": leaf.get("norm_change_type"),
                    "target_unit": leaf.get("target_unit"),
                    "ref": leaf.get("ref"),
                    "raw_sha256": leaf.get("raw_sha256"),
                    "raw_excerpt": " ".join(str(leaf.get("raw") or "")
                                            .split())[:240],
                    "dates": {
                        "adopted": _slim_date(result["adopted"]),
                        "promulgated": _slim_date(result["promulgated"]),
                        "in_force": _slim_date(in_force),
                        "applies_from": _slim_date(result["applies_from"]),
                    },
                    "article_in_force": {
                        "date": whole["in_force"].get("date"),
                        "status": whole["in_force"]["status"],
                        "reason": whole["in_force"].get("reason")},
                    "commencement_article": result["commencement_article"],
                    "findings": result["findings"],
                    "previous": {
                        "effective_at": candidate.get("effective_at"),
                        "effective_date_status": candidate.get(
                            "effective_date_status")},
                }
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
                resolution_rows += 1
                if status == "unknown":
                    missing.write(json.dumps({
                        "document_id": row["document_id"], "era": era,
                        "year": row["publication_year"],
                        "slug": row["slug"], "article": article,
                        "item": leaf["item"], "reason": in_force.get("reason"),
                        "clause": " ".join(str(in_force.get("clause") or "")
                                           .split())[:400] or None,
                        "commencement_article": result[
                            "commencement_article"]["article"],
                        "dip": in_force["dip_cross_check"].get("dip_date"),
                    }, ensure_ascii=False) + "\n")
    with open(out / "documents.jsonl", "w", encoding="utf-8") as stream:
        for key in sorted(documents_out):
            stream.write(json.dumps(documents_out[key], ensure_ascii=False)
                         + "\n")
    summary = build_summary(counts, by_year, reasons, dip, before,
                            after_article, change_types, documents_out,
                            doc_errors, resolution_rows, candidates, args)
    (out / "summary.json").write_text(json.dumps(
        summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8")
    return summary


def _rate(part: int, total: int) -> float | None:
    return round(100.0 * part / total, 1) if total else None


def build_summary(counts, by_year, reasons, dip, before, after_article,
                  change_types, documents_out, doc_errors, rows, candidates,
                  args) -> dict:
    eras = sorted({era for era, _ in counts})
    items = {}
    for era in eras:
        total = sum(v for (e, _), v in counts.items() if e == era)
        known = sum(v for (e, s), v in counts.items()
                    if e == era and s != "unknown")
        items[era] = {
            "items": total, "resolved": known,
            "resolved_percent": _rate(known, total),
            "by_status": {s: v for (e, s), v in sorted(counts.items())
                          if e == era},
        }
    total = sum(counts.values())
    known = sum(v for (_e, s), v in counts.items() if s != "unknown")
    document_status = collections.Counter()
    document_findings = collections.Counter()
    for row in documents_out.values():
        for field in ("adopted", "promulgated"):
            document_status[(row["era"], field, row[field]["status"])] += 1
        for finding in row["findings"]:
            document_findings[(row["era"], finding.get("field"),
                               finding.get("source"),
                               finding.get("status"))] += 1
    return {
        "schema": "kerf-change-date-summary/1",
        "generated_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
        "scope": "all_acts" if args.all_acts else sorted(
            args.acts or PRIORITY_SLUGS),
        "candidates": len(candidates),
        "resolution_rows": rows,
        "items": {"total": total, "resolved": known,
                  "resolved_percent": _rate(known, total), "by_era": items},
        "items_by_year": {key: dict(sorted(value.items()))
                          for key, value in sorted(by_year.items())},
        "unresolved_reasons": {f"{era}:{reason}": value for (era, reason),
                               value in sorted(reasons.items(), key=str)},
        "dip_cross_check": {f"{era}:{status}": value for (era, status),
                            value in sorted(dip.items())},
        "change_types": dict(sorted(change_types.items(), key=str)),
        "article_level": {
            "before": {f"{era}:{status}": value for (era, status), value
                       in sorted(before.items(), key=str)},
            "after": {f"{era}:{status}": value for (era, status), value
                      in sorted(after_article.items(), key=str)},
        },
        "documents": {
            "count": len(documents_out),
            "status": {":".join(key): value for key, value in
                       sorted(document_status.items())},
            "findings": {":".join(str(k) for k in key): value for key, value
                         in sorted(document_findings.items(), key=str)},
            "errors": doc_errors,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data-root", default=os.environ.get(
        "KERF_DATA_ROOT") or str(ROOT),
        help="checkout holding data/snapshots (read-only)")
    parser.add_argument("--legacy-archive", action="append",
                        default=None,
                        help="legacy BGBl archive root(s) with checkpoint.json")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--all-acts", action="store_true",
                        help="every corpus act, not only the priority acts")
    parser.add_argument("--act", dest="acts", action="append",
                        help="restrict to GII slug(s)")
    args = parser.parse_args(argv)
    if args.legacy_archive is None:
        args.legacy_archive = [str(Path(args.data_root) / "data" /
                                   "legacy_bgbl_history"),
                               str(DEFAULT_LEGACY_ARCHIVE)]
    summary = run(args)
    print(json.dumps({key: summary[key] for key in (
        "candidates", "resolution_rows", "items", "unresolved_reasons")},
        ensure_ascii=False, indent=2))
    print(f"-> {Path(args.out_dir).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
