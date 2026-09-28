"""Parse every stored historical raw object into normalized per-norm texts.

For each raw object ``R/raw/<source>/sha256/<xx>/<sha>.<ext>.gz`` referenced
by an ``ok``/``skipped`` row of ``R/ledger/<source>.jsonl`` this writes
``R/parsed/<source>/<act_id>/<sha>.json.gz`` (schema
``lexgraph-history-parsed/1``).  The run is idempotent: an existing parse is
kept when its parser version and capture list are unchanged (``--force``
re-parses everything).

Sources and parsers:

``wayback_gii``  GII full-act HTML -> ``gii_html_parser.parse_gii_html``
                 GII ``xml.zip``   -> ``gii_html_parser.parse_gii_xml_zip``
                 (``fetch_gii.parse_law``, byte-identical to daily states)
``neuris``       LegalDocML.de zip -> ``neuris_ldml_parser.parse_neuris_zip``

Capture dates are observation dates (``known_at``), never legal dates.

Usage::

    python3 tools/parse_history_captures.py [--sources wayback_gii,neuris]
        [--acts fed_sgb_2] [--force]
"""
from __future__ import annotations

import argparse
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT))

import gii_html_parser  # noqa: E402
import neuris_ldml_parser  # noqa: E402
from history_archive import PARSED_SCHEMA, HistoryArchive  # noqa: E402

from api.history_identity import norm_identity  # noqa: E402

SOURCES = ("wayback_gii", "neuris")


def parser_version(source: str, ext: str) -> str:
    if source == "neuris":
        return neuris_ldml_parser.PARSER_VERSION
    if ext == "zip":
        return gii_html_parser.XML_PARSER_VERSION
    return gii_html_parser.PARSER_VERSION


def capture_groups(archive: HistoryArchive, source: str
                   ) -> dict[tuple[str, str], list[dict]]:
    """``{(act_id, sha256): [ledger rows]}`` for ok/skipped captures."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in archive.read_ledger(source):
        if row.get("status") not in {"ok", "skipped"} or not row.get("sha256"):
            continue
        groups[(row["act_id"], row["sha256"])].append(row)
    return groups


def capture_list(rows: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for row in sorted(rows, key=lambda r: (r.get("captured_at") or "",
                                           r.get("archive_url") or "")):
        key = (row.get("captured_at"), row.get("archive_url"),
               row.get("original_url"))
        if key in seen:
            continue
        seen.add(key)
        item = {"captured_at": row.get("captured_at"),
                "original_url": row.get("original_url"),
                "archive_url": row.get("archive_url")}
        for extra in ("capture_timestamp", "kind", "expression_eli",
                      "point_in_time", "manifestation_date"):
            if row.get(extra) is not None:
                item[extra] = row[extra]
        out.append(item)
    return out


def slug_for(rows: list[dict]) -> str:
    for row in rows:
        url = row.get("original_url") or ""
        parts = url.split("/")
        if len(parts) > 3 and parts[3]:
            return parts[3]
    return ""


def parse_object(source: str, ext: str, data: bytes, rows: list[dict]) -> dict:
    if source == "neuris":
        return neuris_ldml_parser.parse_neuris_zip(data)
    if ext == "zip":
        return gii_html_parser.parse_gii_xml_zip(data, slug_for(rows))
    parsed = gii_html_parser.parse_gii_html(data)
    if any(row.get("kind") == "norm_html" for row in rows):
        # a single-norm page is evidence for that norm, never a whole act
        parsed["complete"] = False
        if not str(parsed.get("layout")).endswith("-page"):
            parsed["layout"] = f"{parsed.get('layout')}-norm-page"
        parsed["warnings"] = [w for w in parsed["warnings"]
                              if w != "no Stand block found"]
        parsed["warnings"].append("single-norm page: partial act by design")
    return parsed


def build_record(source: str, act_id: str, sha: str, ext: str, parsed: dict,
                 rows: list[dict]) -> dict:
    captures = capture_list(rows)
    first = captures[0] if captures else {}
    norms = []
    for norm in parsed["norms"]:
        item = dict(norm)
        item["identity"] = norm_identity(norm["enbez"], norm["titel"],
                                         norm["text"])
        norms.append(item)
    record = {
        "schema": PARSED_SCHEMA,
        "parser": parsed["parser"],
        "source": source,
        "act_id": act_id,
        "sha256": sha,
        "media_ext": ext,
        "layout": parsed.get("layout"),
        "captured_at": first.get("captured_at"),
        "original_url": first.get("original_url"),
        "archive_url": first.get("archive_url"),
        "first_captured_at": first.get("captured_at"),
        "last_captured_at": captures[-1].get("captured_at") if captures else None,
        "captures": captures,
        "jurabk": parsed.get("jurabk") or "",
        "title": parsed.get("title") or "",
        "builddate": parsed.get("builddate"),
        "doknr": parsed.get("doknr") or None,
        "stand": parsed.get("stand") or {"raw": "", "entries": []},
        "complete": bool(parsed.get("complete")),
        "norm_count": len(norms),
        "norms": norms,
        "warnings": parsed.get("warnings") or [],
        "date_basis": "capture_observation_not_legal_date",
    }
    for extra in ("source_identifiers", "page_title", "truncated_after"):
        if parsed.get(extra):
            record[extra] = parsed[extra]
    return record


def run(archive: HistoryArchive, sources: list[str], acts: set[str] | None,
        force: bool = False, log=print) -> Counter:
    stats: Counter = Counter()
    for source in sources:
        groups = capture_groups(archive, source)
        for (act_id, sha), rows in sorted(groups.items()):
            if acts and act_id not in acts:
                continue
            path = archive.find_raw(source, sha)
            if path is None:
                stats["missing_raw"] += 1
                continue
            ext = path.name[len(sha) + 1:-3]
            version = parser_version(source, ext)
            captures = capture_list(rows)
            if not force:
                existing = archive.read_parsed(source, act_id, sha)
                if (existing and existing.get("parser") == version
                        and existing.get("captures") == captures):
                    stats["unchanged"] += 1
                    continue
            started = time.monotonic()
            try:
                data = archive.read_raw(source, sha)
                parsed = parse_object(source, ext, data, rows)
            except Exception as exc:  # noqa: BLE001 — record, keep going
                stats["parse_error"] += 1
                log(f"  [error] {source} {act_id} {sha[:12]}: {exc}")
                parsed = {"parser": version, "norms": [], "complete": False,
                          "warnings": [f"parser exception: {exc}"]}
            record = build_record(source, act_id, sha, ext, parsed, rows)
            archive.write_parsed(source, act_id, sha, record)
            stats["parsed"] += 1
            stats["complete" if record["complete"] else "incomplete"] += 1
            log(f"  [parsed] {source} {act_id} {sha[:12]} "
                f"{record['layout']} norms={record['norm_count']} "
                f"complete={record['complete']} "
                f"{time.monotonic() - started:.1f}s")
    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--archive", help="history archive root")
    ap.add_argument("--sources", default=",".join(SOURCES))
    ap.add_argument("--acts", help="comma list of act ids")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args(argv)
    archive = HistoryArchive(args.archive)
    sources = [s for s in args.sources.split(",") if s]
    acts = set(args.acts.split(",")) if args.acts else None
    stats = run(archive, sources, acts, force=args.force)
    print(f"[done] {dict(stats)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
