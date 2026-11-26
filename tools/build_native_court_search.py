"""Build public and operator-only whole-decision FTS indexes from a native Kerf ledger.

Every public decision must have an exact original hash in the pinned public RII
release. The all-decisions database is for the authenticated operator endpoint.
Both store search postings and minimal decision metadata, without the judgments'
full text. A release directory is moved into place only after both builds pass.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import urlsplit

FORMAT = "kerf-native-court-search/1"


def public_originals(directory: Path, expected_hash: str) -> dict[str, set[str]]:
    raw = (directory / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise ValueError("Public RII manifest SHA-256 mismatch")
    manifest = json.loads(raw)
    if manifest["format"] != "kerf-rii-publications/1":
        raise ValueError("Unexpected public RII manifest format")
    found = {}
    for path, item in manifest["files"].items():
        if not re.fullmatch(r"lookup/[0-9a-f]{2}\.json\.gz", path):
            continue
        content = (directory / path).read_bytes()
        if len(content) != item["size"] or hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError(f"Corrupt public lookup {path}")
        for document, entry in json.loads(gzip.decompress(content)).items():
            if document in found:
                raise ValueError("Duplicate document in public lookup")
            found[document] = {s for s in [entry.get("zip_sha256"), entry.get("xml_sha256")] if s}
    if not found:
        raise ValueError("Empty public lookup")
    return found


def new_index(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.executescript("""
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        PRAGMA temp_store=MEMORY;
        CREATE TABLE documents (
            ordinal INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, ecli TEXT,
            source_id TEXT NOT NULL, document TEXT NOT NULL, court TEXT NOT NULL,
            decision_date TEXT, docket TEXT, source_url TEXT NOT NULL
        );
        CREATE TABLE metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
        CREATE VIRTUAL TABLE terms USING fts5(
            text, content='', detail=none, columnsize=0, tokenize='unicode61 remove_diacritics 2'
        );
    """)
    return db


def build(source: Path, output: Path, public: dict[str, set[str]], manifest_hash: str) -> None:
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".court-search-") as tmp:
        stage = Path(tmp) / "index"
        stage.mkdir()
        db_all = new_index(stage / "operator.sqlite")
        db_public = new_index(stage / "public.sqlite")
        read = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
        read.execute("PRAGMA query_only=ON")
        read.execute("BEGIN")
        stats = Counter()
        try:
            # Ordering the rowids this way makes common terms pageable by date
            # directly in FTS posting order without sorting every search hit.
            items = read.execute("""
                SELECT id,ecli,source_id,source_doc_id,court_name,decision_date,
                       case_numbers_raw,source_url,content_hash,paragraph_count
                FROM decisions ORDER BY decision_date DESC,id ASC
            """)
            for row in items:
                ident, ecli, source_id, document, court, day, docket, url, digest, expected = row
                parts = [r[0] for r in read.execute(
                    "SELECT text FROM paragraphs WHERE decision_id=? ORDER BY ordinal", (ident,))]
                if len(parts) != expected:
                    raise ValueError(f"Paragraph count mismatch for {ident}")
                fulltext = " ".join([ecli or "", court or "", docket or "", *parts])
                is_public = (source_id == "rii" and urlsplit(url or "").hostname == "www.rechtsprechung-im-internet.de"
                             and digest in public.get(document, ()))
                targets = [(db_all, "operator")]
                if is_public:
                    targets.append((db_public, "public"))
                for db, scope in targets:
                    stats[scope] += 1
                    ordinal = stats[scope]
                    db.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
                               (ordinal, ident, ecli, source_id, document, court, day, docket, url))
                    db.execute("INSERT INTO terms(rowid,text) VALUES(?,?)", (ordinal, fulltext))
                stats["paragraphs"] += len(parts)
                if stats["operator"] % 25000 == 0:
                    for db in (db_all, db_public):
                        db.commit()
                    print(stats["operator"], stats["public"], flush=True)
            revision = hashlib.sha256((manifest_hash + datetime.now(timezone.utc).isoformat()).encode()).hexdigest()[:20]
            for db, scope in [(db_all, "operator"), (db_public, "public")]:
                if not stats[scope]:
                    raise ValueError(f"Empty {scope} index")
                db.executemany("INSERT INTO metadata VALUES(?,?)", {
                    "format": FORMAT, "scope": scope, "revision": revision,
                    "decisions": str(stats[scope]), "rii_manifest_sha256": manifest_hash,
                }.items())
                db.execute("INSERT INTO terms(terms) VALUES('optimize')")
                db.execute("INSERT INTO terms(terms) VALUES('integrity-check')")
                db.commit()
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError(f"Invalid {scope} index")
        finally:
            db_all.close()
            db_public.close()
            read.close()
        files = {}
        for f in sorted(stage.iterdir()):
            with f.open("rb") as fd:
                files[f.name] = {"sha256": hashlib.file_digest(fd, "sha256").hexdigest(), "bytes": f.stat().st_size}
            os.chmod(f, 0o600)
        (stage / "manifest.json").write_text(json.dumps({"format": FORMAT, "revision": revision,
            "rii_manifest_sha256": manifest_hash, "counts": dict(stats), "files": files}, indent=2) + "\n")
        stage.rename(output)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--rii-release", required=True, type=Path)
    ap.add_argument("--rii-manifest-sha256", required=True)
    args = ap.parse_args()
    build(args.ledger, args.output, public_originals(args.rii_release, args.rii_manifest_sha256), args.rii_manifest_sha256)
