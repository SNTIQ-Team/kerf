"""Read API over segmented legislative materials (Bundestag Drucksachen).

Store layout (written by ``tools/build_materials.py``)::

    <materials dir>/index.json        documents, counts, sha256 of the database
    <materials dir>/materials.sqlite  fragments + relation links

The materials directory is ``$LEX_MATERIALS_DIR`` when that variable is set,
otherwise ``<data_dir>/materials``.  The service is memory-light: the
database is opened read-only (``mode=ro&immutable=1``) with one short-lived
connection per request, only the fragments of the requested page are
decoded, and the only cached state is the sha256 verdict per database file
(keyed by path, inode, size and mtime).

Every response is a ``lexgraph-evidence-envelope/1`` (``api.evidence``):

* ``unavailable`` (HTTP 503, ``total: null``) when the store is missing or
  its index is unreadable;
* ``integrity_check_failed`` when the database does not match the sha256
  recorded in the index (or cannot be read as the expected schema);
* ``not_in_corpus`` when the requested Drucksache is not in the store;
* ``ok`` with ``total`` = number of matching fragments; ``0`` only after all
  listed documents were searched.

A fragment is evidence about a *legislative material*, never about the
enacted law: relation targets use the draft's numbering and carry
``correspondence_to_enacted_norm_verified: false``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterable

from fastapi import APIRouter, Request

from api.evidence import (CACHE_KEY_ARGUMENTS, EnvelopeError, envelope,
                          error_envelope, response, unknown_argument_payload)
from api.legislative_materials import (RELATION_TYPES, ROLES, compact_norm,
                                       expand_norm, mention_context,
                                       parse_citation, relation_row)

INDEX_SCHEMA = "lexgraph-materials-index/2"
DB_SCHEMA = "lexgraph-materials-sqlite/1"
DB_FILE = "materials.sqlite"
ENV_DIR = "LEX_MATERIALS_DIR"
MAX_LIMIT = 200
DEFAULT_LIMIT = 50
SEARCH_ARGUMENTS = frozenset({"drucksache", "role", "target", "relation",
                              "limit", "offset"})
_CANONICAL = re.compile(r"^de/[a-z0-9_.-]+(?:/[a-z0-9_.-]+)*$")
_DRUCKSACHE = re.compile(
    r"^(?:(?:BT-?)?Drs\.?\s*|bt-drs-)?(\d{1,2})[/-](\d{1,6})$", re.I)
# Fields held in columns or once per document instead of in fragment_json.
_OUTSIDE_JSON = ("drucksache", "procedure_stage", "adoption_status",
                 "adoption_basis", "parser_version", "relations")
SUMMARY_KEYS = ("id", "drucksache", "chamber", "type", "initiator", "date",
                "title", "source", "procedure", "outcome", "procedure_stage",
                "adoption_status", "adoption_basis", "body_normalization",
                "parser_version")
_VERDICTS: dict[tuple[Any, ...], bool] = {}
_VERDICT_LIMIT = 32


# --------------------------------------------------------------------------
# Store codec (shared with tools/build_materials.py)
# --------------------------------------------------------------------------

def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True)


def document_summary(document: dict[str, Any]) -> dict[str, Any]:
    return {key: document.get(key) for key in SUMMARY_KEYS}


def pack_fragment(fragment: dict[str, Any], summary: dict[str, Any]
                  ) -> tuple[str, list[tuple[Any, ...]]]:
    """``(fragment_json, link rows)`` for one fragment.

    Relations are not stored: each link row records which target/mention
    it derives from, and :func:`unpack_fragment` rebuilds the relation with
    :func:`api.legislative_materials.relation_row`.  Packing verifies that
    the rebuild is exact, so the store can never serve something the parser
    did not produce.
    """
    for key, doc_key in (("adoption_basis", "adoption_basis"),
                         ("parser_version", "parser_version"),
                         ("drucksache", "drucksache")):
        if fragment.get(key) != summary.get(doc_key):
            raise ValueError(f"{fragment['id']}: {key} differs from the "
                             "document; cannot be stored once per document")
    stored = {k: v for k, v in fragment.items() if k not in _OUTSIDE_JSON}
    stored["target_norms"] = [compact_norm(n) for n in fragment["target_norms"]]
    # quoted_context is rebuilt from the body and the mention offsets
    stored["mentions"] = [{k: v for k, v in compact_norm(n).items()
                           if k != "quoted_context"}
                          for n in fragment["mentions"]]
    links: list[tuple[Any, ...]] = []
    targets = len(fragment["target_norms"])
    for position, relation in enumerate(fragment["relations"]):
        from_mention = position >= targets
        index = position - targets if from_mention else position
        norms = fragment["mentions"] if from_mention else \
            fragment["target_norms"]
        rebuilt = relation_row(fragment, relation=relation["type"],
                               norm=norms[index],
                               confidence=relation["evidence"]["confidence"],
                               from_mention=from_mention)
        if rebuilt != relation:
            raise ValueError(f"{fragment['id']}: relation {position} cannot "
                             "be rebuilt from the fragment")
        links.append((fragment["id"], position, relation["type"],
                      relation["target"]["canonical_id"],
                      relation["target"]["citation"],
                      relation["evidence"]["confidence"],
                      "mention" if from_mention else "target", index))
    packed = _dumps(stored)
    row = {"fragment_json": packed, "drucksache": fragment["drucksache"],
           "procedure_stage": fragment["procedure_stage"],
           "adoption_status": fragment["adoption_status"]}
    link_rows = [{"relation": link[2], "confidence": link[5],
                  "source": link[6], "source_index": link[7]}
                 for link in links]
    if unpack_fragment(row, link_rows, summary) != fragment:
        raise ValueError(f"{fragment['id']}: stored form does not round-trip")
    return packed, links


def unpack_fragment(row: sqlite3.Row, links: Iterable[sqlite3.Row],
                    summary: dict[str, Any]) -> dict[str, Any]:
    fragment = json.loads(row["fragment_json"])
    fragment["target_norms"] = [expand_norm(n) for n in fragment["target_norms"]]
    mentions = []
    for stored in fragment["mentions"]:
        mention = expand_norm(stored)
        text = fragment["body"] if mention["part"] == "body" \
            else fragment["justification"]["body"]
        mention["quoted_context"] = mention_context(
            text, mention["text_start"], mention["text_end"])
        mentions.append(mention)
    fragment["mentions"] = mentions
    fragment.update(drucksache=row["drucksache"],
                    procedure_stage=row["procedure_stage"],
                    adoption_status=row["adoption_status"],
                    adoption_basis=summary["adoption_basis"],
                    parser_version=summary["parser_version"])
    relations = []
    for link in links:
        from_mention = link["source"] == "mention"
        norms = fragment["mentions"] if from_mention else \
            fragment["target_norms"]
        relations.append(relation_row(
            fragment, relation=link["relation"],
            norm=norms[link["source_index"]], confidence=link["confidence"],
            from_mention=from_mention))
    fragment["relations"] = relations
    return fragment


# --------------------------------------------------------------------------
# Store access
# --------------------------------------------------------------------------

def materials_dir(data_dir: Path) -> Path:
    override = os.environ.get(ENV_DIR)
    return Path(override) if override else Path(data_dir) / "materials"


def load_index(data_dir: Path) -> dict[str, Any]:
    """Read and validate ``index.json``; raise ``EnvelopeError`` otherwise."""
    path = materials_dir(data_dir) / "index.json"
    searched = [{"source": "materials:index", "status": "unavailable",
                 "count": None}]
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise EnvelopeError(
            "unavailable", "legislative materials store is not built",
            searched=searched,
            failed_components=[{"component": "materials:index",
                                "status": "missing"}]) from None
    except OSError as exc:
        raise EnvelopeError(
            "unavailable", f"materials index unreadable: {exc.strerror}",
            searched=searched,
            failed_components=[{"component": "materials:index",
                                "status": "unreadable"}]) from None
    try:
        index = json.loads(raw)
    except ValueError:
        index = None
    documents = index.get("documents") if isinstance(index, dict) else None
    database = index.get("database") if isinstance(index, dict) else None
    valid = (isinstance(index, dict) and index.get("schema") == INDEX_SCHEMA
             and isinstance(database, dict)
             and database.get("file") == DB_FILE
             and database.get("schema") == DB_SCHEMA
             and isinstance(database.get("sha256"), str)
             and isinstance(documents, list) and all(
                 isinstance(row, dict) and isinstance(row.get("drucksache"), str)
                 and isinstance(row.get("id"), str) for row in documents))
    if not valid:
        raise EnvelopeError(
            "unavailable", "materials index is corrupt or of an unknown schema",
            searched=searched,
            failed_components=[{"component": "materials:index",
                                "status": "corrupt"}])
    index = dict(index)
    index["_sha256"] = hashlib.sha256(raw).hexdigest()
    index["_dir"] = str(path.parent)
    return index


def _integrity_error(message: str) -> EnvelopeError:
    return EnvelopeError(
        "integrity_check_failed", message,
        searched=[{"source": "materials:database",
                   "status": "integrity_check_failed", "count": None}],
        failed_components=[{"component": "materials:database",
                            "status": "integrity_check_failed"}])


def connect(index: dict[str, Any]) -> sqlite3.Connection:
    """Open the database read-only after checking its sha256 (verdict is
    cached per path/inode/size/mtime; the file content never is)."""
    path = Path(index["_dir"]) / DB_FILE
    expected = index["database"]["sha256"]
    try:
        stat = path.stat()
    except OSError:
        raise EnvelopeError(
            "unavailable", "materials database is missing",
            searched=[{"source": "materials:database",
                       "status": "unavailable", "count": None}],
            failed_components=[{"component": "materials:database",
                                "status": "missing"}]) from None
    key = (str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, expected)
    verdict = _VERDICTS.get(key)
    if verdict is None:
        with path.open("rb") as handle:
            verdict = hashlib.file_digest(handle, "sha256").hexdigest() == \
                expected
        if len(_VERDICTS) >= _VERDICT_LIMIT:
            _VERDICTS.clear()
        _VERDICTS[key] = verdict
    if not verdict:
        raise _integrity_error("materials database does not match the sha256 "
                               "recorded in the index")
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1",
                               uri=True)
        conn.row_factory = sqlite3.Row
        schema = conn.execute("SELECT value FROM meta WHERE key='schema'"
                              ).fetchone()
    except sqlite3.Error:
        raise _integrity_error("materials database is unreadable") from None
    if schema is None or schema["value"] != DB_SCHEMA:
        conn.close()
        raise _integrity_error("materials database has an unknown schema")
    return conn


class _Summaries:
    """Per-request cache of the small per-document summary rows."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.rows: dict[str, dict[str, Any]] = {}

    def get(self, document_id: str) -> dict[str, Any]:
        if document_id not in self.rows:
            row = self.conn.execute(
                "SELECT summary_json FROM documents WHERE id=?",
                (document_id,)).fetchone()
            if row is None:
                raise _integrity_error(f"database lacks document "
                                       f"{document_id}")
            self.rows[document_id] = json.loads(row["summary_json"])
        return self.rows[document_id]


def _load_fragments(conn: sqlite3.Connection, rows: list[sqlite3.Row],
                    summaries: _Summaries) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        links = conn.execute(
            "SELECT relation, confidence, source, source_index FROM links "
            "WHERE fragment_id=? ORDER BY position", (row["id"],)).fetchall()
        out.append(unpack_fragment(row, links,
                                   summaries.get(row["document_id"])))
    return out


# --------------------------------------------------------------------------
# Arguments and matching
# --------------------------------------------------------------------------

def normalize_drucksache(value: str) -> str:
    match = _DRUCKSACHE.match(value.strip())
    if not match:
        raise EnvelopeError("invalid_argument",
                            f"not a Bundestag Drucksache number: {value!r} "
                            "(expected e.g. 19/13824)")
    return f"{int(match.group(1))}/{match.group(2)}"


def resolve_target(value: str, index: dict[str, Any]) -> list[str]:
    """Canonical-id prefixes for a ``target`` argument (id or citation)."""
    value = value.strip()
    if value.startswith("de/"):
        prefix = value.rstrip("/")
        if not _CANONICAL.match(prefix):
            raise EnvelopeError("invalid_argument",
                                f"malformed canonical norm id: {value!r}")
        return [prefix]
    rows = parse_citation(value, index.get("act_slugs") or {})
    prefixes = [row["canonical_id"] for row in rows if row["canonical_id"]]
    if not prefixes:
        raise EnvelopeError(
            "invalid_argument",
            f"target {value!r} could not be resolved to a canonical norm id; "
            "pass an id such as de/bund/sgb_14/para/37 or a citation such as "
            "'§ 37 SGB XIV'")
    return prefixes


def _matches(canonical_id: str | None, prefixes: list[str]) -> bool:
    return bool(canonical_id) and any(
        canonical_id == prefix or canonical_id.startswith(prefix + "/")
        for prefix in prefixes)


def _matched_relations(fragment: dict[str, Any], *, relation: str | None,
                       prefixes: list[str] | None) -> list[dict[str, Any]]:
    rows = []
    for row in fragment.get("relations") or []:
        if relation and row.get("type") != relation:
            continue
        if prefixes is not None and not _matches(
                (row.get("target") or {}).get("canonical_id"), prefixes):
            continue
        rows.append(row)
    return rows


def _prefix_sql(prefixes: list[str], column: str = "l.canonical_id"
                ) -> tuple[str, list[str]]:
    # "=" or a range scan on the index: [prefix + "/", prefix + "0") holds
    # exactly the ids below the prefix ("0" follows "/" in code order).
    parts, args = [], []
    for prefix in prefixes:
        parts.append(f"({column} = ? OR ({column} >= ? AND {column} < ?))")
        args += [prefix, prefix + "/", prefix + "0"]
    return "(" + " OR ".join(parts) + ")", args


def evidence_item(summary: dict[str, Any], fragment: dict[str, Any],
                  matched: list[dict[str, Any]] | None = None
                  ) -> dict[str, Any]:
    source = summary.get("source") or {}
    span = fragment["heading_span"]
    url = source.get("url")
    item = {
        "id": fragment["id"],
        "canonical_id": fragment["id"],
        "kind": "legislative_material_fragment",
        "pinpoint": {
            "drucksache": fragment["drucksache"],
            "pdf_page": span["pdf_page"],
            "printed_page": span.get("printed_page"),
            "pdf_page_end": fragment["page_end"],
            "heading": fragment["heading"],
        },
        "heading": fragment["heading"],
        "text": fragment["body"],
        "role": fragment["role"],
        "section_path": fragment["section_path"],
        "source": {
            "url": f"{url}#page={span['pdf_page']}" if url else None,
            "document_id": summary.get("id"),
            "role": fragment["role"],
            "observed_at": source.get("observed_at"),
            "content_hash": fragment["text_sha256"],
            "pdf_sha256": source.get("pdf_sha256"),
        },
        "quality": {
            "source_exact": True,
            "text_basis": "extracted_text",
            "text_normalization": summary.get("body_normalization"),
            "offsets_reproduce": "heading_span/body_spans index the exact "
                                 "pdftotext page text",
            "complete": fragment["complete"],
            "reconstruction_status": "not_reconstructed",
            "parser_version": fragment.get("parser_version"),
        },
        "temporal": {
            "procedure_stage": fragment["procedure_stage"],
            "document_date": summary.get("date"),
        },
        "legal_status": {
            "adoption_status": fragment["adoption_status"],
            "adoption_basis": fragment["adoption_basis"],
        },
        "heading_span": fragment["heading_span"],
        "body_spans": fragment["body_spans"],
        "target_norms": fragment["target_norms"],
        "relations": fragment["relations"],
        "notes": fragment.get("notes") or [],
    }
    if matched is not None:
        item["matched_relations"] = matched
    for key in ("government_position", "government_position_basis",
                "justification", "responds_to", "ziffer"):
        if key in fragment:
            item[key] = fragment[key]
    return item


def _snapshot(index: dict[str, Any]) -> str:
    return "materials:" + index["database"]["sha256"][:16]


_FRAGMENT_COLUMNS = ("f.id, f.document_id, f.drucksache, f.procedure_stage, "
                     "f.adoption_status, f.fragment_json")


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------

def search(data_dir: Path, *, drucksache: str | None = None,
           role: str | None = None, target: str | None = None,
           relation: str | None = None, limit: int = DEFAULT_LIMIT,
           offset: int = 0) -> dict[str, Any]:
    """Domain operation behind ``GET /materials``; raises EnvelopeError."""
    if role is not None and role not in ROLES:
        raise EnvelopeError("invalid_argument",
                            f"unknown role {role!r}; one of {', '.join(ROLES)}")
    if relation is not None and relation not in RELATION_TYPES:
        raise EnvelopeError("invalid_argument",
                            f"unknown relation {relation!r}; one of "
                            f"{', '.join(RELATION_TYPES)}")
    wanted = normalize_drucksache(drucksache) if drucksache else None
    index = load_index(data_dir)
    prefixes = resolve_target(target, index) if target else None
    entries = index["documents"]
    if wanted is not None:
        entries = [row for row in entries if row["drucksache"] == wanted]
        if not entries:
            raise EnvelopeError(
                "not_in_corpus",
                f"Drucksache {wanted} is not in the legislative materials "
                "store",
                searched=[{"source": "materials:index", "status": "ok",
                           "count": 0}])
    where, args = [], []
    if wanted is not None:
        where.append("f.drucksache = ?")
        args.append(wanted)
    if role is not None:
        where.append("f.role = ?")
        args.append(role)
    link_where, link_args = ["l.fragment_id = f.id"], []
    if relation is not None:
        link_where.append("l.relation = ?")
        link_args.append(relation)
    if prefixes is not None:
        clause, clause_args = _prefix_sql(prefixes)
        link_where.append(clause)
        link_args += clause_args
    if len(link_where) > 1:
        where.append("EXISTS (SELECT 1 FROM links l WHERE "
                     + " AND ".join(link_where) + ")")
        args += link_args
    sql_where = (" WHERE " + " AND ".join(where)) if where else ""
    conn = connect(index)
    try:
        counts = {row[0]: row[1] for row in conn.execute(
            "SELECT f.drucksache, count(*) FROM fragments f" + sql_where
            + " GROUP BY f.drucksache", args)}
        rows = conn.execute(
            f"SELECT {_FRAGMENT_COLUMNS} FROM fragments f{sql_where} "
            "ORDER BY f.wp, f.number, f.ordinal LIMIT ? OFFSET ?",
            [*args, limit, offset]).fetchall()
        summaries = _Summaries(conn)
        page = []
        for fragment in _load_fragments(conn, rows, summaries):
            matched = None
            if relation is not None or prefixes is not None:
                matched = _matched_relations(fragment, relation=relation,
                                             prefixes=prefixes)
            page.append(evidence_item(summaries.get(
                fragment["id"].split(":", 1)[0]), fragment, matched))
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        raise _integrity_error("materials database is unusable") from None
    finally:
        conn.close()
    searched = [{"source": f"materials:{entry['drucksache']}",
                 "status": "ok", "count": counts.get(entry["drucksache"], 0)}
                for entry in entries]
    warnings = []
    if not index["documents"]:
        warnings.append("the legislative materials store lists no documents")
    if prefixes is not None:
        warnings.append(
            "targets use the numbering of the draft (Entwurf); "
            "correspondence to the enacted norm is not verified")
    return envelope(
        status="ok", operation="materials.search",
        request_scope={"drucksache": wanted, "role": role, "target": target,
                       "relation": relation, "limit": limit,
                       "offset": offset},
        applied_constraints={"drucksache": wanted, "role": role,
                             "target_prefixes": prefixes,
                             "relation": relation,
                             "target_match": "canonical id equal to or "
                                             "below the prefix"},
        snapshot_id=_snapshot(index), searched=searched, evidence=page,
        warnings=warnings, total=sum(counts.values()), limit=limit,
        offset=offset, returned=len(page))


def fragment(data_dir: Path, fragment_id: str) -> dict[str, Any]:
    """Domain operation behind ``GET /materials/fragments/{id}``."""
    index = load_index(data_dir)
    doc_id = fragment_id.split(":", 1)[0]
    entry = next((row for row in index["documents"] if row["id"] == doc_id),
                 None)
    if entry is None:
        raise EnvelopeError(
            "not_in_corpus",
            f"no Drucksache {doc_id!r} in the legislative materials store",
            searched=[{"source": "materials:index", "status": "ok",
                       "count": 0}])
    source = f"materials:{entry['drucksache']}"
    conn = connect(index)
    try:
        rows = conn.execute(f"SELECT {_FRAGMENT_COLUMNS} FROM fragments f "
                            "WHERE f.id = ?", (fragment_id,)).fetchall()
        summaries = _Summaries(conn)
        found = _load_fragments(conn, rows, summaries)
        summary = summaries.get(doc_id) if found else None
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        raise _integrity_error("materials database is unusable") from None
    finally:
        conn.close()
    if not found:
        raise EnvelopeError(
            "not_found_in_searched_scope",
            f"fragment {fragment_id!r} not found in Drucksache "
            f"{entry['drucksache']}",
            searched=[{"source": source, "status": "ok", "count": 0}])
    return envelope(
        status="ok", operation="materials.fragment",
        request_scope={"fragment_id": fragment_id},
        snapshot_id=_snapshot(index),
        searched=[{"source": source, "status": "ok", "count": 1}],
        evidence=[evidence_item(summary, found[0])], total=1,
        fragment=found[0],
        document={key: summary.get(key) for key in (
            "id", "drucksache", "type", "initiator", "date", "title",
            "source", "procedure", "outcome", "procedure_stage",
            "adoption_status", "parser_version")})


def materials_for_norm(data_dir: Path, canonical_id: str, *,
                       limit_per_relation: int | None = None) -> dict:
    """Materials relating to one norm, split by relation type.

    ``explains`` lists fragments whose heading targets the norm (or a part
    of it); ``mentions`` lists fragments that merely refer to it;
    ``drafts`` lists the draft wording (Entwurf).  ``totals`` counts every
    match; ``limit_per_relation`` (default: no limit) caps how many items
    per relation are decoded.  Never raises: failures are reported through
    ``status`` and ``searched``.
    """
    kinds = ("explains", "mentions", "drafts")
    result: dict[str, Any] = {
        "status": "ok", "canonical_id": canonical_id,
        "explains": [], "mentions": [], "drafts": [], "searched": [],
        "totals": {kind: 0 for kind in kinds},
        "warnings": ["legislative materials use the numbering of the draft "
                     "(Entwurf); correspondence to the enacted norm is not "
                     "verified and no fragment is the official "
                     "justification of the enacted norm"],
    }
    if not isinstance(canonical_id, str) or \
            not _CANONICAL.match(canonical_id.rstrip("/")):
        result.update(status="invalid_argument", totals=None,
                      warnings=[f"malformed canonical norm id: "
                                f"{canonical_id!r}"])
        return result
    prefixes = [canonical_id.rstrip("/")]
    try:
        index = load_index(data_dir)
        conn = connect(index)
    except EnvelopeError as exc:
        result.update(status=exc.status, searched=exc.searched, totals=None,
                      warnings=[exc.message])
        return result
    result["snapshot_id"] = _snapshot(index)
    clause, args = _prefix_sql(prefixes)
    try:
        hits: dict[str, set[str]] = {}
        per_doc: dict[str, int] = {}
        for row in conn.execute(
                "SELECT l.fragment_id, l.relation, f.drucksache "
                "FROM links l JOIN fragments f ON f.id = l.fragment_id "
                f"WHERE {clause} ORDER BY f.wp, f.number, f.ordinal, "
                "l.position", args):
            kinds_of = hits.setdefault(row["fragment_id"], set())
            if row["relation"] not in kinds_of:
                kinds_of.add(row["relation"])
                per_doc[row["drucksache"]] = per_doc.get(row["drucksache"],
                                                         0) + 1
                result["totals"][row["relation"]] += 1
        taken = {kind: 0 for kind in kinds}
        summaries = _Summaries(conn)
        for fragment_id, kinds_of in hits.items():
            wanted = [kind for kind in kinds if kind in kinds_of and (
                limit_per_relation is None
                or taken[kind] < limit_per_relation)]
            if not wanted:
                continue
            rows = conn.execute(f"SELECT {_FRAGMENT_COLUMNS} FROM fragments f "
                                "WHERE f.id = ?", (fragment_id,)).fetchall()
            [row] = _load_fragments(conn, rows, summaries)
            matched = _matched_relations(row, relation=None, prefixes=prefixes)
            summary = summaries.get(fragment_id.split(":", 1)[0])
            for kind in wanted:
                of_kind = [rel for rel in matched if rel["type"] == kind]
                result[kind].append(evidence_item(summary, row, of_kind))
                taken[kind] += 1
    except (sqlite3.Error, ValueError, KeyError, TypeError, EnvelopeError):
        result.update(status="integrity_check_failed", totals=None,
                      explains=[], mentions=[], drafts=[],
                      searched=[{"source": "materials:database",
                                 "status": "integrity_check_failed",
                                 "count": None}])
        return result
    finally:
        conn.close()
    result["searched"] = [{"source": f"materials:{entry['drucksache']}",
                           "status": "ok",
                           "count": per_doc.get(entry["drucksache"], 0)}
                          for entry in index["documents"]]
    if limit_per_relation is not None and any(
            result["totals"][kind] > len(result[kind]) for kind in kinds):
        result["truncated"] = True
    return result


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def _strict(request: Request, route: str, allowed: frozenset[str]):
    params = request.query_params
    unknown = sorted(set(params.keys()) - allowed - CACHE_KEY_ARGUMENTS)
    repeated = sorted(key for key in set(params.keys())
                      if len(params.getlist(key)) > 1)
    if unknown or repeated:
        return response(unknown_argument_payload(
            route=route, unsupported=unknown, repeated=repeated,
            allowed=allowed))
    return None


def _int_argument(value: str | None, name: str, *, default: int,
                  minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError:
        raise EnvelopeError("invalid_argument",
                            f"{name} must be an integer") from None
    if not minimum <= number <= maximum:
        raise EnvelopeError("invalid_argument",
                            f"{name} must be between {minimum} and {maximum}")
    return number


def create_router(data_dir: Callable[[], Path]) -> APIRouter:
    router = APIRouter()

    @router.get("/materials")
    def materials(request: Request):
        rejected = _strict(request, "/materials", SEARCH_ARGUMENTS)
        if rejected is not None:
            return rejected
        params = request.query_params
        scope = {key: params.get(key) for key in sorted(SEARCH_ARGUMENTS)}
        try:
            limit = _int_argument(params.get("limit"), "limit",
                                  default=DEFAULT_LIMIT, minimum=1,
                                  maximum=MAX_LIMIT)
            offset = _int_argument(params.get("offset"), "offset", default=0,
                                   minimum=0, maximum=10 ** 6)
            payload = search(Path(data_dir()),
                             drucksache=params.get("drucksache") or None,
                             role=params.get("role") or None,
                             target=params.get("target") or None,
                             relation=params.get("relation") or None,
                             limit=limit, offset=offset)
        except EnvelopeError as exc:
            payload = error_envelope(exc, operation="materials.search",
                                     request_scope=scope)
        return response(payload)

    @router.get("/materials/fragments/{fragment_id}")
    def material_fragment(request: Request, fragment_id: str):
        rejected = _strict(request, "/materials/fragments/{fragment_id}",
                           frozenset())
        if rejected is not None:
            return rejected
        try:
            payload = fragment(Path(data_dir()), fragment_id)
        except EnvelopeError as exc:
            payload = error_envelope(exc, operation="materials.fragment",
                                     request_scope={"fragment_id":
                                                    fragment_id})
        return response(payload)

    return router
