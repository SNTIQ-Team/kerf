"""Multi-source norm versions and amendment change history (read side).

``law_history.sqlite`` is a derived, immutable file built by
``tools/build_law_history.py`` into each web-data generation.  It merges

* complete official consolidations (GII daily states, NeuRIS expressions,
  BAYERN.RECHT) — source role ``official_consolidation``;
* archived copies of official pages (Wayback captures of GII/BAYERN.RECHT;
  ``known_at`` = capture date) — ``archived_capture``;
* promulgated wording from the gazette — ``official_promulgation``;
* texts derived by replaying amendment commands — ``reconstruction``;

de-duplicated by the whitespace-free norm identity
(``api.history_identity.norm_identity``), and one *change record* per norm,
amending act, article and commencement date with dates taken only from
evidence (``adopted``/``promulgated``/``in_force``/``applies_from``).

The API reads one norm at a time (indexed SQLite, no whole-act chain in
memory) and composes ``change_summary`` here so every endpoint answers the
same way.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import zlib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

DB_FILE = "law_history.sqlite"
DB_SCHEMA = "lexgraph-law-history-sqlite/1"
SUMMARY_SCHEMA = "lexgraph-change-summary/1"
SOURCE_ROLES = ("official_consolidation", "archived_capture",
                "official_promulgation", "reconstruction")
CHANGE_TYPES = ("geändert", "eingefügt", "neugefasst", "aufgehoben",
                "ersetzt", "unbekannt")

DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE acts(
    act_id TEXT PRIMARY KEY, prefix TEXT NOT NULL, jurabk TEXT, title TEXT,
    juris TEXT, summary_json TEXT NOT NULL);
CREATE TABLE states(
    act_id TEXT NOT NULL, seq INTEGER NOT NULL, state_key TEXT NOT NULL,
    source TEXT NOT NULL, source_role TEXT NOT NULL,
    known_from TEXT, known_until TEXT, url TEXT, archive_url TEXT,
    sha256 TEXT, stand TEXT, notes_json TEXT NOT NULL, complete INTEGER NOT NULL,
    valid_from TEXT, valid_from_status TEXT, valid_from_basis TEXT,
    valid_until TEXT, interval_json TEXT,
    PRIMARY KEY(act_id, seq));
CREATE TABLE texts(
    text_id INTEGER PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
    content_sha256 TEXT NOT NULL, enbez TEXT, titel TEXT, body BLOB NOT NULL);
CREATE TABLE norms(
    norm_id TEXT PRIMARY KEY, act_id TEXT NOT NULL, label TEXT, title TEXT,
    present_in_latest INTEGER NOT NULL, versions INTEGER NOT NULL,
    changes INTEGER NOT NULL, first_known TEXT, summary_json TEXT NOT NULL);
CREATE INDEX norms_act ON norms(act_id);
CREATE TABLE versions(
    norm_id TEXT NOT NULL, vseq INTEGER NOT NULL, act_id TEXT NOT NULL,
    text_id INTEGER NOT NULL REFERENCES texts(text_id),
    first_seq INTEGER, last_seq INTEGER, known_from TEXT, known_until TEXT,
    source_role TEXT NOT NULL, record BLOB NOT NULL,
    PRIMARY KEY(norm_id, vseq));
CREATE INDEX versions_act ON versions(act_id);
CREATE TABLE changes(
    change_id TEXT PRIMARY KEY, act_id TEXT NOT NULL, norm_id TEXT,
    norm_label TEXT, document_id TEXT, sort_date TEXT, state TEXT NOT NULL,
    record BLOB NOT NULL);
CREATE INDEX changes_norm ON changes(norm_id, sort_date);
CREATE INDEX changes_act ON changes(act_id, sort_date);
CREATE TABLE findings(
    id INTEGER PRIMARY KEY, act_id TEXT NOT NULL, norm_id TEXT,
    kind TEXT NOT NULL, detail_json TEXT NOT NULL);
CREATE INDEX findings_norm ON findings(norm_id);
CREATE INDEX findings_act ON findings(act_id, kind);
"""


def pack_text(text: str) -> bytes:
    return zlib.compress(text.encode("utf-8"), 9)


def unpack_text(blob: bytes) -> str:
    return zlib.decompress(blob).decode("utf-8")


def pack_json(value: Any) -> bytes:
    return zlib.compress(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8"), 9)


def unpack_json(blob: bytes) -> Any:
    return json.loads(zlib.decompress(blob))


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


# ------------------------------------------------------------------ reader

@dataclass
class HistoryVersion:
    norm_id: str
    vseq: int
    enbez: str
    titel: str
    text: str
    identity: str
    content_sha256: str
    known_from: str | None
    known_until: str | None
    source_role: str
    record: dict[str, Any] = field(default_factory=dict)


class LawHistoryStore:
    """Read-only access to one immutable ``law_history.sqlite``."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._local = threading.local()
        uri = f"file:{self.path}?mode=ro&immutable=1"
        probe = sqlite3.connect(uri, uri=True)
        try:
            meta = dict(probe.execute("SELECT key, value FROM meta"))
        finally:
            probe.close()
        if meta.get("schema") != DB_SCHEMA:
            raise ValueError(f"unsupported law history schema "
                             f"{meta.get('schema')!r}")
        self.meta = meta
        self._acts: dict[str, dict[str, Any]] | None = None

    def _db(self) -> sqlite3.Connection:
        db = getattr(self._local, "db", None)
        if db is None:
            db = sqlite3.connect(f"file:{self.path}?mode=ro&immutable=1",
                                 uri=True, check_same_thread=False)
            db.row_factory = sqlite3.Row
            self._local.db = db
        return db

    # -- acts
    def acts(self) -> dict[str, dict[str, Any]]:
        if self._acts is None:
            self._acts = {
                row["act_id"]: {"act_id": row["act_id"],
                                "prefix": row["prefix"],
                                "jurabk": row["jurabk"],
                                "title": row["title"], "juris": row["juris"],
                                **_loads(row["summary_json"], {})}
                for row in self._db().execute("SELECT * FROM acts")}
        return self._acts

    def covers(self, act_id: str) -> bool:
        return act_id in self.acts()

    def states(self, act_id: str) -> list[dict[str, Any]]:
        rows = self._db().execute(
            "SELECT * FROM states WHERE act_id=? ORDER BY seq", (act_id,))
        return [{**dict(row), "notes": _loads(row["notes_json"], {}),
                 "interval": _loads(row["interval_json"])} for row in rows]

    def latest_state(self, act_id: str) -> dict[str, Any] | None:
        row = self._db().execute(
            "SELECT * FROM states WHERE act_id=? ORDER BY seq DESC LIMIT 1",
            (act_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "notes": _loads(row["notes_json"], {}),
                "interval": _loads(row["interval_json"])}

    # -- norms
    def norm(self, norm_id: str) -> dict[str, Any] | None:
        row = self._db().execute("SELECT * FROM norms WHERE norm_id=?",
                                 (norm_id,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "summary": _loads(row["summary_json"], {})}

    def versions(self, norm_id: str) -> list[HistoryVersion]:
        rows = self._db().execute(
            "SELECT v.*, t.identity, t.content_sha256, t.enbez, t.titel, "
            "t.body FROM versions v JOIN texts t USING(text_id) "
            "WHERE v.norm_id=? ORDER BY v.vseq", (norm_id,)).fetchall()
        return [HistoryVersion(
            norm_id=row["norm_id"], vseq=row["vseq"],
            enbez=row["enbez"] or "", titel=row["titel"] or "",
            text=unpack_text(row["body"]), identity=row["identity"],
            content_sha256=row["content_sha256"],
            known_from=row["known_from"], known_until=row["known_until"],
            source_role=row["source_role"],
            record=unpack_json(row["record"])) for row in rows]

    def changes(self, *, norm_id: str | None = None,
                act_id: str | None = None) -> list[dict[str, Any]]:
        if norm_id is not None:
            rows = self._db().execute(
                "SELECT record FROM changes WHERE norm_id=? "
                "ORDER BY sort_date, change_id", (norm_id,))
        else:
            rows = self._db().execute(
                "SELECT record FROM changes WHERE act_id=? "
                "ORDER BY sort_date, change_id", (act_id,))
        return [unpack_json(row[0]) for row in rows]

    def findings(self, *, norm_id: str | None = None,
                 act_id: str | None = None,
                 limit: int = 200) -> list[dict[str, Any]]:
        if norm_id is not None:
            rows = self._db().execute(
                "SELECT kind, norm_id, detail_json FROM findings "
                "WHERE norm_id=? ORDER BY id LIMIT ?", (norm_id, limit))
        else:
            rows = self._db().execute(
                "SELECT kind, norm_id, detail_json FROM findings "
                "WHERE act_id=? ORDER BY id LIMIT ?", (act_id, limit))
        return [{"kind": row[0], "norm_id": row[1],
                 **json.loads(row[2])} for row in rows]

    def close(self) -> None:
        db = getattr(self._local, "db", None)
        if db is not None:
            db.close()
            self._local.db = None


_STORES: dict[tuple[str, float], LawHistoryStore] = {}
_STORES_LOCK = threading.Lock()


def open_store(data_dir: Path) -> LawHistoryStore | None:
    """The generation's store, or ``None`` when it is not built."""
    path = Path(data_dir) / DB_FILE
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path.resolve()), stat.st_mtime)
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            for old in list(_STORES):
                _STORES.pop(old).close()
            store = LawHistoryStore(path)
            _STORES[key] = store
        return store


# ---------------------------------------------------------- change summary

def change_effective_date(change: dict[str, Any]) -> str | None:
    """The legal in-force date of a change, when evidenced."""
    in_force = (change.get("dates") or {}).get("in_force") or {}
    return in_force.get("date") if in_force.get("status") not in {
        None, "unknown"} else None


def change_state(change: dict[str, Any], as_of: str) -> str:
    effective = change_effective_date(change)
    if effective:
        return "pending" if effective > as_of else "in_force"
    promulgated = ((change.get("dates") or {}).get("promulgated") or {}
                   ).get("date")
    if promulgated and promulgated > as_of:
        return "not_yet_promulgated"
    if (change.get("text_effect") or {}).get("observed_from"):
        return "incorporated_date_unknown"
    return "date_unknown"


def compact_change(change: dict[str, Any], as_of: str) -> dict[str, Any]:
    act = change.get("amending_act") or {}
    return {
        "change_id": change.get("change_id"),
        "norm_id": change.get("norm_id"),
        "norm_label": change.get("norm_label"),
        "amending_act": {key: act.get(key) for key in (
            "citation", "document_id", "title", "gazette", "article",
            "items", "url", "procedure_id")},
        "change_type": change.get("change_type"),
        "dates": change.get("dates"),
        "source_role": change.get("source_role") or "official_promulgation",
        "state": change_state(change, as_of),
        "pending": change_state(change, as_of) == "pending",
        "text_effect": change.get("text_effect"),
    }


def _unknown_dates(changes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for change in changes:
        dates = change.get("dates") or {}
        for field_name in ("adopted", "promulgated", "in_force"):
            value = dates.get(field_name) or {}
            if not value.get("date"):
                rows.append({
                    "change_id": change.get("change_id"),
                    "norm_label": change.get("norm_label"),
                    "field": field_name,
                    "reason": value.get("reason") or value.get("status")
                    or "not_evidenced",
                    "amending_act": (change.get("amending_act") or {}).get(
                        "citation"),
                })
    return rows


def summarize(changes: list[dict[str, Any]], *, scope: str, subject: str,
              as_of: str | None = None,
              versions: list[dict[str, Any]] | None = None,
              version_role: str | None = None,
              stand_notes: dict[str, Any] | None = None,
              completeness: dict[str, Any] | None = None,
              notes_observed_at: str | None = None,
              unknown_limit: int = 50) -> dict[str, Any]:
    """``change_summary`` (schema lexgraph-change-summary/1).

    ``last_change`` is the latest change in force on ``as_of`` whose in-force
    date is evidenced; a change whose text is observed but whose date is not
    evidenced is reported separately (``last_incorporated_undated``) instead
    of being guessed into the order.
    """
    day = (as_of or date.today().isoformat())[:10]
    in_force = [c for c in changes if change_state(c, day) == "in_force"]
    pending = [c for c in changes if change_state(c, day) == "pending"]
    undated = [c for c in changes if change_state(c, day) in {
        "incorporated_date_unknown", "date_unknown"}]
    in_force.sort(key=lambda c: (change_effective_date(c) or "",
                                 c.get("change_id") or ""))
    pending.sort(key=lambda c: (change_effective_date(c) or "",
                                c.get("change_id") or ""))
    last = in_force[-1] if in_force else None
    later_undated = [
        c for c in undated
        if ((c.get("dates") or {}).get("promulgated") or {}).get("date")
        and (not last or ((c.get("dates") or {}).get("promulgated") or {}
                          ).get("date", "") > (((last.get("dates") or {}
                                                 ).get("promulgated") or {}
                                                ).get("date") or ""))]
    later_undated.sort(key=lambda c: ((c.get("dates") or {}).get(
        "promulgated") or {}).get("date") or "")
    roles: dict[str, int] = {}
    for version in versions or []:
        role = str(version.get("source_role") or "unknown")
        roles[role] = roles.get(role, 0) + 1
    documents = {(c.get("amending_act") or {}).get("document_id")
                 for c in changes}
    notes = stand_notes or {}
    unknown = _unknown_dates(changes)
    return {
        "schema": SUMMARY_SCHEMA,
        "scope": scope,
        "subject": subject,
        "as_of": day,
        "last_change": compact_change(last, day) if last else None,
        "last_incorporated_undated": (compact_change(later_undated[-1], day)
                                      if later_undated else None),
        "next_pending_change": (compact_change(pending[0], day)
                                if pending else None),
        "pending_changes": [compact_change(c, day) for c in pending[:20]],
        "counts": {
            "versions": len(versions or []),
            "versions_by_role": roles,
            "events": len(changes),
            "amending_acts": len({d for d in documents if d}),
            "events_with_in_force": sum(1 for c in changes
                                        if change_effective_date(c)),
            "events_pending": len(pending),
            "events_date_unknown": len(undated),
        },
        "source_role": version_role,
        "act_stand_note": notes.get("act_stand_note"),
        "documentation_notes": [
            {**note, "observed_at": notes_observed_at}
            for note in notes.get("documentation_notes") or []],
        "unknown_dates": unknown[:unknown_limit],
        "unknown_dates_total": len(unknown),
        "completeness": completeness or {},
        "date_policy": ("dates come only from the promulgation document "
                        "(heading, issue header) and the commencement "
                        "clause; an unknown date stays null and is listed "
                        "in unknown_dates"),
    }
