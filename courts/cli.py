"""python -m courts sync|status|get|search|paragraph|cites"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time

from .config import data_home, db_path
from .space import check as check_space
from .http import PoliteHttp
from .read import citing_decisions, coverage, get_decision, get_paragraph, open_db, search
from .sources import get_adapter, registry
from .store import connect, init_db
from .sync import SourceBusy, reparse_source, source_lock, sync_source


def _date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    return value


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def space_ok(what: str) -> bool:
    report = check_space(data_home(), starting=True)
    if report.problems:
        print(json.dumps({"status": "waiting_for_space", "operation": what, **report.as_dict()}), file=sys.stderr)
        return False
    return True


def http_for(adapter) -> PoliteHttp:
    """Per-source politeness: min interval and robots policy come from the adapter's declared policy."""
    return PoliteHttp(min_interval=max(1.0, adapter.min_interval),
                      respect_robots=adapter.robots_policy == "respect")


MAX_PARALLEL_SOURCES = 24


def cmd_sync(args) -> int:
    """One writer process (courts.pipeline): per source a runner and N fetcher threads (adaptive per-host
    concurrency, one token bucket per host), a bounded queue to the single writer (this thread), which owns
    the connection, stores, checkpoints the WAL and runs the space guard."""
    from .pipeline import SourceSpec, run_pipeline
    # A source may carry its own court restriction: "sn:ovg" (several: "sn:ovg+vg").
    restrictions = {}
    for spec in (args.source + "," + (args.then or "")).split(","):
        if spec.strip():
            source_id, _, restriction = spec.strip().partition(":")
            restrictions[source_id] = [c for c in restriction.split("+") if c] or None
    sources = [s.strip().partition(":")[0] for s in args.source.split(",") if s.strip()]
    queued = [s.strip().partition(":")[0] for s in (args.then or "").split(",") if s.strip()]
    max_sources = args.max_sources or len(sources)
    if len(sources) > MAX_PARALLEL_SOURCES or max_sources > MAX_PARALLEL_SOURCES:
        print(json.dumps({"status": "invalid_argument",
                          "detail": f"at most {MAX_PARALLEL_SOURCES} sources at a time"}), file=sys.stderr)
        return 2
    if not space_ok(f"sync {args.source}"):
        return 4
    courts = [c.strip() for c in args.court.split(",")] if args.court else None
    def spec_of(source_id: str) -> SourceSpec:
        adapter = get_adapter(source_id)
        return SourceSpec(adapter, http_for(adapter), restrictions.get(source_id) or courts)
    specs, waiting = [spec_of(s) for s in sources], [spec_of(s) for s in queued]
    conn = connect()
    init_db(conn)
    with contextlib.ExitStack() as stack:
        try:
            for spec in [*specs, *waiting]:
                stack.enter_context(source_lock(spec.adapter.source_id))
        except SourceBusy as exc:
            print(json.dumps({"status": "busy", "detail": str(exc)}), file=sys.stderr)
            return 3
        results = run_pipeline(conn, specs, since=args.since, limit=args.limit, retry_failed=args.retry_failed,
                               relist_after=args.relist_after, max_sources=max_sources, queued=waiting)
    print(json.dumps(results, ensure_ascii=False))
    return 0 if all(r.get("status") in ("ok", "partial") for k, r in results.items() if k != "_writer") else 1


def cmd_reparse(args) -> int:
    if not space_ok("reparse"):
        return 4
    conn = connect()
    init_db(conn)
    sources = [args.source] if args.source else [r[0] for r in conn.execute("SELECT source_id FROM sources")]
    ids = set(args.decision) if args.decision else None
    if args.stale_norm_ids:
        from .acts import resolver
        federal = set(resolver().catalog_slugs) | set(resolver()._by_slug)
        ids = {r[0] for r in conn.execute("SELECT DISTINCT decision_id, target_id FROM relations WHERE "
                                          "target_kind='norm' AND target_id LIKE 'de/bund/%'")
               if r[1].split("/")[2] not in federal} | (ids or set())
    if ids is not None:
        sources = [r[0] for r in conn.execute(
            f"SELECT DISTINCT source_id FROM decisions WHERE id IN ({','.join('?' * len(ids))})", sorted(ids))]
    if args.include_skipped and ids is None:
        from .verify import adopt_verification_captures
        print(json.dumps({"adopted_verification_captures": adopt_verification_captures(conn)}))
    for source_id in sources:
        try:
            with source_lock(source_id):
                print(json.dumps({"source": source_id, **reparse_source(
                    get_adapter(source_id), conn, decision_ids=ids, force=args.force,
                    include_skipped=args.include_skipped and ids is None)}))
        except SourceBusy as exc:
            print(json.dumps({"source": source_id, "status": "busy", "detail": str(exc)}))
    if args.include_skipped and ids is None and args.source is None:
        from .sync import reexamine_twins
        print(json.dumps({"twins": reexamine_twins(conn)}))
    return 0


def cmd_migrate_captures(args) -> int:
    from .migrate import migrate_legacy_captures, verify
    conn = connect()
    init_db(conn)
    if args.verify_only:
        print(json.dumps(verify(conn, data_home()), indent=2))
        return 0
    if not space_ok("migrate-captures"):
        return 4
    sources = [r[0] for r in conn.execute("SELECT source_id FROM sources")]
    with contextlib.ExitStack() as stack:
        try:
            for source_id in sources:  # no sync may append to packs meanwhile
                stack.enter_context(source_lock(source_id))
        except SourceBusy as exc:
            print(json.dumps({"status": "busy", "detail": str(exc)}), file=sys.stderr)
            return 3
        report = migrate_legacy_captures(conn, data_home(), delete_legacy=args.delete_legacy)
    print(json.dumps(report, indent=2))
    return 0 if not report["verify_error_count"] and not report["bad_legacy"] else 1


def cmd_compact(args) -> int:
    """FTS optimize + VACUUM INTO a fresh btrfs-compressed file, then an atomic swap."""
    import os
    import sqlite3 as _sqlite3
    from .packs import _nocow, compressed_empty_file
    if not space_ok("compact"):
        return 4
    home = data_home()
    conn = connect()
    init_db(conn)
    sources = [r[0] for r in conn.execute("SELECT source_id FROM sources")]
    with contextlib.ExitStack() as stack:
        try:
            for source_id in sources:
                stack.enter_context(source_lock(source_id))
        except SourceBusy as exc:
            print(json.dumps({"status": "busy", "detail": str(exc)}), file=sys.stderr)
            return 3
        before = db_path().stat().st_size
        conn.execute("INSERT INTO paragraphs_fts(paragraphs_fts) VALUES('optimize')")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _nocow(home)  # WAL/SHM and packs stay NOCOW (fewer btrfs extents) ...
        target = home / "courts.sqlite.compact"
        compressed_empty_file(target)  # ... the database itself is compressed (zstd: ~40 % of its size)
        conn.execute("VACUUM INTO ?", (str(target),))
        conn.close()
        check = _sqlite3.connect(target)
        ok = check.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        check.execute("INSERT INTO paragraphs_fts(paragraphs_fts) VALUES('integrity-check')")
        check.close()
        if not ok:
            target.unlink()
            print(json.dumps({"status": "failed", "detail": "quick_check failed on compacted copy"}))
            return 1
        os.replace(target, db_path())
        for suffix in ("-wal", "-shm"):
            (home / f"courts.sqlite{suffix}").unlink(missing_ok=True)
        print(json.dumps({"status": "ok", "bytes_before": before, "bytes_after": db_path().stat().st_size}))
    return 0


def cmd_dedupe(args) -> int:
    """Merge stored republications and settle shared ECLIs (dry run unless --apply; no syncs may run)."""
    from .dedupe import dedupe
    if args.apply and not space_ok("dedupe"):
        return 4
    conn = connect()
    init_db(conn)
    sources = [r[0] for r in conn.execute("SELECT source_id FROM sources")]
    with contextlib.ExitStack() as stack:
        try:
            for source_id in sources:
                stack.enter_context(source_lock(source_id))
        except SourceBusy as exc:
            print(json.dumps({"status": "busy", "detail": str(exc)}), file=sys.stderr)
            return 3
        result = dedupe(conn, apply=args.apply)
    journal = data_home() / "logs" / f"dedupe-{time.strftime('%Y%m%d-%H%M%S')}{'' if args.apply else '-dry'}.json"
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    print(json.dumps({"applied": args.apply, "journal": str(journal), **result["summary"]}, ensure_ascii=False))
    return 0


def cmd_running(args) -> int:
    """Active sync/reparse processes per source (a held lock file = running), with their PID."""
    import fcntl
    rows = [["source", "state", "pid"]]
    locks = data_home() / "locks"
    for lock in sorted(locks.glob("*.lock")) if locks.exists() else []:
        with open(lock, "r+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
                state, pid = "idle", "-"
            except BlockingIOError:
                state, pid = "running", handle.read().strip() or "?"
        rows.append([lock.stem, state, pid])
    print(_table(rows))
    writer = data_home() / ".writer.lock"
    if writer.exists():
        with open(writer, "r+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
                print("writer: none")
            except BlockingIOError:
                print(f"writer: {handle.read().strip() or '?'}")
    return 0


def cmd_classify(args) -> int:
    from .polarity import assess_decision
    from .writer import Maintenance, checkpoint
    if not space_ok("classify"):
        return 4
    conn = connect()
    init_db(conn)
    maintenance = Maintenance(conn, every_items=500)
    sql, params = "SELECT id FROM decisions", []
    if args.source:
        sql, params = sql + " WHERE source_id = ?", [args.source]
    total = proposals = 0
    for (decision_id,) in conn.execute(sql, params).fetchall():
        problems = maintenance.tick()
        if problems:
            print(json.dumps({"status": "stopped", "problems": problems, "decisions": total}), file=sys.stderr)
            return 4
        proposals += assess_decision(conn, decision_id)
        total += 1
    checkpoint(conn)
    print(json.dumps({"decisions": total, "proposals": proposals}))
    return 0


def cmd_migrate_schema(args) -> int:
    """v3 -> v4 of the live corpus: build a verified copy next to it, then swap (the v3 file is kept)."""
    import os
    import sqlite3 as _sqlite3
    from .schema_v4 import build_copy, verify
    from .store import create_schema
    home, source = data_home(), db_path()
    probe = _sqlite3.connect(f"file:{source}", uri=True)
    version = probe.execute("PRAGMA user_version").fetchone()[0]
    if version == 4:
        print(json.dumps({"status": "ok", "detail": "already schema v4"}))
        return 0
    if version != 3:
        print(json.dumps({"status": "failed", "detail": f"schema v{version}, expected v3"}))
        return 1
    busy, _, _ = probe.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    probe.close()
    if busy:
        print(json.dumps({"status": "failed", "detail": "the v3 WAL could not be checkpointed (a reader is open)"}))
        return 1
    size = source.stat().st_size
    stat = os.statvfs(home)
    margin_ok = os.environ.get("SNTIQ_COURTS_SPACE_GUARD") == "off" or stat.f_bavail * stat.f_frsize >= size + 50 * 1000 ** 3
    if not space_ok("migrate-schema") or not margin_ok:
        print(json.dumps({"status": "waiting_for_space", "detail": f"needs {size / 1e9:.1f} GB + 50 GB free"}))
        return 4
    target = home / "courts.sqlite.v4-building"
    target.unlink(missing_ok=True)
    log = lambda message: print(message, file=sys.stderr, flush=True)  # noqa: E731
    counts = build_copy(source, target, create_schema, log=log)
    report = verify(source, target, log=log)
    report["copied"] = counts
    report["bytes_v3"], report["bytes_v4"] = size, target.stat().st_size
    (home / "logs").mkdir(exist_ok=True)
    (home / "logs" / "migrate-v3-v4-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    if report["problems"] or args.no_swap:
        print(json.dumps({"status": "failed" if report["problems"] else "built", "problems": report["problems"],
                          "target": str(target), "report": str(home / "logs" / "migrate-v3-v4-report.json")}))
        return 1 if report["problems"] else 0
    backup = home / "courts.sqlite.v3"
    os.replace(source, backup)
    os.replace(target, source)
    for suffix in ("-wal", "-shm"):
        (home / f"courts.sqlite{suffix}").unlink(missing_ok=True)
    conn = connect()
    init_db(conn)
    print(json.dumps({"status": "ok", "bytes_v3": size, "bytes_v4": source.stat().st_size, "v3_kept_as": str(backup),
                      "report": str(home / "logs" / "migrate-v3-v4-report.json")}))
    return 0


def cmd_correct(args) -> int:
    from .ledger import CorrectionError, record
    conn = connect()
    init_db(conn)
    try:
        new = json.loads(args.new)
    except json.JSONDecodeError:
        new = args.new
    try:
        result = record(conn, target=args.target, kind=args.kind, field=args.field, target_id=args.target_id,
                        quote=args.quote, new=new, evidence=args.evidence, author=args.author)
    except CorrectionError as exc:
        print(json.dumps({"status": "invalid_argument", "detail": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False, indent=2))
    return 0


def cmd_corrections(args) -> int:
    conn = open_db()
    sql, params = "SELECT * FROM corrections", []
    if args.target:
        sql, params = sql + " WHERE target = ? OR target LIKE ?", [args.target, args.target + "%"]
    rows = [dict(r) for r in conn.execute(sql + " ORDER BY id", params)]
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def cmd_verify(args) -> int:
    from urllib.parse import urlsplit
    from .verify import verify_sample
    conn = connect()
    init_db(conn)
    http = None
    if not args.offline:
        ignored = {urlsplit(a.base_url).netloc for a in registry().values()
                   if a.robots_policy == "ignore_by_operator_decision"}
        # Slow on purpose: the same hosts may be serving a running sync (combined rate stays ~1 req/s).
        http = PoliteHttp(min_interval=args.interval, ignore_robots_hosts=ignored)
    kinds = set(args.kinds.split(",")) if args.kinds else None
    print(json.dumps(verify_sample(conn, sample=args.sample, http=http, source=args.source, seed=args.seed,
                                   kinds=kinds, reuse_run=args.rerun), indent=2))
    return 0


def cmd_check_norm_ids(args) -> int:
    from .acts import check_norm_ids
    report = check_norm_ids(open_db())
    print(json.dumps(report, indent=2))
    return 0 if not report["not_canonical"] else 1


def cmd_courts_map(args) -> int:
    from .courtmap import fetch_payload, store_mapping
    conn = connect()
    init_db(conn)
    payload = json.loads(open(args.file, encoding="utf-8").read()) if args.file else \
        fetch_payload(PoliteHttp(respect_robots=False))
    print(json.dumps(store_mapping(conn, payload), indent=2))
    return 0


def cmd_ecli_codes(args) -> int:
    """Land-court ECLI court codes observed in the corpus -> court_map (see courts.courtmap.derive_ecli_codes)."""
    from .courtmap import derive_ecli_codes
    conn = connect(readonly=args.dry_run)
    if not args.dry_run:
        init_db(conn)
    report = derive_ecli_codes(conn, store=not args.dry_run)
    logs = data_home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / ("ecli-codes-report.json" if not args.dry_run else "ecli-codes-report.dry-run.json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = {k: v for k, v in report.items() if k not in ("assigned", "ambiguous", "codes_new_to_taper")}
    summary["codes_new_to_taper"] = len(report["codes_new_to_taper"])
    summary["ambiguous"] = sorted(report["ambiguous"])
    summary["report"] = str(path)
    summary["stored"] = not args.dry_run
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_ingest_capture(args) -> int:  # dispatched before argparse (courts.capture.cli); listed for --help
    from .capture import cli
    return cli(sys.argv[2:])


def cmd_prefetch(args) -> int:
    """Fetch a source's captures into <data home>/staging/<source>/ without the writer lock (courts.prefetch)."""
    from .hostgov import Governors
    from .pipeline import concurrency_of, host_of
    from .prefetch import prefetch
    from .sync import log
    adapter = get_adapter(args.source)
    http = http_for(adapter)
    governors = Governors(log=log)
    governors.declare(host_of(adapter.base_url), concurrency_of(adapter), adapter.min_interval)
    http.governors = governors
    result = prefetch(adapter, http, since=args.since, limit=args.limit,
                      courts=args.court.split(",") if args.court else None, log=log, workers=args.workers)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("status") == "ok" else 4


def cmd_space(args) -> int:
    report = check_space(data_home(), starting=True)
    print(json.dumps({"bulk_writes_allowed": not report.problems, **report.as_dict()}, indent=2))
    return 0 if not report.problems else 4


def _table(rows: list[list[str]]) -> str:
    widths = [max(len(str(r[i])) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(str(c).ljust(w) for c, w in zip(r, widths)) for r in rows)


def cmd_status(args) -> int:
    if not db_path().exists():
        print(f"no corpus at {db_path()}", file=sys.stderr)
        return 1
    conn = open_db()
    report = coverage(conn)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    rows = [["source", "status", "robots", "decisions", "date range", "upstream", "pending", "failed",
             "last sync", "last run"]]
    for s in report["sources"]:
        last = s["last_run"] or {}
        robots = "respect" if s.get("robots_policy", "respect") == "respect" else \
            f"IGNORED (operator decision {s.get('policy_decided_at')})"
        rows.append([s["source_id"], s["status"], robots, s["decisions"],
                     f"{s['date_from'] or '-'}..{s['date_to'] or '-'}", s["upstream_listed"] or "-",
                     s["pending_items"], s["failed_items"], (s["last_sync_finished"] or "-")[:19],
                     f"{last.get('status', '-')} a={last.get('attempted', 0)} s={last.get('stored', 0)} "
                     f"f={last.get('failed', 0)}"])
    print(f"corpus: {data_home()}  schema v{report['schema_version']}  {report['parser_version']}")
    print(f"decisions={report['decisions']} paragraphs={report['paragraphs']} relations={report['relations']} "
          f"captures={report.get('captures')} pack_files={report.get('pack_files')}")
    space = check_space(data_home(), starting=True)
    print("space: " + ("ok for bulk writes" if not space.problems else "; ".join(space.problems)) +
          f" (unallocated={space.as_dict()['unallocated_gib']} GiB, "
          f"metadata={space.as_dict()['metadata_effective_fraction']})")
    print(_table(rows))
    for s in report["sources"]:
        if s["status_reason"] or s["failure_kinds"]:
            print(f"  {s['source_id']}: {s['status_reason'] or ''} {s['failure_kinds'] or ''}".rstrip())
    return 0


def cmd_get(args) -> int:
    conn = open_db()
    result = get_decision(conn, args.id, with_paragraphs=not args.meta, with_relations=args.relations)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


def cmd_paragraph(args) -> int:
    result = get_paragraph(open_db(), args.id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "ok" else 1


def cmd_search(args) -> int:
    result = search(open_db(), args.q, court=args.court, date_from=args.date_from, date_to=args.date_to,
                    case_number=args.az, ecli=args.ecli, norm=args.norm, relation=args.relation,
                    source=args.source.split(",") if args.source else None, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_cites(args) -> int:
    result = citing_decisions(open_db(), args.target, relation=args.relation, assessment=args.assessment,
                              limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


WRITING_COMMANDS = {cmd_sync, cmd_reparse, cmd_migrate_captures, cmd_compact, cmd_classify, cmd_correct,
                    cmd_verify, cmd_courts_map, cmd_migrate_schema, cmd_ecli_codes}
# ingest-capture takes the writer lock itself, and only when no writer runs (else: the inbox).


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["ingest-capture"]:
        # Its own parser: every refusal, a usage error too, is one JSON line on stderr (courts.capture).
        from .capture import cli
        return cli(argv[1:])
    parser = argparse.ArgumentParser(prog="python -m courts", description="Local German court decision corpus")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sync", help="incremental sync of one or more sources (one writer process)")
    p.add_argument("--source", required=True, help="comma list, at most 6 (a court restriction per source: "
                   "sn:ovg): rii | nrw | by | <land> ("
                   + ", ".join(sorted(registry())) + ")")
    p.add_argument("--since", type=_date, help="only decisions dated on/after YYYY-MM-DD")
    p.add_argument("--limit", type=_positive, help="fetch at most N decisions (priority order)")
    p.add_argument("--court", help="comma list of court ids/prefixes to restrict (e.g. bsg,bverwg)")
    p.add_argument("--retry-failed", action="store_true", help="also retry previously failed items")
    p.add_argument("--then", help="comma list of sources to sync next, each starting when a running source "
                                  "finishes (at most --max-sources at a time)")
    p.add_argument("--max-sources", type=_positive, help="sources at a time (default: the number in --source)")
    p.add_argument("--relist-after", type=float, default=24.0,
                   help="reuse a complete listing younger than this many hours (default 24; 0 = always list)")
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("reparse", help="re-parse stored raw captures with the current parser (offline)")
    p.add_argument("--source", help="one source (default: all)")
    p.add_argument("--decision", action="append", help="only these decision ids (repeatable)")
    p.add_argument("--stale-norm-ids", action="store_true",
                   help="only decisions with federal norm ids that the Kerf resolver does not know")
    p.add_argument("--force", action="store_true", help="rewrite even if text and parser version are unchanged")
    p.add_argument("--include-skipped", action="store_true",
                   help="also re-examine skipped duplicates (store from their capture, or re-queue them when the "
                        "decision they collided with changed id or is unnumbered)")
    p.set_defaults(func=cmd_reparse)
    p = sub.add_parser("migrate-schema", help="v3 -> v4: build a verified copy of the corpus, then swap")
    p.add_argument("--no-swap", action="store_true", help="build and verify only; leave the v3 file in place")
    p.set_defaults(func=cmd_migrate_schema)
    p = sub.add_parser("migrate-captures", help="move legacy raw/*.gz captures into packs and verify")
    p.add_argument("--delete-legacy", action="store_true", help="delete legacy files after full verification")
    p.add_argument("--verify-only", action="store_true", help="only verify packs against the index")
    p.set_defaults(func=cmd_migrate_captures)
    p = sub.add_parser("compact", help="FTS optimize + VACUUM INTO a NOCOW file (no syncs may run)")
    p.set_defaults(func=cmd_compact)
    p = sub.add_parser("dedupe", help="merge stored republications, settle shared ECLIs (dry run unless --apply)")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_dedupe)
    p = sub.add_parser("running", help="which sources are being synced right now (PIDs)")
    p.set_defaults(func=cmd_running)
    p = sub.add_parser("classify", help="(re)run the rule-based relation assessment stage")
    p.add_argument("--source")
    p.set_defaults(func=cmd_classify)
    p = sub.add_parser("correct", help="append a correction to the ledger, apply it, write a conformance case")
    p.add_argument("--target", required=True, help="decision id, or paragraph id (<decision>#rn-10)")
    p.add_argument("--kind", required=True,
                   choices=["decision_field", "case_numbers", "relation_assessment", "paragraph_boundary", "relation",
                            "other"])
    p.add_argument("--field", help="decision field (decision_field)")
    p.add_argument("--target-id", help="norm or decision id of the relation (relation_assessment, relation)")
    p.add_argument("--quote", help="quote that must resolve to the corrected paragraph (paragraph_boundary)")
    p.add_argument("--new", required=True, help="new value (JSON or plain string)")
    p.add_argument("--evidence", required=True)
    p.add_argument("--author", required=True)
    p.set_defaults(func=cmd_correct)
    p = sub.add_parser("corrections", help="list the corrections ledger")
    p.add_argument("--target")
    p.set_defaults(func=cmd_corrections)
    p = sub.add_parser("verify", help="compare a sample of decisions with an independent representation")
    p.add_argument("--sample", type=_positive, default=20)
    p.add_argument("--source")
    p.add_argument("--seed", type=int)
    p.add_argument("--offline", action="store_true", help="only pairs whose second capture is already packed")
    p.add_argument("--kinds", help="comma list of pair kinds: capture, nrwe-pdf, rii-xml (default: all)")
    p.add_argument("--rerun", type=int, help="re-check exactly the pairs of an earlier run (offline)")
    p.add_argument("--interval", type=float, default=8.0, help="seconds between requests per host (default 8)")
    p.set_defaults(func=cmd_verify)
    p = sub.add_parser("check-norm-ids", help="every relation norm id must be canonical for Kerf's resolver")
    p.set_defaults(func=cmd_check_norm_ids)
    p = sub.add_parser("courts-map", help="map corpus court slugs to Taper court ids (court:de:<XJustiz-ID>)")
    p.add_argument("--file", help="use a saved /v1/courts/mapping payload instead of fetching it")
    p.set_defaults(func=cmd_courts_map)
    p = sub.add_parser("ingest-capture", help="ingest ONE decision page saved from the operator's browser "
                       "(--source by --url <url> --file <html|pdf> [--captured-at ISO]; JSON contract, see "
                       "courts/capture.py)", add_help=False)
    p.set_defaults(func=cmd_ingest_capture)
    p = sub.add_parser("ecli-codes", help="Land-court ECLI court codes observed in the corpus -> court_map")
    p.add_argument("--dry-run", action="store_true", help="report only (read-only, no writer lock needed)")
    p.set_defaults(func=cmd_ecli_codes)
    p = sub.add_parser("prefetch", help="fetch a source's captures into staging while another writer runs "
                       "(no database writes; the next sync of the source stores them)")
    p.add_argument("--source", required=True)
    p.add_argument("--court", help="source-specific restriction (wayback: by,bverwg)")
    p.add_argument("--since", type=_date)
    p.add_argument("--limit", type=_positive)
    p.add_argument("--workers", type=_positive, default=2)
    p.set_defaults(func=cmd_prefetch)
    p = sub.add_parser("space", help="btrfs/disk guard for bulk writes")
    p.set_defaults(func=cmd_space)
    p = sub.add_parser("status", help="coverage per source")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("get", help="decision by id, ECLI, source doc id or Aktenzeichen")
    p.add_argument("id")
    p.add_argument("--meta", action="store_true", help="metadata only")
    p.add_argument("--relations", action="store_true", help="include relations")
    p.set_defaults(func=cmd_get)
    p = sub.add_parser("paragraph", help="exact paragraph by id (<decision>#rn-<n>)")
    p.add_argument("id")
    p.set_defaults(func=cmd_paragraph)
    p = sub.add_parser("search", help="FTS over paragraphs")
    p.add_argument("q", nargs="?")
    for flag in ("--court", "--az", "--ecli", "--norm", "--relation", "--source"):
        p.add_argument(flag)
    p.add_argument("--date-from", type=_date)
    p.add_argument("--date-to", type=_date)
    p.add_argument("--limit", type=_positive, default=10)
    p.set_defaults(func=cmd_search)
    p = sub.add_parser("cites", help="decisions relating to a norm identity or decision")
    p.add_argument("target")
    p.add_argument("--relation")
    p.add_argument("--assessment", help="only relations with this (non-rejected) assessment, e.g. applies")
    p.add_argument("--limit", type=_positive, default=20)
    p.set_defaults(func=cmd_cites)
    args = parser.parse_args(argv)
    # SIGTERM (e.g. `kill <pid>`) during a sync: the current item is finished, then the run stops and is
    # recorded as interrupted; a second SIGTERM (or any other command) interrupts at once like Ctrl-C.
    # Every committed decision stays; pending items resume on the next run.
    import signal
    from .sync import STOP_AFTER_ITEM

    def _terminate(signum, frame):
        if args.func is cmd_sync and not STOP_AFTER_ITEM.is_set():
            STOP_AFTER_ITEM.set()
            return
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, _terminate)
    from .writer import WriterBusy, writer_lock
    try:
        if args.func in WRITING_COMMANDS and not getattr(args, "dry_run", False):
            with writer_lock(f"{args.command} " + " ".join(argv or sys.argv[1:])):
                return args.func(args)
        return args.func(args)
    except WriterBusy as exc:
        print(json.dumps({"status": "writer_busy", "detail": str(exc)}), file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print(json.dumps({"status": "interrupted"}), file=sys.stderr)
        return 130
