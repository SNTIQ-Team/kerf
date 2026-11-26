"""SQLite store (schema v1) and content-hashed raw captures.

The schema is the contract read by the Node MCP (node:sqlite); see SCHEMA.md.
Changes to it must bump ``courts.SCHEMA_VERSION`` and ``PRAGMA user_version``.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import PARSER_VERSION, SCHEMA_VERSION
from .casenumbers import case_key
from .citations import DECISION_REF_RE, Relation, extract_all, parse_date, synthetic_decision_id
from .config import data_home, db_path
from .packs import CaptureIntegrityError, PackStore
from .courtids import resolve as resolve_court
from .errors import UpstreamFailure
from .model import ParsedDecision
from .ledger import SCHEMA as LEDGER_SCHEMA
from .paragraphs import BLOCK_SEPARATOR, assemble, anchor_of, decode_blocks, encode_blocks, split_paragraph_id
from .polarity import SCHEMA as POLARITY_SCHEMA
from .verify import SCHEMA as VERIFY_SCHEMA
from .courtmap import SCHEMA as COURTMAP_SCHEMA, canonical_court_id

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
  last_sync_run INTEGER,
  listing_complete INTEGER NOT NULL DEFAULT 0,
  robots_policy TEXT NOT NULL DEFAULT 'respect'
    CHECK(robots_policy IN ('respect','ignore_by_operator_decision')),
  policy_decided_at TEXT,
  policy_basis TEXT,
  min_interval REAL,
  source_kind TEXT NOT NULL DEFAULT 'primary' CHECK(source_kind IN ('primary','archive','aggregator')),
  licence TEXT
);
CREATE TABLE IF NOT EXISTS captures(
  sha256 TEXT PRIMARY KEY,
  source_id TEXT NOT NULL,
  extension TEXT NOT NULL,
  pack TEXT NOT NULL,
  offset INTEGER NOT NULL,
  stored_size INTEGER NOT NULL,
  raw_size INTEGER NOT NULL,
  codec TEXT NOT NULL CHECK(codec IN ('zstd','gzip')),
  stored_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS captures_pack ON captures(pack, offset);
CREATE TABLE IF NOT EXISTS source_items(
  source_id TEXT NOT NULL,
  source_doc_id TEXT NOT NULL,
  url TEXT NOT NULL,
  listed_date TEXT,
  listed_court TEXT,
  listed_case_numbers TEXT,
  listed_modified TEXT,
  priority INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','claimed','stored','failed','skipped')),
  fetched_modified TEXT,
  content_hash TEXT,
  decision_id TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_attempt TEXT,
  failure_kind TEXT,
  failure_detail TEXT,
  extra TEXT NOT NULL DEFAULT '{}',
  claimed_by TEXT,
  lease_until TEXT,
  PRIMARY KEY(source_id, source_doc_id)
);
CREATE INDEX IF NOT EXISTS source_items_claim ON source_items(source_id, state, priority, listed_date DESC,
                                                               source_doc_id);
CREATE TABLE IF NOT EXISTS decisions(
  id TEXT PRIMARY KEY,
  ecli TEXT,
  source_id TEXT NOT NULL,
  source_doc_id TEXT NOT NULL,
  court_id TEXT NOT NULL,
  court_name TEXT NOT NULL,
  court_slug TEXT NOT NULL DEFAULT '',
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
CREATE INDEX IF NOT EXISTS decisions_court_slug ON decisions(court_slug, decision_date);
CREATE INDEX IF NOT EXISTS decisions_date ON decisions(decision_date);
CREATE INDEX IF NOT EXISTS decisions_ecli ON decisions(ecli);
CREATE INDEX IF NOT EXISTS decisions_ecli_nocase ON decisions(ecli COLLATE NOCASE);
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
  decision_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  anchor TEXT NOT NULL,
  section TEXT NOT NULL,
  authorship TEXT NOT NULL,
  rn TEXT,
  numbering TEXT NOT NULL CHECK(numbering IN ('rn','none')),
  block_count INTEGER NOT NULL,
  blocks TEXT NOT NULL,
  text TEXT NOT NULL,
  text_hash BLOB NOT NULL CHECK(length(text_hash) = 32),
  UNIQUE(decision_id, ordinal)
);
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
  paragraph_ordinal INTEGER,
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
CREATE INDEX IF NOT EXISTS relations_paragraph ON relations(decision_id, paragraph_ordinal);
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
DROP VIEW IF EXISTS source_coverage;
CREATE VIEW IF NOT EXISTS source_coverage AS
SELECT s.source_id, s.label, s.land, s.status, s.status_reason, s.upstream_listed, s.listing_complete,
       s.robots_policy, s.policy_decided_at, s.policy_basis, s.min_interval, s.source_kind, s.licence,
       (SELECT count(*) FROM decisions d WHERE d.source_id = s.source_id) AS decisions,
       (SELECT min(decision_date) FROM decisions d WHERE d.source_id = s.source_id) AS date_from,
       (SELECT max(decision_date) FROM decisions d WHERE d.source_id = s.source_id) AS date_to,
       (SELECT count(*) FROM source_items i WHERE i.source_id = s.source_id AND i.state = 'failed') AS failed_items,
       (SELECT count(*) FROM source_items i WHERE i.source_id = s.source_id AND i.state IN ('pending','claimed'))
         AS pending_items,
       (SELECT count(*) FROM source_items i WHERE i.source_id = s.source_id AND i.state = 'skipped') AS duplicate_items,
       s.last_sync_started, s.last_sync_finished, s.last_sync_status
FROM sources s;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _wal(conn: sqlite3.Connection) -> None:
    """Switch to WAL.  On a new file several connections may try at once; SQLite answers SQLITE_BUSY at once
    for the journal-mode change (the busy handler is not consulted), so it is retried (the flaky
    test_parallel_init_db_does_not_race: 'database is locked' in ~4 % of runs)."""
    for attempt in range(200):
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
                return
            conn.execute("PRAGMA journal_mode=WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
            time.sleep(0.01 + 0.005 * attempt)
    conn.execute("PRAGMA journal_mode=WAL")


def connect(path: Path | None = None, *, readonly: bool = False, threads: bool = False) -> sqlite3.Connection:
    """``threads``: the connection is shared by the source threads of the one writer process
    (wrap it in courts.writer.SharedConnection)."""
    path = path or db_path()
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path, timeout=120, check_same_thread=not threads)
        _wal(conn)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA journal_size_limit=268435456")  # the WAL left after a checkpoint (courts.writer)
    conn.isolation_level = None  # explicit BEGIN/COMMIT only
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=120000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Additive (2026-09-28): secondary copies (archived pages, aggregators) never win over a primary source.
SOURCE_COLUMNS_KIND = {
    "source_kind": "TEXT NOT NULL DEFAULT 'primary' CHECK(source_kind IN ('primary','archive','aggregator'))",
    "licence": "TEXT",
}
# Precedence on identity conflicts (same decision from two sources): lower rank wins.  The source kind of a
# source id is fixed in code (adapters declare the same; tests check it) so that store needs no registry.
SECONDARY_SOURCES = {"wayback": "archive", "old": "aggregator"}
SOURCE_RANK = {"primary": 0, "archive": 1, "aggregator": 2}


def source_kind(source_id: str) -> str:
    return SECONDARY_SOURCES.get(source_id, "primary")


def source_rank(source_id: str) -> int:
    return SOURCE_RANK[source_kind(source_id)]


SOURCE_COLUMNS_V2 = {
    "listing_complete": "INTEGER NOT NULL DEFAULT 0",
    "robots_policy": "TEXT NOT NULL DEFAULT 'respect' CHECK(robots_policy IN ('respect','ignore_by_operator_decision'))",
    "policy_decided_at": "TEXT",
    "policy_basis": "TEXT",
    "min_interval": "REAL",
}


def init_db(conn: sqlite3.Connection) -> None:
    """Create or upgrade the schema; serialised across processes (parallel syncs start together)."""
    import fcntl
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    lock_path = Path(path).with_name(".init.lock") if path else None
    if lock_path is None:
        return _init_db(conn)
    with open(lock_path, "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            _init_db(conn)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def schema_statements() -> list[str]:
    """Every DDL statement of the corpus schema, in order (triggers kept whole)."""
    statements, current = [], ""
    for script in (SCHEMA, POLARITY_SCHEMA, LEDGER_SCHEMA, VERIFY_SCHEMA, COURTMAP_SCHEMA):
        for line in script.splitlines(keepends=True):
            current += line
            if sqlite3.complete_statement(current):
                if current.strip():
                    statements.append(current.strip())
                current = ""
    return statements


def create_schema(conn: sqlite3.Connection, *, triggers: bool = True, indexes: bool = True) -> None:
    """Create the v4 schema statement by statement (no implicit COMMIT, so it can run inside a
    migration's transaction).  ``triggers=False`` leaves out the paragraphs -> FTS triggers and
    ``indexes=False`` the CREATE INDEX statements (bulk loads build them afterwards)."""
    from .schema_v4 import PARAGRAPH_TRIGGERS
    for statement in schema_statements():
        head = " ".join(statement.split()[:6]).upper()
        if not triggers and head.startswith("CREATE TRIGGER IF NOT EXISTS PARAGRAPHS_"):
            continue
        if not indexes and head.startswith("CREATE INDEX"):
            continue
        conn.execute(statement)
    if triggers:
        for statement in filter(str.strip, PARAGRAPH_TRIGGERS.split("END;")):
            conn.execute(statement + "END;")


def _upgrade_source_items(conn: sqlite3.Connection) -> None:
    """v4 addition (2026-09-28): claims.  ``state`` admits ``claimed``; ``claimed_by``/``lease_until`` name the
    fetcher and the lease.  The CHECK list changes, so the table is rebuilt (rows and order kept)."""
    columns = [r[1] for r in conn.execute("PRAGMA table_info(source_items)")]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP VIEW IF EXISTS source_coverage")
        conn.execute("DROP INDEX IF EXISTS source_items_frontier")
        conn.execute("ALTER TABLE source_items RENAME TO source_items_old")
        statement = next(s for s in schema_statements() if s.startswith("CREATE TABLE IF NOT EXISTS source_items("))
        conn.execute(statement)
        cols = ", ".join(columns)
        conn.execute(f"INSERT INTO source_items({cols}) SELECT {cols} FROM source_items_old ORDER BY rowid")
        conn.execute("DROP TABLE source_items_old")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def claim_items(conn: sqlite3.Connection, source_id: str, worker: str, n: int, *, lease_seconds: int = 1800,
                courts: list[str] | None = None, since: str | None = None,
                retry_failed: bool = False) -> list[sqlite3.Row]:
    """Atomically claim the next ``n`` items of a source in frontier order (priority, newest first):
    ``state='claimed'``, ``claimed_by=worker``, ``lease_until`` = now + lease.  One UPDATE … RETURNING in the
    writer, so no item is ever handed to two fetchers."""
    states = ("pending", "failed") if retry_failed else ("pending",)
    where = [f"source_id = ? AND state IN ({','.join('?' * len(states))})"]
    params: list = [source_id, *states]
    if since:
        where.append("(listed_date IS NULL OR listed_date >= ?)")
        params.append(since)
    if courts:
        where.append("(" + " OR ".join("lower(listed_court) LIKE ?" for _ in courts) + ")")
        params += [c.lower() + "%" for c in courts]
    lease = datetime.now(timezone.utc).timestamp() + lease_seconds
    lease_iso = datetime.fromtimestamp(lease, timezone.utc).replace(microsecond=0).isoformat()
    rows = conn.execute(f"""UPDATE source_items SET state='claimed', claimed_by=?, lease_until=?
                            WHERE rowid IN (SELECT rowid FROM source_items WHERE {' AND '.join(where)}
                                            ORDER BY priority ASC, listed_date DESC, source_doc_id ASC LIMIT ?)
                            RETURNING *""", (worker, lease_iso, *params, n)).fetchall()
    conn.commit()
    return sorted(rows, key=lambda r: (r["priority"], _neg(r["listed_date"]), r["source_doc_id"]))


def _neg(value: str | None) -> tuple:
    return (1, "") if value is None else (0, "".join(chr(0x10FFFF - ord(c)) for c in value))


def release_claims(conn: sqlite3.Connection, *, worker: str | None = None, keys: list[tuple[str, str]] | None = None,
                   expired_only: bool = False) -> int:
    """Claimed items back to ``pending``: all (at start-up the writer lock proves no claim is alive), those of
    one worker, the given (source_id, source_doc_id) keys, or only those whose lease has expired."""
    if keys:
        count = 0
        for source_id, doc in keys:
            count += conn.execute("UPDATE source_items SET state='pending', claimed_by=NULL, lease_until=NULL "
                                  "WHERE source_id=? AND source_doc_id=? AND state='claimed'",
                                  (source_id, doc)).rowcount
        conn.commit()
        return count
    sql = "UPDATE source_items SET state='pending', claimed_by=NULL, lease_until=NULL WHERE state='claimed'"
    params: list = []
    if worker:
        sql += " AND claimed_by=?"
        params.append(worker)
    if expired_only:
        sql += " AND lease_until < ?"
        params.append(utcnow())
    count = conn.execute(sql, params).rowcount
    conn.commit()
    return count


def _init_db(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, 2, 3, SCHEMA_VERSION):
        raise RuntimeError(f"database schema v{version} != code schema v{SCHEMA_VERSION}")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='amtsgraph_courts'").fetchone():
        conn.execute("ALTER TABLE amtsgraph_courts RENAME TO taper_courts")
    if version in (1, 2):
        # v2 -> v3: decisions.court_id becomes the Taper id (when mapped); the corpus slug moves to
        # decisions.court_slug (alias, used in synthetic decision ids and citation linking).
        columns = {r[1] for r in conn.execute("PRAGMA table_info(decisions)")}
        if columns and "court_slug" not in columns:
            conn.execute("ALTER TABLE decisions ADD COLUMN court_slug TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE decisions SET court_slug = court_id WHERE court_slug = ''")
    if version == 1:
        # v1 -> v2: source policy columns and the packed-capture index (additive; legacy
        # raw/*.gz captures stay readable until `python -m courts migrate-captures`).
        columns = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
        for column, definition in SOURCE_COLUMNS_V2.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE sources ADD COLUMN {column} {definition}")
    source_columns = {r[1] for r in conn.execute("PRAGMA table_info(sources)")}
    if source_columns:
        for column, definition in SOURCE_COLUMNS_KIND.items():
            if column not in source_columns:
                conn.execute(f"ALTER TABLE sources ADD COLUMN {column} {definition}")
    item_columns = {r[1] for r in conn.execute("PRAGMA table_info(source_items)")}
    if item_columns and "claimed_by" not in item_columns:
        _upgrade_source_items(conn)
    paragraph_columns = {r[1] for r in conn.execute("PRAGMA table_info(paragraphs)")}
    if version in (1, 2, 3) and "id" in paragraph_columns:
        # v3 -> v4 (courts.schema_v4): the live corpus is migrated with `python -m courts migrate-schema`
        # (a verified copy); any other v3 file is upgraded here, inside the file.
        from .schema_v4 import upgrade_in_place
        upgrade_in_place(conn, create_schema)
    create_schema(conn)
    map_columns = {r[1] for r in conn.execute("PRAGMA table_info(court_map)")}
    if map_columns and "ecli_court_code" not in map_columns:
        # Additive (2026-09-28): observed ECLI court codes (courts.courtmap.derive_ecli_codes).
        from .courtmap import ECLI_COLUMNS
        for column, definition in ECLI_COLUMNS:
            if column not in map_columns:
                conn.execute(f"ALTER TABLE court_map ADD COLUMN {column} {definition}")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.execute("INSERT OR IGNORE INTO meta VALUES('created_at', ?)", (utcnow(),))
    conn.execute("INSERT OR REPLACE INTO meta VALUES('parser_version', ?)", (PARSER_VERSION,))
    conn.commit()


# --- raw captures --------------------------------------------------------------

def write_capture(body: bytes, source_id: str, extension: str, home: Path | None = None, *,
                  conn: sqlite3.Connection) -> tuple[str, str]:
    """Append exact response bytes to the source's pack (zstd, sha256-addressed); return (hash, ref)."""
    ref = PackStore(home or data_home(), conn).put(body, source_id, extension)
    return ref.sha256, ref.path_ref


def read_capture(conn: sqlite3.Connection, content_hash: str, capture_path: str | None = None,
                 home: Path | None = None) -> bytes:
    """Exact capture bytes, verified against the content hash (packs first, legacy raw/*.gz second)."""
    home = home or data_home()
    store = PackStore(home, conn)
    ref = store.lookup(content_hash)
    if ref is not None:
        return store.read(ref)
    if capture_path and capture_path.startswith("raw/"):
        data = gzip.decompress((home / capture_path).read_bytes())
        if sha256(data) != content_hash:
            raise CaptureIntegrityError(f"legacy capture {capture_path} does not match its content hash")
        return data
    raise FileNotFoundError(f"capture {content_hash} not found")


# --- sources, runs, frontier ---------------------------------------------------

def register_source(conn: sqlite3.Connection, source_id: str, label: str, base_url: str,
                    land: str | None, adapter: str, policy: dict | None = None) -> None:
    policy = policy or {}
    conn.execute("""INSERT INTO sources(source_id,label,base_url,land,adapter,robots_policy,policy_decided_at,
                                        policy_basis,min_interval,source_kind,licence) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_id) DO UPDATE SET label=excluded.label, base_url=excluded.base_url,
                    land=excluded.land, adapter=excluded.adapter, robots_policy=excluded.robots_policy,
                    policy_decided_at=excluded.policy_decided_at, policy_basis=excluded.policy_basis,
                    min_interval=excluded.min_interval, source_kind=excluded.source_kind,
                    licence=excluded.licence""",
                 (source_id, label, base_url, land, adapter, policy.get("robots_policy", "respect"),
                  policy.get("policy_decided_at"), policy.get("policy_basis"), policy.get("min_interval"),
                  policy.get("source_kind") or source_kind(source_id), policy.get("licence")))
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
                        failure_kind=?, failure_detail=?, claimed_by=NULL, lease_until=NULL
                        WHERE source_id=? AND source_doc_id=?""",
                     (now, failure.kind, failure.detail[:500], source_id, source_doc_id))
    conn.commit()


def upsert_items(conn: sqlite3.Connection, source_id: str, items: list[dict]) -> int:
    """Merge a listing into the frontier.  A changed ``listed_modified`` re-queues an item."""
    rows = [(source_id, i["source_doc_id"], i["url"], i.get("listed_date"), i.get("listed_court"),
             i.get("listed_case_numbers"), i.get("listed_modified"), i.get("priority", 1),
             json.dumps(i.get("extra") or {}, ensure_ascii=False)) for i in items]
    conn.execute("BEGIN IMMEDIATE")
    try:
        _upsert_rows(conn, rows)
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return len(rows)


def _upsert_rows(conn: sqlite3.Connection, rows: list[tuple]) -> None:
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
                                      AND source_items.state <> 'claimed'
                                     THEN 'pending' ELSE source_items.state END,
                          listed_modified=excluded.listed_modified""", rows)


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
    conflict = ecli_date_conflict(parsed.ecli, parsed.decision_date) if parsed.ecli else None
    if conflict:
        parsed.metadata["ecli_published_other_document"] = {"ecli": parsed.ecli, "reason": conflict}
        parsed.ecli = None
    if parsed.ecli and not parsed.metadata.get("ecli_shared_with"):
        return parsed.ecli
    if not parsed.case_numbers:
        raise UpstreamFailure("parse_error", "decision without Aktenzeichen and without ECLI",
                              url=parsed.source_url)
    synthetic = synthetic_decision_id(parsed.court_id, parsed.decision_date, parsed.case_numbers[0])
    variant = parsed.metadata.get("id_variant")
    return f"{synthetic}~{variant}" if variant else synthetic


REMEDY_HEADING = re.compile(r"^\s*(?:Rechtsbehelfsbelehrung(?:en)?|Rechtsmittelbelehrung(?:en)?|"
                            r"Belehrung\s+über\s+(?:die\s+)?Rechts(?:mittel|behelfe)|Rechtsmittelbelehrung\s*:)\s*:?\s*$",
                            re.I | re.M)
REASON_SECTIONS = {"gruende", "entscheidungsgruende", "sonstiger_langtext", "tatbestand"}
REMEDY_MAX_CHARS = 6000  # Belehrungen with signatures run to ~3000 characters


def split_remedy_instructions(sections: list) -> list:
    """The Rechtsmittelbelehrung is not part of the last Randnummer.

    Sources differ: bverwg.de prints it inside the last Rn's <p> (after <br/>), others as unnumbered trailing
    blocks, which the Rn rule would otherwise attach to the last Rn.  From a line that is exactly a remedy
    heading onwards, text moves into its own section 'rechtsmittelbelehrung' - but only if no explicitly
    numbered block follows (the heading then really ends the reasons).
    """
    from .model import Block, Section
    out = []
    for section in sections:
        if section.name not in REASON_SECTIONS:
            out.append(section)
            continue
        split_at = None
        for index, block in enumerate(section.blocks):
            m = REMEDY_HEADING.search(block.text)
            if m and not any(b.label for b in section.blocks[index + 1:]):
                if m.start() == 0 and block.label:
                    break  # a numbered Rn that *is* the Belehrung: the court numbered it, keep it
                tail = len(block.text) - m.start() + sum(len(b.text) for b in section.blocks[index + 1:])
                if tail > REMEDY_MAX_CHARS:
                    break  # too long for a Belehrung: a quoted one inside unnumbered reasons, not the end
                split_at = (index, m.start())
                break
        if split_at is None:
            out.append(section)
            continue
        index, offset = split_at
        block = section.blocks[index]
        head_text = block.text[:offset].rstrip()
        tail_text = block.text[offset:].strip()
        kept = section.blocks[:index] + ([Block(head_text, block.label, block.kind, block.locator)] if head_text else [])
        moved = [Block(line.strip(), None, "text", f"{block.locator}+remedy[{i}]")
                 for i, line in enumerate(tail_text.split("\n"), 1) if line.strip()]
        moved += [Block(b.text, None, b.kind, b.locator) for b in section.blocks[index + 1:]]
        out.append(Section(section.name, section.authorship, section.authorship_basis, kept, section.heading))
        out.append(Section("rechtsmittelbelehrung", "court",
                           "Rechtsbehelfs-/Rechtsmittelbelehrung (split from the reasons at its heading)", moved,
                           heading=moved[0].text if moved else None))
    return out


GERMAN_ECLI_DATE = re.compile(r"^(?:[a-z]{2})?(?:(?P<y4>\d{4})(?P<m4>\d{2})(?P<d4>\d{2})|(?P<d>\d{2})(?P<m>\d{2})(?P<y>\d{2})"
                              r"(?=\.?[A-Z])|(?P<mm>\d{2})(?P<dd>\d{2})\.)")


# Federal ECLI ordinals encode the decision date by construction (ddmmyy + U/B/G, BVerfG yyyymmdd); a
# federal ECLI naming another date belongs to another document.  Länder ECLIs (juris "mmdd.") are often a
# few days off the portal's decision date for the same document, so they are not used to judge identity.
FEDERAL_ECLI_COURTS = {"BGH", "BVerwG", "BSG", "BFH", "BAG", "BVerfG", "BPatG"}


def ecli_date_conflict(ecli: str, decision_date: str) -> str | None:
    """A federal German ECLI whose ordinal names another date than the decision: it belongs to another
    document (RII publishes e.g. an Urteil with the ECLI of its later Berichtigungsbeschluss, or a
    Beschluss with the judgment's ECLI - two documents, one ECLI, and the second was lost as a duplicate)."""
    parts = ecli.split(":")
    if len(parts) != 5 or parts[1] != "DE" or parts[2] not in FEDERAL_ECLI_COURTS:
        return None
    year = parts[3]
    if year.isdigit() and decision_date[:4] != year:
        return f"ECLI year {year} != decision year {decision_date[:4]}"
    encoded = ecli_encoded_date(ecli)
    if encoded is None:
        return None
    return None if encoded == decision_date else f"ECLI encodes {encoded}, decision dated {decision_date}"


def ecli_encoded_date(ecli: str) -> str | None:
    """The date a German ECLI ordinal encodes (federal ddmmyy / BVerfG yyyymmdd / Länder "mmdd."), or None."""
    parts = ecli.split(":")
    if len(parts) != 5 or parts[1] != "DE":
        return None
    # The BPatG ordinal for JURE269032429 starts with DDMMYYYY. Also accept
    # YYYYMMDD when the calendar and the ECLI's year distinguish that reading.
    if parts[2].upper() == "BPATG" and (match := re.match(r"^([0-9]{8})(?=\.?[A-Za-z])", parts[4])):
        digits = match.group(1)
        candidates = set()
        for year, month, day in ((digits[:4], digits[4:6], digits[6:]),
                                 (digits[4:], digits[2:4], digits[:2])):
            if year != parts[3]:
                continue
            try:
                candidates.add(date(int(year), int(month), int(day)).isoformat())
            except ValueError:
                continue
        return candidates.pop() if len(candidates) == 1 else None
    m = GERMAN_ECLI_DATE.match(parts[4])
    if not m:
        return None
    if m.group("y4"):
        return f"{m.group('y4')}-{m.group('m4')}-{m.group('d4')}"
    if m.group("d"):
        return f"20{m.group('y')}-{m.group('m')}-{m.group('d')}"
    return f"{parts[3]}-{m.group('mm')}-{m.group('dd')}"


def _compact_key(text: str) -> str:
    return re.sub(r"(?<![0-9])0+(?=[0-9])", "", re.sub(r"[^A-Z0-9]", "", text.upper()))


def ecli_fit(ecli: str, decision_date: str, case_numbers: list[str]) -> int:
    """How well a German ECLI fits one document (0-3): 2 when its ordinal encodes the decision date (1 when a
    Länder ordinal is a few days off, as juris portals often are), +1 when the ordinal embeds one of the
    Aktenzeichen.  Only used to decide which of several documents sharing one ECLI owns it; the Az part is
    too loose to reject an ECLI on its own ("OVG 11 S 59.18" -> "11S59.18", "4 B 39/17" -> "4B2017.39")."""
    score = 0
    encoded = ecli_encoded_date(ecli)
    if encoded and encoded == decision_date:
        score += 2
    elif encoded:
        try:
            days = abs((datetime.fromisoformat(encoded) - datetime.fromisoformat(decision_date)).days)
        except ValueError:
            days = None
        if days is not None and days <= 7 and ecli.split(":")[2] not in FEDERAL_ECLI_COURTS:
            score += 1
    ordinal = _compact_key(ecli.rsplit(":", 1)[-1])
    for az in case_numbers:
        key = _compact_key(re.sub(r"\(.*?\)", "", az))
        if key and key in ordinal:
            score += 1
            break
    return score


def finalise(parsed: ParsedDecision) -> ParsedDecision:
    """Fill court identity fields and validate the minimum decision contract."""
    parsed.sections = split_remedy_instructions(parsed.sections)
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


def _loose_ecli(ecli: str) -> str:
    return re.sub(r"[^A-Z1-9]", "", ecli.upper())


def same_decision_elsewhere(conn: sqlite3.Connection, parsed: ParsedDecision):
    """The same decision stored from another source under another id: same court, date, first Az and
    decision type, and no two distinct ECLIs (spelling variants such as "…B1KR0924R0"/"…B1KR924R0" or
    "26B.ER"/"26BER" count as one).  Two documents of one day and docket differ in type or ECLI."""
    if not parsed.case_numbers or not parsed.court_id:
        return None
    rows = conn.execute("""SELECT d.id, d.content_hash, d.text_hash, d.parser_version, d.first_seen, d.source_id,
                                  d.source_doc_id, d.numbering, d.ecli, d.decision_type
                           FROM decisions d JOIN case_numbers c ON c.decision_id = d.id AND c.position = 1
                           WHERE d.court_slug = ? AND d.decision_date = ? AND c.case_key = ? AND d.source_id <> ?""",
                        (parsed.court_id, parsed.decision_date, case_key(parsed.case_numbers[0]),
                         parsed.source_id)).fetchall()
    kind = (parsed.decision_type or "").split(" ")[0].lower()
    for row in rows:
        other_kind = (row["decision_type"] or "").split(" ")[0].lower()
        if kind and other_kind and kind != other_kind:
            continue
        if parsed.ecli and row["ecli"]:
            a, b = _loose_ecli(parsed.ecli), _loose_ecli(row["ecli"])
            if not (a.startswith(b) or b.startswith(a)):
                continue
        return row
    return None


REPUBLICATION_MIN_OVERLAP = 0.9


def _paragraph_key(text: str) -> str:
    return re.sub(r"\W+", "", text.casefold())


def paragraph_overlap(a: list[str], b: list[str]) -> float:
    """Share of identical paragraphs (case, spacing and punctuation ignored) over the longer text."""
    keys_a = {k for t in a if (k := _paragraph_key(t))}
    keys_b = {k for t in b if (k := _paragraph_key(t))}
    if not keys_a or not keys_b:
        return 0.0
    return len(keys_a & keys_b) / max(len(keys_a), len(keys_b))


def republication_of(conn: sqlite3.Connection, parsed: ParsedDecision, texts: list[str]):
    """The same decision published twice by one source under two document ids (NRW under a second URL
    spelling or year folder, juris portals under a re-issued document number): same court, date, first Az
    and decision type, no two distinct ECLIs, and at least REPUBLICATION_MIN_OVERLAP of the paragraphs
    identical.  Returns (row, overlap) or None."""
    if not parsed.case_numbers or not parsed.court_id:
        return None
    kind = (parsed.decision_type or "").split(" ")[0].lower()
    rows = conn.execute("""SELECT d.id, d.source_id, d.source_doc_id, d.numbering, d.decision_type, d.ecli,
                                  d.content_hash, d.text_hash, d.parser_version, d.first_seen
                           FROM decisions d JOIN case_numbers c ON c.decision_id = d.id AND c.position = 1
                           WHERE d.court_slug = ? AND d.decision_date = ? AND c.case_key = ? AND d.source_id = ?
                             AND d.source_doc_id <> ?""",
                        (parsed.court_id, parsed.decision_date, case_key(parsed.case_numbers[0]), parsed.source_id,
                         parsed.source_doc_id)).fetchall()
    best = None
    for row in rows:
        if (row["decision_type"] or "").split(" ")[0].lower() != kind:
            continue
        if parsed.ecli and row["ecli"]:
            a, b = _loose_ecli(parsed.ecli), _loose_ecli(row["ecli"])
            if not (a.startswith(b) or b.startswith(a)):
                continue
        theirs = [t for (t,) in conn.execute("SELECT text FROM paragraphs WHERE decision_id=? ORDER BY ordinal",
                                             (row["id"],))]
        overlap = paragraph_overlap(texts, theirs)
        if overlap >= REPUBLICATION_MIN_OVERLAP and (best is None or overlap > best[1]):
            best = (row, overlap)
    return best


DECISION_ID_COLUMNS = (("paragraphs", "decision_id"), ("sections", "decision_id"), ("case_numbers", "decision_id"),
                       ("relations", "decision_id"), ("relation_assessments", "decision_id"),
                       ("verification_checks", "decision_id"), ("verification_findings", "decision_id"),
                       ("verification_captures", "decision_id"), ("source_items", "decision_id"))


def rekey_decision(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
    """Move the document and its reviewed citation targets in the caller's transaction.

    The recorded citation and parser links retain their original ECLI or docket.
    Callers protect document IDs referenced by the append-only corrections ledger.
    """
    conn.execute("""UPDATE relations SET target_decision_id=?
                    WHERE target_decision_id=? AND target_kind='decision' AND derived_by IN ('user', 'model')""",
                 (new_id, old_id))
    for table, column in DECISION_ID_COLUMNS:
        conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (new_id, old_id))
    conn.execute("UPDATE decisions SET id=? WHERE id=?", (new_id, old_id))


def named_by_corrections(conn: sqlite3.Connection, decision_id: str) -> bool:
    return conn.execute("SELECT 1 FROM corrections WHERE target=? OR target LIKE ? LIMIT 1",
                        (decision_id, decision_id + "#%")).fetchone() is not None


def release_ecli(conn: sqlite3.Connection, decision_id: str, ecli: str, reason: str) -> str | None:
    """A stored document carries an ECLI that belongs to another document: it moves to its synthetic id and
    keeps the published ECLI only in metadata (ecli_published_other_document).  Returns the new id, or None
    when the corrections ledger names the old id (then nothing changes)."""
    row = conn.execute("SELECT id, court_slug, decision_date, case_numbers, source_doc_id, metadata FROM decisions "
                       "WHERE id=?", (decision_id,)).fetchone()
    if row is None or named_by_corrections(conn, decision_id):
        return None
    case_numbers = json.loads(row["case_numbers"])
    if not case_numbers:
        return None
    new_id = synthetic_decision_id(row["court_slug"], row["decision_date"], case_numbers[0])
    if new_id != decision_id and conn.execute("SELECT 1 FROM decisions WHERE id=?", (new_id,)).fetchone():
        new_id = f"{new_id}~{row['source_doc_id']}"
    metadata = json.loads(row["metadata"] or "{}")
    metadata.pop("ecli_shared_with", None)
    metadata["ecli_published_other_document"] = {"ecli": ecli, "reason": reason}
    conn.execute("UPDATE decisions SET ecli=NULL, metadata=? WHERE id=?",
                 (json.dumps(metadata, ensure_ascii=False, sort_keys=True), decision_id))
    if new_id != decision_id:
        rekey_decision(conn, decision_id, new_id)
    return new_id


def store_decision(conn: sqlite3.Connection, parsed: ParsedDecision, *, content_hash: str, capture_path: str,
                   media_type: str, observed_at: str | None = None, force: bool = False) -> tuple[str, bool]:
    """Insert or replace one decision; return (decision_id, changed).

    The identity checks read the database, so with source threads sharing one writer connection
    (courts.writer.SharedConnection) the whole store runs under that connection's lock."""
    lock = getattr(conn, "lock", None)
    if lock is not None:
        with lock:
            return _store_decision(conn, parsed, content_hash=content_hash, capture_path=capture_path,
                                   media_type=media_type, observed_at=observed_at, force=force)
    return _store_decision(conn, parsed, content_hash=content_hash, capture_path=capture_path,
                           media_type=media_type, observed_at=observed_at, force=force)


def _store_decision(conn: sqlite3.Connection, parsed: ParsedDecision, *, content_hash: str, capture_path: str,
                    media_type: str, observed_at: str | None = None, force: bool = False) -> tuple[str, bool]:
    parsed = finalise(parsed)
    decision_id = decision_identity(parsed)
    paragraphs, numbering = assemble(decision_id, parsed.sections)
    if not paragraphs:
        raise UpstreamFailure("empty_document", "no paragraphs", url=parsed.source_url)
    text_hash = sha256("\n␞\n".join(f"{p.id}\t{p.text}" for p in paragraphs).encode())
    observed_at = observed_at or utcnow()
    republication = republication_of(conn, parsed, [p.text for p in paragraphs])
    if republication is not None and not (republication[0]["numbering"] == "none" and numbering == "rn"):
        row, overlap = republication
        raise UpstreamFailure("duplicate_identity",
                              f"{row['id']} already stored from {row['source_id']}:{row['source_doc_id']} "
                              f"(republication: {overlap:.0%} of the paragraphs identical)", url=parsed.source_url)
    clash = conn.execute("SELECT id, source_doc_id, text_hash, decision_date, case_numbers FROM decisions "
                         "WHERE id=? AND source_id=? AND source_doc_id<>?",
                         (decision_id, parsed.source_id, parsed.source_doc_id)).fetchone()
    if clash is not None and parsed.ecli and decision_id == parsed.ecli and clash["text_hash"] != text_hash:
        # One portal, two documents, one ECLI.  The document whose date and Az the ordinal encodes owns it
        # (portals copy a judgment's ECLI into later decisions of the docket); the other is a decision of its
        # own under the synthetic id.  When both fit alike (juris portals give two Beschlüsse of one day and
        # docket the same ordinal) decisions.ecli keeps the ECLI on both: an ECLI lookup returns both
        # (ambiguous_identity).
        mine = ecli_fit(parsed.ecli, parsed.decision_date, parsed.case_numbers)
        theirs = ecli_fit(parsed.ecli, clash["decision_date"], json.loads(clash["case_numbers"]))
        released, split = None, True
        if mine > theirs:
            conn.execute("BEGIN IMMEDIATE")
            try:
                released = release_ecli(conn, clash["id"], parsed.ecli,
                                        f"its date/Az fit the ordinal less than {parsed.source_id}:{parsed.source_doc_id}")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        if released is not None:
            split = False  # the ECLI id is free now: this document takes it
        elif theirs > mine:
            parsed.metadata["ecli_published_other_document"] = {
                "ecli": parsed.ecli,
                "reason": f"its date/Az fit the ordinal less than {parsed.source_id}:{clash['source_doc_id']}"}
            parsed.ecli = None
        else:
            parsed.metadata["ecli_shared_with"] = f"{parsed.source_id}:{clash['source_doc_id']}"
    else:
        split = False
    if split:
        decision_id = decision_identity(parsed)
        taken = conn.execute("SELECT source_doc_id FROM decisions WHERE id=? AND source_doc_id<>?",
                             (decision_id, parsed.source_doc_id)).fetchone()
        if taken is not None:
            parsed.metadata["id_variant"] = parsed.source_doc_id
            decision_id = decision_identity(parsed)
        paragraphs, numbering = assemble(decision_id, parsed.sections)
        text_hash = sha256("\n␞\n".join(f"{p.id}\t{p.text}" for p in paragraphs).encode())
    twin = same_decision_elsewhere(conn, parsed)
    if twin is not None and twin["numbering"] == "none" and numbering == "rn" and not parsed.ecli and twin["ecli"]:
        # The explicit-Rn copy replaces an unnumbered twin and keeps its validated ECLI as the identity
        # (RII copies whose published ECLI belongs to another document, bsg.bund.de copies without Rn).
        parsed.ecli = twin["ecli"]
        parsed.metadata["ecli_from_source"] = f"{twin['source_id']}:{twin['source_doc_id']}"
        decision_id = decision_identity(parsed)
        paragraphs, numbering = assemble(decision_id, parsed.sections)
        text_hash = sha256("\n␞\n".join(f"{p.id}\t{p.text}" for p in paragraphs).encode())
    existing = conn.execute("SELECT id, content_hash, text_hash, parser_version, first_seen, source_id, "
                            "source_doc_id, numbering FROM decisions WHERE (source_id=? AND source_doc_id=?) OR id=?",
                            (parsed.source_id, parsed.source_doc_id, decision_id)).fetchall()
    if twin is not None and twin["id"] not in {r["id"] for r in existing}:
        existing.append(twin)
    superseded = None
    for row in existing:
        if (row["source_id"], row["source_doc_id"]) != (parsed.source_id, parsed.source_doc_id):
            mine, theirs = source_rank(parsed.source_id), source_rank(row["source_id"])
            if mine < theirs:
                # A primary source replaces a secondary copy (archived page, aggregator) of the same decision;
                # the copy stays as a skipped item with its capture (an offline verification pair).
                superseded = row
                continue
            if mine == theirs and row["numbering"] == "none" and numbering == "rn":
                # Precedence (SCHEMA.md "Duplicates"): the representation with explicit Randnummern wins; the
                # unnumbered copy (e.g. bsg.bund.de without Rn, RII with Rn) stays as a skipped item + capture.
                superseded = row
                continue
            raise UpstreamFailure("duplicate_identity",
                                  f"{row['id']} already stored from {row['source_id']}:{row['source_doc_id']}"
                                  + ("" if row["id"] == decision_id else f" (same court, date, Az and type as {decision_id})"),
                                  url=parsed.source_url)
    first_seen = min(r["first_seen"] for r in existing) if existing else observed_at
    own = [r for r in existing if superseded is None or r["id"] != superseded["id"]
           or (r["source_id"], r["source_doc_id"]) == (parsed.source_id, parsed.source_doc_id)]
    if not force and superseded is None and own and own[0]["id"] == decision_id \
            and own[0]["text_hash"] == text_hash and own[0]["parser_version"] == PARSER_VERSION:
        conn.execute("UPDATE decisions SET observed_at=?, content_hash=?, capture_path=? WHERE id=?",
                     (observed_at, content_hash, capture_path, decision_id))
        _mark_item(conn, parsed, decision_id, content_hash)
        conn.commit()
        return decision_id, False
    section_authorship = {s.name: s.authorship for s in parsed.sections}
    own_keys = {case_key(c) for c in parsed.case_numbers}
    # Everything CPU-bound (block offsets, citation extraction) happens before the write lock.
    paragraph_rows = []
    relations: list[tuple[Relation, str]] = []
    for p in paragraphs:
        text = p.text
        paragraph_rows.append((decision_id, p.ordinal, anchor_of(p.id), p.section,
                               section_authorship.get(p.section, "unknown"), p.rn, p.numbering, len(p.blocks),
                               encode_blocks(p), text, hashlib.sha256(text.encode()).digest()))
        if section_authorship.get(p.section) == "court":
            for r in extract_all(text, p.id, own_court_id=parsed.court_id, own_case_keys=own_keys,
                                 own_land=parsed.land):
                if r.relation != "quotes" and r.span_start is not None:
                    # Derivable from paragraphs.text + span (see SCHEMA.md): not stored twice.
                    r.quoted_context = ""
                relations.append((r, "decision_text"))
    for norm in parsed.metadata.get("norms", []):
        from .citations import extract_norms
        for r in extract_norms(_metadata_norm_text(norm), None):
            r.quoted_context = norm
            relations.append((r, "editorial_metadata"))
    for proc in parsed.procedural:
        relations.append((_procedural_relation(proc, parsed), proc.get("origin", "editorial_metadata")))
    try:
        conn.execute("BEGIN IMMEDIATE")
        for row in existing:
            _delete_decision(conn, row["id"])
        conn.execute("""INSERT INTO decisions(id, ecli, source_id, source_doc_id, court_id, court_name, court_slug,
                          court_type, jurisdiction, land, chamber, decision_type, decision_date, case_numbers_raw,
                          case_numbers, title, numbering, paragraph_count, source_url, content_hash, capture_path,
                          media_type, text_hash, parser_version, first_seen, observed_at, metadata)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (decision_id, parsed.ecli, parsed.source_id, parsed.source_doc_id,
                      canonical_court_id(conn, parsed.court_id, parsed.land), parsed.court_name, parsed.court_id,
                      parsed.court_type, parsed.jurisdiction, parsed.land, parsed.chamber,
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
        conn.executemany("""INSERT INTO paragraphs(decision_id, ordinal, anchor, section, authorship, rn, numbering,
                              block_count, blocks, text, text_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", paragraph_rows)
        _insert_relations(conn, decision_id, relations, {p.id: p.ordinal for p in paragraphs})
        _mark_item(conn, parsed, decision_id, content_hash)
        if superseded is not None:
            conn.execute("""UPDATE source_items SET state='skipped', decision_id=NULL, failure_kind='duplicate_identity',
                            failure_detail=?, content_hash=coalesce(content_hash, ?)
                            WHERE source_id=? AND source_doc_id=?""",
                         (f"{decision_id} superseded by {parsed.source_id}:{parsed.source_doc_id} "
                          + ("(a primary source takes precedence over a secondary copy)"
                             if source_rank(parsed.source_id) < source_rank(superseded["source_id"])
                             else "(explicit Randnummern take precedence)"), superseded["content_hash"],
                          superseded["source_id"], superseded["source_doc_id"]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return decision_id, True




def _metadata_norm_text(norm: str) -> str:
    # RII/juris norm metadata omits dots ("§ 24 Abs 4 AsylVfG 1992").
    return re.sub(r"\b(Abs|Nr|Buchst|Alt|Halbs)(?=\s)", r"\1.", norm)


def _procedural_relation(proc: dict, parsed: ParsedDecision) -> Relation:
    # A Vorinstanz line without a recognisable court (OVG Sachsen PDFs: "Az. … vom …" only) keeps its raw
    # text; it has no target instead of failing the whole decision.
    info = resolve_court(proc["court"], default_land=parsed.land) if proc.get("court") else None
    target = synthetic_decision_id(info.court_id, proc["date"], proc["case_number"]) \
        if info and proc.get("date") and proc.get("case_number") else None
    return Relation(relation="procedural_reference", target_kind="decision", target_id=target,
                    target_raw=proc["raw"], paragraph_id=None, span_start=None, span_end=None,
                    quoted_context=proc["raw"], confidence=0.9,
                    detail={"court": proc.get("court"), "court_id": info.court_id if info else None, "date": proc.get("date"),
                            "case_number": proc.get("case_number"), "case_key": case_key(proc.get("case_number") or ""),
                            "direction": proc.get("direction", "previous"), "field": proc.get("field")})


def _insert_relations(conn: sqlite3.Connection, decision_id: str, relations: list[tuple[Relation, str]],
                      ordinals: dict[str, int]) -> None:
    from .detail import trim_detail
    rows = []
    for r, origin in relations:
        d = r.detail
        columns = {"target_kind": r.target_kind, "target_id": r.target_id, "target_raw": r.target_raw,
                   "target_court_id": d.get("court_id") if r.target_kind == "decision" else None,
                   "target_date": d.get("date") if r.target_kind == "decision" else None,
                   "target_case_key": d.get("case_key") if r.target_kind == "decision" else None}
        rows.append((decision_id, r.relation, r.target_kind, r.target_id, columns["target_court_id"],
                     columns["target_date"], columns["target_case_key"], r.target_raw,
                     ordinals[r.paragraph_id] if r.paragraph_id is not None else None, r.span_start, r.span_end,
                     r.quoted_context, origin, r.derived_by, int(r.legal_effect_verified), r.confidence,
                     trim_detail(d, columns)))
    conn.executemany("""INSERT INTO relations(decision_id, relation, target_kind, target_id, target_court_id,
                          target_date, target_case_key, target_raw, paragraph_ordinal, span_start, span_end,
                          quoted_context, origin, derived_by, legal_effect_verified, confidence, detail)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rows)


def _delete_decision(conn: sqlite3.Connection, decision_id: str) -> None:
    # Rule proposals are recomputed after every store; user/model assessments survive (they also
    # live in the corrections ledger).
    conn.execute("DELETE FROM relation_assessments WHERE decision_id=? AND derived_by='rules'", (decision_id,))
    for table, column in (("paragraphs", "decision_id"), ("sections", "decision_id"),
                          ("case_numbers", "decision_id"), ("relations", "decision_id"), ("decisions", "id")):
        conn.execute(f"DELETE FROM {table} WHERE {column}=?", (decision_id,))


def _mark_item(conn: sqlite3.Connection, parsed: ParsedDecision, decision_id: str, content_hash: str) -> None:
    conn.execute("""UPDATE source_items SET state='stored', decision_id=?, content_hash=?, attempts=attempts+1,
                    last_attempt=?, fetched_modified=listed_modified, failure_kind=NULL, failure_detail=NULL,
                    claimed_by=NULL, lease_until=NULL WHERE source_id=? AND source_doc_id=?""",
                 (decision_id, content_hash, utcnow(), parsed.source_id, parsed.source_doc_id))


def _decision_family(label: str) -> str | None:
    """The legal form named explicitly in the source. General labels carry no form."""
    value = label.strip().lower()
    if value == "urt." or value.endswith(("urteil", "urteils", "urteile", "urteilen")):
        return "urteil"
    if value == "beschl." or value.endswith(("beschluss", "beschlusses", "beschlüsse", "beschlüssen")):
        return "beschluss"
    if value.endswith(("gerichtsbescheid", "gerichtsbescheids", "gerichtsbescheides", "gerichtsbescheide")):
        return "gerichtsbescheid"
    return None


# A citation may name two forms before a shared date, e.g. "Urteil und
# Beschluss vom …". The date parser captures only its immediately preceding
# form. Check whether another form is joined to it before the shared date.
_CITED_FORM_WORD = re.compile(
    r"(?<!\w)(?:[\w-]*(?:urteil(?:s|es|e|en)?|beschl(?:uss(?:es)?|üsse?n?|uß)|gerichtsbescheid(?:s|es|e|en)?)|urt\.|beschl\.)(?!\w)",
    re.IGNORECASE,
)


def _target_by_cited_form(candidates: str, raw: str, day: str, docket: str) -> str | None:
    """Disambiguate one dated citation with the form printed beside its date and docket."""
    matches = [match for match in DECISION_REF_RE.finditer(raw)
               if match.group("dtype") and parse_date(match) == day and case_key(match.group("az")) == docket]
    if len(matches) != 1:
        return None
    match = matches[0]
    prefix = raw[:match.start("dtype")]
    previous_words = list(_CITED_FORM_WORD.finditer(prefix))
    if previous_words:
        between = prefix[previous_words[-1].end():]
        if re.fullmatch(r"[\s,;/&+()-]*(?:(?:und|oder|sowie|bzw\.?|u\.|o\.|als\s+auch|wie\s+auch)[\s,;/&+()-]*)*",
                        between, re.IGNORECASE):
            return None
    word = match.group("dtype").lower()
    wanted = _decision_family(word)
    if wanted is None:
        return None
    found = None
    for decision_id, label in json.loads(candidates):
        family = _decision_family(label or "")
        if family is None:
            return None
        if family == wanted:
            if found is not None:
                return None
            found = (decision_id, label.lower())
    if found is None:
        return None
    # A subtype such as Teilurteil needs positive evidence of that subtype.
    for qualifier in ("teilurteil", "vorlagebeschl", "kammerbeschl", "senatsbeschl", "senatsurteil"):
        if word.startswith(qualifier) and qualifier not in found[1]:
            return None
    return found[0]


def relink(conn: sqlite3.Connection) -> int:
    """Refresh inferred citation targets and return the number of changed links.

    A docket can cover several documents issued by the same court on the same
    day. Arrival of a second document can therefore invalidate an earlier link.
    Published ECLIs take precedence over docket matching. Existing links set
    by users or models keep their recorded target.
    """
    conn.create_function("kerf_target_by_cited_form", 4, _target_by_cited_form, deterministic=True)
    conn.execute("""
        WITH
        dated AS MATERIALIZED (
            SELECT d.court_slug AS court, d.decision_date AS day, n.case_key AS docket,
                   CASE WHEN count(DISTINCT d.id) = 1 THEN min(d.id) END AS candidate,
                   CASE WHEN count(DISTINCT d.id) > 1 THEN
                       json_group_array(DISTINCT json_array(d.id, d.decision_type)) END AS choices
            FROM decisions d JOIN case_numbers n ON n.decision_id = d.id
            GROUP BY d.court_slug, d.decision_date, n.case_key
        ),
        undated AS MATERIALIZED (
            SELECT d.court_id AS court, n.case_key AS docket,
                   CASE WHEN count(DISTINCT d.id) = 1 THEN min(d.id) END AS candidate
            FROM decisions d JOIN case_numbers n ON n.decision_id = d.id
            GROUP BY d.court_id, n.case_key
        ),
        published AS MATERIALIZED (
            SELECT ecli COLLATE NOCASE AS ecli,
                   CASE WHEN count(*) = 1 THEN min(id) END AS candidate
            FROM decisions WHERE ecli IS NOT NULL AND ecli <> '' GROUP BY ecli COLLATE NOCASE
        ),
        resolved AS (
            SELECT r.id, r.target_decision_id AS previous,
                   CASE WHEN r.target_id LIKE 'ECLI:%' THEN published.candidate
                        WHEN r.target_date IS NULL THEN undated.candidate
                        WHEN dated.choices IS NOT NULL THEN
                            kerf_target_by_cited_form(dated.choices, r.target_raw, r.target_date, r.target_case_key)
                        ELSE dated.candidate END AS candidate
            FROM relations r
            LEFT JOIN published ON published.ecli = r.target_id COLLATE NOCASE
            LEFT JOIN dated ON dated.court = r.target_court_id AND dated.day = r.target_date
                            AND dated.docket = r.target_case_key
            LEFT JOIN undated ON r.target_date IS NULL AND undated.court = r.target_court_id
                              AND undated.docket = r.target_case_key
            WHERE r.target_kind = 'decision'
              AND (r.derived_by = 'parser' OR r.target_decision_id IS NULL)
              AND (r.target_id LIKE 'ECLI:%' OR (r.target_case_key IS NOT NULL AND r.target_case_key <> ''))
        )
        UPDATE relations SET target_decision_id = resolved.candidate
        FROM resolved WHERE relations.id = resolved.id AND resolved.previous IS NOT resolved.candidate
    """)
    changed = conn.execute("SELECT changes()").fetchone()[0]
    conn.commit()
    return changed
