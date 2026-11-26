"""Build an optional search index whose court originals are served from a pinned Hub release."""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from api.compact_court_store import DATABASE, DESCRIPTOR, PROFILE, pinned_archive
from api.court_observations import FILENAME, load_release, stamp


def file_sha(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def compact(web, output, revision):
    web = web.resolve()
    original = web / "decision_passages.sqlite"
    manifest = json.loads((web / "decision_passages.json").read_bytes())
    digest = file_sha(original)
    if (
        digest != manifest.get("database_sha256")
        or manifest.get("profile") != "official-passages/2"
    ):
        raise ValueError("the source index differs from its release receipt")
    # The public feed is prepared while originals are available; its canonical binding stays intact.
    evidence = manifest["public_observations"]
    rows = load_release(str(web), stamp(web / FILENAME), evidence["sha256"], digest)
    if len(rows) != evidence["count"]:
        raise ValueError("the public observation count differs")
    archive = pinned_archive(revision, digest)
    output = output.resolve()
    if output.exists():
        raise FileExistsError(str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        build = Path(temp) / "index"
        build.mkdir()
        target = build / DATABASE
        with closing(
            sqlite3.connect(original.as_uri() + "?mode=ro&immutable=1", uri=True)
        ) as src:
            with closing(sqlite3.connect(target)) as dest:
                src.backup(dest)
                ids = set()
                for key, raw, source_hash in dest.execute(
                    "SELECT id,source_zip,content_sha256 FROM decisions WHERE status='indexed'"
                ):
                    if (
                        not raw
                        or hashlib.sha256(raw).hexdigest() != source_hash
                        or archive.rows[key]["original"]["sha256"] != source_hash
                    ):
                        raise ValueError(f"source digest mismatch for {key}")
                    ids.add(key)
                if (
                    ids != set(archive.rows)
                    or len(ids) != manifest["indexed_decisions"]
                ):
                    raise ValueError(
                        "the court catalogue differs from the source index"
                    )
                mismatch = dest.execute(
                    "SELECT 1 FROM passages WHERE json_extract(data,'$.text') IS NOT text LIMIT 1"
                ).fetchone()
                if mismatch:
                    raise ValueError("searchable text differs from passage data")
                # Save text once. FTS content and rowids stay stable across compaction.
                dest.execute("UPDATE passages SET data=json_remove(data,'$.text')")
                dest.execute(
                    "UPDATE decisions SET source_zip=NULL WHERE status='indexed'"
                )
                dest.execute(
                    "CREATE TABLE court_archive(revision TEXT NOT NULL,public_database_sha256 TEXT NOT NULL)"
                )
                dest.execute(
                    "INSERT INTO court_archive VALUES(?,?)", (revision, digest)
                )
                dest.commit()
                dest.execute("VACUUM")
                key_order = (
                    "SELECT rowid,decision_id,passage_id FROM passages ORDER BY rowid"
                )
                if any(
                    left != right
                    for left, right in zip(
                        src.execute(key_order), dest.execute(key_order), strict=True
                    )
                ):
                    raise ValueError(
                        "the compact index changed the FTS passage addresses"
                    )
                if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("compact database failed SQLite integrity check")
        descriptor = dict(
            profile=PROFILE,
            revision=revision,
            public_database_sha256=digest,
            compact_sha256=file_sha(target),
            bytes=target.stat().st_size,
        )
        (build / DESCRIPTOR).write_text(json.dumps(descriptor, sort_keys=True) + "\n")
        for name in ("decision_passages.json", FILENAME):
            shutil.copyfile(web / name, build / name)
        build.rename(output)
    return descriptor


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("web", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("revision")
    args = parser.parse_args()
    print(json.dumps(compact(args.web, args.output, args.revision)))
