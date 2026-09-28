#!/usr/bin/env python3
"""Inspect, compact, verify, or restore exact historical snapshot bytes."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from snapshot_storage import (  # noqa: E402
    SnapshotArchiveError,
    _directory_fd,
    compact_snapshots,
    restore_snapshots,
    verify_archive,
)


def _default_refresh_lock() -> Path:
    override = os.environ.get("LEXGRAPH_REFRESH_LOCK")
    if override:
        return Path(override)
    if ROOT.is_relative_to("/srv"):
        return Path("/run/lock/lexgraph-refresh.lock")
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "lexgraph-refresh.lock"


@contextmanager
def _refresh_lock(path: Path, inherited_fd: int | None):
    path = Path(os.path.abspath(path))
    owned = inherited_fd is None
    if owned:
        with _directory_fd(path.parent, create=True) as parent:
            fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                         os.O_NONBLOCK, 0o600, dir_fd=parent)
    else:
        fd = inherited_fd
    try:
        info = os.fstat(fd)
        with _directory_fd(path.parent) as parent:
            target = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or not stat.S_ISREG(target.st_mode) or \
                (info.st_dev, info.st_ino) != (target.st_dev, target.st_ino):
            raise SnapshotArchiveError("refresh lock file boundary mismatch")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SnapshotArchiveError(f"shared refresh lock is held: {path}") from exc
        yield
    finally:
        if owned:
            os.close(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("compact", "verify", "restore"),
                        nargs="?", default="compact")
    parser.add_argument("--snapshots-root", type=Path, default=ROOT / "data" / "snapshots")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="perform compact/restore; default is dry-run")
    mode.add_argument("--dry-run", action="store_true", help="inspect without writing source/archive files")
    parser.add_argument("--keep", type=int, default=2, help="newest dated snapshots per family to keep raw (minimum 2)")
    parser.add_argument("--max-new-mib", type=float, default=500,
                        help="new gzip object allowance per invocation, at most 500 MiB")
    parser.add_argument("--max-archive-mib", type=float, default=500,
                        help="cumulative gzip object limit, including existing objects; default and maximum 500 MiB")
    parser.add_argument("--min-free-mib", type=float, default=128)
    parser.add_argument("--lock-file", type=Path, default=_default_refresh_lock())
    parser.add_argument("--lock-fd", type=int,
                        help="reuse this inherited descriptor for --lock-file inside an existing refresh lock")
    parser.add_argument("--details", action="store_true", help="include every excluded auxiliary file")
    args = parser.parse_args(argv)
    if args.command == "verify" and args.apply:
        parser.error("verify is read-only and does not take --apply")
    try:
        minimum = int(args.min_free_mib * 1024 * 1024)
        budget = int(args.max_new_mib * 1024 * 1024)
        archive_budget = int(args.max_archive_mib * 1024 * 1024)
        with _refresh_lock(args.lock_file, args.lock_fd) if args.apply else nullcontext():
            if args.command == "compact":
                result = compact_snapshots(args.snapshots_root, keep=args.keep,
                                           dry_run=not args.apply,
                                           max_new_bytes=budget,
                                           max_archive_bytes=archive_budget,
                                           min_free_bytes=minimum)
            elif args.command == "restore":
                result = restore_snapshots(args.snapshots_root, min_free_bytes=minimum,
                                            dry_run=not args.apply)
            else:
                result = verify_archive(args.snapshots_root)
    except (OSError, ValueError, OverflowError) as exc:
        print(f"snapshot archive {args.command} failed: {exc}", file=sys.stderr)
        return 1
    if not args.details:
        result.pop("exclusions", None)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
