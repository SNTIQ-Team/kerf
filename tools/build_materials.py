#!/usr/bin/env python3
"""Build the legislative-materials store from Bundestag Drucksache PDFs.

Single document::

    python tools/build_materials.py --pdf 1913824.pdf \\
        --url https://dserver.bundestag.de/btd/19/138/1913824.pdf \\
        --drucksache 19/13824 --out data/

``--meta`` takes a JSON object (or a path to a JSON file) with the document
metadata; without it the entry for ``--drucksache`` in ``--sources``
(default ``data/materials_sources.json``) is used.  Every document listed in
the sources file whose PDF lies in ``--pdf-dir`` is built with::

    python tools/build_materials.py --pdf-dir DIR --out data/

Each page is extracted with ``pdftotext -layout -enc UTF-8 -f N -l N`` and
kept byte-exact for offsets; the PDF must match the sha256 recorded in the
metadata.  Output, always in ``<out>/materials`` (the API reads the same
directory, or ``$LEX_MATERIALS_DIR`` when that is set):

* ``materials.sqlite`` -- one row per fragment plus a ``links`` table
  (fragment, relation, canonical id) indexed for norm lookups.  The file is
  rebuilt from scratch in a canonical order on every run (other documents
  are carried over from the previous database after checking its sha256),
  so identical input gives a byte-identical file;
* ``index.json`` -- documents, counts and the database sha256 the API
  verifies before serving.

No network access.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.legislative_materials import (PARSER_VERSION,  # noqa: E402
                                       segment_drucksache)
from api.materials_routes import (DB_FILE, DB_SCHEMA,  # noqa: E402
                                  INDEX_SCHEMA, document_summary,
                                  pack_fragment)

DEFAULT_SOURCES = ROOT / "data" / "materials_sources.json"
PDFTOTEXT_ARGS = ("-layout", "-enc", "UTF-8")


class BuildError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def pdftotext_version() -> str:
    run = subprocess.run(["pdftotext", "-v"], capture_output=True, text=True)
    lines = (run.stderr or run.stdout).strip().splitlines()
    return lines[0].strip() if lines else "unknown"


def pdf_page_count(pdf: Path) -> int:
    run = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True,
                         check=True)
    match = re.search(r"^Pages:\s+(\d+)", run.stdout, re.M)
    if not match:
        raise BuildError(f"pdfinfo reports no page count for {pdf}")
    return int(match.group(1))


def extract_page(pdf: Path, page: int) -> str:
    """Exact ``pdftotext -layout`` text of one page (keeps the trailing \\f)."""
    run = subprocess.run(["pdftotext", *PDFTOTEXT_ARGS, "-f", str(page),
                          "-l", str(page), str(pdf), "-"],
                         capture_output=True, check=True)
    return run.stdout.decode("utf-8")


def extract_pages(pdf: Path, count: int, workers: int = 8) -> list[str]:
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(lambda n: extract_page(pdf, n),
                             range(1, count + 1)))


def load_sources(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"act_slugs": {}, "documents": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("documents"), list):
        raise BuildError(f"{path}: no 'documents' list")
    return data


def parse_meta(value: str | None) -> dict[str, Any] | None:
    if value is None:
        return None
    text = value
    if not value.lstrip().startswith("{"):
        text = Path(value).read_text(encoding="utf-8")
    meta = json.loads(text)
    if not isinstance(meta, dict):
        raise BuildError("--meta must be a JSON object")
    return meta


def observed_at(meta: dict[str, Any], pdf: Path,
                override: str | None) -> tuple[str, str]:
    if override:
        return override, "command_line"
    if meta.get("retrieved_at"):
        return str(meta["retrieved_at"]), "sources_config"
    stamp = dt.datetime.fromtimestamp(pdf.stat().st_mtime, dt.timezone.utc)
    return stamp.replace(microsecond=0).isoformat(), "pdf_file_mtime"


def document_meta(entry: dict[str, Any], *, url: str, pdf_sha256: str,
                  page_count: int, observed: tuple[str, str],
                  version: str) -> dict[str, Any]:
    meta = {k: v for k, v in entry.items() if k not in {
        "pdf_url", "pdf_sha256", "page_count", "retrieved_at"}}
    meta["source"] = {"url": url, "pdf_sha256": pdf_sha256,
                      "page_count": page_count, "observed_at": observed[0],
                      "observed_at_basis": observed[1]}
    meta["extraction"] = {"pdftotext_version": version,
                          "command": "pdftotext " + " ".join(PDFTOTEXT_ARGS)
                          + " -f N -l N <pdf> -"}
    return meta


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _drucksache_key(value: str) -> tuple[int, int]:
    wp, number = value.split("/")
    return int(wp), int(number)


SCHEMA_SQL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE documents (
  id TEXT PRIMARY KEY,
  drucksache TEXT NOT NULL UNIQUE,
  wp INTEGER NOT NULL,
  number INTEGER NOT NULL,
  content_sha256 TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  document_json TEXT NOT NULL,
  sections_json TEXT NOT NULL
);
CREATE TABLE fragments (
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL,
  drucksache TEXT NOT NULL,
  wp INTEGER NOT NULL,
  number INTEGER NOT NULL,
  role TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  page_start INTEGER NOT NULL,
  page_end INTEGER NOT NULL,
  heading TEXT NOT NULL,
  procedure_stage TEXT NOT NULL,
  adoption_status TEXT NOT NULL,
  complete INTEGER NOT NULL,
  fragment_json TEXT NOT NULL
);
CREATE TABLE links (
  fragment_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  relation TEXT NOT NULL,
  canonical_id TEXT,
  citation TEXT,
  confidence TEXT NOT NULL,
  source TEXT NOT NULL,
  source_index INTEGER NOT NULL,
  PRIMARY KEY (fragment_id, position)
) WITHOUT ROWID;
"""
INDEX_SQL = """
CREATE INDEX fragments_by_drucksache_role ON fragments(drucksache, role, ordinal);
CREATE INDEX fragments_by_order ON fragments(wp, number, ordinal);
CREATE INDEX links_by_canonical_id ON links(canonical_id, relation);
"""
_DOC_COLUMNS = ("id", "drucksache", "wp", "number", "content_sha256",
                "summary_json", "document_json", "sections_json")
_FRAGMENT_COLUMNS = ("id", "document_id", "drucksache", "wp", "number",
                     "role", "ordinal", "page_start", "page_end", "heading",
                     "procedure_stage", "adoption_status", "complete",
                     "fragment_json")
_LINK_COLUMNS = ("fragment_id", "position", "relation", "canonical_id",
                 "citation", "confidence", "source", "source_index")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)


def document_rows(result: dict[str, Any], act_slugs: dict[str, str]
                  ) -> dict[str, Any]:
    """Database rows (and the index entry) for one segmented document."""
    doc = result["document"]
    wp, number = _drucksache_key(doc["drucksache"])
    summary = document_summary(doc)
    content = _dumps({"document": doc, "sections": result["sections"],
                      "fragments": result["fragments"]})
    content_sha = sha256_bytes(content.encode("utf-8"))
    fragments, links = [], []
    for fragment in result["fragments"]:
        fragment_json, fragment_links = pack_fragment(fragment, summary)
        fragments.append((
            fragment["id"], doc["id"], doc["drucksache"], wp, number,
            fragment["role"], fragment["ordinal"], fragment["page_start"],
            fragment["page_end"], fragment["heading"],
            fragment["procedure_stage"], fragment["adoption_status"],
            int(bool(fragment["complete"])), fragment_json))
        links.extend(fragment_links)
    document = (doc["id"], doc["drucksache"], wp, number, content_sha,
                _dumps(summary),
                _dumps({**doc, "act_slugs": dict(sorted(act_slugs.items()))}),
                _dumps(result["sections"]))
    source = doc.get("source") or {}
    entry = {
        "id": doc["id"],
        "drucksache": doc["drucksache"],
        "content_sha256": content_sha,
        "type": doc.get("type"),
        "initiator": doc.get("initiator"),
        "date": doc.get("date"),
        "title": doc.get("title"),
        "source_url": source.get("url"),
        "pdf_sha256": source.get("pdf_sha256"),
        "page_count": source.get("page_count"),
        "pages_missing": len(doc["extraction"]["pages_missing"]),
        "fragment_count": doc["counts"]["fragments"],
        "counts_by_role": doc["counts"]["by_role"],
        "relation_counts": doc["counts"]["relations"],
        "parser_version": doc.get("parser_version"),
    }
    return {"entry": entry, "document": document, "fragments": fragments,
            "links": links}


def _previous_documents(directory: Path, keep_out: str
                        ) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Rows of every other document in the current store (sha-checked)."""
    index_path = directory / "index.json"
    db_path = directory / DB_FILE
    if not index_path.exists():
        return [], {}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except ValueError:
        raise BuildError(f"{index_path} is not valid JSON; rebuild every "
                         "document into an empty directory") from None
    if index.get("schema") != INDEX_SCHEMA:
        return [], {}       # an older layout: start a new store
    if not db_path.exists():
        raise BuildError(f"{index_path} lists a database but {db_path} is "
                         "missing; rebuild every document")
    if sha256_file(db_path) != (index.get("database") or {}).get("sha256"):
        raise BuildError(f"{db_path} does not match the sha256 in "
                         f"{index_path}; refusing to carry its documents over")
    entries = {row["drucksache"]: row for row in index.get("documents") or []}
    kept: list[dict[str, Any]] = []
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        for drucksache, entry in sorted(entries.items(),
                                        key=lambda kv: _drucksache_key(kv[0])):
            if drucksache == keep_out:
                continue
            document = conn.execute(
                f"SELECT {', '.join(_DOC_COLUMNS)} FROM documents "
                "WHERE drucksache = ?", (drucksache,)).fetchone()
            if document is None:
                raise BuildError(f"{db_path} lacks {drucksache}")
            fragments = conn.execute(
                f"SELECT {', '.join(_FRAGMENT_COLUMNS)} FROM fragments "
                "WHERE drucksache = ? ORDER BY ordinal", (drucksache,)).fetchall()
            links = conn.execute(
                f"SELECT {', '.join('l.' + c for c in _LINK_COLUMNS)} "
                "FROM links l JOIN fragments f ON f.id = l.fragment_id "
                "WHERE f.drucksache = ? ORDER BY f.ordinal, l.position",
                (drucksache,)).fetchall()
            kept.append({"entry": entry, "document": tuple(document),
                         "fragments": [tuple(r) for r in fragments],
                         "links": [tuple(r) for r in links]})
    finally:
        conn.close()
    return kept, dict(index.get("act_slugs") or {})


def _write_database(path: Path, documents: list[dict[str, Any]]) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp, isolation_level=None)
    try:
        conn.execute("PRAGMA page_size = 4096")
        conn.execute("PRAGMA journal_mode = OFF")
        conn.execute("PRAGMA synchronous = OFF")
        conn.executescript(SCHEMA_SQL)
        conn.execute("BEGIN")
        conn.executemany("INSERT INTO meta VALUES (?, ?)", [
            ("parser_version", PARSER_VERSION), ("schema", DB_SCHEMA)])
        for doc in documents:
            conn.execute(f"INSERT INTO documents VALUES "
                         f"({', '.join('?' * len(_DOC_COLUMNS))})",
                         doc["document"])
            conn.executemany(f"INSERT INTO fragments VALUES "
                             f"({', '.join('?' * len(_FRAGMENT_COLUMNS))})",
                             doc["fragments"])
            conn.executemany(f"INSERT INTO links VALUES "
                             f"({', '.join('?' * len(_LINK_COLUMNS))})",
                             doc["links"])
        conn.execute("COMMIT")
        conn.executescript(INDEX_SQL)
        conn.execute("PRAGMA user_version = 1")
        conn.execute("VACUUM")
    finally:
        conn.close()
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def write_store(out_dir: Path, result: dict[str, Any],
                act_slugs: dict[str, str]) -> dict[str, Any]:
    """Add (or replace) one document in the store and rewrite
    ``materials.sqlite`` and ``index.json`` deterministically."""
    directory = Path(out_dir) / "materials"
    directory.mkdir(parents=True, exist_ok=True)
    rows = document_rows(result, act_slugs)
    kept, old_slugs = _previous_documents(directory,
                                          rows["entry"]["drucksache"])
    documents = sorted([*kept, rows],
                       key=lambda d: _drucksache_key(d["entry"]["drucksache"]))
    db_path = directory / DB_FILE
    _write_database(db_path, documents)
    database = {"file": DB_FILE, "schema": DB_SCHEMA,
                "sha256": sha256_file(db_path),
                "bytes": db_path.stat().st_size}
    index = {"schema": INDEX_SCHEMA, "parser_version": PARSER_VERSION,
             "database": database,
             "documents": [d["entry"] for d in documents],
             "act_slugs": dict(sorted({**old_slugs, **act_slugs}.items()))}
    _atomic_write(directory / "index.json",
                  (json.dumps(index, ensure_ascii=False, indent=1) + "\n")
                  .encode("utf-8"))
    return {**rows["entry"], "database": database}


def build_one(*, pdf: Path, url: str | None, drucksache: str,
              meta: dict[str, Any], act_slugs: dict[str, str], out: Path,
              observed_override: str | None = None,
              workers: int = 8) -> dict[str, Any]:
    if not pdf.exists():
        raise BuildError(f"PDF not found: {pdf}")
    digest = sha256_file(pdf)
    expected = meta.get("pdf_sha256")
    if expected and expected != digest:
        raise BuildError(f"{pdf} has sha256 {digest}, the metadata records "
                         f"{expected}; refusing to build")
    count = pdf_page_count(pdf)
    if meta.get("page_count") and int(meta["page_count"]) != count:
        raise BuildError(f"{pdf} has {count} pages, the metadata records "
                         f"{meta['page_count']}")
    url = url or meta.get("pdf_url")
    if not url:
        raise BuildError("no PDF URL (pass --url or set pdf_url)")
    pages = extract_pages(pdf, count, workers)
    full_meta = document_meta(meta, url=url, pdf_sha256=digest,
                              page_count=count,
                              observed=observed_at(meta, pdf,
                                                   observed_override),
                              version=pdftotext_version())
    result = segment_drucksache(pages, drucksache=drucksache,
                                document_meta=full_meta, act_slugs=act_slugs)
    entry = write_store(out, result, act_slugs)
    return {"drucksache": drucksache,
            "database": str(Path(out) / "materials" / DB_FILE),
            "database_bytes": entry["database"]["bytes"],
            "database_sha256": entry["database"]["sha256"],
            "content_sha256": entry["content_sha256"],
            "pages": count, "fragments": entry["fragment_count"],
            "by_role": entry["counts_by_role"],
            "relations": entry["relation_counts"],
            "warnings": result["document"]["warnings"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--pdf", type=Path)
    parser.add_argument("--url")
    parser.add_argument("--drucksache")
    parser.add_argument("--meta", help="JSON object or path to a JSON file")
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES,
                        help="sources config (act slugs, document metadata)")
    parser.add_argument("--pdf-dir", type=Path,
                        help="build every listed document whose PDF is here")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--observed-at")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    try:
        sources = load_sources(args.sources)
        act_slugs = dict(sources.get("act_slugs") or {})
        entries = {row["drucksache"]: row for row in sources["documents"]}
        reports = []
        if args.pdf is not None:
            if not args.drucksache:
                parser.error("--pdf requires --drucksache")
            meta = parse_meta(args.meta)
            if meta is None:
                meta = entries.get(args.drucksache)
                if meta is None:
                    raise BuildError(f"no metadata for {args.drucksache}: pass "
                                     "--meta or list it in --sources")
            meta = dict(meta)
            meta.setdefault("drucksache", args.drucksache)
            if meta["drucksache"] != args.drucksache:
                raise BuildError("--drucksache differs from the metadata")
            act_slugs.update(meta.pop("act_slugs", None) or {})
            reports.append(build_one(
                pdf=args.pdf, url=args.url, drucksache=args.drucksache,
                meta=meta, act_slugs=act_slugs, out=args.out,
                observed_override=args.observed_at, workers=args.workers))
        elif args.pdf_dir is not None:
            for drucksache, meta in entries.items():
                pdf = args.pdf_dir / Path(meta["pdf_url"]).name
                if not pdf.exists():
                    reports.append({"drucksache": drucksache,
                                    "skipped": f"{pdf} not found"})
                    continue
                reports.append(build_one(
                    pdf=pdf, url=None, drucksache=drucksache, meta=meta,
                    act_slugs=act_slugs, out=args.out,
                    observed_override=args.observed_at,
                    workers=args.workers))
        else:
            parser.error("pass --pdf (with --drucksache) or --pdf-dir")
    except (BuildError, subprocess.CalledProcessError, OSError,
            ValueError) as exc:
        print(f"build_materials: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(reports, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
