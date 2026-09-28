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
import zlib
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
    "gvbl": "official_promulgation",
    "bgbl": "official_promulgation",
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
    warnings_json TEXT NOT NULL, first_captured_at TEXT,
    last_captured_at TEXT, capture_count INTEGER NOT NULL DEFAULT 1,
    captures_json TEXT NOT NULL DEFAULT '[]', layout TEXT,
    source_identifiers_json TEXT, norms_blob BLOB NOT NULL);
CREATE INDEX captures_act ON captures(act_id, captured_at);
CREATE TABLE capture_texts(
    text_id INTEGER PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
    enbez TEXT, titel TEXT, body BLOB NOT NULL);
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


def _captures_json(captures: list[Any], limit: int = 20000) -> str:
    """Capture list as JSON of at most ``limit`` characters.

    Drops trailing captures instead of cutting the string, so the value is
    always valid JSON.  The builder reads only each capture's ``kind``.
    """
    items = list(captures)
    text = json.dumps(items, ensure_ascii=False)
    while len(text) > limit and items:
        keep = max(0, min(len(items) - 1, len(items) * limit // len(text)))
        items = items[:keep]
        text = json.dumps(items, ensure_ascii=False)
    return text


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
        captures = parsed.get("captures") or []
        first = str(parsed.get("first_captured_at") or captured)
        last = str(parsed.get("last_captured_at") or captured)
        text_ids = []
        for norm in norms:
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
            text_ids.append(text_id)
        blob = zlib.compress(json.dumps(text_ids).encode(), 9)
        self.db.execute(
            "INSERT INTO captures VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (capture_id, act_id, source, role, parsed.get("original_url"),
             parsed.get("archive_url"), first, sha,
             _stand_text(parsed.get("stand")),
             int(bool(parsed.get("complete", True))), parsed.get("parser"),
             len(norms), json.dumps(parsed.get("warnings") or [],
                                    ensure_ascii=False),
             first, last, max(1, len(captures)),
             _captures_json(captures),
             parsed.get("layout"),
             json.dumps(parsed.get("source_identifiers"), ensure_ascii=False)
             if parsed.get("source_identifiers") else None, blob))
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


def _slim_date(value: dict | None, *, clause: bool = False) -> dict | None:
    if not isinstance(value, dict):
        return None
    keep = ("date", "status", "basis", "reason", "retroactive",
            "retroactive_phrase", "alternative_reading", "computation_rule",
            "issue")
    out = {k: value[k] for k in keep if value.get(k) not in (None, "", [])}
    if clause and value.get("clause"):
        out["clause"] = " ".join(str(value["clause"]).split())[:900]
    evidence = []
    for item in (value.get("evidence") or [])[:2]:
        if isinstance(item, dict):
            evidence.append({k: item.get(k) for k in (
                "kind", "snippet", "document_id", "text_sha256",
                "pdf_sha256", "page_index") if item.get(k) is not None})
    if evidence:
        out["evidence"] = evidence
    checks = [{k: c.get(k) for k in ("source", "field", "value", "status")}
              for c in (value.get("cross_checks") or [])[:3]
              if isinstance(c, dict)]
    if checks:
        out["cross_checks"] = checks
    dip = value.get("dip_cross_check")
    if isinstance(dip, dict):
        out["dip_cross_check"] = {k: dip.get(k) for k in (
            "status", "dip_date", "printed_date") if dip.get(k) is not None}
    return out


def change_date_records(directory: Path, target: Path) -> int:
    """resolutions.jsonl + documents.jsonl of tools/resolve_change_dates.py
    → compact rows.  Adoption/promulgation are per document; the in-force
    and applies-from results are interned per document (items of one rule
    share one payload), so the builder holds ~40k items in a few MB."""
    documents: dict[str, dict] = {}
    path = directory / "documents.jsonl"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                documents[str(row.get("document_id"))] = {
                    "kind": "document", "document_id": row.get("document_id"),
                    "era": row.get("era"), "title": row.get("title"),
                    "adopted": _slim_date(row.get("adopted")),
                    "promulgated": _slim_date(row.get("promulgated")),
                    "variants": {}}
    items: list[dict] = []
    seen: set[tuple] = set()
    with (directory / "resolutions.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            item = row.get("item") or {}
            ref = row.get("ref") or {}
            doc_id = str(row.get("document_id"))
            key = (doc_id, row.get("amending_article"), item.get("absatz"),
                   item.get("nummer"), item.get("buchstabe"),
                   item.get("doppelbuchstabe"), ref.get("para"),
                   row.get("slug"))
            if key in seen:
                continue
            seen.add(key)
            document = documents.setdefault(doc_id, {
                "kind": "document", "document_id": doc_id, "variants": {}})
            dates = row.get("dates") or {}
            if not document.get("adopted"):
                document["adopted"] = _slim_date(dates.get("adopted"))
            if not document.get("promulgated"):
                document["promulgated"] = _slim_date(dates.get("promulgated"))
            refs = {}
            for field in ("in_force", "applies_from"):
                payload = _slim_date(dates.get(field), clause=True)
                blob = json.dumps(payload, ensure_ascii=False,
                                  sort_keys=True)
                variants = document["variants"]
                if blob not in variants:
                    variants[blob] = str(len(variants))
                refs[field] = variants[blob]
            items.append({
                "kind": "item", "document_id": doc_id,
                "article": row.get("amending_article"),
                "slug": row.get("slug"), "absatz": item.get("absatz"),
                "nummer": item.get("nummer"),
                "buchstabe": item.get("buchstabe"),
                "doppelbuchstabe": item.get("doppelbuchstabe"),
                "ref_para": ref.get("para"),
                "change_type": row.get("change_type"),
                "in_force": refs["in_force"],
                "applies_from": refs["applies_from"]})
    count = 0
    with gzip.open(target, "wt", encoding="utf-8", compresslevel=9) as out:
        for document in documents.values():
            document["variants"] = {ident: json.loads(blob) for blob, ident
                                    in document["variants"].items()}
            out.write(json.dumps(document, ensure_ascii=False,
                                 sort_keys=True) + "\n")
            count += 1
        for item in items:
            out.write(json.dumps(item, ensure_ascii=False, sort_keys=True)
                      + "\n")
            count += 1
    return count


def bavarian_records(directory: Path, target: Path) -> int:
    """versions/changes/findings.jsonl of tools/bavarian_history.py → one
    typed JSONL (``record_type`` version | change | finding)."""
    count = 0
    with gzip.open(target, "wt", encoding="utf-8", compresslevel=9) as out:
        for name, kind in (("versions.jsonl", "version"),
                           ("changes.jsonl", "change"),
                           ("findings.jsonl", "finding")):
            path = directory / name
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    row["record_type"] = kind
                    out.write(json.dumps(row, ensure_ascii=False,
                                         sort_keys=True) + "\n")
                    count += 1
    return count


def export(archive: Path, out: Path, *, change_dates: Path | None = None,
           bavarian: Path | None = None, key: Path | None = None,
           acts: set[str] | None = None,
           sources: set[str] | None = None) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    corpus = out / "corpus.sqlite"
    writer = CorpusWriter(corpus)
    per_act: dict[str, int] = {}
    parsers: set[str] = set()
    for parsed in iter_parsed(archive, sources):
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
        if source and Path(source).is_dir():
            if name == "bavarian.jsonl.gz":
                bavarian_records(Path(source), target)
            else:
                change_date_records(Path(source), target)
            files[name] = target
        elif source:
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


def install(incoming: Path, root: Path, *, signers: Path | None,
            keep: int = 3) -> dict[str, Any]:
    """Verify an uploaded bundle and make it ``<root>/current`` atomically."""
    manifest = verify(incoming, signers=signers)
    stamp = manifest["built_at"].replace("-", "").replace(":", "")
    target = root / f"bundle-{stamp}"
    root.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(incoming)
    else:
        os.replace(incoming, target)
    link = root / "current"
    tmp = root / f".current-{os.getpid()}"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target.name)
    os.replace(tmp, link)
    bundles = sorted(p for p in root.glob("bundle-*") if p.is_dir())
    for old in bundles[:-keep]:
        if old.resolve() != target.resolve():
            shutil.rmtree(old)
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
    ex.add_argument("--sources", default="wayback_gii,neuris",
                    help="parsed sources to include (comma-separated)")
    ve = sub.add_parser("verify")
    ve.add_argument("bundle", type=Path)
    ve.add_argument("--signers", type=Path)
    ins = sub.add_parser("install")
    ins.add_argument("incoming", type=Path)
    ins.add_argument("--root", type=Path,
                     default=ROOT / "data" / "history_bundle")
    ins.add_argument("--signers", type=Path, default=Path(os.environ.get(
        "LEXGRAPH_ARCHIVE_SIGNERS", "/etc/sntiq/lexgraph-archive.allowed_signers")))
    args = parser.parse_args(argv)
    if args.command == "export":
        manifest = export(args.archive, args.out,
                          change_dates=args.change_dates,
                          bavarian=args.bavarian, key=args.key,
                          acts={a for a in args.acts.split(",") if a} or None,
                          sources={x for x in args.sources.split(",") if x}
                          or None)
    elif args.command == "install":
        manifest = install(args.incoming, args.root, signers=args.signers)
    else:
        manifest = verify(args.bundle, signers=args.signers)
    print(json.dumps({k: manifest.get(k) for k in (
        "built_at", "captures", "texts", "per_act", "sha256")},
        ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
