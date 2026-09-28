"""python -m courts sync|status|get|search|paragraph|cites"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys

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


def cmd_sync(args) -> int:
    adapter = get_adapter(args.source)
    if not space_ok(f"sync {args.source}"):
        return 4
    conn = connect()
    init_db(conn)
    http = http_for(adapter)
    courts = [c.strip() for c in args.court.split(",")] if args.court else None
    try:
        result = sync_source(adapter, conn, http, since=args.since, limit=args.limit, courts=courts,
                             retry_failed=args.retry_failed)
    except SourceBusy as exc:
        print(json.dumps({"status": "busy", "detail": str(exc)}), file=sys.stderr)
        return 3
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in ("ok", "partial") else 1


def cmd_reparse(args) -> int:
    if not space_ok("reparse"):
        return 4
    conn = connect()
    init_db(conn)
    sources = [args.source] if args.source else [r[0] for r in conn.execute("SELECT source_id FROM sources")]
    for source_id in sources:
        try:
            with source_lock(source_id):
                print(json.dumps({"source": source_id, **reparse_source(get_adapter(source_id), conn)}))
        except SourceBusy as exc:
            print(json.dumps({"source": source_id, "status": "busy", "detail": str(exc)}))
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
    """FTS optimize + VACUUM INTO a fresh file in the NOCOW corpus directory, then an atomic swap."""
    import os
    import sqlite3 as _sqlite3
    from .packs import _nocow
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
        _nocow(home)  # new files in the corpus root inherit NOCOW (fewer btrfs extents)
        target = home / "courts.sqlite.compact"
        target.unlink(missing_ok=True)
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
    return 0


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
    result = citing_decisions(open_db(), args.target, relation=args.relation, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m courts", description="Local German court decision corpus")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("sync", help="incremental sync of one source")
    p.add_argument("--source", required=True, help="rii | nrw | by | <land> (" + ", ".join(sorted(registry())) + ")")
    p.add_argument("--since", type=_date, help="only decisions dated on/after YYYY-MM-DD")
    p.add_argument("--limit", type=_positive, help="fetch at most N decisions (priority order)")
    p.add_argument("--court", help="comma list of court ids/prefixes to restrict (e.g. bsg,bverwg)")
    p.add_argument("--retry-failed", action="store_true", help="also retry previously failed items")
    p.set_defaults(func=cmd_sync)
    p = sub.add_parser("reparse", help="re-parse stored raw captures with the current parser (offline)")
    p.add_argument("--source", help="one source (default: all)")
    p.set_defaults(func=cmd_reparse)
    p = sub.add_parser("migrate-captures", help="move legacy raw/*.gz captures into packs and verify")
    p.add_argument("--delete-legacy", action="store_true", help="delete legacy files after full verification")
    p.add_argument("--verify-only", action="store_true", help="only verify packs against the index")
    p.set_defaults(func=cmd_migrate_captures)
    p = sub.add_parser("compact", help="FTS optimize + VACUUM INTO a NOCOW file (no syncs may run)")
    p.set_defaults(func=cmd_compact)
    p = sub.add_parser("running", help="which sources are being synced right now (PIDs)")
    p.set_defaults(func=cmd_running)
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
    p.add_argument("--limit", type=_positive, default=20)
    p.set_defaults(func=cmd_cites)
    args = parser.parse_args(argv)
    return args.func(args)
