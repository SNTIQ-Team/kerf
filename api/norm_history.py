"""Per-norm version chains derived from Lexgraph's canonical stores.

Lexgraph stays canonical.  This module only *projects* what the data plane
already proves into per-norm versions, which both the REST operations
(``log``/``at``/``diff``/``blame``) and the Git export consume:

Federal (deep GII corpus)
    complete content-addressed GII states (``federal_states``), ordered by
    first retrieval observation, plus the bitemporal retrospective intervals
    that give a *verified* legal start (final BGBl command + commencement
    clause) for some states.  A state without such an interval keeps
    ``valid_from: null`` — a retrieval date is never promoted to a legal date.

Bavaria (BAYERN.RECHT)
    per-norm old/new texts from Lexgraph's archived official pages (Wayback
    captures of gesetze-bayern.de) and its own daily snapshots, chained
    backwards from the current consolidated text.  Chains stop at the first
    missing or inconsistent link rather than guessing.

EU and treaties
    German article texts of the curated EU corpus and the EMRK
    (pipeline/fetch_eu_texts.py, tools/eu_history.py) are served from the
    law history store only; other EU instruments are metadata only and
    answer ``not_in_corpus``.
"""
from __future__ import annotations

import difflib
import gzip
import hashlib
import json
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from api.evidence import EnvelopeError
from api.law_structure import (
    human_locator,
    normalize_sentence,
    parse_norm_structure,
    sentence_units,
)
from api.norm_identity import (
    ActResolver,
    NormIdentityError,
    act_slug,
    designator_unit,
    exact_act_aliases,
    gii_norm_url,
    norm_rows_with_ids,
    parse_citation,
    split_norm_id,
    unit_label,
)
from api.retrospective_store import (
    RetrospectiveIntegrityError,
    RetrospectiveNotFound,
    act_history,
)

HISTORY_SCHEMA = "lexgraph-norm-history/1"
_DIGEST = re.compile(r"[0-9a-f]{64}")


# ----------------------------------------------------------------- helpers

def is_annex(norm_id: str) -> bool:
    """Anlagen/Anhänge are tables or forms, not Absatz-structured text."""
    parts = norm_id.split("/")
    return len(parts) > 3 and parts[3] in {"anlage", "anhang"}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def norm_content_hash(row: dict[str, Any]) -> str:
    """Hash of the legal content of one norm (designator, title, body)."""
    payload = {"enbez": str(row.get("enbez") or ""),
               "titel": str(row.get("titel") or ""),
               "text": str(row.get("text") or "")}
    return _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")))


def bgbl_reference(document_id: str | None) -> str | None:
    """``bgbl-1-2024-54`` → ``BGBl. 2024 I Nr. 54``; legacy page numbers."""
    raw = str(document_id or "")
    if match := re.fullmatch(r"bgbl-(1|2)-(\d{4})-s(\d+)", raw):
        part = "I" if match.group(1) == "1" else "II"
        return f"BGBl. {part} {match.group(2)} S. {match.group(3)}"
    if match := re.fullmatch(r"bgbl-(1|2)-(\d{4})-(\d+)", raw):
        part = "I" if match.group(1) == "1" else "II"
        return f"BGBl. {match.group(2)} {part} Nr. {match.group(3)}"
    return None


def parse_stand(stand: str | None) -> dict[str, Any]:
    """Split GII's ``Stand`` note; the last amendment is source-declared."""
    raw = " ".join(str(stand or "").split())
    result: dict[str, Any] = {"raw": raw or None}
    if match := re.search(r"zuletzt geändert durch (.+?)(?:\s*\||$)", raw):
        result["last_amended_by"] = match.group(1).strip().rstrip(";")
    if match := re.search(r"Hinweis: (.+?)(?:\s*\||$)", raw):
        result["pending_note"] = match.group(1).strip()
    return result


def _lower_date(*values: str | None) -> str | None:
    dates = [value for value in values if value]
    return min(dates) if dates else None


# -------------------------------------------------------------- data model

@dataclass
class StateVersion:
    """One consolidated version of an act (a complete state or edition)."""
    act_id: str
    key: str                      # state digest or Bavarian edition key
    index: int = 0
    first_observed_at: str | None = None
    last_observed_at: str | None = None
    builddate: str | None = None
    stand: str | None = None
    text_status: str = "official_exact"
    source_role: str = "official_consolidation"
    source_url: str | None = None
    valid_from: str | None = None
    valid_from_status: str = "unresolved"
    valid_from_basis: str | None = None
    valid_until: str | None = None
    valid_until_status: str = "unresolved"
    valid_until_basis: str | None = None
    published_at: str | None = None
    amendment: dict[str, Any] = field(default_factory=dict)
    interval_id: str | None = None
    knowledge_from: str | None = None
    gaps: list[Any] = field(default_factory=list)
    derived: bool = False
    # first observation (or legal start) of the version that replaced this
    # one when its own legal end is unknown: a bound for selection only,
    # never reported as a legal date
    superseded_by_observation: str | None = None

    @property
    def order_date(self) -> str:
        return self.valid_from or self.first_observed_at or "9999-12-31"

    def public(self) -> dict[str, Any]:
        return {
            "state": self.key,
            "text_status": self.text_status,
            "source_role": self.source_role,
            "first_observed_at": self.first_observed_at,
            "last_observed_at": self.last_observed_at,
            "source_build": self.builddate,
            "valid_from": self.valid_from,
            "valid_from_status": self.valid_from_status,
            "valid_from_basis": self.valid_from_basis,
            "valid_until_exclusive": self.valid_until,
            "valid_until_status": self.valid_until_status,
            "valid_until_basis": self.valid_until_basis,
            "published_at": self.published_at,
            "interval_id": self.interval_id,
            "knowledge_from": self.knowledge_from,
        }


@dataclass
class NormVersion:
    norm_id: str
    label: str
    title: str
    text: str
    content_sha256: str
    first_state: StateVersion
    last_state: StateVersion
    states: list[StateVersion]
    bavarian: bool = False
    source_capture: dict[str, Any] = field(default_factory=dict)

    @property
    def annex(self) -> bool:
        return is_annex(self.norm_id)

    @property
    def version_id(self) -> str:
        anchor = (self.valid_from or
                  f"observed-{self.first_state.first_observed_at or 'unknown'}")
        return f"{self.norm_id}@{anchor}#{self.content_sha256[:12]}"

    @property
    def valid_from(self) -> str | None:
        return self.first_state.valid_from

    @property
    def valid_until(self) -> str | None:
        return self.last_state.valid_until

    def temporal(self) -> dict[str, Any]:
        observed = [s.first_observed_at for s in self.states
                    if s.first_observed_at]
        last_observed = [s.last_observed_at for s in self.states
                         if s.last_observed_at]
        return {
            "valid_from": self.first_state.valid_from,
            "valid_from_status": self.first_state.valid_from_status,
            "valid_from_basis": self.first_state.valid_from_basis,
            "valid_until_exclusive": self.last_state.valid_until,
            "valid_until_status": self.last_state.valid_until_status,
            "valid_until_basis": self.last_state.valid_until_basis,
            "observed_from": min(observed) if observed else None,
            "observed_until": max(last_observed) if last_observed else None,
            "known_at": self.first_state.knowledge_from,
            "promulgated_in": self.first_state.amendment.get("reference"),
            "commencement_basis":
                self.first_state.amendment.get("commencement_basis"),
            "date_axes_note": (
                "valid_* is legal validity; observed_* is when the official "
                "source served this text; they are never substituted."),
        }


@dataclass
class ActChain:
    act: dict[str, Any]
    prefix: str
    states: list[StateVersion]
    norms: dict[str, list[tuple[int, dict[str, Any]]]]
    events: list[dict[str, Any]]
    history_start: str | None
    bavarian: bool
    warnings: list[str] = field(default_factory=list)
    retrospective_status: str = "ok"
    snapshot: dict[str, Any] = field(default_factory=dict)
    eu: bool = False   # EU/treaty text: Unterabsätze (api.law_structure)

    def versions_of(self, norm_id: str) -> list[NormVersion]:
        rows = self.norms.get(norm_id) or []
        versions: list[NormVersion] = []
        for state_index, row in rows:
            digest = norm_content_hash(row)
            state = self.states[state_index]
            if (versions and versions[-1].content_sha256 == digest
                    and versions[-1].last_state.index + 1 == state_index):
                versions[-1].last_state = state
                versions[-1].states.append(state)
                continue
            versions.append(NormVersion(
                norm_id=norm_id, label=str(row.get("enbez") or ""),
                title=str(row.get("titel") or ""),
                text=str(row.get("text") or ""), content_sha256=digest,
                first_state=state, last_state=state, states=[state],
                bavarian=self.bavarian,
                source_capture=dict(row.get("_capture") or {})))
        return versions

    def present_in_last_state(self, norm_id: str) -> bool:
        rows = self.norms.get(norm_id) or []
        return bool(rows) and rows[-1][0] == len(self.states) - 1


# ------------------------------------------------------------ act registry

class NormHistoryStore:
    """Lazy, bounded projection over one immutable data generation."""

    def __init__(self, data_dir: Path, *, toc_paths: Iterable[Path] = (),
                 manifest_loader=None, cache_size: int = 3):
        self.data_dir = Path(data_dir)
        self.toc_paths = [Path(p) for p in toc_paths]
        self._manifest_loader = manifest_loader
        self._chains: OrderedDict[tuple[str, str | None], ActChain] = \
            OrderedDict()
        self._lock = threading.RLock()
        self._cache_size = cache_size
        self._index: list[dict[str, Any]] | None = None

    # -- act index ---------------------------------------------------------
    def act_index(self) -> list[dict[str, Any]]:
        if self._index is None:
            path = self.data_dir / "wiki.json"
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise EnvelopeError(
                    "unavailable", f"act index unavailable: {exc}",
                    failed_components=[{"component": "act_index",
                                        "reason": "source_unavailable"}]
                ) from exc
            self._index = [row for row in rows if isinstance(row, dict)]
        return self._index

    def resolve(self, raw: str) -> dict[str, Any]:
        """Resolve a canonical id, alias id or citation to one norm."""
        value = (raw or "").strip()
        if not value:
            raise EnvelopeError("invalid_argument", "empty norm identifier")
        if value.startswith(("eu/", "int/")):
            try:
                juris, slug, unit, number, sub = split_norm_id(value)
            except NormIdentityError as exc:
                raise EnvelopeError("invalid_argument", str(exc)) from exc
            candidates = [row for row in self.act_index()
                          if act_slug(row) == (juris, slug)]
            if not candidates:
                raise EnvelopeError(
                    "not_in_corpus",
                    f"{juris}/{slug} is not in Lexgraph's text corpus (EU "
                    "instruments outside the curated corpus are metadata "
                    "only)",
                    searched=[{"source": "act_index", "status": "ok",
                               "count": 0}])
            return self._one(candidates, value, unit, number, sub)
        if value.startswith("de/"):
            try:
                juris, slug, unit, number, sub = split_norm_id(value)
            except NormIdentityError as exc:
                raise EnvelopeError("invalid_argument", str(exc)) from exc
            candidates = [row for row in self.act_index()
                          if act_slug(row) == (juris, slug)]
            if not candidates:
                folded = slug.replace("_", "").replace("-", "")
                candidates = [row for row in self.act_index()
                              if act_slug(row)[0] == juris and
                              act_slug(row)[1].replace("_", "").replace(
                                  "-", "") == folded]
            return self._one(candidates, value, unit, number, sub)
        citation = parse_citation(value)
        if citation is None:
            raise EnvelopeError(
                "invalid_argument",
                "use a canonical id (de/bund/<act>/para/<n>) or a citation "
                "such as '§ 37 SGB XIV'")
        # The canonical act resolver (api.norm_identity.ActResolver), shared with the
        # courts corpus: exact jurabk aliases (with and without GII's year suffix),
        # SGB Roman/Arabic books, renamed acts (AsylG -> asylvfg_1992).
        identity = self._resolver().resolve_act(citation["act"])
        if identity.basis == "ambiguous":
            candidates = [row for row in self.act_index()
                          if row.get("id") in set(identity.candidates)]
        elif identity.in_corpus:
            candidates = [row for row in self.act_index()
                          if row.get("id") == identity.act_id]
        else:
            keys = exact_act_aliases(citation["act"])
            candidates = [row for row in self.act_index()
                          if keys & exact_act_aliases(row.get("id") or "")]
        return self._one(candidates, value, citation["unit"],
                         citation["number"], citation["sub"])

    def _resolver(self) -> ActResolver:
        index = self.act_index()
        if getattr(self, "_act_resolver_source", None) is not index:
            self._act_resolver = ActResolver(index)
            self._act_resolver_source = index
        return self._act_resolver

    def resolve_act_norm(self, act_id: str, designator: str
                         ) -> dict[str, Any]:
        """Lexgraph act id + source designator (``§ 29a``) → identity."""
        rows = [row for row in self.act_index() if row.get("id") == act_id]
        unit, number = designator_unit(designator)
        return self._one(rows, f"{act_id} {designator}", unit, number, "")

    def _one(self, candidates: list[dict[str, Any]], raw: str, unit: str,
             number: str, sub: str) -> dict[str, Any]:
        if not candidates:
            raise EnvelopeError(
                "not_in_corpus", f"no act in the Lexgraph corpus for {raw!r}",
                searched=[{"source": "act_index", "status": "ok",
                           "count": 0}])
        if len(candidates) > 1:
            raise EnvelopeError(
                "ambiguous_identity", f"{raw!r} matches several acts",
                candidates=[{"act_id": row.get("id"),
                             "jurabk": row.get("jurabk"),
                             "prefix": "/".join(act_slug(row))}
                            for row in candidates])
        act = candidates[0]
        juris, slug = act_slug(act)
        norm_id = f"{juris}/{slug}/{unit}/{number}"
        return {"act_id": act.get("id"), "act": act, "norm_id": norm_id,
                "sub": sub, "requested": raw}

    # -- chains ------------------------------------------------------------
    def chain(self, act_id: str, *, as_of: str | None = None) -> ActChain:
        key = (act_id, as_of)
        with self._lock:
            if key in self._chains:
                self._chains.move_to_end(key)
                return self._chains[key]
        act = self._load_act(act_id)
        if act_id.startswith(("eu_", "int_")):
            raise EnvelopeError(
                "unavailable",
                f"{act_id}: EU and treaty texts are served from the law "
                "history store, which this data generation lacks",
                failed_components=[{"component": "law_history",
                                    "reason": "source_unavailable"}])
        if act_id.startswith("by_"):
            chain = self._bavarian_chain(act)
        else:
            chain = self._federal_chain(act, as_of=as_of)
        with self._lock:
            self._chains[key] = chain
            while len(self._chains) > self._cache_size:
                self._chains.popitem(last=False)
        return chain

    def _load_act(self, act_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z0-9_]+", act_id or ""):
            raise EnvelopeError("invalid_argument", "invalid act id")
        path = self.data_dir / "acts" / f"{act_id}.json"
        if not path.is_file():
            raise EnvelopeError("not_in_corpus", f"unknown act {act_id}")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise EnvelopeError(
                "integrity_check_failed", f"act file unreadable: {exc}",
                failed_components=[{"component": f"acts/{act_id}.json",
                                    "reason": "integrity_check_failed"}]
            ) from exc

    def _read_state(self, digest: str, act: dict[str, Any]) -> dict[str, Any]:
        """Verified, uncached read of one content-addressed state."""
        if not _DIGEST.fullmatch(digest):
            raise EnvelopeError("integrity_check_failed",
                                "invalid state digest")
        path = (self.data_dir / "federal_states" / "objects" / "sha256"
                / digest[:2] / f"{digest}.json.gz")
        try:
            with gzip.open(path, "rb") as handle:
                payload = handle.read()
        except (OSError, EOFError) as exc:
            raise EnvelopeError(
                "integrity_check_failed",
                f"official state object {digest[:12]} is unreadable",
                failed_components=[{"component": "federal_states",
                                    "reason": "integrity_check_failed",
                                    "state": digest}]) from exc
        if hashlib.sha256(payload).hexdigest() != digest:
            raise EnvelopeError(
                "integrity_check_failed",
                f"official state object {digest[:12]} failed its SHA-256 "
                "check", failed_components=[{
                    "component": "federal_states",
                    "reason": "integrity_check_failed", "state": digest}])
        state = json.loads(payload)
        if str(state.get("id") or state.get("act_id") or "") != act.get("id"):
            raise EnvelopeError(
                "integrity_check_failed",
                f"state {digest[:12]} belongs to another act")
        return state

    def _federal_chain(self, act: dict[str, Any], *, as_of: str | None
                       ) -> ActChain:
        act_id = str(act["id"])
        warnings: list[str] = []
        retrospective_status = "ok"
        intervals: list[dict[str, Any]] = []
        events: list[dict[str, Any]] = []
        history_start = None
        manifest = None
        if self._manifest_loader is not None:
            try:
                manifest = self._manifest_loader()
            except EnvelopeError:
                raise
            except Exception as exc:  # typed below, never silently empty
                raise EnvelopeError(
                    "integrity_check_failed",
                    f"retrospective history failed validation: {exc}",
                    failed_components=[{"component": "retrospective_history",
                                        "reason": "integrity_check_failed"}]
                ) from exc
        if manifest is None:
            retrospective_status = "unavailable"
            warnings.append("retrospective history is not built: no legal "
                            "validity intervals or amendment events")
        else:
            try:
                history = act_history(manifest, act_id, as_of=as_of)
                intervals = list(history.get("intervals") or [])
                events = list(history.get("events") or [])
                history_start = history.get("history_start")
            except RetrospectiveNotFound as exc:
                if as_of is not None and "horizon" in str(exc):
                    raise EnvelopeError("temporally_unresolved",
                                        str(exc)) from exc
                retrospective_status = "not_in_corpus"
                warnings.append(f"no retrospective record for {act_id}")
            except RetrospectiveIntegrityError as exc:
                raise EnvelopeError(
                    "integrity_check_failed",
                    f"retrospective history integrity failure: {exc}",
                    failed_components=[{"component": "retrospective_history",
                                        "reason": "integrity_check_failed"}]
                ) from exc

        observations: dict[str, dict[str, Any]] = {}
        cutoff = as_of[:10] if as_of else None
        for row in act.get("official_states") or []:
            digest = str(row.get("state_sha256") or "")
            observed = str(row.get("observed_at") or "")[:10]
            if not _DIGEST.fullmatch(digest) or not observed:
                continue
            if cutoff and observed > cutoff:
                continue
            entry = observations.setdefault(digest, {
                "first": observed, "last": observed,
                "builddate": row.get("builddate"),
                "source_url": row.get("source_url")})
            entry["first"] = min(entry["first"], observed)
            entry["last"] = max(entry["last"], observed)
        by_state: dict[str, list[dict[str, Any]]] = {}
        for row in intervals:
            by_state.setdefault(str(row.get("state_sha256")), []).append(row)

        digests = set(observations) | set(by_state)
        states: list[StateVersion] = []
        for digest in digests:
            obs = observations.get(digest) or {}
            rows = sorted(by_state.get(digest) or [],
                          key=lambda r: str(r.get("effective_from") or ""))
            version = StateVersion(
                act_id=act_id, key=digest,
                first_observed_at=obs.get("first"),
                last_observed_at=obs.get("last"),
                builddate=obs.get("builddate"),
                source_url=obs.get("source_url") or act.get("source_url"))
            if rows:
                first = rows[0]
                version.valid_from = first.get("effective_from")
                version.valid_from_status = str(
                    first.get("date_status") or "official_verified")
                version.valid_from_basis = first.get("date_basis")
                version.valid_until = rows[-1].get("effective_to")
                if version.valid_until:
                    version.valid_until_status = version.valid_from_status
                    version.valid_until_basis = "retrospective_interval"
                version.text_status = str(first.get("text_status")
                                          or "official_exact")
                version.published_at = first.get("published_at")
                version.interval_id = first.get("id")
                version.knowledge_from = first.get("knowledge_from")
                version.gaps = list(first.get("gaps") or [])
                version.amendment = _interval_amendment(first)
                if version.text_status == "derived_verified":
                    version.derived = True
                    version.source_role = "verified_reconstruction"
            states.append(version)
        if not states:
            raise EnvelopeError(
                "not_in_corpus",
                f"no complete official state of {act_id} is in the store")
        # Chain order: verified legal starts where known, else first
        # observation; monotone so a retroactive start never reorders the
        # sequence in which GII actually published the states.
        states.sort(key=lambda s: (
            s.first_observed_at or s.valid_from or "9999",
            s.valid_from or ""))
        # Derived states have no observation; place them by legal start.
        states.sort(key=lambda s: (s.valid_from or s.first_observed_at
                                   or "9999") if s.derived else
                    (s.first_observed_at or "9999"))
        for index, state in enumerate(states):
            state.index = index
        titles = {str(event.get("document_id")): event.get("procedure_title")
                  for event in events if event.get("procedure_title")}
        for state in states:
            document = state.amendment.get("document_id")
            if document and titles.get(str(document)):
                state.amendment["act"] = titles[str(document)]
        norms: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        # One state holds every norm; consecutive states rarely differ.  Reuse
        # the previous row object for unchanged norms so a chain costs about
        # one state plus its changes (the API runs under a 300 MB limit).
        latest: dict[str, tuple[str, dict[str, Any]]] = {}
        for state in states:
            payload = self._read_state(state.key, act)
            state.stand = payload.get("stand")
            if not state.builddate:
                state.builddate = payload.get("build")
            if not state.amendment:
                stand = parse_stand(state.stand)
                if stand.get("last_amended_by"):
                    state.amendment = {
                        "act": stand["last_amended_by"],
                        "reference": stand["last_amended_by"],
                        "basis": "gii_stand_note",
                        "commencement_basis": None,
                        "legal_effect_verified": False,
                        "note": ("GII's own 'zuletzt geändert durch' note "
                                 "for this consolidation; not a verified "
                                 "commencement date"),
                    }
                if stand.get("pending_note"):
                    state.amendment["pending_note"] = stand["pending_note"]
            for norm_id, row in norm_rows_with_ids(act, payload.get("norms")
                                                   or []):
                compact = {"enbez": row.get("enbez"),
                           "titel": row.get("titel"), "text": row.get("text")}
                digest = norm_content_hash(compact)
                previous = latest.get(norm_id)
                if previous is not None and previous[0] == digest:
                    compact = previous[1]
                else:
                    latest[norm_id] = (digest, compact)
                norms.setdefault(norm_id, []).append((state.index, compact))
            del payload
        for previous, following in zip(states, states[1:]):
            if previous.valid_until is None and following.valid_from:
                previous.valid_until = following.valid_from
                previous.valid_until_status = following.valid_from_status
                previous.valid_until_basis = (
                    "verified start of the following consolidated version")
        juris, slug = act_slug(act)
        return ActChain(
            act=_act_summary(act), prefix=f"{juris}/{slug}", states=states,
            norms=norms, events=events,
            history_start=history_start, bavarian=False, warnings=warnings,
            retrospective_status=retrospective_status)

    def _bavarian_chain(self, act: dict[str, Any]) -> ActChain:
        """Per-norm chains from archived official pages, newest first."""
        act_id = str(act["id"])
        head_state = StateVersion(
            act_id=act_id, key=f"{act_id}:head",
            first_observed_at=_bavarian_date(act.get("build")),
            last_observed_at=_bavarian_date(act.get("build")),
            builddate=str(act.get("build") or ""),
            source_role="official_consolidation_bayern_recht",
            source_url=act.get("permalink"))
        changes_by_norm: dict[str, list[dict[str, Any]]] = {}
        for version in act.get("versions") or []:
            for change in version.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                changes_by_norm.setdefault(str(change.get("para") or ""),
                                           []).append({
                    **change, "_version_text": version.get("text"),
                    "_version_date": version.get("date")})
        states: dict[str, StateVersion] = {head_state.key: head_state}
        norm_rows: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        warnings: list[str] = []
        for norm_id, row in norm_rows_with_ids(act, act.get("norms") or []):
            label = str(row.get("enbez") or "")
            chain_rows: list[tuple[str, dict[str, Any]]] = []
            current_text = str(row.get("text") or "")
            current_key = head_state.key
            changes = sorted(changes_by_norm.get(label) or [],
                             key=lambda c: str(c.get("effective_date") or ""),
                             reverse=True)
            head_row = {"enbez": label, "titel": row.get("titel"),
                        "text": current_text}
            pending = [(current_key, head_row)]
            for change in changes:
                new = str(change.get("new") or "")
                if _loose(new) != _loose(pending[-1][1]["text"]):
                    warnings.append(
                        f"{label}: archived chain breaks before "
                        f"{change.get('effective_date')} (text mismatch)")
                    break
                start = str(change.get("effective_date") or "") or None
                key = f"{act_id}:{start}:{change.get('transition_id') or change.get('source')}"
                state = states.get(key)
                if state is None:
                    state = StateVersion(
                        act_id=act_id, key=key,
                        source_role=_bavarian_role(change),
                        source_url=act.get("permalink"),
                        valid_from=start,
                        valid_from_status=("official_archived_version_date"
                                           if start else "unresolved"),
                        valid_from_basis=("BAYERN.RECHT 'gültig ab' of the "
                                          "archived official page"
                                          if change.get("source") == "wayback"
                                          else "first daily snapshot showing "
                                               "the change"),
                        first_observed_at=_capture_date(
                            change.get("new_capture")),
                        amendment=_bavarian_amendment(change))
                    states[key] = state
                # The newer text starts at this change.
                pending[-1] = (key, {**pending[-1][1], "_capture": {
                    "source": change.get("source"),
                    "capture": change.get("new_capture"),
                    "transition_id": change.get("transition_id")}})
                old = str(change.get("old") or "")
                if not old.strip():
                    break  # the norm did not exist before this change
                old_valid = change.get("old_valid")
                old_key = f"{act_id}:{old_valid or 'unknown'}:before:{change.get('transition_id') or start}"
                if old_key not in states:
                    states[old_key] = StateVersion(
                        act_id=act_id, key=old_key,
                        source_role=_bavarian_role(change),
                        source_url=act.get("permalink"),
                        valid_from=old_valid or None,
                        valid_from_status=("official_archived_version_date"
                                           if old_valid else "unresolved"),
                        valid_from_basis=("BAYERN.RECHT 'gültig ab' of the "
                                          "archived official page"
                                          if old_valid else None),
                        valid_until=start,
                        valid_until_status="official_archived_version_date",
                        valid_until_basis="start of the following archived "
                                          "version",
                        first_observed_at=_capture_date(
                            change.get("old_capture")))
                pending.append((old_key, {
                    "enbez": label, "titel": row.get("titel"), "text": old,
                    "_capture": {"source": change.get("source"),
                                 "capture": change.get("old_capture"),
                                 "transition_id": change.get(
                                     "transition_id")}}))
            chain_rows.extend(reversed(pending))
            norm_rows[norm_id] = chain_rows
        # Chronological: a version whose start is unknown sorts just before
        # the version that superseded it (its only known date is that end).
        ordered = sorted(states.values(), key=lambda s: (
            s.key == head_state.key,
            s.valid_from or s.valid_until or s.first_observed_at or "0000",
            0 if not s.valid_from else 1, s.first_observed_at or "", s.key))
        for index, state in enumerate(ordered):
            state.index = index
        index_of = {state.key: state.index for state in ordered}
        norms = {norm_id: [(index_of[key], row) for key, row in rows]
                 for norm_id, rows in norm_rows.items()}
        # The head starts where the newest archived change of each norm
        # starts; the consolidated head itself has no single legal date.
        juris, slug = act_slug(act)
        return ActChain(
            act=_act_summary(act), prefix=f"{juris}/{slug}", states=ordered,
            norms=norms, events=[], history_start=None, bavarian=True,
            warnings=sorted(set(warnings)), retrospective_status="ok")


def _act_summary(act: dict[str, Any]) -> dict[str, Any]:
    return {key: act.get(key) for key in (
        "id", "jurabk", "juris", "title", "source_url", "permalink",
        "build", "stand")}


def _interval_amendment(interval: dict[str, Any]) -> dict[str, Any]:
    review = ((interval.get("provenance") or {}).get("incoming_legal_review")
              or {})
    bgbl = review.get("bgbl") or {}
    document_id = bgbl.get("document_id")
    evidence = interval.get("evidence") or []
    if not document_id:
        document_id = next((row.get("document") for row in evidence
                            if row.get("source") == "BGBl"), None)
    procedure = review.get("procedure_id") or next(
        (row.get("procedure") for row in evidence
         if row.get("source") == "DIP"), None)
    return {
        "act": review.get("procedure_title") or bgbl_reference(document_id),
        "document_id": document_id,
        "reference": bgbl_reference(document_id),
        "eli": bgbl.get("eli"),
        "url": bgbl.get("pdf_url") or next(
            (row.get("url") for row in evidence
             if row.get("source") == "BGBl"), None),
        "pdf_sha256": bgbl.get("pdf_sha256"),
        "published_at": interval.get("published_at") or review.get(
            "published_at"),
        "effective_at": interval.get("effective_from"),
        "procedure_id": procedure,
        "commencement_basis": interval.get("date_basis")
        or review.get("date_basis"),
        "review_id": review.get("review_id"),
        "basis": "retrospective_interval",
        "legal_effect_verified": interval.get("date_status")
        == "official_verified",
    }


def _bavarian_role(change: dict[str, Any]) -> str:
    if change.get("source") == "wayback":
        return "archived_official_page_bayern_recht"
    if change.get("source") == "daily_snapshot":
        return "official_daily_snapshot_bayern_recht"
    return "official_consolidation_bayern_recht"


def _bavarian_amendment(change: dict[str, Any]) -> dict[str, Any]:
    text = str(change.get("_version_text") or "")
    reference = None
    if match := re.search(r"\(([^()]*\bv\.\s*\d{1,2}\.\d{1,2}\.\d{4}[^()]*)\)",
                          text):
        reference = match.group(1)
        if re.search(r"S\.\s*\d+", reference):
            reference = re.sub(r",\s*S\.\s*(\d+)", r" (GVBl. S. \1)",
                               reference)
    return {
        "act": reference or text or None,
        "reference": reference,
        "event": text or None,
        "event_date": change.get("_version_date"),
        "effective_at": change.get("effective_date"),
        "commencement_basis": ("BAYERN.RECHT version table (gültig ab)"
                               if change.get("source") == "wayback" else
                               "daily snapshot difference"),
        "basis": "bayern_recht_version_table",
        "legal_effect_verified": False,
        "confidence": change.get("confidence"),
    }


def _loose(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _bavarian_date(value: Any) -> str | None:
    raw = str(value or "")
    if match := re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", raw):
        return f"{match.group(3)}-{match.group(2)}-{match.group(1)}"
    if match := re.fullmatch(r"(\d{4})(\d{2})(\d{2})", raw):
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


def _capture_date(value: Any) -> str | None:
    raw = str(value or "")
    if match := re.fullmatch(r"(\d{4})(\d{2})(\d{2})\d*", raw):
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    return None


# --------------------------------------------------------- norm operations

def event_targets_norm(event: dict[str, Any], unit: str, number: str) -> bool:
    for label in event.get("affected_norms") or []:
        if designator_unit(label) == (unit, number):
            return True
    for command in event.get("commands") or []:
        ref = (command or {}).get("ref") or {}
        if unit == "para" and str(ref.get("para") or "").casefold() == number:
            return True
        if unit == "art" and str(ref.get("article") or "").casefold() == number:
            return True
    return False


def amendment_events(chain: ActChain, unit: str, number: str
                     ) -> list[dict[str, Any]]:
    """Event-only BGBl amendments that target this norm (no text claims)."""
    from api.page_furniture import strip_page_furniture
    rows = []
    for event in chain.events:
        if not event_targets_norm(event, unit, number):
            continue
        commands = []
        for command in event.get("commands") or []:
            ref = (command or {}).get("ref") or {}
            if unit == "para" and str(ref.get("para") or "").casefold() \
                    not in {"", number}:
                continue
            raw = str(command.get("raw") or "")
            clean, removed = strip_page_furniture(raw)
            commands.append({
                "operation": command.get("operation"),
                "ref": ref,
                "raw": raw,
                "raw_sha256": command.get("raw_sha256"),
                "text": " ".join(clean.split()),
                "page_furniture_removed": [
                    {key: item[key] for key in ("kind", "start", "end",
                                                "text")}
                    for item in removed],
                "new_text": command.get("new_text"),
                "old_text_constraint": command.get("old_text_constraint"),
            })
        rows.append({
            "event_id": event.get("event_id") or event.get("id"),
            "document_id": event.get("document_id"),
            "reference": bgbl_reference(event.get("document_id")),
            "amending_article": event.get("amending_article"),
            "procedure_title": event.get("procedure_title"),
            "procedure_id": event.get("procedure_id"),
            "published_at": event.get("published_at"),
            "effective_at": event.get("effective_at"),
            "date_status": event.get("date_status"),
            "date_basis": event.get("date_basis"),
            "text_status": event.get("text_status") or "event_only",
            "url": event.get("official_html_url")
            or event.get("official_pdf_url"),
            "verification": event.get("verification"),
            "legal_effect_verified": False,
            "commands": commands,
        })
    rows.sort(key=lambda row: (str(row.get("effective_at")
                                   or row.get("published_at") or ""),
                               str(row.get("document_id") or "")),
              reverse=True)
    return rows


def version_evidence(chain: ActChain, version: NormVersion,
                     toc: dict[str, Any] | None, *,
                     include_text: bool = True,
                     include_structure: bool = False) -> dict[str, Any]:
    unit, number = version.norm_id.split("/")[-2:]
    number = number.split("~")[0]
    slug = chain.prefix.split("/")[-1]
    first = version.first_state
    if chain.eu:
        link = {"url": first.source_url, "act_url": chain.act.get("source_url"),
                "status": "publication_item",
                "basis": "CELLAR item of the edition that carries this "
                         "wording; EUR-Lex per-article URLs are not "
                         "constructed from numbers"}
        role = first.source_role
    elif chain.bavarian:
        link = {"url": None, "act_url": chain.act.get("permalink"),
                "status": "not_resolved",
                "basis": "per-norm BAYERN.RECHT URLs are not constructed "
                         "from numbers"}
        role = first.source_role
    else:
        link = gii_norm_url(toc, slug, unit, number)
        role = ("verified_reconstruction" if first.derived else
                "official_consolidation_gii")
    source_exact = not any(state.derived for state in version.states)
    item = {
        "id": version.version_id,
        "canonical_id": version.norm_id,
        "version_id": version.version_id,
        "pinpoint": {"label": version.label, "title": version.title,
                     "unit": unit, "number": number},
        "content_hash": {"algorithm": "sha256", "value":
                         version.content_sha256,
                         "text_sha256": _sha256(version.text)},
        "source": {
            "url": link.get("url") or link.get("act_url"),
            "norm_url": link.get("url"),
            "norm_url_status": link.get("status"),
            "norm_url_basis": link.get("basis"),
            "act_url": link.get("act_url"),
            "role": role,
            "observed_at": first.first_observed_at,
            "state_sha256": [s.key for s in version.states
                             if _DIGEST.fullmatch(s.key)],
            "capture": version.source_capture or None,
        },
        "quality": {
            "source_exact": source_exact,
            "complete": True,
            "reconstruction_status": ("verified_reconstruction"
                                      if not source_exact else
                                      "not_reconstructed"),
            "text_history_complete": False if chain.bavarian else None,
        },
        "temporal": version.temporal(),
        "amendment": first.amendment or None,
        "relations": [],
    }
    if include_text:
        item["text"] = version.text
    if include_structure:
        item["structure"] = parse_norm_structure(version.text,
                                                 bavarian=chain.bavarian,
                                                 annex=version.annex,
                                                 eu=chain.eu)
    return item


def select_version(versions: list[NormVersion], on: str,
                   chain: ActChain) -> tuple[str, NormVersion | None,
                                             dict[str, Any]]:
    """Pick the version valid on ``on``; never infer a legal interval.

    Returns ``(status, version, resolution)``.
    """
    verified = [v for v in versions
                if v.first_state.valid_from and
                v.first_state.valid_from_status not in {"unresolved"}]
    for version in versions:
        # The norm text is established on ``on`` when any consolidated
        # version carrying it has a verified interval containing ``on`` —
        # even if this wording itself is older than the verified start.
        for state in version.states:
            start, end = state.valid_from, state.valid_until
            if not start or state.valid_from_status == "unresolved":
                continue
            if end is None and state.superseded_by_observation and \
                    on >= state.superseded_by_observation:
                continue   # a later wording was already in the source
            if start <= on and (end is None or on < end):
                status = state.valid_from_status
                attribution = (state.amendment or {}).get("attribution") or {}
                # a start attributed only by the capture bracket (no quoted
                # wording confirmed) is evidence of the date, not of the text
                weak = attribution.get("verification") in {
                    "bracketed_by_captures", "replay_unverified"}
                resolution = {
                    "official_verified": "verified_interval",
                    "official_archived_version_date":
                        "archived_official_interval",
                    "official_eu_consolidation": "official_eu_interval",
                    "official_archived_in_force_at_latest":
                        "archived_in_force_at_latest",
                }.get(status, "commencement_attributed_interval")
                if status == "official_archived_in_force_at_latest":
                    weak = False
                    note = (f"in force at the latest from {start} (the archived "
                            "official page's version date); the wording may be "
                            "older")
                extra = {"attribution": attribution} if attribution else {}
                if status == "official_archived_in_force_at_latest":
                    extra["note"] = note
                if weak:
                    extra["note"] = (
                        "future wording reconstructed from the promulgated "
                        "amendment; no official consolidation shows it yet"
                        if attribution.get("verification") ==
                        "replay_unverified" else
                        "the legal start comes from the in-force date of the "
                        "only amendment between two captures; its wording was "
                        "not confirmed in the text")
                return ("temporally_unresolved" if weak else "ok"), version, {
                    "resolution": resolution, **extra,
                    "valid_from_status": status,
                    "consolidated_version_valid_from": start,
                    "consolidated_version_valid_until_exclusive": end,
                    "valid_from": version.first_state.valid_from,
                    "valid_until_exclusive": version.last_state.valid_until}
    # An EU act (or article) repealed before ``on``: the last wording with
    # its evidenced end, never as the text in force.
    last = versions[-1] if versions else None
    if chain.eu and last is not None and last.last_state.valid_until and \
            last.last_state.valid_until_status != "unresolved" and \
            on >= last.last_state.valid_until:
        return "temporally_unresolved", last, {
            "resolution": "after_verified_end",
            "valid_until_exclusive": last.last_state.valid_until,
            "valid_until_basis": last.last_state.valid_until_basis,
            "note": f"not in force on {on}: validity ended before "
                    f"{last.last_state.valid_until} "
                    f"({last.last_state.valid_until_basis}); the last "
                    "wording is returned for transitional cases only"}
    # Bounded by a verified end only: "valid until D" with unknown start.
    for version in versions:
        end = version.last_state.valid_until
        if not version.first_state.valid_from and end and on < end and (
                version.first_state.first_observed_at or "9999") <= on:
            return "temporally_unresolved", version, {
                "resolution": "observed_before_verified_end",
                "observed_from": version.first_state.first_observed_at,
                "valid_until_exclusive": end,
                "note": "the official source served this text on or before "
                        "the requested date and a verified later version "
                        "starts after it; its own legal start is unresolved"}
    observed = [v for v in versions
                if any(s.first_observed_at and s.first_observed_at <= on <=
                       (s.last_observed_at or s.first_observed_at)
                       for s in v.states)]
    if observed:
        return "temporally_unresolved", observed[-1], {
            "resolution": "retrieval_observation_only",
            "note": "the official source served this text as the current "
                    "consolidation on the requested date; its legal "
                    "validity interval is not verified"}
    # Between two observations of the same wording, or after the newest
    # observation: the text is known, its validity on ``on`` is not.
    for version in versions:
        firsts = [s.first_observed_at for s in version.states
                  if s.first_observed_at]
        lasts = [s.last_observed_at or s.first_observed_at
                 for s in version.states if s.first_observed_at]
        if firsts and min(firsts) <= on <= max(lasts):
            return "temporally_unresolved", version, {
                "resolution": "bracketed_by_observations",
                "observed_from": min(firsts), "observed_until": max(lasts),
                "note": "the official source served this wording before and "
                        "after the requested date; its legal validity on "
                        "that date is not verified"}
    latest_state = chain.states[-1] if chain.states else None
    last_seen = max((s.last_observed_at or s.first_observed_at or ""
                     for s in chain.states), default="")
    if versions and latest_state is not None and last_seen and \
            on > last_seen and versions[-1].last_state is latest_state:
        return "temporally_unresolved", versions[-1], {
            "resolution": "after_last_observation",
            "last_observed_at": last_seen,
            "note": f"latest wording Lexgraph observed (last on {last_seen}); "
                    "later changes may not be recorded yet"}
    earliest = min((s.first_observed_at or s.valid_from or "9999"
                    for v in versions for s in v.states), default=None)
    if earliest and on < earliest and not any(
            v.first_state.valid_from and v.first_state.valid_from <= on
            for v in verified):
        return "not_in_corpus", None, {
            "resolution": "before_text_history",
            "text_history_start": earliest}
    return "temporally_unresolved", None, {
        "resolution": "between_observations",
        "candidates": [v.version_id for v in versions]}


def sentence_diff(old_text: str, new_text: str, *, bavarian: bool,
                  annex: bool = False, eu: bool = False
                  ) -> list[dict[str, Any]]:
    """Sentence-level diff with structure-aware locators on both sides."""
    left = (sentence_units(old_text, bavarian=bavarian, annex=annex, eu=eu)
            if old_text else [])
    right = (sentence_units(new_text, bavarian=bavarian, annex=annex, eu=eu)
             if new_text else [])
    matcher = difflib.SequenceMatcher(
        a=[row["text"] for row in left], b=[row["text"] for row in right],
        autojunk=False)
    ops: list[dict[str, Any]] = []

    def loc(row: dict[str, Any]) -> dict[str, Any]:
        return {"locator": row["locator"],
                "label": human_locator(row["locator"]),
                "items": row["items"]}

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for a, b in zip(left[i1:i2], right[j1:j2]):
                entry = {"op": "equal", "old": loc(a), "new": loc(b),
                         "text": b["text"]}
                if a["locator"] != b["locator"]:
                    entry["op"] = "moved"
                    entry["note"] = "unchanged wording at a new locator"
                ops.append(entry)
            continue
        olds, news = left[i1:i2], right[j1:j2]
        pairs = min(len(olds), len(news)) if tag == "replace" else 0
        for a, b in zip(olds[:pairs], news[:pairs]):
            ops.append({"op": "replace", "old": loc(a), "new": loc(b),
                        "old_text": a["text"], "new_text": b["text"],
                        "words": word_diff(a["text"], b["text"])})
        for a in olds[pairs:]:
            ops.append({"op": "delete", "old": loc(a), "new": None,
                        "old_text": a["text"]})
        for b in news[pairs:]:
            ops.append({"op": "insert", "old": None, "new": loc(b),
                        "new_text": b["text"]})
    return ops


def word_diff(old: str, new: str) -> list[dict[str, str]]:
    a, b = old.split(), new.split()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
            a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            out.append({"op": "equal", "text": " ".join(a[i1:i2])})
        if tag in {"delete", "replace"}:
            out.append({"op": "delete", "text": " ".join(a[i1:i2])})
        if tag in {"insert", "replace"}:
            out.append({"op": "insert", "text": " ".join(b[j1:j2])})
    return out


def blame(versions: list[NormVersion], target: NormVersion,
          events: list[dict[str, Any]], *, bavarian: bool, eu: bool = False
          ) -> list[dict[str, Any]]:
    """Attribute each sentence of ``target`` to the version introducing it.

    A sentence present since the earliest stored text is *not* attributed to
    that version: its origin predates Lexgraph's text history.  Only then are
    amendment commands whose quoted new wording contains the sentence offered
    as parser-derived candidates.
    """
    history = versions[:versions.index(target) + 1]
    units_by_version = [
        {row["text"] for row in sentence_units(v.text, bavarian=bavarian,
                                               annex=target.annex, eu=eu)}
        for v in history]
    rows = []
    for unit in sentence_units(target.text, bavarian=bavarian,
                               annex=target.annex, eu=eu):
        text = unit["text"]
        index = len(history) - 1
        while index > 0 and text in units_by_version[index - 1]:
            index -= 1
        # Only consecutive presence counts; a gap reintroduces the sentence.
        introduced = history[index]
        row = {
            "locator": unit["locator"],
            "label": human_locator(unit["locator"]),
            "text": text,
            "sentence_sha256": unit["sha256"],
        }
        if index == 0:
            row.update({
                "status": "predates_text_history",
                "introduced_by": None,
                "present_since": {
                    "version_id": introduced.version_id,
                    "observed_from": introduced.first_state.first_observed_at,
                    "valid_from": introduced.first_state.valid_from,
                },
                "candidates": _command_candidates(text, events),
            })
        else:
            row.update({
                "status": "introduced_within_history",
                "introduced_by": {
                    "version_id": introduced.version_id,
                    "valid_from": introduced.first_state.valid_from,
                    "valid_from_status":
                        introduced.first_state.valid_from_status,
                    "observed_from": introduced.first_state.first_observed_at,
                    "amendment": introduced.first_state.amendment or None,
                    "previous_version_id": history[index - 1].version_id,
                },
                "candidates": [],
            })
        rows.append(row)
    return rows


def _command_candidates(sentence: str, events: list[dict[str, Any]]
                        ) -> list[dict[str, Any]]:
    folded = normalize_sentence(sentence).casefold()
    if len(folded) < 25:
        return []
    found = []
    for event in events:
        for command in event.get("commands") or []:
            quoted = normalize_sentence(command.get("new_text") or "")
            if len(quoted) < 25:
                continue
            quoted_fold = quoted.casefold()
            if folded in quoted_fold or (len(quoted_fold) >= 40 and
                                         quoted_fold in folded):
                found.append({
                    "document_id": event.get("document_id"),
                    "reference": event.get("reference"),
                    "published_at": event.get("published_at"),
                    "effective_at": event.get("effective_at"),
                    "date_status": event.get("date_status"),
                    "match": ("sentence_within_quoted_new_wording"
                              if folded in quoted_fold else
                              "quoted_new_wording_within_sentence"),
                    "derived_by": "parser",
                    "legal_effect_verified": False,
                    "confidence": "medium",
                })
                break
    return found[:5]


def lower_bound(values: Iterable[str | None]) -> str | None:
    return _lower_date(*values)


def today_iso() -> str:
    return date.today().isoformat()
