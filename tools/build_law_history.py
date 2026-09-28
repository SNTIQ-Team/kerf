#!/usr/bin/env python3
"""Build ``law_history.sqlite``: multi-source norm versions + change history.

Inputs (all already in, or next to, one web-data generation):

``<data>/acts/<id>.json`` + ``<data>/federal_states``
    complete official GII consolidations observed daily (official_consolidation)
    and Bavarian BAYERN.RECHT heads/versions.
``<data>/retrospective_history.sqlite``
    amendment events (BGBl articles + parsed commands) and verified legal
    intervals of GII states.
``--bundle <dir>`` (optional, built on the workstation)
    ``corpus.sqlite``   archived captures (Wayback/GII, NeuRIS …) as parsed
                        norm lists, de-duplicated by identity;
    ``change_dates.jsonl.gz``  per (document, article, item) date resolutions
                        from the promulgation documents and commencement
                        clauses (legacy BGBl texts live only on the workstation);
    ``bavarian.jsonl.gz``      Bavarian version/change records.
``--documents <snapshots dir>``
    BGBl document metadata (recht.bund.de official fields) for 2023+.

Output: one immutable SQLite file (schema in ``api/law_history.py``).

Rules: a date is only ever copied from evidence (promulgation document
metadata/heading, commencement clause, verified interval).  A version's legal
start is set only when its text change is attributed to exactly one evidenced
amendment (or several with the same in-force date) between the two captures
that bracket it; otherwise it stays null and a finding records why.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline"))

from api.history_identity import norm_identity  # noqa: E402
from api.law_history import (  # noqa: E402
    DB_FILE, DB_SCHEMA, DDL, change_effective_date, pack_json, pack_text,
    summarize)
from api.norm_identity import (  # noqa: E402
    act_prefix, designator_unit, norm_rows_with_ids)
from api.stand_notes import parse_notes  # noqa: E402

BUILDER = "build_law_history/1"
_DIGEST = re.compile(r"[0-9a-f]{64}")
SOURCE_PRIORITY = {"gii_daily": 0, "neuris": 1, "wayback_gii_xml": 2,
                   "wayback_gii": 3, "bayern_recht": 0, "wayback_bayern": 3,
                   "reconstruction": 9}
TEXT_PRIORITY = {"gii_daily": 0, "wayback_gii_xml": 1, "neuris": 2,
                 "wayback_gii": 3, "bayern_recht": 0, "wayback_bayern": 3,
                 "reconstruction": 9}
MONTHS = {m: i for i, m in enumerate((
    "januar", "februar", "märz", "april", "mai", "juni", "juli", "august",
    "september", "oktober", "november", "dezember"), start=1)}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _day(value: Any) -> str | None:
    text = str(value or "")[:10]
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else None


def _german_day(value: Any) -> str | None:
    text = str(value or "").strip()
    if match := re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text):
        return f"{match.group(3)}-{int(match.group(2)):02d}-" \
               f"{int(match.group(1)):02d}"
    return None


# ------------------------------------------------------------ observations

@dataclass
class StateObs:
    """One complete (or explicitly partial) consolidation of an act."""
    key: str
    source: str
    source_role: str
    known_from: str | None
    known_until: str | None
    norms: list[dict[str, Any]] | None = None     # lazily loaded rows
    loader: Any = None
    url: str | None = None
    archive_url: str | None = None
    sha256: str | None = None
    stand: str | None = None
    complete: bool = True
    captured_at: str | None = None
    valid_from: str | None = None
    valid_from_status: str | None = None
    valid_from_basis: str | None = None
    valid_until: str | None = None
    interval: dict[str, Any] | None = None
    amendment: dict[str, Any] | None = None
    seq: int = 0

    def rows(self) -> list[dict[str, Any]]:
        if self.norms is None and self.loader is not None:
            self.norms = self.loader()
        return self.norms or []

    @property
    def sort_key(self) -> tuple[str, int, str]:
        return (self.known_from or self.valid_from or "9999-12-31",
                SOURCE_PRIORITY.get(self.source, 5), self.captured_at or "")


def _read_state_object(data_dir: Path, digest: str) -> dict[str, Any]:
    path = (data_dir / "federal_states" / "objects" / "sha256" / digest[:2]
            / f"{digest}.json.gz")
    payload = gzip.decompress(path.read_bytes())
    if hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError(f"state {digest[:12]} failed its SHA-256 check")
    return json.loads(payload)


def gii_states(act: dict[str, Any], data_dir: Path,
               intervals: list[dict[str, Any]]) -> list[StateObs]:
    observations: dict[str, dict[str, Any]] = {}
    for row in act.get("official_states") or []:
        digest = str(row.get("state_sha256") or "")
        observed = _day(row.get("observed_at"))
        if not _DIGEST.fullmatch(digest) or not observed:
            continue
        entry = observations.setdefault(digest, {
            "first": observed, "last": observed,
            "source_url": row.get("source_url")})
        entry["first"] = min(entry["first"], observed)
        entry["last"] = max(entry["last"], observed)
    by_state: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in intervals:
        by_state[str(row.get("state_sha256"))].append(row)
    states: list[StateObs] = []
    for digest in set(observations) | set(by_state):
        obs = observations.get(digest) or {}
        rows = sorted(by_state.get(digest) or [],
                      key=lambda r: str(r.get("effective_from") or ""))
        derived = bool(rows) and not obs and str(
            rows[0].get("text_status")) == "derived_verified"
        if not obs and not derived:
            continue

        def load(digest: str = digest) -> list[dict[str, Any]]:
            payload = _read_state_object(data_dir, digest)
            return [{"enbez": n.get("enbez"), "titel": n.get("titel"),
                     "text": n.get("text")} for n in payload.get("norms")
                    or []]

        state = StateObs(
            key=digest, source="reconstruction" if derived else "gii_daily",
            source_role="reconstruction" if derived else
            "official_consolidation",
            known_from=obs.get("first"), known_until=obs.get("last"),
            loader=load, url=obs.get("source_url") or act.get("source_url"),
            sha256=digest)
        if rows:
            first = rows[0]
            state.valid_from = _day(first.get("effective_from"))
            state.valid_from_status = str(first.get("date_status")
                                          or "official_verified")
            state.valid_from_basis = first.get("date_basis")
            state.valid_until = _day(rows[-1].get("effective_to"))
            state.interval = {"id": first.get("id"),
                              "date_basis": first.get("date_basis"),
                              "evidence": first.get("evidence")}
            from api.norm_history import _interval_amendment
            state.amendment = _interval_amendment(first)
        states.append(state)
    # A verified start of the following consolidation closes the previous
    # one (same rule as api.norm_history for the GII chain).
    states.sort(key=lambda s: s.sort_key)
    for previous, following in zip(states, states[1:]):
        if previous.valid_until is None and previous.valid_from and \
                following.valid_from:
            previous.valid_until = following.valid_from
    return states


def bundle_states(bundle: sqlite3.Connection | None, act_id: str
                  ) -> list[StateObs]:
    if bundle is None:
        return []
    states = []
    for row in bundle.execute(
            "SELECT * FROM captures WHERE act_id=? ORDER BY captured_at",
            (act_id,)):
        capture_id = row["capture_id"]

        def load(capture_id: str = capture_id) -> list[dict[str, Any]]:
            out = []
            for norm in bundle.execute(
                    "SELECT n.ord, t.enbez, t.titel, t.body FROM capture_norms n "
                    "JOIN capture_texts t USING(text_id) WHERE n.capture_id=? "
                    "ORDER BY n.ord", (capture_id,)):
                out.append({"enbez": norm["enbez"], "titel": norm["titel"],
                            "text": gzip.decompress(norm["body"]).decode()})
            return out

        captured = str(row["captured_at"] or "")
        states.append(StateObs(
            key=f"{row['source']}:{row['sha256'][:16]}",
            source=row["source"], source_role=row["source_role"],
            known_from=_day(captured), known_until=_day(captured),
            loader=load, url=row["original_url"],
            archive_url=row["archive_url"], sha256=row["sha256"],
            stand=row["stand"], complete=bool(row["complete"]),
            captured_at=captured))
    return states


# ------------------------------------------------------------- change dates

class ChangeDates:
    """Date resolutions keyed by (document, article, item[, letter])."""

    def __init__(self, paths: Iterable[Path]):
        self.rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.documents: dict[str, dict[str, Any]] = {}
        for path in paths:
            if not path or not Path(path).is_file():
                continue
            opener = gzip.open if str(path).endswith(".gz") else open
            with opener(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    row = json.loads(line)
                    doc = str(row.get("document_id") or "")
                    if row.get("kind") == "document":
                        self.documents[doc] = row
                        continue
                    key = (doc, str(row.get("article") or ""),
                           str(row.get("item_key") or ""))
                    self.rows[key] = row

    def resolve(self, document_id: str, article: str, item_key: str
                ) -> dict[str, Any] | None:
        parts = item_key.split(".") if item_key else []
        for depth in range(len(parts), -1, -1):
            key = (document_id, article, ".".join(parts[:depth]))
            if key in self.rows:
                return self.rows[key]
        return None


def load_bgbl_documents(snapshots: Path | None) -> dict[str, dict[str, Any]]:
    """Latest metadata row per BGBl document (2023+ recht.bund.de)."""
    if snapshots is None:
        return {}
    root = snapshots / "bgbl_documents"
    result: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return result
    try:
        from common import read_jsonl  # resolves compacted snapshot refs
    except Exception:  # pragma: no cover - pipeline not importable
        read_jsonl = None
    for day in sorted(p for p in root.iterdir() if p.is_dir()):
        path = day / "documents.jsonl"
        if not path.is_file():
            continue
        try:
            rows = list(read_jsonl(path)) if read_jsonl else [
                json.loads(line) for line in path.read_text().splitlines()
                if line.strip()]
        except Exception:
            continue  # offloaded/compacted older snapshots: newer ones win
        for row in rows:
            doc = str(row.get("document_id") or "")
            if doc:
                result[doc] = {k: row.get(k) for k in (
                    "document_id", "title", "execution_date",
                    "publication_date", "official_fields", "official_html_url",
                    "official_pdf_url", "eli", "procedure_id", "issue",
                    "entry_into_force", "dip_entry_into_force", "pdf_object",
                    "sha256", "year")}
    return result


def gazette(document_id: str | None) -> dict[str, Any] | None:
    raw = str(document_id or "")
    if match := re.fullmatch(r"bgbl-(1|2)-(\d{4})-s(\d+)", raw):
        part = "I" if match.group(1) == "1" else "II"
        return {"name": "BGBl.", "part": part, "year": int(match.group(2)),
                "number": None, "page": int(match.group(3)),
                "reference": f"BGBl. {part} {match.group(2)} S. "
                             f"{match.group(3)}"}
    if match := re.fullmatch(r"bgbl-(1|2)-(\d{4})-(\d+)", raw):
        part = "I" if match.group(1) == "1" else "II"
        return {"name": "BGBl.", "part": part, "year": int(match.group(2)),
                "number": int(match.group(3)), "page": None,
                "reference": f"BGBl. {match.group(2)} {part} Nr. "
                             f"{match.group(3)}"}
    return None


_TYPE_PATTERNS = (
    ("aufgehoben", re.compile(r"\bwird\s+aufgehoben\b|\bwerden\s+aufgehoben\b"
                              r"|\bwird\s+gestrichen\b|\bwerden\s+gestrichen\b"
                              r"|\bentfällt\b|\bentfallen\b", re.I)),
    ("neugefasst", re.compile(r"\bwird\s+wie\s+folgt\s+gefasst\b|\bwerden\s+"
                              r"wie\s+folgt\s+gefasst\b|\bneu\s+gefasst\b",
                              re.I)),
    ("ersetzt", re.compile(r"\bwird\s+durch\s+(?:den|die|das)\s+folgenden?\s+"
                           r"(?:§|Paragraphen|Artikel|Absatz|Absätze|Satz|"
                           r"Sätze|Nummer|Nummern|Buchstaben?)", re.I)),
    ("eingefügt", re.compile(r"\bwird\s+(?:folgender|die\s+folgende|der\s+"
                             r"folgende|das\s+folgende|folgendes|folgende)?"
                             r".{0,80}\beingefügt\b|\bangefügt\b|\bvorangestellt"
                             r"\b", re.I | re.S)),
)


def classify_change(norm_label: str, commands: list[dict[str, Any]]) -> str:
    """Change type of one norm under one amending article (conservative)."""
    label = re.escape(norm_label.strip())
    whole = []
    for command in commands:
        raw = " ".join(str(command.get("raw") or "").split())
        head = raw[:220]
        # the whole norm is inserted/repealed/replaced
        if re.match(rf"^(?:Nach|Vor)\s+{label}\s+wird\s+(?:der|das)?\s*folgende"
                    rf".{{0,40}}eingefügt", head) or re.search(
                    r"werden\s+die\s+folgenden\s+§§.*eingefügt", head):
            whole.append("eingefügt")
        elif re.match(rf"^{label}\s+wird\s+aufgehoben", head) or re.match(
                rf"^{label}\s+wird\s+gestrichen", head):
            whole.append("aufgehoben")
        elif re.match(rf"^{label}\s+wird\s+wie\s+folgt\s+gefasst", head):
            whole.append("neugefasst")
        elif re.match(rf"^{label}\s+wird\s+durch\s+den\s+folgenden\s+§", head):
            whole.append("ersetzt")
        elif str(command.get("operation")) == "insert" and re.search(
                rf"{label}\s+(?:wird|werden)\s+eingefügt", head):
            whole.append("eingefügt")
    if len(set(whole)) == 1 and len(whole) == len(commands):
        return whole[0]
    if whole and len(set(whole)) == 1 and not any(
            not str(c.get("raw") or "").strip() for c in commands):
        # the norm itself is inserted and later commands adjust it
        return whole[0] if whole[0] in {"eingefügt", "aufgehoben"} else \
            "geändert"
    return "geändert" if commands else "unbekannt"


def _letters(raw: str) -> list[str]:
    """Top-level letters (a), b) …) of one numbered command, in order."""
    found = []
    for match in re.finditer(r"(?m)^\s*([a-z])\)\s", raw):
        letter = match.group(1)
        if not found or ord(letter) == ord(found[-1]) + 1:
            found.append(letter)
    return found


def event_dates(event: dict[str, Any], item_key: str,
                bgbl: dict[str, dict[str, Any]], dates: ChangeDates
                ) -> dict[str, Any]:
    """adopted / promulgated / in_force / applies_from from evidence only."""
    document_id = str(event.get("document_id") or "")
    article = str(event.get("amending_article") or "")
    resolved = dates.resolve(document_id, article, item_key)
    document = dates.documents.get(document_id) or {}
    meta = bgbl.get(document_id) or {}
    official = meta.get("official_fields") or {}
    out: dict[str, Any] = {}
    # adopted (Ausfertigung)
    adopted = (resolved or {}).get("adopted") or document.get("adopted")
    if adopted and adopted.get("date"):
        out["adopted"] = adopted
    elif _german_day(official.get("Ausfertigungsdatum")):
        out["adopted"] = {
            "date": _german_day(official.get("Ausfertigungsdatum")),
            "status": "official",
            "basis": "recht.bund.de document metadata 'Ausfertigungsdatum' "
                     "of the promulgated document",
            "evidence": [{"source": "recht.bund.de",
                          "url": meta.get("official_html_url"),
                          "field": "Ausfertigungsdatum",
                          "value": official.get("Ausfertigungsdatum")}]}
    else:
        out["adopted"] = {"date": None, "status": "unknown",
                          "reason": "no evidenced Ausfertigungsdatum in the "
                                    "stored promulgation document"}
    # promulgated (Verkündung)
    promulgated = (resolved or {}).get("promulgated") or \
        document.get("promulgated")
    if promulgated and promulgated.get("date"):
        out["promulgated"] = promulgated
    elif _german_day(official.get("Veröffentlichungsdatum")):
        out["promulgated"] = {
            "date": _german_day(official.get("Veröffentlichungsdatum")),
            "status": "official",
            "basis": "recht.bund.de document metadata "
                     "'Veröffentlichungsdatum' (Verkündung)",
            "evidence": [{"source": "recht.bund.de",
                          "url": meta.get("official_html_url"),
                          "field": "Veröffentlichungsdatum",
                          "value": official.get("Veröffentlichungsdatum")}]}
    elif _day(event.get("published_at")) and event.get("integrity_basis"):
        out["promulgated"] = {
            "date": _day(event.get("published_at")), "status": "official",
            "basis": "BGBl issue header of the integrity-checked final PDF "
                     "(matched with the DIP citation)",
            "evidence": [{"source": "BGBl", "url": event.get(
                "official_pdf_url"), "sha256": event.get("pdf_sha256")}]}
    elif _day(event.get("published_at")):
        out["promulgated"] = {
            "date": _day(event.get("published_at")), "status": "official",
            "basis": "publication date of the captured final BGBl document",
            "evidence": [{"source": "BGBl", "url": event.get(
                "official_pdf_url") or event.get("official_html_url")}]}
    else:
        out["promulgated"] = {"date": None, "status": "unknown",
                              "reason": "no evidenced promulgation date"}
    # in_force
    in_force = (resolved or {}).get("in_force")
    if in_force and (in_force.get("date") or in_force.get("status")):
        out["in_force"] = in_force
    elif _day(event.get("effective_at")) and str(
            event.get("date_status")) == "official_verified":
        out["in_force"] = {
            "date": _day(event.get("effective_at")),
            "status": "official_commencement_clause",
            "basis": ("commencement clause of the amending act, resolved for "
                      "the whole amending article ("
                      f"{event.get('date_basis')})"),
            "clause": _clause_text((meta.get("entry_into_force") or {}
                                    ).get("text")),
            "evidence": [e for e in event.get("evidence") or []
                         if e.get("source") in {"BGBl", "DIP"}]}
    else:
        out["in_force"] = {
            "date": None, "status": "unknown",
            "reason": str(event.get("date_basis") or "unresolved")}
    applies = (resolved or {}).get("applies_from")
    out["applies_from"] = applies or {"date": None, "status": "not_stated"}
    if (resolved or {}).get("findings"):
        out["findings"] = resolved["findings"]
    return out


def _clause_text(value: Any) -> str | None:
    """The commencement article without signature block and page footer."""
    text = str(value or "")
    if not text:
        return None
    for marker in ("Die verfassungsmäßigen Rechte", "Das vorstehende Gesetz",
                   "Die vorstehende", "Der Bundesrat hat zugestimmt",
                   "Herausgeber:"):
        cut = text.find(marker)
        if cut > 0:
            text = text[:cut]
    text = " ".join(text.split())
    return text[:1500] + ("…" if len(text) > 1500 else "")


# ------------------------------------------------------------------ events

class Retrospective:
    """Current-knowledge amendment events and legal intervals.

    Reads ``retrospective_history.sqlite`` when present (memory-light, used
    on the VPS) and otherwise the validated JSON manifest.
    """

    def __init__(self, data_dir: Path):
        self.db = None
        self.manifest = None
        path = data_dir / "retrospective_history.sqlite"
        if path.is_file():
            self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            return
        path = data_dir / "retrospective_history.json"
        if path.is_file():
            from api.retrospective_store import validate_manifest
            self.manifest = validate_manifest(json.loads(path.read_text(
                encoding="utf-8")))

    def _act(self, act_id: str) -> dict[str, Any]:
        from api.retrospective_store import (RetrospectiveNotFound,
                                             act_history)
        try:
            return act_history(self.manifest, act_id)
        except RetrospectiveNotFound:
            return {}

    def events(self, act_id: str) -> list[dict[str, Any]]:
        if self.db is not None:
            rows = [json.loads(row[0]) for row in self.db.execute(
                "SELECT payload_json FROM amendment_events WHERE act_id=? AND "
                "knowledge_to IS NULL", (act_id,))]
        elif self.manifest is not None:
            rows = list(self._act(act_id).get("events") or [])
        else:
            return []
        events: dict[tuple[str, str, str], dict[str, Any]] = {}
        for event in rows:
            key = (str(event.get("document_id")),
                   str(event.get("amending_article")),
                   str(event.get("event_id") or event.get("id")))
            previous = events.get(key)
            if previous is None or str(event.get("knowledge_from")) > str(
                    previous.get("knowledge_from")):
                events[key] = event
        return list(events.values())

    def intervals(self, act_id: str) -> list[dict[str, Any]]:
        if self.db is not None:
            return [json.loads(row[0]) for row in self.db.execute(
                "SELECT payload_json FROM legal_intervals WHERE act_id=? AND "
                "knowledge_to IS NULL", (act_id,))]
        if self.manifest is not None:
            return list(self._act(act_id).get("intervals") or [])
        return []


def _command_targets(command: dict[str, Any], unit: str, number: str) -> bool:
    ref = command.get("ref") or {}
    if unit == "para":
        return str(ref.get("para") or "").casefold() == number
    if unit == "art":
        return str(ref.get("article") or ref.get("art") or ""
                   ).casefold() == number
    return False


def norm_changes(act: dict[str, Any], events: list[dict[str, Any]],
                 bgbl: dict[str, dict[str, Any]], dates: ChangeDates,
                 as_of: str) -> list[dict[str, Any]]:
    """One change record per (norm, document, article, in-force date)."""
    prefix = act_prefix(act)
    changes: dict[str, dict[str, Any]] = {}
    for event in events:
        labels = list(dict.fromkeys(event.get("affected_norms") or []))
        commands = [c for c in event.get("commands") or [] if isinstance(
            c, dict)]
        for label in labels:
            unit, number = designator_unit(label)
            if unit not in {"para", "art", "anlage", "anhang"}:
                continue
            norm_id = f"{prefix}/{unit}/{number}"
            own = [c for c in commands if _command_targets(c, unit, number)]
            if not own and unit in {"anlage", "anhang"}:
                own = [c for c in commands if label.casefold() in str(
                    c.get("raw") or "").casefold()[:200]]
            groups: dict[str, list[tuple[dict[str, Any], str]]] = \
                defaultdict(list)
            for command in own or [{}]:
                item = str(command.get("item") or "")
                letters = _letters(str(command.get("raw") or ""))
                keys = [f"{item}.{letter}" for letter in letters] if \
                    letters else [item]
                # group by the most specific date resolution
                resolved = {key: event_dates(event, key, bgbl, dates)
                            for key in keys}
                by_date: dict[str, list[str]] = defaultdict(list)
                for key, value in resolved.items():
                    by_date[_json(value.get("in_force"))].append(key)
                for date_key, item_keys in by_date.items():
                    groups[date_key].append((command, ",".join(item_keys)))
            for date_key, members in groups.items():
                cmd_list = [c for c, _ in members if c]
                item_keys = [k for _, k in members if k]
                first_key = item_keys[0].split(",")[0] if item_keys else ""
                values = event_dates(event, first_key, bgbl, dates)
                document_id = str(event.get("document_id") or "")
                article = str(event.get("amending_article") or "")
                change_id = "chg:" + _sha(_json([
                    norm_id, document_id, article, date_key]))[:24]
                title = event.get("procedure_title") or (
                    bgbl.get(document_id) or {}).get("title")
                gz = gazette(document_id)
                adopted = (values.get("adopted") or {}).get("date")
                kind = "G"
                if title and re.match(r"(?:Erste|Zweite|Dritte|\w+te)?\s*"
                                      r"Verordnung|Verordnung", title):
                    kind = "V"
                items = [k for k in item_keys if k]
                citation = _citation(article, items, kind, adopted, gz)
                record = {
                    "change_id": change_id,
                    "act_id": act.get("id"),
                    "norm_id": norm_id,
                    "norm_label": label,
                    "amending_act": {
                        "document_id": document_id,
                        "title": title,
                        "citation": citation,
                        "citation_basis": ("composed from the evidenced "
                                           "article, adoption date and "
                                           "gazette reference"),
                        "article": article,
                        "items": items,
                        "article_heading": event.get("article_heading"),
                        "gazette": gz,
                        "url": event.get("official_html_url")
                        or event.get("official_pdf_url"),
                        "pdf_url": event.get("official_pdf_url"),
                        "procedure_id": event.get("procedure_id"),
                        "event_id": event.get("event_id") or event.get("id"),
                    },
                    "dates": {k: values[k] for k in (
                        "adopted", "promulgated", "in_force",
                        "applies_from")},
                    "change_type": classify_change(label, cmd_list),
                    "source_role": "official_promulgation",
                    "commands": [{
                        "item": c.get("item"),
                        "operation": c.get("operation"),
                        "ref": c.get("ref"),
                        "raw": c.get("raw"),
                        "new_text": c.get("new_text"),
                        "old_text_constraint": c.get("old_text_constraint"),
                    } for c in cmd_list],
                    "text_effect": None,
                    "evidence": [e for e in event.get("evidence") or []],
                    "verification": event.get("verification"),
                }
                if values.get("findings"):
                    record["date_findings"] = values["findings"]
                record["sort_date"] = (
                    change_effective_date(record)
                    or (record["dates"]["promulgated"] or {}).get("date")
                    or "0000-00-00")
                changes[change_id] = record
    return sorted(changes.values(), key=lambda c: (c["sort_date"],
                                                   c["change_id"]))


def _citation(article: str, items: list[str], kind: str,
              adopted: str | None, gz: dict[str, Any] | None) -> str | None:
    if not gz:
        return None
    nr = ""
    if items:
        numbers = sorted({k.split(".")[0] for k in items if k},
                         key=lambda v: (len(v), v))
        if len(numbers) == 1:
            nr = f" Nr. {numbers[0]}"
    when = ""
    if adopted:
        y, m, d = adopted.split("-")
        when = f" v. {int(d)}.{int(m)}.{y}"
    head = f"Art. {article}{nr} " if article else ""
    return f"{head}{kind}{when} ({gz['reference']})".strip()


# ---------------------------------------------------------------- versions

@dataclass
class Run:
    identity: str
    states: list[StateObs] = field(default_factory=list)
    row: dict[str, Any] | None = None

    @property
    def known_from(self) -> str | None:
        days = [s.known_from for s in self.states if s.known_from]
        return min(days) if days else None

    @property
    def known_until(self) -> str | None:
        days = [s.known_until or s.known_from for s in self.states
                if s.known_until or s.known_from]
        return max(days) if days else None


def _norm_runs(sequence: list[tuple[StateObs, str]],
               findings: list[dict[str, Any]], norm_id: str) -> list[Run]:
    """Consecutive identical texts → runs; drop time-contradicting captures."""
    runs: list[Run] = []
    kept: list[tuple[StateObs, str]] = []
    for state, identity in sequence:
        if kept and identity != kept[-1][1]:
            previous = kept[-1][0]
            # a capture dated inside a previous official observation
            # interval that shows different text contradicts that
            # observation; the daily official consolidation wins.
            prev_until = previous.known_until or previous.known_from
            if state.source != "gii_daily" and state.source_role != \
                    "official_consolidation" and previous.source == \
                    "gii_daily" and state.known_from and prev_until and \
                    state.known_from < prev_until:
                findings.append({
                    "kind": "capture_contradicts_official_observation",
                    "norm_id": norm_id,
                    "capture": {"source": state.source,
                                "archive_url": state.archive_url,
                                "captured_at": state.captured_at},
                    "official": {"state": previous.key,
                                 "observed_from": previous.known_from,
                                 "observed_until": prev_until},
                    "resolution": "capture not used as a version"})
                continue
        kept.append((state, identity))
    for state, identity in kept:
        if runs and runs[-1].identity == identity:
            runs[-1].states.append(state)
        else:
            runs.append(Run(identity=identity, states=[state]))
    return runs


def _attestations(run: Run) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for state in run.states:
        entry = grouped.setdefault(state.source, {
            "source": {"gii_daily": "GII", "neuris": "NeuRIS",
                       "wayback_gii": "Wayback/GII",
                       "wayback_gii_xml": "Wayback/GII",
                       "bayern_recht": "BAYERN.RECHT",
                       "wayback_bayern": "Wayback/BAYERN.RECHT",
                       "reconstruction": "Lexgraph reconstruction"}.get(
                           state.source, state.source),
            "role": state.source_role, "known_from": None,
            "known_until": None, "count": 0, "samples": []})
        entry["count"] += 1
        if state.known_from and (entry["known_from"] is None or
                                 state.known_from < entry["known_from"]):
            entry["known_from"] = state.known_from
        until = state.known_until or state.known_from
        if until and (entry["known_until"] is None or
                      until > entry["known_until"]):
            entry["known_until"] = until
        if len(entry["samples"]) < 3:
            entry["samples"].append({k: v for k, v in {
                "url": state.url, "archive_url": state.archive_url,
                "sha256": state.sha256, "known_at": state.known_from,
                "state_seq": state.seq}.items() if v is not None})
    return sorted(grouped.values(), key=lambda e: (e["known_from"] or "",
                                                   e["source"]))


_QUOTE = re.compile(r"[„“\"”»«]")


def _fold(value: str | None) -> str:
    return re.sub(r"\s+", "", _QUOTE.sub("", str(value or ""))).casefold()


def _fold_variants(value: str | None) -> set[str]:
    """Folded wording, with and without PDF line-end hyphenation."""
    raw = str(value or "")
    joined = re.sub(r"(?<=[a-zäöüß])-\s*\n\s*(?=[a-zäöüß])", "", raw)
    return {v for v in (_fold(raw), _fold(joined)) if v}


def command_reflection(change: dict[str, Any], before: str | None,
                       after: str) -> str:
    """Is the change's quoted wording visible across one text transition?"""
    old_folded, new_folded = _fold(before), _fold(after)
    verdicts = []
    for command in change.get("commands") or []:
        news = {v for v in _fold_variants(command.get("new_text"))
                if len(v) >= 6}
        olds = {v for v in _fold_variants(command.get("old_text_constraint"))
                if len(v) >= 6}
        if news:
            present = any(v in new_folded for v in news)
            was = any(v in old_folded for v in news)
            if present and (before is None or not was or olds):
                verdicts.append("reflected")
            elif not present:
                verdicts.append("not_reflected")
            else:
                verdicts.append("unknown")
        elif olds:
            verdicts.append("reflected" if any(
                v in old_folded and v not in new_folded for v in olds)
                else "unknown")
        else:
            verdicts.append("unknown")
    if not verdicts:
        return "unknown"
    if "not_reflected" in verdicts and "reflected" not in verdicts:
        return "not_reflected"
    if "reflected" in verdicts and "not_reflected" not in verdicts:
        return "reflected"
    return "mixed" if "reflected" in verdicts else "unknown"


def attribute(runs: list[Run], changes: list[dict[str, Any]],
              findings: list[dict[str, Any]], norm_id: str,
              as_of: str) -> list[dict[str, Any]]:
    """Per-version temporal/amendment records; mutates change text_effect."""
    records: list[dict[str, Any]] = []
    used: set[str] = set()
    for index, run in enumerate(runs):
        record: dict[str, Any] = {"valid_from": None,
                                  "valid_from_status": "unresolved",
                                  "valid_from_basis": None,
                                  "change_ids": [], "attribution": None}
        first_state = run.states[0]
        # a verified GII interval for the state where this text first appears
        if first_state.valid_from and first_state.valid_from_status not in {
                None, "unresolved"} and index > 0:
            record.update(valid_from=first_state.valid_from,
                          valid_from_status=first_state.valid_from_status,
                          valid_from_basis=first_state.valid_from_basis
                          or "verified retrospective interval",
                          interval_amendment=first_state.amendment)
        if index == 0:
            record["attribution"] = {"status": "predates_text_history"}
            records.append(record)
            continue
        previous = runs[index - 1]
        low = previous.known_until or previous.known_from
        high = run.known_from
        window = {"after": low, "until": high}
        candidates = []
        for change in changes:
            if change["change_id"] in used:
                continue
            effective = change_effective_date(change)
            promulgated = ((change.get("dates") or {}).get("promulgated")
                           or {}).get("date")
            if effective and low and high and low < effective <= high:
                candidates.append((change, "in_force_in_window"))
            elif not effective and promulgated and high and \
                    promulgated <= high and (not low or promulgated > low):
                candidates.append((change, "promulgated_in_window_date_"
                                           "unknown"))
        before_text = previous.row.get("text") if previous.row else None
        after_text = run.row.get("text") if run.row else ""
        scored = []
        for change, why in candidates:
            verdict = command_reflection(change, before_text, after_text)
            scored.append((change, why, verdict))
        # Every amendment targeting this norm whose evidenced in-force date
        # lies between the two captures explains (part of) the change; the
        # quoted wording may have been overwritten by a later one, so a
        # wording mismatch never rejects a dated candidate.  Undated
        # candidates need the wording check not to contradict them.
        dated = [s for s in scored if s[1] == "in_force_in_window"]
        plausible = dated or [s for s in scored if s[2] != "not_reflected"]
        chosen: list[tuple[dict[str, Any], str, str]] = []
        status = None
        if dated:
            dates_seen = sorted({change_effective_date(s[0]) for s in dated})
            if len(dates_seen) == 1:
                chosen = dated
                status = "bracketed_by_captures"
            else:
                # several amendments with different dates fall between two
                # captures: the text before the last one was never captured
                chosen = dated
                status = "multiple_amendments_between_captures"
        elif len(plausible) == 1:
            chosen = plausible
            status = "bracketed_promulgation_only"
        elif not plausible:
            findings.append({
                "kind": "unattributed_text_change", "norm_id": norm_id,
                "window": window,
                "candidates_rejected": [
                    {"change_id": s[0]["change_id"], "reason": s[2]}
                    for s in scored],
                "note": "the text changed between these captures but no "
                        "stored amendment explains it (missing amending "
                        "act, editorial correction or parser gap)"})
        else:
            findings.append({
                "kind": "ambiguous_attribution", "norm_id": norm_id,
                "window": window,
                "candidates": [s[0]["change_id"] for s in plausible]})
        if chosen:
            verified = any(s[2] == "reflected" for s in chosen)
            for change, why, verdict in chosen:
                used.add(change["change_id"])
                change["text_effect"] = {
                    "before_version": index - 1, "after_version": index,
                    "observed_from": run.known_from,
                    "previous_observed_until": low,
                    "verification": ("command_wording_reflected"
                                     if verdict == "reflected" else
                                     "bracketed_by_captures"),
                    "attribution": status}
            record["change_ids"] = [s[0]["change_id"] for s in chosen]
            record["attribution"] = {
                "status": status, "window": window,
                "verification": "command_wording_reflected" if verified
                else "bracketed_by_captures"}
            latest = max((change_effective_date(s[0]) or "" for s in chosen),
                         default="")
            if latest and record["valid_from"] is None:
                source_change = next(s[0] for s in chosen
                                     if change_effective_date(s[0]) == latest)
                in_force = source_change["dates"]["in_force"]
                amending = source_change["amending_act"]
                label = amending.get("citation") or amending.get(
                    "document_id")
                record.update(
                    valid_from=latest,
                    valid_from_status=in_force.get("status"),
                    valid_from_basis=(
                        f"in force date of {label} "
                        f"({in_force.get('basis')}); the text change is "
                        f"observed between {low} and {high}"))
            if status == "multiple_amendments_between_captures":
                findings.append({
                    "kind": "intermediate_version_not_captured",
                    "norm_id": norm_id, "window": window,
                    "changes": [{"change_id": s[0]["change_id"],
                                 "in_force": change_effective_date(s[0])}
                                for s in chosen],
                    "note": "a version valid between the earlier and the "
                            "later amendment is not in any captured source"})
        records.append(record)
    for position, record in enumerate(records[:-1]):
        following = records[position + 1]
        if following.get("valid_from"):
            record["valid_until"] = following["valid_from"]
            record["valid_until_status"] = following["valid_from_status"]
            record["valid_until_basis"] = "legal start of the next version"
    return records


# ------------------------------------------------------------------ writer

class Writer:
    def __init__(self, path: Path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.executescript(DDL)
        self.text_ids: dict[str, int] = {}

    def text_id(self, identity: str, row: dict[str, Any]) -> int:
        if identity in self.text_ids:
            return self.text_ids[identity]
        text = str(row.get("text") or "")
        payload = {"enbez": str(row.get("enbez") or ""),
                   "titel": str(row.get("titel") or ""), "text": text}
        content = _sha(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")))
        cursor = self.db.execute(
            "INSERT INTO texts(identity, content_sha256, enbez, titel, body) "
            "VALUES (?,?,?,?,?)", (identity, content, payload["enbez"],
                                   payload["titel"], pack_text(text)))
        self.text_ids[identity] = int(cursor.lastrowid)
        return self.text_ids[identity]

    def close(self, meta: dict[str, Any]) -> None:
        self.db.executemany("INSERT INTO meta VALUES (?,?)",
                            [(k, v if isinstance(v, str) else _json(v))
                             for k, v in meta.items()])
        self.db.commit()
        self.db.execute("VACUUM")
        self.db.close()


# ------------------------------------------------------------------ driver

def build_act(writer: Writer, act: dict[str, Any], data_dir: Path,
              retro: Retrospective,
              bundle: sqlite3.Connection | None,
              bgbl: dict[str, dict[str, Any]], dates: ChangeDates,
              as_of: str) -> dict[str, Any]:
    act_id = str(act["id"])
    prefix = act_prefix(act)
    findings: list[dict[str, Any]] = []
    if act_id.startswith("by_") and not _bundle_has(bundle, act_id):
        return build_bavarian_archive_act(writer, act, as_of)
    if act_id.startswith("by_"):
        states = bavarian_states(act, bundle)
    else:
        states = gii_states(act, data_dir, retro.intervals(act_id))
        states += bundle_states(bundle, act_id)
    states.sort(key=lambda s: s.sort_key)
    for seq, state in enumerate(states):
        state.seq = seq
    events = retro.events(act_id)
    changes = norm_changes(act, events, bgbl, dates, as_of)
    changes_by_norm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for change in changes:
        changes_by_norm[change["norm_id"]].append(change)

    # norm_id -> [(state, identity)] in chain order; one canonical row per
    # identity (GII XML wording preferred) keeps memory at unique texts.
    sequences: dict[str, list[tuple[StateObs, str]]] = defaultdict(list)
    best: dict[str, tuple[tuple[int, tuple[str, int, str]], dict[str, Any]]] \
        = {}
    stand_by_state: dict[int, dict[str, Any]] = {}
    for state in states:
        try:
            rows = state.rows()
        except Exception as exc:  # unreadable state: typed finding
            findings.append({"kind": "state_unreadable", "norm_id": None,
                             "state": state.key, "error": str(exc)[:300]})
            continue
        if state.source == "gii_daily" and state.stand is None:
            try:
                payload = _read_state_object(data_dir, state.key)
                state.stand = payload.get("stand")
                del payload
            except Exception:
                pass
        stand_by_state[state.seq] = parse_notes(state.stand)
        rank = (TEXT_PRIORITY.get(state.source, 5), state.sort_key)
        for norm_id, row in norm_rows_with_ids(act, rows):
            identity = norm_identity(row.get("enbez"), row.get("titel"),
                                     row.get("text"))
            sequences[norm_id].append((state, identity))
            current = best.get(identity)
            if current is None or rank < current[0]:
                best[identity] = (rank, {"enbez": row.get("enbez"),
                                         "titel": row.get("titel"),
                                         "text": row.get("text")})
        if state.loader is not None:
            state.norms = None
        del rows

    latest_seq = states[-1].seq if states else -1
    version_count = 0
    norm_rows = []
    for norm_id, sequence in sequences.items():
        runs = _norm_runs(sequence, findings, norm_id)
        for run in runs:
            run.row = best[run.identity][1]
        own_changes = changes_by_norm.get(norm_id, [])
        temporal = attribute(runs, own_changes, findings, norm_id, as_of)
        for vseq, (run, time_record) in enumerate(zip(runs, temporal)):
            role = min((s.source_role for s in run.states),
                       key=lambda r: ("official_consolidation",
                                      "official_promulgation",
                                      "archived_capture",
                                      "reconstruction").index(r)
                       if r in ("official_consolidation",
                                "official_promulgation", "archived_capture",
                                "reconstruction") else 9)
            complete = all(s.complete for s in run.states)
            sources = {s.source for s in run.states}
            state_intervals = [{
                "state": s.key, "valid_from": s.valid_from,
                "valid_until": s.valid_until,
                "valid_from_status": s.valid_from_status,
                "valid_from_basis": s.valid_from_basis}
                for s in run.states if s.valid_from and s.valid_from_status
                not in {None, "unresolved"}]
            record = {
                "state_intervals": state_intervals,
                "attestations": _attestations(run),
                "temporal": {**time_record, "known_from": run.known_from,
                             "known_until": run.known_until},
                "completeness": {
                    "status": ("complete" if complete else "partial")
                    if role != "reconstruction" else "reconstructed",
                    "reason": None if complete else
                    "only partial captures of this act contain this text"},
                "verification": {
                    "cross_source": ("agree" if len(sources) > 1 else
                                     "single_source"),
                    "sources": sorted(sources)},
                "first_state": run.states[0].key,
                "last_state": run.states[-1].key,
                "state_count": len(run.states),
            }
            writer.db.execute(
                "INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (norm_id, vseq, act_id, writer.text_id(run.identity, run.row),
                 run.states[0].seq, run.states[-1].seq, run.known_from,
                 run.known_until, role, pack_json(record)))
            version_count += 1
        last_row = runs[-1].row or {}
        present = runs[-1].states[-1].seq == latest_seq
        summary = summarize(own_changes, scope="norm", subject=norm_id,
                            as_of=as_of,
                            versions=[{"source_role": s.source_role}
                                      for s in (r.states[0] for r in runs)])
        norm_rows.append((norm_id, act_id, str(last_row.get("enbez") or ""),
                          str(last_row.get("titel") or ""), int(present),
                          len(runs), len(own_changes), runs[0].known_from,
                          _json({"counts": summary["counts"]})))
    # changes whose norm never appears in any stored text
    for norm_id, own in changes_by_norm.items():
        if norm_id not in sequences:
            findings.append({"kind": "change_targets_norm_without_text",
                             "norm_id": norm_id,
                             "changes": [c["change_id"] for c in own][:20]})
    writer.db.executemany("INSERT INTO norms VALUES (?,?,?,?,?,?,?,?,?)",
                          norm_rows)
    for change in changes:
        writer.db.execute(
            "INSERT INTO changes VALUES (?,?,?,?,?,?,?,?)",
            (change["change_id"], act_id, change["norm_id"],
             change["norm_label"], change["amending_act"]["document_id"],
             change["sort_date"],
             "in_force" if change_effective_date(change) else "unknown",
             pack_json(change)))
    for state in states:
        notes = stand_by_state.get(state.seq) or parse_notes(state.stand)
        writer.db.execute(
            "INSERT INTO states VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (act_id, state.seq, state.key, state.source, state.source_role,
             state.known_from, state.known_until, state.url,
             state.archive_url, state.sha256, state.stand, _json(notes),
             int(state.complete), state.valid_from, state.valid_from_status,
             state.valid_from_basis, state.valid_until,
             _json(state.interval) if state.interval else None))
    # act-level stand notes vs. event store: every GII "zuletzt geändert"
    # act must be a stored amendment document
    known_docs = {c["amending_act"]["document_id"] for c in changes}
    event_docs = {str(e.get("document_id")) for e in events}
    for seq, notes in stand_by_state.items():
        ref = ((notes.get("act_stand_note") or {}).get("amending_act") or {}
               ).get("gazette_lookup") or {}
        doc = ref.get("document_id")
        if doc and doc not in event_docs:
            findings.append({"kind": "stand_amending_act_not_in_event_store",
                             "norm_id": None, "document_id": doc,
                             "citation_text": (notes["act_stand_note"]
                                               .get("last_amended_by_text")),
                             "state_seq": seq})
    dedup = {}
    for finding in findings:
        dedup[_json(finding)] = finding
    writer.db.executemany(
        "INSERT INTO findings(act_id, norm_id, kind, detail_json) "
        "VALUES (?,?,?,?)",
        [(act_id, f.get("norm_id"), f["kind"],
          _json({k: v for k, v in f.items() if k not in {"kind", "norm_id"}}))
         for f in dedup.values()])
    roles: dict[str, int] = defaultdict(int)
    for state in states:
        roles[state.source_role] += 1
    act_summary = summarize(
        changes, scope="act", subject=act_id, as_of=as_of,
        stand_notes=stand_by_state.get(latest_seq))
    summary = {
        "states": len(states),
        "states_by_role": dict(roles),
        "states_by_source": {src: sum(1 for s in states if s.source == src)
                             for src in sorted({s.source for s in states})},
        "earliest_text": min((s.known_from for s in states if s.known_from),
                             default=None),
        "latest_text": max((s.known_until or s.known_from for s in states
                            if s.known_from), default=None),
        "norm_versions": version_count,
        "norms": len(sequences),
        "changes": len(changes),
        "amending_documents": len(known_docs),
        "findings": {kind: sum(1 for f in dedup.values() if f["kind"] == kind)
                     for kind in sorted({f["kind"] for f in dedup.values()})},
        "event_history_start": min((c["sort_date"] for c in changes
                                    if c["sort_date"] > "0000"), default=None),
        "change_summary_counts": act_summary["counts"],
    }
    writer.db.execute("INSERT INTO acts VALUES (?,?,?,?,?,?)",
                      (act_id, prefix, act.get("jurabk"), act.get("title"),
                       act.get("juris"), _json(summary)))
    writer.db.commit()
    return summary


def _bundle_has(bundle: sqlite3.Connection | None, act_id: str) -> bool:
    if bundle is None:
        return False
    return bundle.execute("SELECT 1 FROM captures WHERE act_id=? LIMIT 1",
                          (act_id,)).fetchone() is not None


_BAVARIAN_ROLE = {
    "archived_official_page_bayern_recht": "archived_capture",
    "official_daily_snapshot_bayern_recht": "official_consolidation",
    "official_consolidation_bayern_recht": "official_consolidation",
}


def build_bavarian_archive_act(writer: Writer, act: dict[str, Any],
                               as_of: str) -> dict[str, Any]:
    """Bavarian per-norm chains from archived official pages (no bundle).

    Reuses the per-norm chain logic of ``api.norm_history`` (archived
    BAYERN.RECHT pages + daily snapshots, validity from the official
    "gültig ab" table) so an act keeps every version it had before; the
    change history of Bavarian acts comes from the workstation bundle.
    """
    from api.norm_history import NormHistoryStore
    act_id = str(act["id"])
    chain = NormHistoryStore._bavarian_chain(None, act)  # type: ignore[arg-type]
    versions_total = 0
    norm_rows = []
    for norm_id in chain.norms:
        versions = chain.versions_of(norm_id)
        for vseq, version in enumerate(versions):
            first, last = version.first_state, version.last_state
            role = _BAVARIAN_ROLE.get(first.source_role,
                                      "official_consolidation")
            capture = version.source_capture or {}
            record = {
                "attestations": [{
                    "source": ("Wayback/BAYERN.RECHT" if role ==
                               "archived_capture" else "BAYERN.RECHT"),
                    "role": role, "known_from": first.first_observed_at,
                    "known_until": last.last_observed_at
                    or first.first_observed_at, "count": len(version.states),
                    "samples": [{k: v for k, v in {
                        "url": first.source_url,
                        "capture": capture.get("capture"),
                        "known_at": first.first_observed_at}.items() if v}]}],
                "temporal": {
                    "valid_from": first.valid_from,
                    "valid_from_status": first.valid_from_status,
                    "valid_from_basis": first.valid_from_basis,
                    "valid_until": last.valid_until,
                    "valid_until_status": last.valid_until_status,
                    "valid_until_basis": last.valid_until_basis,
                    "change_ids": [], "attribution": None,
                    "known_from": first.first_observed_at,
                    "known_until": last.last_observed_at},
                "completeness": {"status": "complete", "reason": None},
                "verification": {"cross_source": "single_source",
                                 "sources": [first.source_role]},
                "bavarian_amendment": first.amendment or None,
            }
            row = {"enbez": version.label, "titel": version.title,
                   "text": version.text}
            identity = norm_identity(version.label, version.title,
                                     version.text)
            writer.db.execute(
                "INSERT INTO versions VALUES (?,?,?,?,?,?,?,?,?,?)",
                (norm_id, vseq, act_id, writer.text_id(identity, row),
                 first.index, last.index, first.first_observed_at,
                 last.last_observed_at or first.first_observed_at, role,
                 pack_json(record)))
            versions_total += 1
        if versions:
            norm_rows.append((norm_id, act_id, versions[-1].label,
                              versions[-1].title,
                              int(chain.present_in_last_state(norm_id)),
                              len(versions), 0,
                              versions[0].first_state.first_observed_at,
                              _json({})))
    writer.db.executemany("INSERT INTO norms VALUES (?,?,?,?,?,?,?,?,?)",
                          norm_rows)
    for state in chain.states:
        writer.db.execute(
            "INSERT INTO states VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (act_id, state.index, state.key, "bayern_recht",
             _BAVARIAN_ROLE.get(state.source_role, "official_consolidation"),
             state.first_observed_at, state.last_observed_at,
             state.source_url, None, None, None, _json({}), 1,
             state.valid_from, state.valid_from_status,
             state.valid_from_basis, state.valid_until, None))
    summary = {
        "states": len(chain.states),
        "states_by_role": {}, "states_by_source": {"bayern_recht":
                                                   len(chain.states)},
        "earliest_text": min((s.first_observed_at for s in chain.states
                              if s.first_observed_at), default=None),
        "latest_text": None, "norm_versions": versions_total,
        "norms": len(norm_rows), "changes": 0, "amending_documents": 0,
        "findings": {}, "event_history_start": None,
        "event_history_status": "not_built",
        "event_history_note": ("the Bavarian change history is built from "
                               "the workstation bundle; none was supplied"),
    }
    writer.db.execute("INSERT INTO acts VALUES (?,?,?,?,?,?)",
                      (act_id, act_prefix(act), act.get("jurabk"),
                       act.get("title"), act.get("juris"), _json(summary)))
    writer.db.commit()
    return summary


def bavarian_states(act: dict[str, Any], bundle: sqlite3.Connection | None
                    ) -> list[StateObs]:
    """BAYERN.RECHT head (+ bundle captures when available)."""
    build = str(act.get("build") or "")
    day = None
    if match := re.fullmatch(r"(\d{4})(\d{2})(\d{2})", build):
        day = f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    elif match := re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", build):
        day = f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    head = StateObs(
        key=f"{act['id']}:head", source="bayern_recht",
        source_role="official_consolidation", known_from=day,
        known_until=day, url=act.get("permalink"),
        norms=[{"enbez": n.get("enbez"), "titel": n.get("titel"),
                "text": n.get("text")} for n in act.get("norms") or []])
    return [head] + bundle_states(bundle, str(act["id"]))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, default=ROOT / "web" / "data")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--bundle", type=Path, default=None)
    parser.add_argument("--bundle-signers", type=Path, default=None,
                        help="allowed_signers file; the bundle manifest "
                             "signature must verify or the bundle is ignored")
    parser.add_argument("--documents", type=Path,
                        default=ROOT / "data" / "snapshots")
    parser.add_argument("--change-dates", type=Path, action="append",
                        default=[])
    parser.add_argument("--acts", default="",
                        help="comma-separated act ids (default: all)")
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    data_dir = args.data_dir.resolve()
    out = (args.out or data_dir / DB_FILE).resolve()
    as_of = args.as_of or date.today().isoformat()
    started = time.time()
    retro = Retrospective(data_dir)
    bundle = None
    bundle_meta: dict[str, Any] = {}
    date_paths = list(args.change_dates)
    if args.bundle and args.bundle_signers:
        try:
            from tools.history_bundle import verify as verify_bundle
            verify_bundle(args.bundle, signers=args.bundle_signers)
        except (SystemExit, Exception) as exc:  # never build on bad bytes
            print(f"[warn] history bundle rejected ({exc}); building from "
                  "the official daily states only", file=sys.stderr)
            args.bundle = None
    if args.bundle and (args.bundle / "corpus.sqlite").is_file():
        bundle = sqlite3.connect(
            f"file:{args.bundle / 'corpus.sqlite'}?mode=ro", uri=True)
        bundle.row_factory = sqlite3.Row
        manifest = args.bundle / "manifest.json"
        if manifest.is_file():
            bundle_meta = json.loads(manifest.read_text())
    if args.bundle and (args.bundle / "change_dates.jsonl.gz").is_file():
        date_paths.append(args.bundle / "change_dates.jsonl.gz")
    dates = ChangeDates(date_paths)
    bgbl = load_bgbl_documents(args.documents if args.documents and
                               args.documents.is_dir() else None)
    wanted = {a for a in args.acts.split(",") if a}
    act_files = sorted((data_dir / "acts").glob("*.json"))
    tmp = Path(tempfile.mkstemp(prefix=".law_history-", suffix=".sqlite",
                                dir=out.parent)[1])
    tmp.unlink()
    writer = Writer(tmp)
    totals = {"acts": 0, "norm_versions": 0, "changes": 0, "states": 0}
    try:
        for path in act_files:
            act_id = path.stem
            if wanted and act_id not in wanted:
                continue
            if not (act_id.startswith("fed_") or act_id.startswith("by_")):
                continue
            act = json.loads(path.read_text(encoding="utf-8"))
            if not (act.get("norms") or act.get("official_states")):
                continue
            summary = build_act(writer, act, data_dir, retro, bundle, bgbl,
                                dates, as_of)
            totals["acts"] += 1
            for key in ("norm_versions", "changes", "states"):
                totals[key] += summary[key]
        writer.close({
            "schema": DB_SCHEMA, "builder": BUILDER,
            "built_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds").replace("+00:00", "Z"),
            "as_of": as_of, "totals": totals,
            "bundle": {k: bundle_meta.get(k) for k in (
                "built_at", "schema", "sha256", "sources")},
            "change_date_rows": len(dates.rows),
        })
        os.chmod(tmp, 0o644)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()
    print(json.dumps({"out": str(out), **totals,
                      "seconds": round(time.time() - started, 1)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
