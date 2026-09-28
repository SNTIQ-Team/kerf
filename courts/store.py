"""SQLite store (schema v1) and content-hashed raw captures.

The schema is the contract read by the Node MCP (node:sqlite); see SCHEMA.md.
Changes to it must bump ``courts.SCHEMA_VERSION`` and ``PRAGMA user_version``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import PARSER_VERSION, SCHEMA_VERSION
from .casenumbers import case_key
from .citations import Relation, extract_all, synthetic_decision_id
from .config import db_path, raw_dir
from .courtids import resolve as resolve_court
from .errors import UpstreamFailure
from .model import ParsedDecision
from .paragraphs import assemble

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
  source_id TEXT PRIMARY KEY,
  label TEXT NOT NULL,
  base_url TEXT NOT NULL,
  land TEXT,
  adapter TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'never_synced'
    CHECK(status IN ('ok','partial','unavailable','blocked','never_synced')),
  status_reason TEXT,
  upstream_listed INTEGER,
  last_sync_started TEXT,
  last_sync_finished TEXT,
  last_sync_status TEXT,
  last_sync_run INTEGER
);
CREATE TABLE IF NOT EXISTS source_items(
  source_id TEXT NOT NULL,
  source_doc_id TEXT NOT NULL,
  url TEXT NOT NULL,
  listed_date TEXT,
  listed_court TEXT,
  listed_case_numbers TEXT,
  listed_modified TEXT,
  priority INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','stored','failed','skipped')),
  fetched_modified TEXT,
  content_hash TEXT,
  decision_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt TEXT,
  failure_kind TEXT,
  failure_detail TEXT,
  extra TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY(source_id, source_doc_id)
);
CREATE INDEX IF NOT EXISTS source_items_frontier ON source_items(source_id, state, priority, listed_date);
CREATE TABLE IF NOT EXISTS decisions(
  id TEXT PRIMARY KEY,
  ecli TEXT,
  source_id TEXT NOT NULL,
  source_doc_id TEXT NOT NULL,
  court_id TEXT NOT NULL,
  court_name TEXT NOT NULL,
  court_type TEXT,
  jurisdiction TEXT,
  land TEXT,
  chamber TEXT,
  decision_type TEXT,
  decision_date TEXT NOT NULL,
  case_numbers_raw TEXT NOT NULL,
  case_numbers TEXT NOT NULL,
  title TEXT,
  numbering TEXT NOT NULL CHECK(numbering IN ('rn','none')),
  paragraph_count INTEGER NOT NULL,
  source_url TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  capture_path TEXT NOT NULL,
  media_type TEXT,
  text_hash TEXT NOT NULL,
  parser_version TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source_id, source_doc_id)
);
CREATE INDEX IF NOT EXISTS decisions_court_date ON decisions(court_id, decision_date);
CREATE INDEX IF NOT EXISTS decisions_date ON decisions(decision_date);
CREATE INDEX IF NOT EXISTS decisions_ecli ON decisions(ecli);
CREATE INDEX IF NOT EXISTS decisions_source ON decisions(source_id, decision_date);
CREATE TABLE IF NOT EXISTS case_numbers(
  decision_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  case_number TEXT NOT NULL,
  case_key TEXT NOT NULL,
  PRIMARY KEY(decision_id, position)
);
CREATE INDEX IF NOT EXISTS case_numbers_key ON case_numbers(case_key);
CREATE TABLE IF NOT EXISTS sections(
  decision_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  name TEXT NOT NULL,
  authorship TEXT NOT NULL CHECK(authorship IN ('court','editorial','unknown')),
  authorship_basis TEXT NOT NULL,
  heading TEXT,
  PRIMARY KEY(decision_id, position)
);
CREATE TABLE IF NOT EXISTS paragraphs(
  rowid INTEGER PRIMARY KEY,
  id TEXT NOT NULL UNIQUE,
  decision_id TEXT NOT NULL,
  section TEXT NOT NULL,
  authorship TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  rn TEXT,
  numbering TEXT NOT NULL CHECK(numbering IN ('rn','none')),
  block_count INTEGER NOT NULL,
  blocks TEXT NOT NULL,
  text TEXT NOT NULL,
  text_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS paragraphs_decision ON paragraphs(decision_id, ordinal);
CREATE VIRTUAL TABLE IF NOT EXISTS paragraphs_fts USING fts5(
  text, content='paragraphs', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS paragraphs_ai AFTER INSERT ON paragraphs BEGIN
  INSERT INTO paragraphs_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS paragraphs_ad AFTER DELETE ON paragraphs BEGIN
  INSERT INTO paragraphs_fts(paragraphs_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TABLE IF NOT EXISTS relations(
  id INTEGER PRIMARY KEY,
  decision_id TEXT NOT NULL,
  relation TEXT NOT NULL,
  target_kind TEXT NOT NULL CHECK(target_kind IN ('norm','decision')),
  target_id TEXT,
  target_decision_id TEXT,
  target_court_id TEXT,
  target_date TEXT,
  target_case_key TEXT,
  target_raw TEXT NOT NULL,
  paragraph_id TEXT,
  span_start INTEGER,
  span_end INTEGER,
  quoted_context TEXT NOT NULL,
  origin TEXT NOT NULL CHECK(origin IN ('decision_text','editorial_metadata','source_metadata')),
  derived_by TEXT NOT NULL CHECK(derived_by IN ('parser','model','user')),
  legal_effect_verified INTEGER NOT NULL CHECK(legal_effect_verified IN (0,1)),
  confidence REAL NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  CHECK(derived_by <> 'parser' OR relation IN ('mentions','quotes','procedural_reference')),
  CHECK(derived_by <> 'parser' OR legal_effect_verified = 0),
  CHECK(derived_by = 'user' OR legal_effect_verified = 0)
);
CREATE INDEX IF NOT EXISTS relations_target ON relations(target_id);
CREATE INDEX IF NOT EXISTS relations_target_decision ON relations(target_decision_id);
CREATE INDEX IF NOT EXISTS relations_decision ON relations(decision_id);
CREATE INDEX IF NOT EXISTS relations_target_case ON relations(target_court_id, target_date, target_case_key);
CREATE TABLE IF NOT EXISTS sync_runs(
  id INTEGER PRIMARY KEY,
  source_id TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  status TEXT NOT NULL DEFAULT 'running'
    CHECK(status IN ('running','ok','partial','failed','blocked','interrupted')),
  since TEXT,
  limit_n INTEGER,
  listed INTEGER NOT NULL DEFAULT 0,
  attempted INTEGER NOT NULL DEFAULT 0,
  stored INTEGER NOT NULL DEFAULT 0,
  unchanged INTEGER NOT NULL DEFAULT 0,
  failed INTEGER NOT NULL DEFAULT 0,
  requests INTEGER NOT NULL DEFAULT 0,
  notes TEXT
);
CREATE TABLE IF NOT EXISTS failures(
  id INTEGER PRIMARY KEY,
  run_id INTEGER,
  source_id TEXT NOT NULL,
  source_doc_id TEXT,
  url TEXT,
  kind TEXT NOT NULL,
  detail TEXT,
  http_status INTEGER,
  at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS failures_source ON failures(source_id, at);
CREATE TABLE IF NOT EXISTS http_validators(
  url TEXT PRIMARY KEY,
  etag TEXT,
  last_modified TEXT,
  content_hash TEXT,
  fetched_at TEXT NOT NULL
);
CREATE VIEW IF NOT EXISTS source_coverage AS
SELECT s.source_id, s.label, s.land, s.status, s.status_reason, s.upstream_listed,
       (SELECT count(*) FROM decisions d WHERE d.source_id = s.source_id) AS decisions,
       (SELECT min(decision_date) FROM decisions d WHERE d.source_id = s.source_id) AS date_from,
       (SELECT max(decision_date) FROM decisions d WHERE d.source_id = s.source_id) AS date_to,
       (SELECT count(*) FROM source_items i WHERE i.source_id = s.source_id AND i.state = 'failed') AS failed_items,
       (SELECT count(*) FROM source_items i WHERE i.source_id = s.source_id AND i.state = 'pending') AS pending_items,
       s.last_sync_started, s.last_sync_finished, s.last_sync_status
FROM sources s;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def connect(path: Path | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    path = path or db_path()
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=120)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.isolation_level = None  # explicit BEGIN/COMMIT only
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=120000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, SCHEMA_VERSION):
        raise RuntimeError(f"database schema v{version} != code schema v{SCHEMA_VERSION}")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.execute("INSERT OR IGNORE INTO meta VALUES('created_at', ?)", (utcnow(),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES('parser_version', ?)", (PARSER_VERSION,))
    conn.commit()


# --- raw captures ------------------------------------------------------------

def write_capture(body: bytes, source_id: str, extension: str, home: Path | None = None) -> tuple[str, str]:
    """Store exact response bytes gzip-compressed under their sha256; return (hash, relpath)."""
    digest = sha256(body)
    rel = Path("raw") / source_id / digest[:2] / f"{digest}.{extension}.gz"
    target = (home or raw_dir().parent) / rel
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".capture-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(body)
            os.replace(tmp, target)
        finally:
            Path(tmp).unlink(missing_ok=True)
    return digest, rel.as_posix()


def read_capture(relpath: str, expected_hash: str, home: Path | None = None) -> bytes:
    data = gzip.decompress(((home or raw_dir().parent) / relpath).read_bytes())
    if sha256(data) != expected_hash:
        raise ValueError(f"capture {relpath} does not match its content hash")
    return data


# --- sources, runs, frontier ---------------------------------------------------

def register_source(conn: sqlite3.Connection, source_id: str, label: str, base_url: str,
                    land: str | None, adapter: str) -> None:
    conn.execute("""INSERT INTO sources(source_id,label,base_url,land,adapter) VALUES(?,?,?,?,?)
                    ON CONFLICT(source_id) DO UPDATE SET label=excluded.label, base_url=excluded.base_url,
                    land=excluded.land, adapter=excluded.adapter""",
                 (source_id, label, base_url, land, adapter))
    conn.commit()


def set_source_status(conn: sqlite3.Connection, source_id: str, status: str, reason: str | None) -> None:
    conn.execute("UPDATE sources SET status=?, status_reason=? WHERE source_id=?", (status, reason, source_id))
    conn.commit()


def start_run(conn: sqlite3.Connection, source_id: str, since: str | None, limit: int | None) -> int:
    now = utcnow()
    cur = conn.execute("INSERT INTO sync_runs(source_id, started_at, since, limit_n) VALUES(?,?,?,?)",
                       (source_id, now, since, limit))
    conn.execute("UPDATE sources SET last_sync_started=?, last_sync_run=? WHERE source_id=?",
                 (now, cur.lastrowid, source_id))
    conn.commit()
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, source_id: str, status: str, counts: dict,
               notes: str | None = None) -> None:
    now = utcnow()
    conn.execute("""UPDATE sync_runs SET finished_at=?, status=?, listed=?, attempted=?, stored=?, unchanged=?,
                    failed=?, requests=?, notes=? WHERE id=?""",
                 (now, status, counts.get("listed", 0), counts.get("attempted", 0), counts.get("stored", 0),
                  counts.get("unchanged", 0), counts.get("failed", 0), counts.get("requests", 0), notes, run_id))
    conn.execute("UPDATE sources SET last_sync_finished=?, last_sync_status=? WHERE source_id=?",
                 (now, status, source_id))
    conn.commit()


def record_failure(conn: sqlite3.Connection, run_id: int | None, source_id: str, source_doc_id: str | None,
                   failure: UpstreamFailure) -> None:
    now = utcnow()
    conn.execute("""INSERT INTO failures(run_id, source_id, source_doc_id, url, kind, detail, http_status, at)
                    VALUES(?,?,?,?,?,?,?,?)""",
                 (run_id, source_id, source_doc_id, failure.url, failure.kind, failure.detail[:1000],
                  failure.status, now))
    if source_doc_id:
        conn.execute("""UPDATE source_items SET state='failed', attempts=attempts+1, last_attempt=?,
                        failure_kind=?, failure_detail=? WHERE source_id=? AND source_doc_id=?""",
                     (now, failure.kind, failure.detail[:500], source_id, source_doc_id))
    conn.commit()


def upsert_items(conn: sqlite3.Connection, source_id: str, items: list[dict]) -> int:
    """Merge a listing into the frontier.  A changed ``listed_modified`` re-queues an item."""
    rows = [(source_id, i["source_doc_id"], i["url"], i.get("listed_date"), i.get("listed_court"),
             i.get("listed_case_numbers"), i.get("listed_modified"), i.get("priority", 1),
             json.dumps(i.get("extra") or {}, ensure_ascii=False)) for i in items]
    conn.execute("BEGIN IMMEDIATE")
    conn.executemany("""INSERT INTO source_items(source_id, source_doc_id, url, listed_date, listed_court,
                          listed_case_numbers, listed_modified, priority, extra) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(source_id, source_doc_id) DO UPDATE SET
                          url=excluded.url, listed_date=coalesce(excluded.listed_date, listed_date),
                          listed_court=coalesce(excluded.listed_court, listed_court),
                          listed_case_numbers=coalesce(excluded.listed_case_numbers, listed_case_numbers),
                          priority=excluded.priority, extra=excluded.extra,
                          state=CASE WHEN excluded.listed_modified IS NOT NULL
                                      AND source_items.fetched_modified IS NOT NULL
                                      AND excluded.listed_modified <> source_items.fetched_modified
                                     THEN 'pending' ELSE source_items.state END,
                          listed_modified=excluded.listed_modified""", rows)
    conn.execute("COMMIT")
    return len(rows)


def select_frontier(conn: sqlite3.Connection, source_id: str, *, since: str | None, limit: int | None,
                    courts: list[str] | None = None, retry_failed: bool = False) -> list[sqlite3.Row]:
    states = ("pending", "failed") if retry_failed else ("pending",)
    sql = [f"SELECT * FROM source_items WHERE source_id=? AND state IN ({','.join('?' * len(states))})"]
    params: list = [source_id, *states]
    if since:
        sql.append("AND (listed_date IS NULL OR listed_date >= ?)")
        params.append(since)
    if courts:
        sql.append("AND (" + " OR ".join("lower(listed_court) LIKE ?" for _ in courts) + ")")
        params += [c.lower() + "%" for c in courts]
    sql.append("ORDER BY priority ASC, listed_date DESC, source_doc_id ASC")
    if limit:
        sql.append("LIMIT ?")
        params.append(limit)
    return conn.execute(" ".join(sql), params).fetchall()


def get_validators(conn: sqlite3.Connection, url: str) -> tuple[str | None, str | None] | None:
    row = conn.execute("SELECT etag, last_modified FROM http_validators WHERE url=?", (url,)).fetchone()
    return (row["etag"], row["last_modified"]) if row else None


def put_validators(conn: sqlite3.Connection, url: str, headers: dict, content_hash: str | None) -> None:
    conn.execute("INSERT OR REPLACE INTO http_validators VALUES(?,?,?,?,?)",
                 (url, headers.get("etag"), headers.get("last-modified"), content_hash, utcnow()))
    conn.commit()


# --- decisions -----------------------------------------------------------------

ECLI_RE = re.compile(r"^ECLI:[A-Z]{2}:[A-Za-z0-9]{1,7}:\d{4}:[A-Za-z0-9.]{1,25}$")


def decision_identity(parsed: ParsedDecision) -> str:
    malformed = parsed.ecli and (not ECLI_RE.match(parsed.ecli) or (
        # German ordinals embed the Aktenzeichen ("0813.L9SO202.26B.ER.00", "180626UB4AS1025R0",
        # "rs20150127.1bvr047110"); a bare date + counter such as "0525.00" is not unique.
        parsed.ecli.startswith("ECLI:DE:") and not re.search(r"[A-Za-z]", parsed.ecli.rsplit(":", 1)[1])
        and parsed.ecli.rsplit(":", 1)[1].count(".") < 2))
    if malformed:
        # A malformed ECLI (e.g. without the ordinal part) cannot be a unique id.
        parsed.metadata["ecli_published_invalid"] = parsed.ecli
        parsed.ecli = None
    if parsed.ecli:
        return parsed.ecli
    if not parsed.case_numbers:
        raise UpstreamFailure("parse_error", "decision without Aktenzeichen and without ECLI",
                              url=parsed.source_url)
    return synthetic_decision_id(parsed.court_id, parsed.decision_date, parsed.case_numbers[0])


def finalise(parsed: ParsedDecision) -> ParsedDecision:
    """Fill court identity fields and validate the minimum decision contract."""
    if not parsed.court_id:
        info = resolve_court(parsed.court_name, default_land=parsed.land)
        parsed.court_id = info.court_id
        parsed.court_type = parsed.court_type or info.court_type
        parsed.jurisdiction = parsed.jurisdiction or info.jurisdiction
        parsed.land = parsed.land or info.land
    if not parsed.court_name or not parsed.decision_date or len(parsed.decision_date) != 10:
        raise UpstreamFailure("parse_error", "decision lacks court or ISO date", url=parsed.source_url)
    if not any(b.text.strip() for s in parsed.sections for b in s.blocks):
        raise UpstreamFailure("empty_document", "no decision text blocks", url=parsed.source_url)
    return parsed


def store_decision(conn: sqlite3.Connection, parsed: ParsedDecision, *, content_hash: str, capture_path: str,
                   media_type: str, observed_at: str | None = None) -> tuple[str, bool]:
    """Insert or replace one decision; return (decision_id, changed)."""
    parsed = finalise(parsed)
    decision_id = decision_identity(parsed)
    paragraphs, numbering = assemble(decision_id, parsed.sections)
    if not paragraphs:
        raise UpstreamFailure("empty_document", "no paragraphs", url=parsed.source_url)
    text_hash = sha256("\n␞\n".join(f"{p.id}\t{p.text}" for p in paragraphs).encode())
    observed_at = observed_at or utcnow()
    existing = conn.execute("SELECT id, content_hash, text_hash, parser_version, first_seen, source_id, "
                            "source_doc_id FROM decisions WHERE (source_id=? AND source_doc_id=?) OR id=?",
                            (parsed.source_id, parsed.source_doc_id, decision_id)).fetchall()
    for row in existing:
        if (row["source_id"], row["source_doc_id"]) != (parsed.source_id, parsed.source_doc_id):
            raise UpstreamFailure("duplicate_identity",
                                  f"{decision_id} already stored from {row['source_id']}:{row['source_doc_id']}",
                                  url=parsed.source_url)
    first_seen = existing[0]["first_seen"] if existing else observed_at
    if existing and existing[0]["id"] == decision_id and existing[0]["text_hash"] == text_hash \
            and existing[0]["parser_version"] == PARSER_VERSION:
        conn.execute("UPDATE decisions SET observed_at=?, content_hash=?, capture_path=? WHERE id=?",
                     (observed_at, content_hash, capture_path, decision_id))
        _mark_item(conn, parsed, decision_id, content_hash)
        conn.commit()
        return decision_id, False
    section_authorship = {s.name: s.authorship for s in parsed.sections}
    own_keys = {case_key(c) for c in parsed.case_numbers}
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row in existing:
            _delete_decision(conn, row["id"])
        conn.execute("""INSERT INTO decisions(id, ecli, source_id, source_doc_id, court_id, court_name, court_type,
                          jurisdiction, land, chamber, decision_type, decision_date, case_numbers_raw, case_numbers,
                          title, numbering, paragraph_count, source_url, content_hash, capture_path, media_type,
                          text_hash, parser_version, first_seen, observed_at, metadata)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (decision_id, parsed.ecli, parsed.source_id, parsed.source_doc_id, parsed.court_id,
                      parsed.court_name, parsed.court_type, parsed.jurisdiction, parsed.land, parsed.chamber,
                      parsed.decision_type, parsed.decision_date, parsed.case_numbers_raw,
                      json.dumps(parsed.case_numbers, ensure_ascii=False), parsed.title, numbering,
                      len(paragraphs), parsed.source_url, content_hash, capture_path, media_type, text_hash,
                      PARSER_VERSION, first_seen, observed_at,
                      json.dumps(parsed.metadata, ensure_ascii=False, sort_keys=True)))
        conn.executemany("INSERT INTO case_numbers VALUES(?,?,?,?)",
                         [(decision_id, i, c, case_key(c)) for i, c in enumerate(parsed.case_numbers, 1)])
        conn.executemany("INSERT INTO sections VALUES(?,?,?,?,?,?)",
                         [(decision_id, i, s.name, s.authorship, s.authorship_basis, s.heading)
                          for i, s in enumerate(parsed.sections, 1) if any(b.text.strip() for b in s.blocks)])
        relations: list[tuple[Relation, str]] = []
        for p in paragraphs:
            text = p.text
            conn.execute("""INSERT INTO paragraphs(id, decision_id, section, authorship, ordinal, rn, numbering,
                              block_count, blocks, text, text_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                         (p.id, decision_id, p.section, section_authorship.get(p.section, "unknown"), p.ordinal,
                          p.rn, p.numbering, len(p.blocks),
                          json.dumps([{"text": b.text, "locator": b.locator, "kind": b.kind} for b in p.blocks],
                                     ensure_ascii=False),
                          text, sha256(text.encode())))
            if section_authorship.get(p.section) == "court":
                relations += [(r, "decision_text") for r in extract_all(
                    text, p.id, own_court_id=parsed.court_id, own_case_keys=own_keys, own_land=parsed.land)]
        for norm in parsed.metadata.get("norms", []):
            from .citations import extract_norms
            for r in extract_norms(_metadata_norm_text(norm), None):
                r.quoted_context = norm
                relations.append((r, "editorial_metadata"))
        for proc in parsed.procedural:
            relations.append((_procedural_relation(proc, parsed), proc.get("origin", "editorial_metadata")))
        _insert_relations(conn, decision_id, relations)
        _mark_item(conn, parsed, decision_id, content_hash)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return decision_id, True


def _metadata_norm_text(norm: str) -> str:
    # RII/juris norm metadata omits dots ("§ 24 Abs 4 AsylVfG 1992").
    return re.sub(r"\b(Abs|Nr|Buchst|Alt|Halbs)(?=\s)", r"\1.", norm)


def _procedural_relation(proc: dict, parsed: ParsedDecision) -> Relation:
    info = resolve_court(proc["court"], default_land=parsed.land)
    target = synthetic_decision_id(info.court_id, proc["date"], proc["case_number"]) if proc.get("date") and \
        proc.get("case_number") else None
    return Relation(relation="procedural_reference", target_kind="decision", target_id=target,
                    target_raw=proc["raw"], paragraph_id=None, span_start=None, span_end=None,
                    quoted_context=proc["raw"], confidence=0.9,
                    detail={"court": proc["court"], "court_id": info.court_id, "date": proc.get("date"),
                            "case_number": proc.get("case_number"), "case_key": case_key(proc.get("case_number") or ""),
                            "direction": proc.get("direction", "previous"), "field": proc.get("field")})


def _insert_relations(conn: sqlite3.Connection, decision_id: str, relations: list[tuple[Relation, str]]) -> None:
    rows = []
    for r, origin in relations:
        d = r.detail
        rows.append((decision_id, r.relation, r.target_kind, r.target_id,
                     d.get("court_id") if r.target_kind == "decision" else None,
                     d.get("date") if r.target_kind == "decision" else None,
                     d.get("case_key") if r.target_kind == "decision" else None,
                     r.target_raw, r.paragraph_id, r.span_start, r.span_end, r.quoted_context, origin,
                     r.derived_by, int(r.legal_effect_verified), r.confidence,
                     json.dumps(d, ensure_ascii=False, sort_keys=True)))
    conn.executemany("""INSERT INTO relations(decision_id, relation, target_kind, target_id, target_court_id,
                          target_date, target_case_key, target_raw, paragraph_id, span_start, span_end,
                          quoted_context, origin, derived_by, legal_effect_verified, confidence, detail)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)


def _delete_decision(conn: sqlite3.Connection, decision_id: str) -> None:
    for table, column in (("paragraphs", "decision_id"), ("sections", "decision_id"),
                          ("case_numbers", "decision_id"), ("relations", "decision_id"), ("decisions", "id")):
        conn.execute(f"DELETE FROM {table} WHERE {column}=?", (decision_id,))


def _mark_item(conn: sqlite3.Connection, parsed: ParsedDecision, decision_id: str, content_hash: str) -> None:
    conn.execute("""UPDATE source_items SET state='stored', decision_id=?, content_hash=?, attempts=attempts+1,
                    last_attempt=?, fetched_modified=listed_modified, failure_kind=NULL, failure_detail=NULL
                    WHERE source_id=? AND source_doc_id=?""",
                 (decision_id, content_hash, utcnow(), parsed.source_id, parsed.source_doc_id))


def relink(conn: sqlite3.Connection) -> int:
    """Resolve decision citations to corpus decisions (by ECLI or court+date+Az)."""
    cur = conn.execute("""
      UPDATE relations SET target_decision_id = (
        SELECT d.id FROM decisions d JOIN case_numbers c ON c.decision_id = d.id
        WHERE d.court_id = relations.target_court_id AND d.decision_date = relations.target_date
          AND c.case_key = relations.target_case_key LIMIT 1)
      WHERE target_kind = 'decision' AND target_decision_id IS NULL AND target_case_key IS NOT NULL""")
    changed = cur.rowcount
    cur = conn.execute("""
      UPDATE relations SET target_decision_id = (
        SELECT min(d.id) FROM decisions d JOIN case_numbers c ON c.decision_id = d.id
        WHERE d.court_id = relations.target_court_id AND c.case_key = relations.target_case_key
        HAVING count(DISTINCT d.id) = 1)
      WHERE target_kind = 'decision' AND target_decision_id IS NULL AND target_date IS NULL
        AND target_case_key IS NOT NULL AND target_case_key <> ''""")
    changed += cur.rowcount
    cur = conn.execute("""
      UPDATE relations SET target_decision_id = (SELECT d.id FROM decisions d WHERE d.ecli = relations.target_id)
      WHERE target_kind = 'decision' AND target_decision_id IS NULL AND target_id LIKE 'ECLI:%'""")
    conn.commit()
    return changed + cur.rowcount
