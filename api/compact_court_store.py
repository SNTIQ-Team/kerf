"""Read retained originals for a compact court index from its immutable Hub release."""

from contextlib import closing
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from api.court_observations import stamp
from tools.read_court_archive import CourtArchive

DATABASE = "decision_passages.compact.sqlite"
DESCRIPTOR = "decision_passages.compact.json"
PROFILE = "kerf-compact-court-index/1"


def description(root: Path, base_digest: str):
    data = json.loads((root / DESCRIPTOR).read_bytes())
    if (
        data.get("profile") != PROFILE
        or data.get("public_database_sha256") != base_digest
        or not re.fullmatch(r"[0-9a-f]{64}", data.get("compact_sha256", ""))
        or not re.fullmatch(r"[0-9a-f]{40}", data.get("revision", ""))
    ):
        raise ValueError("compact court index belongs to another release")
    return data


def verified_index(root):
    database = root / "decision_passages.sqlite"
    compact_stamp = None
    if not database.exists():
        database = root / DATABASE
        compact_stamp = stamp(root / DESCRIPTOR)
    manifest = _verified(
        root,
        database,
        stamp(database),
        stamp(root / "decision_passages.json"),
        compact_stamp,
    )
    return database, manifest


@lru_cache(maxsize=8)
def _verified(root, database, database_stamp, manifest_stamp, compact_stamp):
    manifest = json.loads((root / "decision_passages.json").read_bytes())
    if (
        manifest.get("schema_version") != 2
        or manifest.get("profile") != "official-passages/2"
    ):
        raise ValueError("invalid court passage manifest")
    canonical_hash = manifest["database_sha256"]
    config = description(root, canonical_hash) if compact_stamp is not None else None
    expected = config["compact_sha256"] if config else canonical_hash
    with database.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError("court passage database hash mismatch")
    if config:
        with closing(
            sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as conn:
            binding = conn.execute(
                "SELECT revision,public_database_sha256 FROM court_archive"
            ).fetchall()
        if binding != [(config["revision"], canonical_hash)]:
            raise ValueError("compact archive binding mismatch")
    return manifest


@lru_cache(maxsize=3)
def pinned_archive(revision, base_digest):
    archive = CourtArchive(revision)
    if archive.public_database_sha256 != base_digest:
        raise ValueError("court archive belongs to another passage database")
    return archive


def read_source(conn, decision_id, base_digest):
    binding = conn.execute(
        "SELECT revision, public_database_sha256 FROM court_archive"
    ).fetchall()
    if len(binding) != 1 or binding[0][1] != base_digest:
        raise ValueError("invalid binding to public court archive")
    return pinned_archive(binding[0][0], base_digest).read(decision_id, original=True)
