#!/usr/bin/env python3
"""Export the archive worker history archive into a compact, signed bundle.

The raw Wayback/NeuRIS/BAYERN.RECHT bytes stay on the archive worker
(``~/.local/share/sntiq-legal/lexgraph-archive/history``).  The VPS receives
only this bundle:

``corpus.sqlite``
    one row per parsed capture (source, role, capture time, archive URL,
    raw sha256, Stand block) and its ordered norm list, with every distinct
    norm wording stored once (gzip) under its whitespace-free identity.
``change_dates.jsonl.gz``
    date resolutions from the promulgation documents and commencement
    clauses (optional; produced by tools/resolve_change_dates.py).
``bavarian.jsonl.gz``
    Bavarian version/change records (optional).
``manifest.json`` (+ ``manifest.json.sig``)
    sha256 of every file, counts, parser versions, build time.

Usage:
    python3 tools/history_bundle.py export --archive <R> --out <dir>
        [--change-dates f.jsonl[.gz]] [--bavarian f.jsonl[.gz]]
        [--key ~/.config/sntiq/lexgraph-archive-signing-key]
    python3 tools/history_bundle.py verify <dir>
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

from api.history_identity import norm_identity  # noqa: E402

BUNDLE_SCHEMA = "lexgraph-history-bundle/1"
CORPUS_SCHEMA = "lexgraph-history-corpus/1"
SIGN_NAMESPACE = "lexgraph-history-bundle"
DEFAULT_ARCHIVE = Path(os.environ.get(
    "LEXGRAPH_HISTORY_ARCHIVE",
    Path.home() / ".local/share/sntiq-legal/lexgraph-archive/history"))
ROLE_BY_SOURCE = {
    "wayback_gii": "archived_capture",
    "wayback_gii_xml": "archived_capture",
    "wayback_bayern": "archived_capture",
    "neuris": "official_consolidation",
    "bayern_recht": "official_consolidation",
    "gii": "official_consolidation",
}
DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE captures(
    capture_id TEXT PRIMARY KEY, act_id TEXT NOT NULL, source TEXT NOT NULL,
    source_role TEXT NOT NULL, original_url TEXT, archive_url TEXT,
    captured_at TEXT NOT NULL, sha256 TEXT NOT NULL, stand TEXT,
    complete INTEGER NOT NULL, parser TEXT, norm_count INTEGER NOT NULL,
    warnings_json TEXT NOT NULL);
CREATE INDEX captures_act ON captures(act_id, captured_at);
CREATE TABLE capture_texts(
    text_id INTEGER PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
    enbez TEXT, titel TEXT, body BLOB NOT NULL);
CREATE TABLE capture_norms(
    capture_id TEXT NOT NULL, ord INTEGER NOT NULL, text_id INTEGER NOT NULL,
    PRIMARY KEY(capture_id, ord));
"""


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stand_text(stand: Any) -> str | None:
    """Parsed ``stand`` → the flattened ``Typ: text | …`` form of GII."""
    if isinstance(stand, str):
        return stand or None
    if not isinstance(stand, dict):
        return None
    entries = stand.get("entries") or []
    if entries:
        return " | ".join(f"{e.get('type')}: {e.get('text')}"
                          for e in entries if e.get("text")) or None
    return stand.get("raw") or None


class CorpusWriter:
    def __init__(self, path: Path):
        if path.exists():
            path.unlink()
        self.db = sqlite3.connect(path)
        self.db.executescript(DDL)
        self.ids: dict[str, int] = {}
        self.captures = 0

    def add(self, parsed: dict[str, Any]) -> bool:
        source = str(parsed.get("source") or "")
        sha = str(parsed.get("sha256") or "")
        act_id = str(parsed.get("act_id") or "")
        captured = str(parsed.get("captured_at") or "")
        if not (source and sha and act_id and captured):
            return False
        capture_id = f"{source}:{sha}"
        if self.db.execute("SELECT 1 FROM captures WHERE capture_id=?",
                           (capture_id,)).fetchone():
            return False
        norms = [n for n in parsed.get("norms") or [] if isinstance(n, dict)
                 and str(n.get("enbez") or "").strip()]
        role = str(parsed.get("source_role") or ROLE_BY_SOURCE.get(
            source, "archived_capture"))
        self.db.execute(
            "INSERT INTO captures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (capture_id, act_id, source, role, parsed.get("original_url"),
             parsed.get("archive_url"), captured, sha,
             _stand_text(parsed.get("stand")),
             int(bool(parsed.get("complete", True))), parsed.get("parser"),
             len(norms), json.dumps(parsed.get("warnings") or [],
                                    ensure_ascii=False)))
        for order, norm in enumerate(norms):
            identity = norm_identity(norm.get("enbez"), norm.get("titel"),
                                     norm.get("text"))
            text_id = self.ids.get(identity)
            if text_id is None:
                cursor = self.db.execute(
                    "INSERT INTO capture_texts(identity, enbez, titel, body) "
                    "VALUES (?,?,?,?)",
                    (identity, str(norm.get("enbez") or ""),
                     str(norm.get("titel") or ""),
                     gzip.compress(str(norm.get("text") or "").encode(),
                                   compresslevel=9, mtime=0)))
                text_id = int(cursor.lastrowid)
                self.ids[identity] = text_id
            self.db.execute("INSERT INTO capture_norms VALUES (?,?,?)",
                            (capture_id, order, text_id))
        self.captures += 1
        return True

    def close(self, meta: dict[str, Any]) -> None:
        self.db.executemany("INSERT INTO meta VALUES (?,?)", [
            (key, value if isinstance(value, str) else json.dumps(value))
            for key, value in {"schema": CORPUS_SCHEMA, **meta}.items()])
        self.db.commit()
        self.db.execute("VACUUM")
        self.db.close()


def iter_parsed(archive: Path, sources: Iterable[str] | None = None
                ) -> Iterator[dict[str, Any]]:
    root = archive / "parsed"
    if not root.is_dir():
        return
    wanted = set(sources or [])
    for source_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if wanted and source_dir.name not in wanted:
            continue
        for path in sorted(source_dir.rglob("*.json.gz")):
            try:
                parsed = json.loads(gzip.decompress(path.read_bytes()))
            except (OSError, ValueError):
                continue
            parsed.setdefault("source", source_dir.name)
            yield parsed


def export(archive: Path, out: Path, *, change_dates: Path | None = None,
           bavarian: Path | None = None, key: Path | None = None,
           acts: set[str] | None = None) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    corpus = out / "corpus.sqlite"
    writer = CorpusWriter(corpus)
    per_act: dict[str, int] = {}
    parsers: set[str] = set()
    for parsed in iter_parsed(archive):
        if acts and parsed.get("act_id") not in acts:
            continue
        if writer.add(parsed):
            per_act[parsed["act_id"]] = per_act.get(parsed["act_id"], 0) + 1
            parsers.add(str(parsed.get("parser") or ""))
    built = datetime.now(timezone.utc).isoformat(timespec="seconds"
                                                 ).replace("+00:00", "Z")
    writer.close({"built_at": built, "captures": writer.captures,
                  "texts": len(writer.ids), "per_act": per_act})
    files = {"corpus.sqlite": corpus}
    for name, source in (("change_dates.jsonl.gz", change_dates),
                         ("bavarian.jsonl.gz", bavarian)):
        target = out / name
        if source:
            if str(source).endswith(".gz"):
                shutil.copyfile(source, target)
            else:
                with open(source, "rb") as src, gzip.open(
                        target, "wb", compresslevel=9) as dst:
                    shutil.copyfileobj(src, dst)
            files[name] = target
        elif target.exists():
            target.unlink()
    manifest = {
        "schema": BUNDLE_SCHEMA, "built_at": built,
        "files": {name: {"sha256": _sha_file(path),
                         "bytes": path.stat().st_size}
                  for name, path in files.items()},
        "captures": writer.captures, "texts": len(writer.ids),
        "per_act": per_act, "parsers": sorted(p for p in parsers if p),
        "sources": sorted({s for s in ("wayback_gii", "neuris",
                                       "wayback_bayern", "bayern_recht")}),
    }
    manifest["sha256"] = hashlib.sha256(json.dumps(
        manifest["files"], sort_keys=True).encode()).hexdigest()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1,
                                                  sort_keys=True) + "\n")
    if key:
        from archive_receipts import sign_file
        sign_file(out / "manifest.json", key, SIGN_NAMESPACE)
    return manifest


def verify(bundle: Path, *, signers: Path | None = None) -> dict[str, Any]:
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise SystemExit("unsupported bundle schema")
    for name, meta in manifest["files"].items():
        if _sha_file(bundle / name) != meta["sha256"]:
            raise SystemExit(f"bundle file {name} failed its SHA-256 check")
    signature = bundle / "manifest.json.sig"
    if signers is not None:
        from archive_receipts import verify_signature
        if not signature.is_file():
            raise SystemExit("bundle manifest is not signed")
        verify_signature((bundle / "manifest.json").read_bytes(), signature,
                         SIGN_NAMESPACE, signers)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    ex = sub.add_parser("export")
    ex.add_argument("--archive", type=Path, default=DEFAULT_ARCHIVE)
    ex.add_argument("--out", type=Path, required=True)
    ex.add_argument("--change-dates", type=Path)
    ex.add_argument("--bavarian", type=Path)
    ex.add_argument("--key", type=Path)
    ex.add_argument("--acts", default="")
    ve = sub.add_parser("verify")
    ve.add_argument("bundle", type=Path)
    ve.add_argument("--signers", type=Path)
    args = parser.parse_args(argv)
    if args.command == "export":
        manifest = export(args.archive, args.out,
                          change_dates=args.change_dates,
                          bavarian=args.bavarian, key=args.key,
                          acts={a for a in args.acts.split(",") if a} or None)
    else:
        manifest = verify(args.bundle, signers=args.signers)
    print(json.dumps({k: manifest.get(k) for k in (
        "built_at", "captures", "texts", "per_act", "sha256")},
        ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
