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
from datetime import datetime, timezone
from pathlib import Path

from . import PARSER_VERSION, SCHEMA_VERSION
from .casenumbers import case_key
from .citations import Relation, extract_all, synthetic_decision_id
from .config import data_home, db_path
from .packs import CaptureIntegrityError, PackStore
from .courtids import resolve as resolve_court
from .errors import UpstreamFailure
from .model import ParsedDecision
from .ledger import SCHEMA as LEDGER_SCHEMA
from .paragraphs import assemble
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
  min_interval REAL
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
DROP VIEW IF EXISTS source_coverage;
CREATE VIEW IF NOT EXISTS source_coverage AS
SELECT s.source_id, s.label, s.land, s.status, s.status_reason, s.upstream_listed, s.listing_complete,
       s.robots_policy, s.policy_decided_at, s.policy_basis, s.min_interval,
       (SELECT count(*) FROM decisions d WHERE d.source_id = s.source_id) AS decisions,
       (SELECT min(decision_date) FROM decisions d WHERE d.source_id = s.source_id) AS date_from,
       (SELECT max(decision_date) FROM decisions d WHERE d.source_id = s.source_id) AS date_to,
       (SELECT count(*) FROM source_items i WHERE i.source_id = s.source_id AND i.state = 'failed') AS failed_items,
       (SELECT count(*) FROM source_items i WHERE i.source_id = s.source_id AND i.state = 'pending') AS pending_items,
       (SELECT count(*) FROM source_items i WHERE i.source_id = s.source_id AND i.state = 'skipped') AS duplicate_items,
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


def _init_db(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version not in (0, 1, 2, SCHEMA_VERSION):
        raise RuntimeError(f"database schema v{version} != code schema v{SCHEMA_VERSION}")
    if version in (1, 2):
        # v2 -> v3: decisions.court_id becomes the Amtsgraph id (when mapped); the corpus slug moves to
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
    conn.executescript(SCHEMA)
    conn.executescript(POLARITY_SCHEMA)
    conn.executescript(LEDGER_SCHEMA)
    conn.executescript(VERIFY_SCHEMA)
    conn.executescript(COURTMAP_SCHEMA)
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
                                        policy_basis,min_interval) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(source_id) DO UPDATE SET label=excluded.label, base_url=excluded.base_url,
                    land=excluded.land, adapter=excluded.adapter, robots_policy=excluded.robots_policy,
                    policy_decided_at=excluded.policy_decided_at, policy_basis=excluded.policy_basis,
                    min_interval=excluded.min_interval""",
                 (source_id, label, base_url, land, adapter, policy.get("robots_policy", "respect"),
                  policy.get("policy_decided_at"), policy.get("policy_basis"), policy.get("min_interval")))
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
    conflict = ecli_date_conflict(parsed.ecli, parsed.decision_date) if parsed.ecli else None
    if conflict:
        parsed.metadata["ecli_published_other_document"] = {"ecli": parsed.ecli, "reason": conflict}
        parsed.ecli = None
    if parsed.ecli:
        return parsed.ecli
    if not parsed.case_numbers:
        raise UpstreamFailure("parse_error", "decision without Aktenzeichen and without ECLI",
                              url=parsed.source_url)
    return synthetic_decision_id(parsed.court_id, parsed.decision_date, parsed.case_numbers[0])


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
    m = GERMAN_ECLI_DATE.match(parts[4])
    if not m:
        return None
    if m.group("y4"):
        encoded = f"{m.group('y4')}-{m.group('m4')}-{m.group('d4')}"
    elif m.group("d"):
        encoded = f"20{m.group('y')}-{m.group('m')}-{m.group('d')}"
    else:
        encoded = f"{year}-{m.group('mm')}-{m.group('dd')}"
    return None if encoded == decision_date else f"ECLI encodes {encoded}, decision dated {decision_date}"


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


def store_decision(conn: sqlite3.Connection, parsed: ParsedDecision, *, content_hash: str, capture_path: str,
                   media_type: str, observed_at: str | None = None, force: bool = False) -> tuple[str, bool]:
    """Insert or replace one decision; return (decision_id, changed)."""
    parsed = finalise(parsed)
    decision_id = decision_identity(parsed)
    paragraphs, numbering = assemble(decision_id, parsed.sections)
    if not paragraphs:
        raise UpstreamFailure("empty_document", "no paragraphs", url=parsed.source_url)
    text_hash = sha256("\n␞\n".join(f"{p.id}\t{p.text}" for p in paragraphs).encode())
    observed_at = observed_at or utcnow()
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
            if row["numbering"] == "none" and numbering == "rn":
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
        paragraph_rows.append((p.id, decision_id, p.section, section_authorship.get(p.section, "unknown"), p.ordinal,
                               p.rn, p.numbering, len(p.blocks), block_offsets(p), text, sha256(text.encode())))
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
        conn.executemany("""INSERT INTO paragraphs(id, decision_id, section, authorship, ordinal, rn, numbering,
                              block_count, blocks, text, text_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?)""", paragraph_rows)
        _insert_relations(conn, decision_id, relations)
        _mark_item(conn, parsed, decision_id, content_hash)
        if superseded is not None:
            conn.execute("""UPDATE source_items SET state='skipped', failure_kind='duplicate_identity',
                            failure_detail=?, content_hash=coalesce(content_hash, ?)
                            WHERE source_id=? AND source_doc_id=?""",
                         (f"{decision_id} superseded by {parsed.source_id}:{parsed.source_doc_id} "
                          "(explicit Randnummern take precedence)", superseded["content_hash"],
                          superseded["source_id"], superseded["source_doc_id"]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return decision_id, True


BLOCK_SEPARATOR = "\n\n"


def block_offsets(paragraph) -> str:
    """Block boundaries as code-point offsets into paragraphs.text (text = blocks joined by a blank line)."""
    out, position = [], 0
    for block in paragraph.blocks:
        end = position + len(block.text)
        out.append({"start": position, "end": end, "locator": block.locator, "kind": block.kind})
        position = end + len(BLOCK_SEPARATOR)
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


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
    # Rule proposals are recomputed after every store; user/model assessments survive (they also
    # live in the corrections ledger).
    conn.execute("DELETE FROM relation_assessments WHERE decision_id=? AND derived_by='rules'", (decision_id,))
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
        WHERE d.court_slug = relations.target_court_id AND d.decision_date = relations.target_date
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
