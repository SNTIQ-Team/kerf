"""Corrections ledger (operating principle 5): append-only, with evidence.

Every error found by the user, an agent or a verifier is one row in
``corrections``.  Supported kinds are applied as an overlay after every
(re)parse, so a re-parse never silently undoes a correction:

    decision_field       decisions.<field> := new         (decision_date, decision_type, court_id, ...)
    case_numbers         decisions.case_numbers := new (JSON array)
    relation_assessment  confirm or reject an assessment of a parser relation
    paragraph_boundary   a quote belongs to another paragraph (recorded; needs a parser fix)
    relation             a parser relation is wrong (recorded; needs a parser fix)

Recording a correction also writes a conformance case under
``conformance/cases/courts/`` (inputs copied out of the packs, so the case is
self-contained) with ``status: open`` until the parser itself produces the
corrected result.  The table rejects UPDATE and DELETE.
"""
from __future__ import annotations

import gzip
import json
import re
import sqlite3
from pathlib import Path

from .paragraphs import split_paragraph_id
from . import paragraph_ranges

SCHEMA = """
CREATE TABLE IF NOT EXISTS corrections(
  id INTEGER PRIMARY KEY,
  target TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('decision_field','case_numbers','relation_assessment','paragraph_boundary',
                                     'relation','other')),
  field TEXT,
  old TEXT,
  new TEXT NOT NULL,
  evidence TEXT NOT NULL,
  author TEXT NOT NULL,
  created_at TEXT NOT NULL,
  fixture TEXT
);
CREATE INDEX IF NOT EXISTS corrections_target ON corrections(target);
CREATE TRIGGER IF NOT EXISTS corrections_append_only_update BEFORE UPDATE ON corrections
BEGIN SELECT RAISE(ABORT, 'corrections ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS corrections_append_only_delete BEFORE DELETE ON corrections
BEGIN SELECT RAISE(ABORT, 'corrections ledger is append-only'); END;
"""
DECISION_FIELDS = {"decision_date", "decision_type", "court_id", "court_slug", "court_name", "ecli", "chamber", "title",
                   "jurisdiction", "land"}
ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "conformance" / "cases" / "courts"
FIXTURES = ROOT / "conformance" / "fixtures" / "corrections"


class CorrectionError(ValueError):
    pass


def _decision_of(target: str) -> str:
    return target.split("#", 1)[0].split("|", 1)[0]


def record(conn: sqlite3.Connection, *, target: str, kind: str, new, evidence: str, author: str,
           field: str | None = None, target_id: str | None = None, quote: str | None = None,
           home: Path | None = None, write_fixture: bool = True) -> dict:
    """Validate, append to the ledger, apply, and write the conformance case."""
    if not evidence.strip() or not author.strip():
        raise CorrectionError("a correction needs evidence and an author")
    decision_id = _decision_of(target)
    decision = conn.execute("SELECT * FROM decisions WHERE id=?", (decision_id,)).fetchone()
    if decision is None:
        raise CorrectionError(f"unknown decision {decision_id!r}")
    old = None
    if kind == "decision_field":
        if field not in DECISION_FIELDS:
            raise CorrectionError(f"field must be one of {sorted(DECISION_FIELDS)}")
        old = decision[field]
        if field == "decision_date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(new)):
            raise CorrectionError("decision_date must be YYYY-MM-DD")
    elif kind == "case_numbers":
        if not (isinstance(new, list) and new and all(isinstance(x, str) and x for x in new)):
            raise CorrectionError("case_numbers must be a non-empty JSON array of strings")
        old = json.loads(decision["case_numbers"])
    elif kind == "relation_assessment":
        if not target_id or not isinstance(new, dict) or new.get("status") not in ("confirmed", "rejected") \
                or not new.get("assessment"):
            raise CorrectionError('relation_assessment needs --target-id and new={"assessment": .., '
                                  '"status": "confirmed"|"rejected"}')
        from .polarity import STORED_ASSESSMENTS
        if new["assessment"] not in STORED_ASSESSMENTS:
            raise CorrectionError(f"assessment must be one of {sorted(STORED_ASSESSMENTS)}")
        field = target_id
    elif kind == "paragraph_boundary" and isinstance(new, dict):
        key = split_paragraph_id(target)
        if not key or not isinstance(new.get("anchor"), str) or not quote or paragraph_ranges.text_hash(quote) != new.get("fragment_sha256"):
            raise CorrectionError("a former paragraph range requires its full earlier text")
        row = conn.execute("""SELECT p.anchor, p.text, lower(hex(p.text_hash)) AS text_hash, d.content_hash
                             FROM paragraphs p JOIN decisions d ON d.id=p.decision_id
                             WHERE p.decision_id=? AND p.anchor=?""", (decision_id, new.get("anchor"))).fetchone()
        if not paragraph_ranges.valid_range(new, row, key[1]):
            raise CorrectionError("former range differs from the source capture or canonical paragraph")
        existing = conn.execute("SELECT text FROM paragraphs WHERE decision_id=? AND anchor=?", key).fetchone()
        if existing is not None and existing["text"] != quote:
            raise CorrectionError("earlier address currently identifies different text")
        field = paragraph_ranges.FIELD
        prior = conn.execute("SELECT id,new,fixture FROM corrections WHERE target=? AND kind=? AND field=? ORDER BY id DESC LIMIT 1",
                             (target, kind, field)).fetchone()
        if prior and json.loads(prior["new"]) == new:
            return {"id": prior["id"], "target": target, "kind": kind, "field": field,
                    "old": None, "new": new, "fixture": prior["fixture"]}
    elif kind == "paragraph_boundary":
        if not quote or not isinstance(new, str):
            raise CorrectionError("paragraph_boundary needs --quote and the correct paragraph id suffix as new")
        field = quote
    elif kind == "relation" and not target_id:
        raise CorrectionError("relation corrections need --target-id")
    ranged = kind == "paragraph_boundary" and isinstance(new, dict)
    fixture = _write_fixture(conn, decision, kind=kind, field=quote if ranged else field,
                             new=new["anchor"] if ranged else new, target=target, home=home) if write_fixture else None
    cur = conn.execute("""INSERT INTO corrections(target, kind, field, old, new, evidence, author, created_at,
                                                  fixture)
                          VALUES(?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%S+00:00','now'),?)""",
                       (target, kind, field, json.dumps(old, ensure_ascii=False), json.dumps(new, ensure_ascii=False),
                        evidence, author, fixture))
    apply_corrections(conn, decision_id)
    return {"id": cur.lastrowid, "target": target, "kind": kind, "field": field, "old": old, "new": new,
            "fixture": fixture}


def paragraph_ordinal(conn: sqlite3.Connection, paragraph_id: str) -> int | None:
    """The (decision_id, ordinal) key of a public paragraph id ``<decision>#<anchor>`` (schema v4)."""
    key = split_paragraph_id(paragraph_id)
    row = conn.execute("SELECT ordinal FROM paragraphs WHERE decision_id=? AND anchor=?", key).fetchone() \
        if key else None
    return row[0] if row else None


def apply_corrections(conn: sqlite3.Connection, decision_id: str) -> int:
    """Overlay the ledger on one decision (run after every store / re-parse)."""
    rows = conn.execute("SELECT * FROM corrections WHERE target = ? OR target LIKE ? OR target LIKE ? ORDER BY id",
                        (decision_id, decision_id + "#%", decision_id + "|%")).fetchall()
    applied = 0
    for row in rows:
        new = json.loads(row["new"])
        if row["kind"] == "decision_field":
            conn.execute(f"UPDATE decisions SET {row['field']}=? WHERE id=?", (new, decision_id))
        elif row["kind"] == "case_numbers":
            from .casenumbers import case_key
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute("UPDATE decisions SET case_numbers=? WHERE id=?", (json.dumps(new, ensure_ascii=False),
                                                                              decision_id))
                conn.execute("DELETE FROM case_numbers WHERE decision_id=?", (decision_id,))
                conn.executemany("INSERT INTO case_numbers VALUES(?,?,?,?)",
                                 [(decision_id, i, c, case_key(c)) for i, c in enumerate(new, 1)])
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        elif row["kind"] == "relation_assessment":
            ordinal = paragraph_ordinal(conn, row["target"]) if "#" in row["target"] else None
            if "#" in row["target"] and ordinal is None:
                continue  # the paragraph is gone (re-parse changed the anchors): the correction waits
            where = "decision_id=? AND target_id=? AND assessment=?" + \
                (" AND paragraph_ordinal=?" if ordinal is not None else "")
            params = [decision_id, row["field"], new["assessment"]] + ([ordinal] if ordinal is not None else [])
            conn.execute(f"UPDATE relation_assessments SET status=? WHERE derived_by='rules' AND {where}",
                         [new["status"], *params])
            if new["status"] == "confirmed" and not conn.execute(
                    f"SELECT 1 FROM relation_assessments WHERE derived_by='user' AND {where}", params).fetchone():
                from .polarity import ASSESSMENTS
                conn.execute("""INSERT INTO relation_assessments(decision_id, paragraph_ordinal, target_kind, target_id,
                                  assessment, polarity, cue, evidence_text, rule_id, derived_by, legal_effect_verified,
                                  confidence, classifier_version, status, created_at)
                                VALUES(?,?,?,?,?,?,'',?,?, 'user', 1, 1.0, 'corrections-ledger', 'confirmed',
                                       strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))""",
                             (decision_id, ordinal, "decision" if not str(row["field"]).startswith(("de/", "eu/", "int/"))
                              else "norm", row["field"], new["assessment"], ASSESSMENTS[new["assessment"]],
                              row["evidence"], f"correction:{row['id']}"))
        else:
            continue  # recorded only: the parser must be fixed (the conformance case stays open until then)
        applied += 1
    return applied


# --- conformance fixture ------------------------------------------------------------------------

def _copy_capture(conn: sqlite3.Connection, decision, home: Path | None) -> tuple[str, str] | None:
    from .store import read_capture
    try:
        body = read_capture(conn, decision["content_hash"], decision["capture_path"], home)
    except (OSError, ValueError):
        return None
    ext = (conn.execute("SELECT extension FROM captures WHERE sha256=?", (decision["content_hash"],)).fetchone()
           or ["bin"])[0]
    FIXTURES.mkdir(parents=True, exist_ok=True)
    path = FIXTURES / f"{decision['content_hash'][:16]}.{ext}.gz"
    if not path.exists():
        path.write_bytes(gzip.compress(body, mtime=0))
    return str(path.relative_to(ROOT)), ext


def _item(conn: sqlite3.Connection, decision) -> dict:
    row = conn.execute("SELECT * FROM source_items WHERE source_id=? AND source_doc_id=?",
                       (decision["source_id"], decision["source_doc_id"])).fetchone()
    item = {"source_doc_id": decision["source_doc_id"], "url": decision["source_url"]}
    if row is not None:
        item.update({k: row[k] for k in ("url", "listed_date", "listed_court", "listed_case_numbers")})
        item["extra"] = json.loads(row["extra"] or "{}")
    return item


def _write_fixture(conn: sqlite3.Connection, decision, *, kind: str, field, new, target: str,
                   home: Path | None) -> str | None:
    number = (conn.execute("SELECT coalesce(max(id), 0) + 1 FROM corrections").fetchone()[0])
    slug = re.sub(r"[^a-z0-9]+", "-", f"{decision['source_id']}-{decision['id']}".lower()).strip("-")[:60]
    case: dict = {"format": "sntiq-conformance/1", "id": f"courts.correction.{number}.{slug}", "module": "courts",
                  "status": "open", "origin": f"corrections ledger #{number} ({kind})", "match": "subset"}
    if kind in ("decision_field", "case_numbers", "paragraph_boundary"):
        copied = _copy_capture(conn, decision, home)
        if copied is None:
            return None
        fixture, _ = copied
        base = {"adapter": decision["source_id"], "fixture": fixture, "item": _item(conn, decision),
                "url": decision["source_url"]}
        if kind == "decision_field":
            case.update(kind="decision_metadata", input=base, expected={field: new},
                        description=f"{decision['id']}: {field} must be {new!r}")
        elif kind == "case_numbers":
            case.update(kind="decision_metadata", input=base, expected={"case_numbers": new},
                        description=f"{decision['id']}: case numbers must be {new!r}")
        else:
            case.update(kind="quote_to_paragraph", input={**base, "quote": field}, expected={"paragraph_ids": [new]},
                        description=f"{decision['id']}: quote belongs to {new}")
    elif kind in ("relation_assessment", "relation"):
        key = split_paragraph_id(target) if "#" in target else None
        text = conn.execute("SELECT text FROM paragraphs WHERE decision_id=? AND anchor=?", key).fetchone() \
            if key else None
        if text is None:
            return None
        inp = {"text": text[0], "own_court_id": decision["court_slug"], "land": decision["land"],
               "jurisdiction": decision["jurisdiction"]}
        if kind == "relation_assessment":
            key = f"{new['assessment']}|{field}"
            expected = {"assessment_keys": {"$contains" if new["status"] == "confirmed" else "$excludes": [key]}}
            case.update(kind="relation_assessments", input=inp, expected=expected,
                        description=f"{target}: {new['assessment']} of {field} is {new['status']}")
        else:
            case.update(kind="norm_citations" if str(field).startswith(("de/", "eu/", "int/")) else
                        "decision_citations", input=inp, expected={"target_set": {"$contains": [new]}},
                        description=f"{target}: parser must produce {new!r}")
    else:
        return None
    CASES.mkdir(parents=True, exist_ok=True)
    path = CASES / f"correction-{number:04d}-{slug}.json"
    path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return str(path.relative_to(ROOT))
