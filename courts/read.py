"""Read API over the local corpus, returning the spec's evidence envelope.

Rules implemented here (and mirrored by the Node MCP, see SCHEMA.md):
* ``total`` is ``null`` and ``status`` is ``partial`` whenever an in-scope
  source is unavailable (blocked, unavailable, never synced).  Never a silent 0.
* A zero result over sources whose backfill is incomplete is
  ``not_found_in_searched_scope``, not ``ok``.
* Paragraph text is returned exactly as stored and verified against its hash.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

from . import PARSER_VERSION, SCHEMA_VERSION
from .casenumbers import case_key
from .config import db_path

UNAVAILABLE = {"blocked", "unavailable", "never_synced"}
EXCERPT = 600


class InvalidArgument(ValueError):
    pass


def open_db(path: Path | None = None) -> sqlite3.Connection:
    path = path or db_path()
    if not path.exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.close()
        raise RuntimeError(f"corpus schema v{version}, reader expects v{SCHEMA_VERSION}")
    return conn


def _envelope(status: str, *, request_scope: dict, searched: list, evidence: list, total, warnings=None,
              failed=None, applied=None) -> dict:
    return {"status": status, "request_scope": request_scope, "applied_constraints": applied or {},
            "ignored_arguments": [], "snapshot_id": None, "searched": searched, "evidence": evidence,
            "warnings": warnings or [], "failed_components": failed or [], "total": total}


def fts_query(q: str) -> str | None:
    """Safe FTS5 query: quoted phrases kept, other tokens AND-ed as literals."""
    phrases = re.findall(r'"([^"]+)"', q)
    rest = re.sub(r'"[^"]+"', " ", q)
    parts = []
    for phrase in phrases:
        tokens = re.findall(r"[^\W_]+", phrase, re.UNICODE)
        if tokens:
            parts.append('"' + " ".join(tokens) + '"')
    parts += ['"' + t + '"' for t in re.findall(r"[^\W_]+", rest, re.UNICODE)]
    return " AND ".join(parts) if parts else None


def sources_state(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {r["source_id"]: r for r in conn.execute("SELECT * FROM source_coverage")}


def _pinpoint(row) -> str:
    if row["rn"]:
        return f"Rn. {row['rn']}"
    return row["id"].split("#", 1)[1]


def evidence_item(row, *, excerpt: bool = True, match_terms: list[str] = ()) -> dict:
    text = row["text"]
    shown, start = text, 0
    if excerpt and len(text) > EXCERPT:
        positions = [m.start() for t in match_terms for m in [re.search(re.escape(t), text, re.I)] if m]
        start = max(0, min(positions) - 150) if positions else 0
        shown = text[start:start + EXCERPT]
    return {
        "id": row["id"], "canonical_id": row["decision_id"], "version_id": row["text_hash"],
        "pinpoint": _pinpoint(row),
        "text": shown, "text_offset": start, "text_truncated": len(shown) < len(text),
        "section": row["section"], "authorship": row["authorship"],
        "decision": {"id": row["decision_id"], "ecli": row["ecli"], "court_id": row["court_id"],
                     "court_slug": row["court_slug"],
                     "court_name": row["court_name"], "decision_type": row["decision_type"],
                     "decision_date": row["decision_date"], "case_numbers": json.loads(row["case_numbers"]),
                     "case_numbers_raw": row["case_numbers_raw"], "numbering": row["numbering_d"]},
        "source": {"url": row["source_url"], "role": "official_court_publication", "source_id": row["source_id"],
                   "observed_at": row["observed_at"], "content_hash": row["content_hash"],
                   "parser_version": row["parser_version"]},
        "quality": {"source_exact": True, "complete": not (len(shown) < len(text)),
                    "reconstruction_status": "exact_source_text"},
        "temporal": {"decision_date": row["decision_date"],
                     "note": "decision date is not the date of the applicable law (paper 5.2)"},
        "relations": [],
    }


PARAGRAPH_COLUMNS = """p.rowid AS rowid, p.id, p.decision_id, p.section, p.authorship, p.rn, p.numbering, p.text,
  p.text_hash, p.block_count, d.ecli, d.court_id, d.court_slug, d.court_name, d.decision_type, d.decision_date,
  d.case_numbers, d.case_numbers_raw, d.numbering AS numbering_d, d.source_url, d.source_id,
  d.observed_at, d.content_hash, d.parser_version"""


def search(conn: sqlite3.Connection, q: str | None = None, *, court: str | None = None,
           date_from: str | None = None, date_to: str | None = None, case_number: str | None = None,
           ecli: str | None = None, norm: str | None = None, relation: str | None = None,
           source: str | list[str] | None = None, section: str | None = None, limit: int = 10,
           offset: int = 0) -> dict:
    scope = {k: v for k, v in dict(q=q, court=court, date_from=date_from, date_to=date_to,
                                   case_number=case_number, ecli=ecli, norm=norm, relation=relation,
                                   source=source, section=section, limit=limit, offset=offset).items()
             if v is not None}
    if not 1 <= limit <= 50 or offset < 0:
        return _envelope("invalid_argument", request_scope=scope, searched=[], evidence=[], total=None,
                         warnings=["limit must be 1..50, offset >= 0"])
    for label, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return _envelope("invalid_argument", request_scope=scope, searched=[], evidence=[], total=None,
                             warnings=[f"{label} must be YYYY-MM-DD"])
    if relation is not None and relation not in {"mentions", "quotes", "procedural_reference"}:
        return _envelope("invalid_argument", request_scope=scope, searched=[], evidence=[], total=None,
                         warnings=["relation must be mentions|quotes|procedural_reference"])
    match = fts_query(q) if q else None
    if q and not match:
        return _envelope("invalid_argument", request_scope=scope, searched=[], evidence=[], total=None,
                         warnings=["q has no searchable tokens"])
    states = sources_state(conn)
    wanted = [source] if isinstance(source, str) else list(source or [])
    in_scope = sorted(set(wanted)) if wanted else sorted(states)
    unknown = [s for s in in_scope if s not in states]
    if unknown:
        return _envelope("not_in_corpus", request_scope=scope, evidence=[], total=None,
                         searched=[{"source": s, "status": "unknown", "count": None} for s in unknown])
    where, params = ["1=1"], []
    join = ""
    if match:
        join += " JOIN paragraphs_fts f ON f.rowid = p.rowid"
        where.append("paragraphs_fts MATCH ?")
        params.append(match)
    if court:
        where.append("(d.court_id = ? OR d.court_slug = ?)")   # Amtsgraph id or corpus slug
        params += [court, court]
    if date_from:
        where.append("d.decision_date >= ?")
        params.append(date_from)
    if date_to:
        where.append("d.decision_date <= ?")
        params.append(date_to)
    if ecli:
        where.append("d.ecli = ?")
        params.append(ecli)
    if section:
        where.append("p.section = ?")
        params.append(section)
    if case_number:
        where.append("d.id IN (SELECT decision_id FROM case_numbers WHERE case_key = ?)")
        params.append(case_key(case_number))
    if norm or relation:
        sub = ["r.paragraph_id = p.id"]
        if norm:
            sub.append("(r.target_id = ? OR (r.target_id >= ? AND r.target_id < ?))")
            params += [norm, norm + "/", norm + "0"]
        if relation:
            sub.append("r.relation = ?")
            params.append(relation)
        where.append("EXISTS (SELECT 1 FROM relations r WHERE " + " AND ".join(sub) + ")")
    if wanted:
        where.append(f"d.source_id IN ({','.join('?' * len(in_scope))})")
        params += in_scope
    base = f" FROM paragraphs p JOIN decisions d ON d.id = p.decision_id{join} WHERE " + " AND ".join(where)
    counts = {r[0]: r[1] for r in conn.execute("SELECT d.source_id, count(*)" + base + " GROUP BY d.source_id",
                                                params)}
    order = "bm25(paragraphs_fts), d.decision_date DESC, p.ordinal" if match else "d.decision_date DESC, p.decision_id, p.ordinal"
    rows = conn.execute(f"SELECT {PARAGRAPH_COLUMNS}" + base + f" ORDER BY {order} LIMIT ? OFFSET ?",
                        (*params, limit, offset)).fetchall()
    searched, warnings, failed = [], [], []
    unavailable = False
    incomplete = False
    for sid in in_scope:
        st = states[sid]
        available = st["status"] not in UNAVAILABLE
        unavailable |= not available
        if available and ((st["pending_items"] or 0) > 0 or not st["listing_complete"]):
            incomplete = True
            upstream = st["upstream_listed"]
            warnings.append(f"{sid}: local corpus holds {st['decisions']} decisions; backfill incomplete "
                            f"({st['pending_items']} listed but not synced; upstream total "
                            f"{upstream if upstream is not None else 'unknown'})")
        if not available:
            failed.append({"source": sid, "status": st["status"], "reason": st["status_reason"]})
        searched.append({"source": sid, "status": st["status"] if not available else "ok",
                         "count": counts.get(sid, 0) if available else None})
    terms = re.findall(r"[^\W_]+", q or "", re.UNICODE)
    evidence = [evidence_item(r, match_terms=terms) for r in rows]
    if unavailable:
        return _envelope("partial", request_scope=scope, searched=searched, evidence=evidence, total=None,
                         warnings=warnings + ["an in-scope source is unavailable: total is unknown"],
                         failed=failed, applied=scope)
    total = sum(counts.get(s, 0) for s in in_scope)
    if total == 0 and incomplete:
        status = "not_found_in_searched_scope"
    else:
        status = "partial" if incomplete else "ok"
    return _envelope(status, request_scope=scope, searched=searched, evidence=evidence, total=total,
                     warnings=warnings, applied=scope)


def resolve_decision(conn: sqlite3.Connection, ref: str) -> tuple[str, list[str]]:
    """Return (status, candidate ids) for an id, ECLI, source doc id or Aktenzeichen."""
    ids = [r["id"] for r in conn.execute("SELECT id FROM decisions WHERE id = ? OR ecli = ? ORDER BY id", (ref, ref))]
    if len(ids) == 1:
        return "ok", ids
    if ids:  # one portal gave two documents the same ECLI: both are candidates
        return "ambiguous_identity", ids
    if ":" in ref and not ref.startswith(("court:", "ECLI:")):
        source_id, _, doc = ref.partition(":")
        row = conn.execute("SELECT id FROM decisions WHERE source_id=? AND source_doc_id=?", (source_id, doc)).fetchone()
        if row:
            return "ok", [row["id"]]
    row = conn.execute("SELECT id FROM decisions WHERE source_doc_id = ?", (ref,)).fetchone()
    if row:
        return "ok", [row["id"]]
    ids = [r["decision_id"] for r in conn.execute(
        "SELECT DISTINCT decision_id FROM case_numbers WHERE case_key = ?", (case_key(ref),))]
    if len(ids) == 1:
        return "ok", ids
    if len(ids) > 1:
        return "ambiguous_identity", ids
    return "not_in_corpus", []


def get_decision(conn: sqlite3.Connection, ref: str, *, with_paragraphs: bool = True,
                 with_relations: bool = True) -> dict:
    status, ids = resolve_decision(conn, ref)
    if status != "ok":
        return {"status": status, "request": ref, "candidates": ids}
    d = conn.execute("SELECT * FROM decisions WHERE id = ?", (ids[0],)).fetchone()
    result = {k: d[k] for k in d.keys() if k not in ("metadata", "case_numbers")}
    result["case_numbers"] = json.loads(d["case_numbers"])
    result["metadata"] = json.loads(d["metadata"])
    result["sections"] = [dict(r) for r in conn.execute(
        "SELECT name, authorship, authorship_basis, heading FROM sections WHERE decision_id=? ORDER BY position",
        (d["id"],))]
    if with_paragraphs:
        result["paragraphs"] = [
            {"id": p["id"], "section": p["section"], "authorship": p["authorship"], "rn": p["rn"],
             "numbering": p["numbering"], "block_count": p["block_count"], "text": p["text"],
             "text_hash": p["text_hash"]}
            for p in conn.execute("SELECT * FROM paragraphs WHERE decision_id=? ORDER BY ordinal", (d["id"],))]
    if with_relations:
        result["relations"] = [relation_row(r, conn) for r in conn.execute(
            "SELECT * FROM relations WHERE decision_id=? ORDER BY id", (d["id"],)).fetchall()]
    return {"status": "ok", "decision": result}


CONTEXT_WIDTH = 160


def context_for(conn, r) -> str:
    """quoted_context as stored, or derived from paragraphs.text and the span (stored empty to save space)."""
    if r["quoted_context"] or r["paragraph_id"] is None or r["span_start"] is None:
        return r["quoted_context"]
    row = conn.execute("SELECT text FROM paragraphs WHERE id = ?", (r["paragraph_id"],)).fetchone()
    if row is None:
        return ""
    text = row[0]
    return " ".join(text[max(0, r["span_start"] - CONTEXT_WIDTH):r["span_end"] + CONTEXT_WIDTH].split())


# Which relation_assessments rows belong to parser relation r (alias a / r).  Rule rows carry relation_id.
# User rows (corrections ledger) carry none: they attach within their own decision only - a paragraph-level
# row to the relations of that paragraph with the same target, a decision-level row (paragraph_id NULL) to
# every relation of the decision with that target.  The single definition, used by every reader.
ASSESSMENT_OF_RELATION = ("(a.relation_id = r.id OR (a.relation_id IS NULL AND a.decision_id = r.decision_id"
                          " AND a.target_id IS r.target_id AND (a.paragraph_id IS NULL"
                          " OR a.paragraph_id = r.paragraph_id)))")


def assessments_for(conn, r) -> list[dict]:
    """Classifier proposals and user decisions for one parser relation (polarity always explicit)."""
    rows = conn.execute(f"""SELECT a.assessment, a.polarity, a.cue, a.rule_id, a.derived_by, a.legal_effect_verified,
                                   a.status, a.confidence, a.classifier_version
                            FROM relation_assessments a JOIN relations r ON r.id = ?
                            WHERE {ASSESSMENT_OF_RELATION}
                            ORDER BY a.derived_by DESC, a.confidence DESC""", (r["id"],)).fetchall()
    return [dict(x, legal_effect_verified=bool(x["legal_effect_verified"])) for x in rows]


def relation_row(r, conn=None) -> dict:
    return {"relation": r["relation"], "target_kind": r["target_kind"], "target_id": r["target_id"],
            "assessments": assessments_for(conn, r) if conn is not None else [],
            "target_decision_id": r["target_decision_id"], "target_raw": r["target_raw"],
            "origin": r["origin"], "source_id": r["decision_id"],
            "evidence": {"paragraph_id": r["paragraph_id"],
                         "quoted_context": context_for(conn, r) if conn is not None else r["quoted_context"],
                         "span": [r["span_start"], r["span_end"]] if r["span_start"] is not None else None,
                         "derived_by": r["derived_by"], "legal_effect_verified": bool(r["legal_effect_verified"]),
                         "confidence": r["confidence"]},
            "detail": json.loads(r["detail"])}


def get_paragraph(conn: sqlite3.Connection, paragraph_id: str) -> dict:
    row = conn.execute(f"SELECT {PARAGRAPH_COLUMNS}, p.blocks FROM paragraphs p JOIN decisions d "
                       "ON d.id = p.decision_id WHERE p.id = ?", (paragraph_id,)).fetchone()
    if row is None:
        return {"status": "not_in_corpus", "request": paragraph_id}
    if hashlib.sha256(row["text"].encode()).hexdigest() != row["text_hash"]:
        return {"status": "integrity_check_failed", "request": paragraph_id}
    item = evidence_item(row, excerpt=False)
    item["blocks"] = [dict(b, text=row["text"][b["start"]:b["end"]]) for b in json.loads(row["blocks"])]
    item["relations"] = [relation_row(r, conn) for r in conn.execute(
        "SELECT * FROM relations WHERE paragraph_id=? ORDER BY span_start", (paragraph_id,)).fetchall()]
    return {"status": "ok", "evidence": [item]}


def citing_decisions(conn: sqlite3.Connection, target: str, *, relation: str | None = None,
                     assessment: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    """Decisions whose text relates to a norm (prefix match on the identity) or a decision."""
    if relation is not None and relation not in {"mentions", "quotes", "procedural_reference"}:
        return {"status": "invalid_argument", "warnings": ["unknown relation"]}
    if target.startswith(("de/", "eu/", "int/")):
        cond, params = "(r.target_id = ? OR (r.target_id >= ? AND r.target_id < ?))", [target, target + "/", target + "0"]
    else:
        status, ids = resolve_decision(conn, target)
        cond = "(r.target_id = ? OR r.target_decision_id = ?)"
        params = [target, ids[0] if status == "ok" else target]
    if relation:
        cond += " AND r.relation = ?"
        params.append(relation)
    if assessment:
        from .polarity import STORED_ASSESSMENTS
        if assessment not in STORED_ASSESSMENTS:
            return {"status": "invalid_argument",
                    "warnings": [f"assessment must be one of {sorted(STORED_ASSESSMENTS)}"]}
        cond += (" AND EXISTS (SELECT 1 FROM relation_assessments a WHERE a.assessment = ? AND a.status <> 'rejected'"
                 f" AND {ASSESSMENT_OF_RELATION})")
        params.append(assessment)
    total = conn.execute(f"SELECT count(*) FROM relations r WHERE {cond}", params).fetchone()[0]
    rows = conn.execute(f"SELECT r.* FROM relations r JOIN decisions d ON d.id = r.decision_id WHERE {cond} "
                        "ORDER BY d.decision_date DESC, r.id LIMIT ? OFFSET ?", (*params, limit, offset)).fetchall()
    return {"status": "ok", "target": target,
            "relation_types_included": [relation] if relation else ["mentions", "quotes", "procedural_reference"],
            "assessment_filter": assessment,
            "note": "parser relations (mentions/quotes/procedural_reference) record that a decision cites a target, "
                    "not that it applies it; each carries its rule/user assessments with explicit polarity "
                    "(negative ones such as holds_inapplicable, rejects_application_of, distinguishes, "
                    "departs_from must never be presented as support)",
            "total": total, "relations": [relation_row(r, conn) for r in rows]}


def coverage(conn: sqlite3.Connection) -> dict:
    sources = []
    for row in conn.execute("SELECT * FROM source_coverage ORDER BY source_id"):
        item = dict(row)
        item["failure_kinds"] = {r[0]: r[1] for r in conn.execute(
            "SELECT failure_kind, count(*) FROM source_items WHERE source_id=? AND state='failed' "
            "GROUP BY failure_kind", (row["source_id"],))}
        last = conn.execute("SELECT * FROM sync_runs WHERE source_id=? ORDER BY id DESC LIMIT 1",
                            (row["source_id"],)).fetchone()
        item["last_run"] = dict(last) if last else None
        sources.append(item)
    totals = conn.execute("SELECT (SELECT count(*) FROM decisions), (SELECT count(*) FROM paragraphs), "
                          "(SELECT count(*) FROM relations), (SELECT count(*) FROM captures), "
                          "(SELECT count(DISTINCT pack) FROM captures)").fetchone()
    for item in sources:
        if item.get("robots_policy") and item["robots_policy"] != "respect":
            item["policy_note"] = (f"robots.txt/TDM reservation not applied: operator decision "
                                   f"{item.get('policy_decided_at')}")
    return {"status": "ok", "schema_version": SCHEMA_VERSION, "parser_version": PARSER_VERSION,
            "decisions": totals[0], "paragraphs": totals[1], "relations": totals[2], "captures": totals[3],
            "pack_files": totals[4], "sources": sources}
