"""Validate parsed historical captures and report per-act statistics.

Three checks, all by :func:`api.history_identity.norm_identity`:

1. **daily-state check** - every parsed capture whose capture date falls into
   the period covered by Lexgraph's daily GII states
   (``web/data/acts/<id>.json`` ``official_states``) is compared with the
   state observed on or before that date and with the best-matching state of
   the period (GII may publish a build a day before Lexgraph observes it).
   Match rate = reference norms whose identity occurs in the capture.
2. **xml cross-check** - every archived ``xml.zip`` (parsed with
   ``fetch_gii.parse_law``) is compared with the HTML captures of the same
   act that carry the identical Stand block, which validates the older HTML
   layouts (2012+) against official XML of the same moment.
3. **statistics** - captures listed/fetched/failed, parsed complete/
   incomplete, distinct act-level texts and distinct norm versions compared
   with the distinct norm identities over Lexgraph's official states.

Usage::

    LEXGRAPH_WEB_DATA=/path/to/Gesetz/web/data \
    python3 tools/validate_history_captures.py [--acts fed_sgb_2] \
        [--json report.json] [--show-mismatches 5]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT))

from history_archive import HistoryArchive, web_data_root  # noqa: E402

from api.history_identity import norm_identity  # noqa: E402

SOURCES = ("wayback_gii", "neuris")


def load_states(web_data: Path, act_id: str) -> tuple[list[dict], dict]:
    """``(official_states, {state_sha: state_object})``."""
    path = web_data / "acts" / f"{act_id}.json"
    if not path.is_file():
        return [], {}
    data = json.loads(path.read_text(encoding="utf-8"))
    states = data.get("official_states") or []
    objects = {}
    for state in states:
        sha = state.get("state_sha256")
        if not sha or sha in objects:
            continue
        obj = (web_data / "federal_states" / "objects" / "sha256" / sha[:2]
               / f"{sha}.json.gz")
        if obj.is_file():
            objects[sha] = json.loads(gzip.decompress(obj.read_bytes()))
    return states, objects


def identities(norms: list[dict]) -> list[str]:
    return [n.get("identity") or norm_identity(n.get("enbez"), n.get("titel"),
                                               n.get("text"))
            for n in norms]


def compare(ref_norms: list[dict], got: set[str]) -> tuple[int, int, list[dict]]:
    ref_ids = identities(ref_norms)
    missing = [n for n, i in zip(ref_norms, ref_ids) if i not in got]
    return len(ref_ids) - len(missing), len(ref_ids), missing


def state_on(states: list[dict], day: str) -> dict | None:
    chosen = None
    for state in states:
        if (state.get("observed_at") or "") <= day:
            chosen = state
    return chosen


def days_between(a: str | None, b: str | None) -> float:
    from datetime import datetime
    try:
        da = datetime.strptime((a or "")[:10], "%Y-%m-%d")
        db = datetime.strptime((b or "")[:10], "%Y-%m-%d")
    except ValueError:
        return float("inf")
    return abs((da - db).days)


def act_text_key(norm_ids: list[str]) -> str:
    return hashlib.sha256("\n".join(norm_ids).encode()).hexdigest()


def cdx_listed(archive: HistoryArchive, web_data: Path, act_id: str) -> Counter:
    """Capture targets exactly as the fetcher lists them (cached CDX only)."""
    import fetch_history_wayback as wb
    try:
        slug, doknr = wb.load_acts(web_data, [act_id])[act_id]
    except SystemExit:
        return Counter()
    targets = wb.discover(None, archive, act_id, slug, doknr,
                          wb.DEFAULT_KINDS, max_age_days=0, offline=True)
    counter = Counter(t.kind for t in targets)
    counter["total"] = len(targets)
    return counter


def ledger_stats(archive: HistoryArchive, source: str) -> dict[str, Counter]:
    per_act: dict[str, Counter] = defaultdict(Counter)
    final: dict[tuple, str] = {}
    for row in archive.read_ledger(source):
        key = (row.get("act_id"), row.get("capture_timestamp")
               or row.get("archive_url"), row.get("wayback_original")
               or row.get("original_url"))
        status = row.get("status")
        if final.get(key) in {"ok", "skipped"}:
            continue
        final[key] = status
    for (act_id, _, _), status in final.items():
        per_act[act_id][status] += 1
    return per_act


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--archive")
    ap.add_argument("--web-data")
    ap.add_argument("--acts")
    ap.add_argument("--json", help="write the full report as JSON")
    ap.add_argument("--show-mismatches", type=int, default=3)
    ap.add_argument("--xml-window-days", type=float, default=60)
    args = ap.parse_args(argv)
    archive = HistoryArchive(args.archive)
    web_data = web_data_root(args.web_data)

    parsed: dict[str, list[dict]] = defaultdict(list)
    for source in SOURCES:
        for record in archive.iter_parsed(source):
            parsed[record["act_id"]].append(record)
    acts = (args.acts.split(",") if args.acts else sorted(parsed))
    ledgers = {s: ledger_stats(archive, s) for s in SOURCES}
    report: dict = {"acts": {}}
    table = []
    for act_id in acts:
        records = sorted(parsed.get(act_id, []),
                         key=lambda r: r.get("captured_at") or "")
        states, objects = load_states(web_data, act_id)
        lex_ids: set[str] = set()
        for obj in objects.values():
            lex_ids.update(identities(obj["norms"]))
        period_start = states[0]["observed_at"] if states else None
        entry: dict = {"lexgraph_states": len(objects),
                       "lexgraph_distinct_norm_versions": len(lex_ids),
                       "period_start": period_start}

        # 1. daily-state check
        checks = []
        for record in records:
            day = (record.get("captured_at") or "")[:10]
            if not period_start or not day or day < period_start:
                continue
            got = set(identities(record["norms"]))
            on = state_on(states, day)
            on_obj = objects.get(on["state_sha256"]) if on else None
            best = None
            for sha, obj in objects.items():
                match, total, missing = compare(obj["norms"], got)
                if best is None or match / total > best[0] / best[1]:
                    best = (match, total, missing, sha)
            on_match = compare(on_obj["norms"], got) if on_obj else None
            checks.append({
                "source": record["source"], "sha256": record["sha256"],
                "captured_at": record.get("captured_at"),
                "layout": record.get("layout"),
                "state_on_capture_day": on["state_sha256"] if on else None,
                "on_day_match": on_match[0] if on_match else None,
                "on_day_total": on_match[1] if on_match else None,
                "best_state": best[3], "best_match": best[0],
                "best_total": best[1],
                "best_missing": [{"enbez": n["enbez"], "titel": n["titel"]}
                                 for n in best[2]],
            })
        entry["daily_state_checks"] = checks

        # 2. xml cross-check: each xml.zip vs the best-matching HTML capture
        #    with the identical Stand block captured within +-window days
        cross = []
        zips = [r for r in records if r.get("media_ext") == "zip"
                and r["source"] == "wayback_gii"]
        htmls = [r for r in records if r.get("media_ext") == "html"
                 and r["source"] == "wayback_gii"]
        for z in zips:
            z_ids = identities(z["norms"])
            z_set = set(z_ids)
            candidates = []
            for h in htmls:
                if not h["stand"]["raw"] or h["stand"]["raw"] != z["stand"]["raw"]:
                    continue
                if days_between(h.get("captured_at"), z.get("captured_at")) > args.xml_window_days:
                    continue
                got = set(identities(h["norms"]))
                missing = [n for n, i in zip(z["norms"], z_ids) if i not in got]
                candidates.append((len(missing), h, got, missing))
            candidates.sort(key=lambda c: (c[0], days_between(
                c[1].get("captured_at"), z.get("captured_at"))))
            for _, h, got, missing in candidates[:1]:
                cross.append({
                    "xml_sha256": z["sha256"], "xml_captured_at": z["captured_at"],
                    "html_sha256": h["sha256"], "html_captured_at": h["captured_at"],
                    "layout": h.get("layout"), "match": len(z_ids) - len(missing),
                    "total": len(z_ids),
                    "missing": [{"enbez": n["enbez"], "titel": n["titel"]}
                                for n in missing],
                    "html_extra": len(got - z_set)})
        entry["xml_cross_checks"] = cross

        # 3. statistics
        wb = [r for r in records if r["source"] == "wayback_gii"]
        nr = [r for r in records if r["source"] == "neuris"]
        all_ids: set[str] = set()
        act_texts: set[str] = set()
        for record in records:
            if not record["complete"]:
                continue
            ids = identities(record["norms"])
            all_ids.update(ids)
            act_texts.add(act_text_key(ids))
        dates = [r.get("first_captured_at") for r in wb if r.get("first_captured_at")]
        dates += [r.get("last_captured_at") for r in wb if r.get("last_captured_at")]
        led = ledgers["wayback_gii"].get(act_id, Counter())
        entry.update({
            "cdx_listed": dict(cdx_listed(archive, web_data, act_id)),
            "ledger": dict(led),
            "parsed_wayback": len(wb), "parsed_neuris": len(nr),
            "complete": sum(r["complete"] for r in records),
            "incomplete": sum(not r["complete"] for r in records),
            "layouts": dict(Counter(r.get("layout") for r in records)),
            "distinct_act_texts": len(act_texts),
            "distinct_norm_versions": len(all_ids),
            "new_vs_lexgraph": len(all_ids - lex_ids),
            "lexgraph_covered": len(lex_ids & all_ids),
            "earliest_capture": min(dates) if dates else None,
            "latest_capture": max(dates) if dates else None,
        })
        report["acts"][act_id] = entry
        newest = {src: next((c for c in reversed(checks) if c["source"] == src),
                            None) for src in SOURCES}
        xml_rates = [c["match"] / c["total"] for c in cross if c["total"]]
        entry["newest_in_period"] = newest
        table.append((act_id, entry, newest, xml_rates))

    def cell(check) -> str:
        if not check:
            return "-"
        return (f"{check['best_match']}/{check['best_total']} "
                f"{check['captured_at'][:10]}")

    print(f"{'act':18} {'listed':>6} {'ok':>4} {'dup':>4} {'fail':>4} {'parsed':>6} "
          f"{'incompl':>7} {'acttexts':>8} {'normvers':>8} {'lexgraph':>8} "
          f"{'new':>5} {'wayback newest in period':>25} "
          f"{'neuris newest':>21} {'xml-check':>9} captures")
    for act_id, entry, newest, xml_rates in table:
        led = entry["ledger"]
        xml_s = (f"{100 * sum(c['match'] for c in entry['xml_cross_checks']) / sum(c['total'] for c in entry['xml_cross_checks']):.2f}%"
                 if xml_rates else "-")
        print(f"{act_id:18} {entry['cdx_listed'].get('total', 0):6} "
              f"{led.get('ok', 0):4} {led.get('skipped', 0):4} "
              f"{led.get('error', 0):4} "
              f"{entry['parsed_wayback'] + entry['parsed_neuris']:6} "
              f"{entry['incomplete']:7} {entry['distinct_act_texts']:8} "
              f"{entry['distinct_norm_versions']:8} "
              f"{entry['lexgraph_distinct_norm_versions']:8} "
              f"{entry['new_vs_lexgraph']:5} {cell(newest['wayback_gii']):>25} "
              f"{cell(newest['neuris']):>21} {xml_s:>9} "
              f"{(entry['earliest_capture'] or '')[:10]}..{(entry['latest_capture'] or '')[:10]}")
        if args.show_mismatches:
            for src, check in newest.items():
                for miss in (check or {}).get("best_missing", [])[:args.show_mismatches]:
                    print(f"    residual {src}: {miss['enbez']} {miss['titel'][:60]}")
    for src in SOURCES:
        checks = [t[2][src] for t in table if t[2][src]]
        match = sum(c["best_match"] for c in checks)
        total = sum(c["best_total"] for c in checks)
        if total:
            print(f"{src}: newest in-period capture per act: {match}/{total} "
                  f"= {100 * match / total:.2f}% identical norms "
                  f"({len(checks)} acts)")
    xml_match = sum(c["match"] for t in table for c in t[1]["xml_cross_checks"])
    xml_total = sum(c["total"] for t in table for c in t[1]["xml_cross_checks"])
    if xml_total:
        print(f"xml cross-checks: {xml_match}/{xml_total} = "
              f"{100 * xml_match / xml_total:.2f}% identical norms")
    if args.json:
        Path(args.json).write_text(json.dumps(report, ensure_ascii=False,
                                              indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
