"""Schema v3 -> v4 (2026-09-28): the size trims the operator accepted, FTS positions kept.

* ``paragraphs``: keyed by ``UNIQUE(decision_id, ordinal)``; the public id column ``id`` becomes ``anchor``
  (the part after '#'; id = decision_id || '#' || anchor); ``text_hash`` is the raw 32-byte SHA-256;
  ``blocks`` is compact JSON ``[[length, locator(, kind)], …]``.  Row ids are kept, so the external-content
  FTS index (``paragraphs_fts``) stays valid as it is.
* ``relations``: ``paragraph_id`` becomes ``paragraph_ordinal`` (with ``decision_id`` the paragraph key);
  ``detail`` without derivable keys (courts.detail); one index ``(decision_id, paragraph_ordinal)``
  replaces ``relations_decision`` and ``relations_paragraph``.
* ``relation_assessments``: ``paragraph_id`` becomes ``paragraph_ordinal``; ``assessment`` admits
  ``mentions_with_cue``; new column ``cue_suggests``.

Two ways, one transformation (:func:`_copy`):

* :func:`upgrade_in_place` - ``init_db`` on any v3 database (tests, copies): the three tables are
  rebuilt inside the file; the FTS shadow tables are untouched.
* :func:`build_copy` - the live corpus: a new file is built from the v3 file (opened read-only and left
  unchanged as the fallback), the FTS shadow tables are copied verbatim, and :func:`verify` compares
  every row before the files are swapped (``python -m courts migrate-schema``).
"""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import time
from pathlib import Path

from .detail import expand_detail, trim_detail

TABLES = ("paragraphs", "relations", "relation_assessments")
OLD_INDEXES = ("paragraphs_decision", "relations_decision", "relations_paragraph", "relations_target",
               "relations_target_decision", "relations_target_case", "relation_assessments_decision",
               "relation_assessments_target")
FTS_SHADOW = ("paragraphs_fts_data", "paragraphs_fts_idx", "paragraphs_fts_docsize", "paragraphs_fts_config")
PARAGRAPH_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS paragraphs_ai AFTER INSERT ON paragraphs BEGIN
  INSERT INTO paragraphs_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS paragraphs_ad AFTER DELETE ON paragraphs BEGIN
  INSERT INTO paragraphs_fts(paragraphs_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
"""


def _v4_blocks(blocks: str) -> str:
    out = []
    for b in json.loads(blocks):
        item = [b["end"] - b["start"], b["locator"]]
        if b.get("kind", "text") != "text":
            item.append(b["kind"])
        out.append(item)
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def _archived_detail(raw: str) -> dict:
    value = json.loads(raw or "{}")
    if "lexgraph_act_id" in value:
        value["kerf_act_id"] = value.pop("lexgraph_act_id")
    return value


def _v4_detail(detail, target_kind, target_id, target_raw, target_court_id, target_date, target_case_key) -> str:
    row = {"target_kind": target_kind, "target_id": target_id, "target_raw": target_raw,
           "target_court_id": target_court_id, "target_date": target_date, "target_case_key": target_case_key}
    return trim_detail(_archived_detail(detail), row)


def register_functions(conn: sqlite3.Connection) -> None:
    conn.create_function("v4_blocks", 1, _v4_blocks, deterministic=True)
    conn.create_function("v4_detail", 7, _v4_detail, deterministic=True)


def _copy(conn: sqlite3.Connection, src: dict[str, str], dst: str = "main", *, assessments: bool = True) -> dict:
    """INSERT … SELECT the three tables from ``src`` (table -> qualified v3 name) into ``dst``."""
    p, r, a = src["paragraphs"], src["relations"], src["relation_assessments"]
    counts = {}
    counts["paragraphs"] = conn.execute(f"""
        INSERT INTO {dst}.paragraphs(rowid, decision_id, ordinal, anchor, section, authorship, rn, numbering,
                                     block_count, blocks, text, text_hash)
        SELECT rowid, decision_id, ordinal, substr(id, instr(id, '#') + 1), section, authorship, rn, numbering,
               block_count, v4_blocks(blocks), text, unhex(text_hash)
        FROM {p} ORDER BY rowid""").rowcount
    counts["relations"] = conn.execute(f"""
        INSERT INTO {dst}.relations(id, decision_id, relation, target_kind, target_id, target_decision_id,
                                    target_court_id, target_date, target_case_key, target_raw, paragraph_ordinal,
                                    span_start, span_end, quoted_context, origin, derived_by, legal_effect_verified,
                                    confidence, detail)
        SELECT r.id, r.decision_id, r.relation, r.target_kind, r.target_id, r.target_decision_id, r.target_court_id,
               r.target_date, r.target_case_key, r.target_raw,
               (SELECT o.ordinal FROM {p} o WHERE o.id = r.paragraph_id),
               r.span_start, r.span_end, r.quoted_context, r.origin, r.derived_by, r.legal_effect_verified,
               r.confidence, v4_detail(r.detail, r.target_kind, r.target_id, r.target_raw, r.target_court_id,
                                       r.target_date, r.target_case_key)
        FROM {r} r ORDER BY r.id""").rowcount
    if not assessments:
        return counts
    counts["relation_assessments"] = conn.execute(f"""
        INSERT INTO {dst}.relation_assessments(id, relation_id, decision_id, paragraph_ordinal, target_kind,
                                               target_id, span_start, span_end, assessment, cue_suggests, polarity,
                                               cue, cue_start, cue_end, evidence_text, rule_id, derived_by,
                                               legal_effect_verified, confidence, classifier_version, status,
                                               created_at)
        SELECT a.id, a.relation_id, a.decision_id, (SELECT o.ordinal FROM {p} o WHERE o.id = a.paragraph_id),
               a.target_kind, a.target_id, a.span_start, a.span_end, a.assessment, NULL, a.polarity, a.cue,
               a.cue_start, a.cue_end, a.evidence_text, a.rule_id, a.derived_by, a.legal_effect_verified,
               a.confidence, a.classifier_version, a.status, a.created_at
        FROM {a} a ORDER BY a.id""").rowcount
    return counts


def _preflight(conn: sqlite3.Connection, schema: str = "main", suffix: str = "") -> None:
    """Facts the transformation relies on; a v3 database that breaks one is not migrated."""
    p = f"{schema}.paragraphs{suffix}"
    bad = conn.execute(f"SELECT count(*) FROM {p} WHERE instr(id, '#') = 0 OR substr(id, 1, instr(id, '#') - 1) "
                       "<> decision_id OR length(text_hash) <> 64").fetchone()[0]
    if bad:
        raise RuntimeError(f"{bad} paragraphs whose id is not '<decision_id>#<anchor>' or whose hash is not hex")
    dup = conn.execute(f"SELECT count(*) FROM (SELECT 1 FROM {p} GROUP BY decision_id, ordinal "
                       "HAVING count(*) > 1)").fetchone()[0]
    if dup:
        raise RuntimeError(f"{dup} (decision_id, ordinal) pairs are not unique")


IN_PLACE_LIMIT = 200_000   # paragraphs; above it the one big transaction would grow the WAL by gigabytes


def upgrade_in_place(conn: sqlite3.Connection, create_schema) -> dict:
    """v3 -> v4 inside the file.  ``create_schema(conn)`` creates the v4 tables (store._create_schema)."""
    size = conn.execute("SELECT max(rowid) FROM paragraphs").fetchone()[0] or 0
    if size > IN_PLACE_LIMIT:
        raise RuntimeError(f"a v3 corpus with {size} paragraphs is migrated with `python -m courts migrate-schema` "
                           "(verified copy, then swap), not in place")
    register_functions(conn)
    _preflight(conn)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DROP TRIGGER IF EXISTS paragraphs_ai")
        conn.execute("DROP TRIGGER IF EXISTS paragraphs_ad")
        for name in OLD_INDEXES:
            conn.execute(f"DROP INDEX IF EXISTS {name}")
        present = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in TABLES:
            if table in present:
                conn.execute(f"ALTER TABLE {table} RENAME TO {table}_v3")
            else:  # a v1/v2 file without the polarity stage: an empty stand-in with the v3 columns
                conn.execute(f"CREATE TABLE {table}_v3 AS SELECT * FROM (SELECT NULL AS id) WHERE 0")
        create_schema(conn, triggers=False)
        counts = _copy(conn, {t: f"main.{t}_v3" for t in TABLES}) if "relation_assessments" in present else \
            _copy(conn, {t: f"main.{t}_v3" for t in TABLES}, assessments=False)
        for table in TABLES:
            conn.execute(f"DROP TABLE {table}_v3")
        for statement in filter(str.strip, PARAGRAPH_TRIGGERS.split("END;")):
            conn.execute(statement + "END;")
        conn.execute("PRAGMA user_version=4")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return counts


def build_copy(source: Path, target: Path, create_schema, log=print) -> dict:
    """Build the v4 corpus ``target`` from the v3 file ``source`` (read-only).  Returns the row counts."""
    if target.exists():
        raise FileExistsError(target)
    started = time.monotonic()
    conn = sqlite3.connect(f"file:{target}", uri=True)   # URI mode: the source is attached read-only
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode=OFF")      # a scratch file until verified and swapped
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-1048576")    # 1 GiB page cache for the index builds
    register_functions(conn)
    # The schema first: unqualified DDL (e.g. DROP VIEW IF EXISTS) must not reach the attached v3 file.
    create_schema(conn, triggers=False, indexes=False)
    conn.execute(f"ATTACH DATABASE 'file:{source}?mode=ro' AS v3")
    if conn.execute("PRAGMA v3.user_version").fetchone()[0] != 3:
        raise RuntimeError("source is not a schema v3 corpus")
    _preflight(conn, "v3")
    conn.execute("BEGIN")
    counts = _copy(conn, {t: f"v3.{t}" for t in TABLES})
    log(f"[{time.monotonic() - started:6.0f}s] paragraphs, relations, assessments copied: {counts}")
    names = [r[0] for r in conn.execute("SELECT name FROM main.sqlite_master WHERE type='table' AND name NOT LIKE "
                                        "'sqlite_%'")]
    archived_courts = conn.execute("SELECT 1 FROM v3.sqlite_master WHERE name='amtsgraph_courts'").fetchone()
    for name in names:
        source_name = "amtsgraph_courts" if archived_courts and name == "taper_courts" else name
        if name in TABLES or name in FTS_SHADOW or name == "paragraphs_fts":
            continue
        columns = [r[1] for r in conn.execute(f"PRAGMA main.table_info({name})")]
        present = {r[1] for r in conn.execute(f"PRAGMA v3.table_info({source_name})")}
        if not present:
            continue
        cols = ", ".join(c for c in columns if c in present)
        counts[name] = conn.execute(f"INSERT INTO main.{name}({cols}) SELECT {cols} FROM v3.{source_name}").rowcount
    for shadow in FTS_SHADOW:
        conn.execute(f"DELETE FROM main.{shadow}")
        counts[shadow] = conn.execute(f"INSERT INTO main.{shadow} SELECT * FROM v3.{shadow}").rowcount
    conn.execute("COMMIT")
    conn.execute("DETACH DATABASE v3")
    log(f"[{time.monotonic() - started:6.0f}s] all tables copied")
    create_schema(conn, triggers=True, indexes=True)   # indexes after the load: sorted, dense
    log(f"[{time.monotonic() - started:6.0f}s] indexes built")
    conn.execute("PRAGMA user_version=4")
    conn.execute("INSERT OR REPLACE INTO meta VALUES('schema_version', '4')")
    conn.execute("INSERT OR REPLACE INTO meta VALUES('migrated_v3_v4_at', strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()
    return counts


def verify(source: Path, target: Path, *, sample: int = 20000, log=print) -> dict:
    """Every row of the three rebuilt tables against v3, sampled SHA-256 recomputation, FTS integrity."""
    from .paragraphs import decode_blocks
    started = time.monotonic()
    conn = sqlite3.connect(f"file:{target}", uri=True)   # writable: the FTS integrity-check is an INSERT
    conn.execute(f"ATTACH DATABASE 'file:{source}?mode=ro' AS v3")
    report: dict = {"problems": []}
    tables = [r[0] for r in conn.execute("SELECT name FROM v3.sqlite_master WHERE type='table' AND name NOT LIKE "
                                         "'sqlite_%'")]
    counts = {}
    for name in tables:
        old = conn.execute(f"SELECT count(*) FROM v3.{name}").fetchone()[0]
        target_name = "taper_courts" if name == "amtsgraph_courts" else name
        new = conn.execute(f"SELECT count(*) FROM main.{target_name}").fetchone()[0]
        counts[name] = [old, new]
        if old != new and not (name == "meta" and new == old + 1):   # + migrated_v3_v4_at
            report["problems"].append(f"{name}: {old} rows in v3, {new} in v4")
    report["row_counts"] = counts
    # paragraphs: every row - id, ordinal, text, hash, blocks
    mismatch = conn.execute("""
        SELECT count(*) FROM v3.paragraphs o JOIN main.paragraphs n ON n.rowid = o.rowid
        WHERE n.decision_id || '#' || n.anchor <> o.id OR n.decision_id <> o.decision_id OR n.ordinal <> o.ordinal
           OR n.text <> o.text OR n.text_hash <> unhex(o.text_hash) OR n.section <> o.section
           OR n.authorship <> o.authorship OR n.rn IS NOT o.rn OR n.numbering <> o.numbering
           OR n.block_count <> o.block_count""").fetchone()[0]
    report["paragraph_rows_differing"] = mismatch
    if mismatch:
        report["problems"].append(f"{mismatch} paragraphs differ")
    bad_blocks = 0
    for old_blocks, new_blocks in conn.execute("SELECT o.blocks, n.blocks FROM v3.paragraphs o JOIN main.paragraphs n "
                                               "ON n.rowid = o.rowid"):
        if decode_blocks(new_blocks) != json.loads(old_blocks):
            bad_blocks += 1
    report["blocks_differing"] = bad_blocks
    if bad_blocks:
        report["problems"].append(f"{bad_blocks} blocks do not decode to the v3 blocks")
    log(f"[{time.monotonic() - started:6.0f}s] paragraphs compared")
    # sampled SHA-256 recomputation (the stored hash is what readers check)
    top = conn.execute("SELECT max(rowid) FROM main.paragraphs").fetchone()[0] or 0
    rng = random.Random(4)
    rowids = rng.sample(range(1, top + 1), min(sample, top))
    wrong = 0
    for i in range(0, len(rowids), 500):
        chunk = rowids[i:i + 500]
        for text, digest in conn.execute(f"SELECT text, text_hash FROM main.paragraphs WHERE rowid IN "
                                         f"({','.join('?' * len(chunk))})", chunk):
            wrong += hashlib.sha256(text.encode()).digest() != digest
    report["sampled_hashes"] = [len(rowids), wrong]
    if wrong:
        report["problems"].append(f"{wrong} sampled paragraph hashes do not match their text")
    # relations: every row - columns, paragraph mapping, detail round trip
    mismatch = conn.execute("""
        SELECT count(*) FROM v3.relations o JOIN main.relations n ON n.id = o.id
        LEFT JOIN main.paragraphs p ON p.decision_id = n.decision_id AND p.ordinal = n.paragraph_ordinal
        WHERE n.decision_id <> o.decision_id OR n.relation <> o.relation OR n.target_id IS NOT o.target_id
           OR n.target_raw <> o.target_raw OR n.span_start IS NOT o.span_start OR n.span_end IS NOT o.span_end
           OR n.quoted_context <> o.quoted_context OR n.target_decision_id IS NOT o.target_decision_id
           OR (o.paragraph_id IS NULL) <> (n.paragraph_ordinal IS NULL)
           OR (o.paragraph_id IS NOT NULL AND p.decision_id || '#' || p.anchor IS NOT o.paragraph_id)""").fetchone()[0]
    report["relation_rows_differing"] = mismatch
    if mismatch:
        report["problems"].append(f"{mismatch} relations differ")
    bad_detail = 0
    conn.row_factory = sqlite3.Row
    for row in conn.execute("SELECT o.detail AS old, n.detail, n.target_kind, n.target_id, n.target_raw, "
                            "n.target_court_id, n.target_date, n.target_case_key FROM v3.relations o "
                            "JOIN main.relations n ON n.id = o.id"):
        if expand_detail(row["detail"], row) != _archived_detail(row["old"]):
            bad_detail += 1
    conn.row_factory = None
    report["details_differing"] = bad_detail
    if bad_detail:
        report["problems"].append(f"{bad_detail} relation details do not expand to the v3 detail")
    log(f"[{time.monotonic() - started:6.0f}s] relations compared")
    mismatch = conn.execute("""
        SELECT count(*) FROM v3.relation_assessments o JOIN main.relation_assessments n ON n.id = o.id
        LEFT JOIN main.paragraphs p ON p.decision_id = n.decision_id AND p.ordinal = n.paragraph_ordinal
        WHERE n.assessment <> o.assessment OR n.target_id IS NOT o.target_id OR n.status <> o.status
           OR (o.paragraph_id IS NOT NULL AND p.decision_id || '#' || p.anchor IS NOT o.paragraph_id)""").fetchone()[0]
    report["assessment_rows_differing"] = mismatch
    if mismatch:
        report["problems"].append(f"{mismatch} relation_assessments differ")
    # FTS: the copied index against the new content table
    try:
        conn.execute("INSERT INTO main.paragraphs_fts(paragraphs_fts, rank) VALUES('integrity-check', 1)")
        report["fts_integrity"] = "ok"
    except sqlite3.DatabaseError as exc:
        report["fts_integrity"] = str(exc)
        report["problems"].append(f"FTS integrity-check: {exc}")
    probes = ['"Streitgegenstand der erhobenen Klage"', '"keine Anwendung" AND Anhörungsrüge',
              'Untätigkeitsklage NEAR(Mitteilung, 10)', '"im Sinne des" AND Rechtssache']
    report["fts_probes"] = {}
    for q in probes:
        old = conn.execute("SELECT count(*) FROM v3.paragraphs_fts(?)", (q,)).fetchone()[0]
        new = conn.execute("SELECT count(*) FROM main.paragraphs_fts(?)", (q,)).fetchone()[0]
        report["fts_probes"][q] = [old, new]
        if old != new:
            report["problems"].append(f"FTS {q}: {old} hits in v3, {new} in v4")
    report["integrity_check"] = conn.execute("PRAGMA main.quick_check").fetchone()[0]
    if report["integrity_check"] != "ok":
        report["problems"].append(f"quick_check: {report['integrity_check']}")
    log(f"[{time.monotonic() - started:6.0f}s] verified: {len(report['problems'])} problems")
    conn.close()
    return report
