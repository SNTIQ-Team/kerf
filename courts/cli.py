"""python -m courts sync|status|get|search|paragraph|cites"""
from __future__ import annotations

import argparse
import json
import re
import sys

from .config import data_home, db_path
from .http import PoliteHttp
from .read import citing_decisions, coverage, get_decision, get_paragraph, open_db, search
from .sources import get_adapter, registry
from .store import connect, init_db
from .sync import sync_source


def _date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    return value


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def cmd_sync(args) -> int:
    adapter = get_adapter(args.source)
    conn = connect()
    init_db(conn)
    http = PoliteHttp()
    courts = [c.strip() for c in args.court.split(",")] if args.court else None
    result = sync_source(adapter, conn, http, since=args.since, limit=args.limit, courts=courts,
                         retry_failed=args.retry_failed)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in ("ok", "partial") else 1


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
    rows = [["source", "status", "decisions", "date range", "upstream", "pending", "failed", "last sync",
             "last run"]]
    for s in report["sources"]:
        last = s["last_run"] or {}
        rows.append([s["source_id"], s["status"], s["decisions"],
                     f"{s['date_from'] or '-'}..{s['date_to'] or '-'}", s["upstream_listed"] or "-",
                     s["pending_items"], s["failed_items"], (s["last_sync_finished"] or "-")[:19],
                     f"{last.get('status', '-')} a={last.get('attempted', 0)} s={last.get('stored', 0)} "
                     f"f={last.get('failed', 0)}"])
    print(f"corpus: {data_home()}  schema v{report['schema_version']}  {report['parser_version']}")
    print(f"decisions={report['decisions']} paragraphs={report['paragraphs']} relations={report['relations']}")
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
                    source=args.source, limit=args.limit)
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
