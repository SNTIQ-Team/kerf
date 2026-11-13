#!/usr/bin/env python3
"""Run one source fetch and retain its result without capturing payloads/secrets."""
import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from api.source_freshness import (SOURCE_CADENCE_DAYS, build_freshness,
                                 record_attempt, utc_now, write_json_atomic)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=ROOT / 'data/source_freshness.json')
    sub = parser.add_subparsers(dest='operation', required=True)
    run = sub.add_parser('run')
    run.add_argument('--source', choices=sorted(SOURCE_CADENCE_DAYS), required=True)
    run.add_argument('command', nargs=argparse.REMAINDER)
    export = sub.add_parser('export', help='write public metadata only; no source fetch or corpus build')
    export.add_argument('--snapshots', type=Path, default=ROOT / 'data/snapshots')
    export.add_argument('--output', type=Path, required=True)
    record = sub.add_parser('record', help='record a real, independently observed fetch outcome')
    record.add_argument('--source', choices=sorted(SOURCE_CADENCE_DAYS), required=True)
    record.add_argument('--exit-code', type=int, required=True)
    record.add_argument('--at', required=True, help='actual observed RFC3339 timestamp, never inferred from snapshot age')
    args = parser.parse_args()
    if args.operation == 'export':
        write_json_atomic(args.output, build_freshness(args.snapshots, args.state, utc_now()))
        return 0
    if args.operation == 'record':
        record_attempt(args.state, args.source, args.exit_code, args.at)
        return 0
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('a source command is required')
    try:
        code = subprocess.run(command).returncode
    except OSError:
        code = 127
    record_attempt(args.state, args.source, code, utc_now())
    return code


if __name__ == '__main__':
    raise SystemExit(main())
