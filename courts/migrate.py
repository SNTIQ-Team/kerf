"""Move legacy per-file captures (raw/<source>/<xx>/<sha256>.<ext>.gz) into packs.

Steps: (1) append every legacy file to its source's pack (idempotent; hash
checked before appending); (2) point decisions.capture_path at the pack
records; (3) verify every capture in the index against its sha256 and that
every decision and every legacy file is covered; (4) only if all checks pass
and ``delete_legacy`` is set, delete the legacy files and empty directories.
"""
from __future__ import annotations

import gzip
import hashlib
import sqlite3
from pathlib import Path

from .packs import CaptureIntegrityError, PackStore, scan_pack


def legacy_files(home: Path) -> list[Path]:
    raw = home / "raw"
    return sorted(raw.rglob("*.gz")) if raw.exists() else []


def migrate_legacy_captures(conn: sqlite3.Connection, home: Path, *, delete_legacy: bool = False) -> dict:
    home = Path(home)
    store = PackStore(home, conn)
    files = legacy_files(home)
    report = {"legacy_files": len(files), "packed": 0, "already_packed": 0, "bad_legacy": [],
              "decisions_repointed": 0, "verified": 0, "verify_errors": [], "deleted": 0}
    legacy_hashes: set[str] = set()
    for path in files:
        source_id = path.relative_to(home / "raw").parts[0]
        digest, extension = path.name.split(".")[0], path.name.split(".")[1]
        data = gzip.decompress(path.read_bytes())
        if hashlib.sha256(data).hexdigest() != digest:
            report["bad_legacy"].append(str(path.relative_to(home)))
            continue
        legacy_hashes.add(digest)
        if store.lookup(digest) is not None:
            report["already_packed"] += 1
            continue
        store.put(data, source_id, extension)
        report["packed"] += 1
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute("""UPDATE decisions SET capture_path = (
                              SELECT c.pack || '@' || c.offset FROM captures c WHERE c.sha256 = decisions.content_hash)
                          WHERE capture_path LIKE 'raw/%'
                            AND EXISTS (SELECT 1 FROM captures c WHERE c.sha256 = decisions.content_hash)""")
    report["decisions_repointed"] = cur.rowcount
    conn.execute("COMMIT")
    report.update(verify(conn, home, expected=legacy_hashes))
    if delete_legacy:
        if report["verify_errors"] or report["bad_legacy"]:
            report["deleted"] = 0
            report["delete_skipped"] = "verification failed; legacy files kept"
        else:
            for path in files:
                path.unlink()
                report["deleted"] += 1
            for directory in sorted((home / "raw").rglob("*"), key=lambda p: len(p.parts), reverse=True):
                if directory.is_dir() and not any(directory.iterdir()):
                    directory.rmdir()
            if (home / "raw").exists() and not any((home / "raw").iterdir()):
                (home / "raw").rmdir()
    return report


def verify(conn: sqlite3.Connection, home: Path, *, expected: set[str] = frozenset()) -> dict:
    """Read back every indexed capture, and check decisions and packs against the index."""
    store = PackStore(home, conn)
    errors: list[str] = []
    verified = 0
    indexed: set[str] = set()
    for row in conn.execute("SELECT sha256, pack, offset, stored_size, raw_size, codec FROM captures"):
        indexed.add(row[0])
        try:
            from .packs import CaptureRef
            store.read(CaptureRef(*row))
            verified += 1
        except (OSError, CaptureIntegrityError) as exc:
            errors.append(f"{row[0]}: {exc}")
    for (content_hash,) in conn.execute("SELECT content_hash FROM decisions"):
        if content_hash not in indexed:
            errors.append(f"decision capture {content_hash} not in pack index")
    for digest in expected - indexed:
        errors.append(f"legacy capture {digest} not in pack index")
    for (bad,) in conn.execute("SELECT count(*) FROM decisions WHERE capture_path LIKE 'raw/%'"):
        if bad:
            errors.append(f"{bad} decisions still point at legacy raw/ files")
    packs = sorted((home / "packs").rglob("*.pack")) if (home / "packs").exists() else []
    scanned = {digest for pack in packs for _, digest, *_ in scan_pack(pack)}
    for digest in indexed - scanned:
        errors.append(f"indexed capture {digest} not found by scanning the packs")
    return {"verified": verified, "verify_errors": errors[:50], "verify_error_count": len(errors),
            "pack_files": len(packs), "pack_bytes": sum(p.stat().st_size for p in packs)}
