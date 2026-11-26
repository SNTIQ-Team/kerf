#!/usr/bin/env python3
"""Archive source objects and verify receipts before applying retention.

VPS side (``/srv/sntiq-kerf``)::

    archive_offload.py manifest            # list unreceipted files + SHA-256
    archive_offload.py prune [--apply]     # delete receipted, out-of-window bytes
    archive_offload.py install-legacy DIR  # admit an attested legacy rebuild
    archive_offload.py status

Archive commands::

    archive_offload.py receipt  --manifest M --archive A --key K --out DIR
    archive_offload.py attest   --snapshot-dir S --key K [--receipt R]

The orchestration lives in ``deploy/archive-pull.sh`` (daily user timer) and
``deploy/legacy-rebuild-push.sh`` (after a parser change).  A file is deleted
on the VPS only when a signed receipt lists its exact path and SHA-256, and
only if the daily refresh no longer reads it.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from archive_receipts import (  # noqa: E402
    ATTESTATION_NAME, ATTESTATION_NAMESPACE, MANIFEST_SCHEMA, RECEIPT_NAMESPACE,
    RECEIPT_SCHEMA, ArchiveReceiptError, build_attestation, canonical_json,
    iter_files, load_manifest, receipted_hashes, sha256_bytes, sha256_file,
    sign_file, verify_attestation, write_json_atomic,
)
from snapshot_storage import (  # noqa: E402
    STORE_NAME, SnapshotArchiveError, load_manifest as load_snapshot_manifest,
    offload_objects,
)

DATA = ROOT / "data"
OFFLOAD = Path(os.environ.get("KERF_OFFLOAD_ROOT", DATA / "archive-offload"))
# What the source archives: the pre-2023 BGBl capture store and every
# dated source snapshot (including the compaction archive).
ARCHIVED_PREFIXES = ("legacy_bgbl_history", "snapshots")
LEGACY_SOURCE_PREFIXES = ("legacy_bgbl_history/objects/",
                          "legacy_bgbl_history/texts/")
LEGACY_SNAPSHOTS = "snapshots/bgbl_legacy_history_backfill"
DEFAULT_WINDOW_DAYS = 14
MIB = 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


# ------------------------------------------------------------------ hashing

class HashCache:
    """``(size, mtime_ns, inode)``-keyed SHA-256 cache for unchanged files."""

    def __init__(self, path: Path):
        self.path = Path(path)
        try:
            self.rows = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            self.rows = {}
        self.dirty = False

    def sha256(self, relative: str, path: Path) -> tuple[str, int]:
        info = path.stat()
        key = [info.st_size, info.st_mtime_ns, info.st_ino]
        row = self.rows.get(relative)
        if row and row[:3] == key:
            return row[3], info.st_size
        digest = sha256_file(path)
        self.rows[relative] = [*key, digest]
        self.dirty = True
        return digest, info.st_size

    def save(self, present: set[str]) -> None:
        for stale in set(self.rows) - present:
            self.rows.pop(stale)
            self.dirty = True
        if self.dirty:
            write_json_atomic(self.path, self.rows)


# ------------------------------------------------------------------ locking

def _refresh_lock(path: Path, wait: float, inherited_fd: int | None):
    """Take the shared refresh lock (or reuse the caller's descriptor)."""
    if inherited_fd is not None:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise SystemExit(f"refresh lock busy: {path}")
            time.sleep(5)


def _default_lock() -> Path:
    override = os.environ.get("KERF_REFRESH_LOCK")
    if override:
        return Path(override)
    if ROOT.is_relative_to("/srv"):
        return Path("/run/lock/kerf-refresh.lock")
    return Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "kerf-refresh.lock"


# --------------------------------------------------------------- VPS: manifest

def cmd_manifest(args) -> int:
    """List every archived file not yet receipted with its current hash."""
    data, offload = Path(args.data_root), Path(args.offload_root)
    receipted = receipted_hashes(offload)
    cache = HashCache(offload / "hash-cache.json")
    files, present, total = {}, set(), 0
    for relative, path in iter_files(data, ARCHIVED_PREFIXES):
        present.add(relative)
        digest, size = cache.sha256(relative, path)
        if digest in receipted.get(relative, set()):
            continue
        files[relative] = {"sha256": digest, "size": size}
        total += size
    cache.save(present)
    manifest = {"schema": MANIFEST_SCHEMA, "created_at": _now(),
                "files": files}
    name = f"{_stamp()}.json"
    target = offload / "manifests" / name
    write_json_atomic(target, manifest)
    print(json.dumps({"manifest": name, "sha256": sha256_file(target),
                      "files": len(files), "bytes": total}))
    return 0


# ------------------------------------------------------- archive: receipt

def cmd_receipt(args) -> int:
    manifest_path = Path(args.manifest)
    manifest = load_manifest(manifest_path)
    archive = Path(args.archive)
    cache = HashCache(Path(args.out).parent / "hash-cache.json")
    excluded, verified_bytes = [], 0
    for relative, entry in sorted(manifest["files"].items()):
        path = archive / relative
        if not path.is_file() or path.is_symlink():
            excluded.append(relative)
            continue
        digest, size = cache.sha256(relative, path)
        if digest != entry["sha256"] or size != entry["size"]:
            excluded.append(relative)
            continue
        verified_bytes += size
    cache.dirty and write_json_atomic(cache.path, cache.rows)
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "manifest": manifest_path.name,
        "manifest_sha256": sha256_file(manifest_path),
        "received_at": _now(),
        "verified_files": len(manifest["files"]) - len(excluded),
        "verified_bytes": verified_bytes,
        "excluded": excluded,
        "statement": ("every listed file except 'excluded' was re-hashed in "
                      "the source archive and matches the manifest"),
    }
    out = Path(args.out) / f"{manifest_path.stem}.receipt.json"
    write_json_atomic(out, receipt)
    sign_file(out, Path(args.key), RECEIPT_NAMESPACE)
    print(json.dumps({"receipt": out.name, "verified_files": receipt["verified_files"],
                      "verified_bytes": verified_bytes, "excluded": len(excluded)}))
    return 0 if not excluded or args.allow_excluded else 3


# ------------------------------------------------------- archive: attest

def cmd_attest(args) -> int:
    snapshot = Path(args.snapshot_dir)
    summary = json.loads((snapshot / "summary.json").read_text())
    attestation = build_attestation(
        snapshot, repo_root=ROOT, rebuilt_at=_now(), receipt=args.receipt,
        git_commit=args.git_commit,
        verified_documents=int(summary.get("verified_documents") or 0))
    target = snapshot / ATTESTATION_NAME
    write_json_atomic(target, attestation)
    sign_file(target, Path(args.key), ATTESTATION_NAMESPACE)
    print(json.dumps({"attested": snapshot.name,
                      "candidates": attestation["candidate_count"],
                      "parser": attestation["parser"]["sha256"][:12]}))
    return 0


# --------------------------------------------------------- VPS: install legacy

def cmd_install_legacy(args) -> int:
    """Move a verified, attested rebuild into the legacy snapshot family."""
    incoming = Path(args.incoming)
    fd = _refresh_lock(Path(args.lock_file), args.lock_wait, args.lock_fd)
    try:
        snapshot_name = json.loads((incoming / ATTESTATION_NAME).read_text())["snapshot"]
        staged = incoming.parent / snapshot_name
        if incoming != staged:
            if staged.exists():
                raise SystemExit(f"staging collision: {staged}")
            incoming.rename(staged)
        attestation = verify_attestation(staged)
        family = DATA / LEGACY_SNAPSHOTS
        target = family / attestation["snapshot"]
        superseded = None
        if target.exists():
            superseded = family / ".superseded" / f"{target.name}-{_stamp()}"
            superseded.parent.mkdir(parents=True, exist_ok=True)
            target.rename(superseded)
        family.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(target))
        verify_attestation(target)
        print(json.dumps({"installed": str(target),
                          "candidates": attestation["candidate_count"],
                          "superseded": str(superseded) if superseded else None}))
        return 0
    finally:
        if fd is not None:
            os.close(fd)


# ----------------------------------------------------------------- VPS: prune

def _gii_protected(snapshots: Path) -> set[str]:
    """GII days the daily refresh still archives (both JSONL files present)."""
    protected = set()
    gii = snapshots / "gii"
    if gii.is_dir():
        for day in gii.iterdir():
            if (day / "acts.jsonl").is_file() and (day / "norms.jsonl").is_file():
                protected.add(f"gii/{day.name}")
    return protected


def _newest_days(snapshots: Path, keep: int = 2) -> set[str]:
    newest = set()
    for family in snapshots.iterdir():
        if family.name.startswith(".") or not family.is_dir():
            continue
        days = sorted(p.name for p in family.iterdir()
                      if p.is_dir() and not p.name.startswith("."))
        newest.update(f"{family.name}/{day}" for day in days[-keep:])
    return newest


def plan_prune(data: Path, offload: Path, *, window_days: int,
               today: date | None = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=window_days)).isoformat()
    receipted = receipted_hashes(offload)
    cache = HashCache(offload / "hash-cache.json")
    plan = {"legacy_sources": [], "compact_objects": {}, "superseded": [],
            "blocked": {}, "cutoff": cutoff}

    def ok(relative: str, path: Path) -> bool:
        digest, _ = cache.sha256(relative, path)
        return digest in receipted.get(relative, set())

    # 1. Pre-2023 BGBl source bytes: never read by the VPS once the latest
    #    candidate snapshot carries a valid archive attestation.
    snapshots = data / "snapshots"
    legacy_family = data / LEGACY_SNAPSHOTS
    days = sorted(p for p in legacy_family.iterdir()
                  if p.is_dir() and not p.name.startswith(".")) \
        if legacy_family.is_dir() else []
    attested = False
    if days:
        try:
            verify_attestation(days[-1])
            attested = True
        except (ArchiveReceiptError, OSError, ValueError) as exc:
            plan["blocked"]["legacy_sources"] = (
                f"latest legacy snapshot {days[-1].name} is not attested: {exc}")
    else:
        plan["blocked"]["legacy_sources"] = "no legacy candidate snapshot"
    if attested:
        for relative, path in iter_files(data, LEGACY_SOURCE_PREFIXES):
            if ok(relative, path):
                plan["legacy_sources"].append((relative, path.stat().st_size))

    # 2. Compacted snapshot objects outside the window.
    if (snapshots / STORE_NAME / "manifest.json").is_file():
        manifest = load_snapshot_manifest(snapshots)
        protected = _gii_protected(snapshots) | _newest_days(snapshots)
        users: dict[str, list[str]] = {}
        for relative, record in manifest["files"].items():
            users.setdefault(record["sha256"], []).append(relative)
        for digest, obj in manifest["objects"].items():
            path = snapshots / STORE_NAME / obj["path"]
            if not path.is_file():
                continue
            owners = users.get(digest, [])
            if any(rel.split("/")[1] >= cutoff or
                   "/".join(rel.split("/")[:2]) in protected for rel in owners):
                continue
            relative = f"snapshots/{STORE_NAME}/{obj['path']}"
            if ok(relative, path):
                plan["compact_objects"][digest] = path.stat().st_size

    # 3. Superseded legacy rebuilds older than the window.
    superseded = legacy_family / ".superseded"
    if superseded.is_dir():
        for relative, path in iter_files(data, (f"{LEGACY_SNAPSHOTS}/.superseded",)):
            day = relative.split("/")[3][:10]
            if day < cutoff and ok(relative, path):
                plan["superseded"].append((relative, path.stat().st_size))
    cache.save(set(cache.rows))
    return plan


def cmd_prune(args) -> int:
    data, offload = Path(args.data_root), Path(args.offload_root)
    fd = None if not args.apply else _refresh_lock(
        Path(args.lock_file), args.lock_wait, args.lock_fd)
    try:
        before = shutil.disk_usage(data).free
        plan = plan_prune(data, offload, window_days=args.window_days)
        legacy_bytes = sum(size for _, size in plan["legacy_sources"])
        superseded_bytes = sum(size for _, size in plan["superseded"])
        compact_bytes = sum(plan["compact_objects"].values())
        result = {
            "apply": bool(args.apply), "window_days": args.window_days,
            "cutoff": plan["cutoff"],
            "legacy_source_files": len(plan["legacy_sources"]),
            "legacy_source_mib": round(legacy_bytes / MIB, 1),
            "compact_objects": len(plan["compact_objects"]),
            "compact_object_mib": round(compact_bytes / MIB, 1),
            "superseded_files": len(plan["superseded"]),
            "superseded_mib": round(superseded_bytes / MIB, 1),
            "blocked": plan["blocked"],
        }
        if args.apply:
            receipts = sorted((offload / "receipts").glob("*.json"))
            receipt_label = receipts[-1].name if receipts else "workstation-receipt"
            if plan["compact_objects"]:
                offload_objects(data / "snapshots",
                                {d: receipt_label for d in plan["compact_objects"]},
                                at=_now(), dry_run=False)
            for relative, _size in plan["legacy_sources"] + plan["superseded"]:
                (data / relative).unlink()
            for folder in ("legacy_bgbl_history/objects",
                           "legacy_bgbl_history/texts",
                           f"{LEGACY_SNAPSHOTS}/.superseded"):
                base = data / folder
                if base.is_dir():
                    for directory, _dirs, _names in sorted(
                            os.walk(base), key=lambda row: -len(row[0])):
                        if Path(directory) != base and not os.listdir(directory):
                            os.rmdir(directory)
            log = offload / "prune-log.jsonl"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as stream:
                stream.write(json.dumps({**result, "at": _now()}) + "\n")
        after = shutil.disk_usage(data).free
        result["freed_mib"] = round((after - before) / MIB, 1)
        result["free_mib_after"] = round(after / MIB)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ArchiveReceiptError, SnapshotArchiveError) as exc:
        print(f"archive prune failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if fd is not None:
            os.close(fd)


def cmd_status(args) -> int:
    data, offload = Path(args.data_root), Path(args.offload_root)
    receipts = sorted((offload / "receipts").glob("*.json")) \
        if (offload / "receipts").is_dir() else []
    receipted = receipted_hashes(offload)
    legacy = sorted(p for p in (data / LEGACY_SNAPSHOTS).iterdir()
                    if p.is_dir() and not p.name.startswith(".")) \
        if (data / LEGACY_SNAPSHOTS).is_dir() else []
    attestation = None
    if legacy:
        try:
            value = verify_attestation(legacy[-1])
            attestation = {"snapshot": value["snapshot"],
                           "candidates": value["candidate_count"],
                           "rebuilt_at": value["rebuilt_at"]}
        except (ArchiveReceiptError, OSError, ValueError) as exc:
            attestation = {"error": str(exc)}
    print(json.dumps({"receipts": len(receipts),
                      "last_receipt": receipts[-1].name if receipts else None,
                      "receipted_paths": len(receipted),
                      "legacy_attestation": attestation}, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-root", type=Path, default=DATA)
    parser.add_argument("--offload-root", type=Path, default=OFFLOAD)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("manifest").set_defaults(func=cmd_manifest)
    sub.add_parser("status").set_defaults(func=cmd_status)

    receipt = sub.add_parser("receipt")
    receipt.add_argument("--manifest", required=True, type=Path)
    receipt.add_argument("--archive", required=True, type=Path)
    receipt.add_argument("--key", required=True, type=Path)
    receipt.add_argument("--out", required=True, type=Path)
    receipt.add_argument("--allow-excluded", action="store_true")
    receipt.set_defaults(func=cmd_receipt)

    attest = sub.add_parser("attest")
    attest.add_argument("--snapshot-dir", required=True, type=Path)
    attest.add_argument("--key", required=True, type=Path)
    attest.add_argument("--receipt")
    attest.add_argument("--git-commit")
    attest.set_defaults(func=cmd_attest)

    for name, func in (("prune", cmd_prune), ("install-legacy", cmd_install_legacy)):
        command = sub.add_parser(name)
        command.add_argument("--lock-file", type=Path, default=_default_lock())
        command.add_argument("--lock-fd", type=int)
        command.add_argument("--lock-wait", type=float, default=3600)
        command.set_defaults(func=func)
        if name == "prune":
            command.add_argument("--apply", action="store_true")
            command.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS)
        else:
            command.add_argument("incoming", type=Path)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
