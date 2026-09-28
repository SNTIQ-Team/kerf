"""Lexgraph — temporal legislation and procedure API.

Serves the pre-built web data plane (`web/data/*.json`) as a small, honest
REST API. It does NOT recompute anything: `tools/build_web_data.py` is the
build step, these files ARE the data, and the endpoints below just project
them. Response shapes deliberately equal the JSON files' shapes — the web
visualizer and `docs/API.md` already rely on them.

Endpoints (see docs/API.md → "C) REST API"):

  GET /health                 liveness + data-plane check
  GET /version                dataset + built_at (from summary.json)
  GET /stats                  the summary.json counts
  GET /data-policy            public-build source exclusions and mode
  GET /feed?limit=            realtime event stream, newest first
  GET /acts                   the act index (wiki.json)
  GET /acts/{id}              one full act (acts/<id>.json); 404 if unknown
  GET /acts/{id}/archive      selectable HEAD + historical transition dates
  GET /acts/{id}/history      bitemporal legal/knowledge-time assertions
  GET /acts/{id}/diff         exact state diff at one knowledge-time slice
  GET /acts/{id}/markdown     full act or one norm as dated Markdown
  GET /changes                official amendment-command history
  GET /amending-acts/{id}     one BGBl change law across affected acts
  GET /retrospective-history.sqlite  portable retrospective database
  GET /git?lane=&limit=       Laws-as-Git event log, optionally by lane
  GET /graph                  the QFS arena export (nodes/edges/beliefs/…)
  GET /hierarchy              competence-aware legal layers (EU/Bund/Länder)
  GET /citations              current statutory citations and backlinks
  GET /eu-index               all in-force EU directives + basic regulations
  GET /procedures/watched     persistent DIP/EUR-Lex watch state + history
  GET /amendment-fates        reviewed amendment document-chain validations
  GET /federal-history        official-only verified federal state/patch events
  GET /official-states        exact GII retrieval observations + state hashes
  GET /official-transition-reviews  BGBl/DIP accepted legal transitions
  GET /verified-reconstructions  replay-proven complete derived states
  GET /search?q=              deep search + complete GII metadata discovery
  GET /decisions?q=&act=      court decisions (decisions.json), filterable
  GET /decisions/{id}         one decision; 404 if unknown
  GET /digest                 LLM digest of legislative activity; 404 if none

Small JSON metadata is loaded lazily and cached in-process; large search and
citation indexes are queried read-only in SQLite. The dataset is static per
deploy. Override the data directory with LEXGRAPH_DATA=/path/to/web/data.
"""
from __future__ import annotations

import json
import os
import re
import threading
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from api.act_archive import (
    ArchiveRequestError,
    UnknownNormError,
    build_archive_index,
    markdown_filename,
    render_markdown_snapshot,
)
from api.decision_passages import create_router as decision_passages_router, coverage_status as decision_passage_coverage
from api.evidence import (CACHE_KEY_ARGUMENTS, EnvelopeError,
                          unknown_argument_payload)
from api.law_routes import create_router as law_router
try:
    from api.materials_routes import create_router as materials_router
except ImportError:  # optional component; /capabilities still declares it
    materials_router = None
from api.gii_catalog import GiiCatalogIndex
from api.citation_store import CitationStoreError, query_citations
from api.search_engine import SearchEngine, normalize_search_text
from api.procedure_search import ProcedureSearchIndex
from api.official_state_store import OfficialStateError, load_observed_state
from api.retrospective_store import (
    RetrospectiveAmbiguity,
    RetrospectiveIntegrityError,
    RetrospectiveNotFound,
    act_history as resolve_act_history,
    amending_act as resolve_amending_act,
    diff_intervals,
    list_changes as resolve_changes,
    load_interval_state,
    resolve_interval,
    validate_manifest as validate_retrospective_manifest,
)

# Deployment override: LEXGRAPH_DATA=/path/to/web/data (default: repo layout)
DATA_DIR = Path(os.environ.get(
    "LEXGRAPH_DATA",
    Path(__file__).resolve().parent.parent / "web" / "data")).resolve()

app = FastAPI(title="Lexgraph", version="1.5")
app.include_router(decision_passages_router(lambda: DATA_DIR))
# Norm-level Git semantics (log/at/diff/blame) and role-typed legislative
# materials.  Both validate their own query strings and answer with the typed
# evidence envelope from api.evidence.
app.include_router(law_router(
    lambda: DATA_DIR, lambda: _norm_history_manifest(), lambda: corpus_snapshot()))
if materials_router is not None:
    app.include_router(materials_router(lambda: DATA_DIR))

# Routes that parse and reject their own query strings with the same typed
# envelope (they read parameters dynamically, so declared FastAPI parameters
# are not the whole contract).
_SELF_VALIDATING_PREFIXES = ("/norms/", "/materials")
# Transport-only cache keys that deployed clients append (``?t=<ms>``,
# ``/digest?d=<day>``).  They never select content; they are accepted but
# disclosed in X-Lexgraph-Ignored-Arguments instead of being silently dropped.
_CACHE_KEYS = set(CACHE_KEY_ARGUMENTS)
_ROUTE_CACHE_KEYS = {"/digest": {"d"}}


def _declared_query_names(route_path: str, method: str) -> set[str] | None:
    """Declared query parameters of the API route serving ``route_path``."""
    from starlette.routing import Match
    scope = {"type": "http", "path": route_path, "method": method,
             "root_path": ""}
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return {param.alias or param.name
                    for param in dependant.query_params}
    return None


@app.middleware("http")
async def search_contract(request: Request, call_next):
    """Strict runtime schemas: undeclared arguments are rejected, never
    silently ignored (a dropped ``at=`` would answer for another date)."""
    route_path = request.url.path
    root_path = request.scope.get("root_path", "")
    if root_path and route_path.startswith(root_path + "/"):
        route_path = route_path[len(root_path):]
    params = request.query_params
    ignored: list[str] = []
    if params and not route_path.startswith(_SELF_VALIDATING_PREFIXES):
        allowed = _declared_query_names(route_path, request.method)
        if allowed is not None:
            cache_keys = (_CACHE_KEYS | _ROUTE_CACHE_KEYS.get(route_path, set())) - allowed
            ignored = sorted(set(params) & cache_keys)
            unsupported = sorted(set(params) - allowed - cache_keys)
            repeated = sorted(k for k in params if len(params.getlist(k)) > 1)
            if unsupported or repeated:
                payload = unknown_argument_payload(
                    route=route_path, unsupported=unsupported,
                    repeated=repeated, allowed=allowed,
                    snapshot_id=corpus_snapshot())
                if route_path == "/search":
                    payload["search_scope"] = "current_norms_and_recorded_history"
                    payload["historical_alternative"] = "/norms/{id}/at?date="
                return JSONResponse(status_code=422, content=payload)
    snapshot = corpus_snapshot()
    requested = request.headers.get("if-lexgraph-snapshot")
    if requested and requested != snapshot:
        return JSONResponse(status_code=409, content={"status": "stale_snapshot", "snapshot": snapshot})
    response = await call_next(request)
    if snapshot:
        response.headers["X-Lexgraph-Snapshot"] = snapshot
    if ignored:
        response.headers["X-Lexgraph-Ignored-Arguments"] = ",".join(ignored)
    return response


def corpus_snapshot():
    # DATA_DIR is pinned to one immutable generation at process start. Publishing
    # switches the symlink and restarts this worker; in-flight reads retain theirs.
    return DATA_DIR.name if DATA_DIR.name.startswith("web-data.release-") else None


@app.get("/capabilities")
def capabilities():
    return {"schema_version": 1, "service": "lexgraph",
            "snapshot": corpus_snapshot(), "snapshot_precondition": "If-Lexgraph-Snapshot",
            "decision_passage_coverage": decision_passage_coverage(DATA_DIR),
            "search": {"norms": "current_only", "changes": "recorded_history",
                       "decisions": "metadata", "decision_passages": "retained_official_source_blocks", "decision_relations": "source_bound_citation_mentions", "component_status": True},
            "historical_text": {"operation": "acts/{id}/markdown", "parameters": ["at", "as_of"],
                                "resolution": "source_capture_or_verified_reconstruction"},
            # Current-text discovery and historical resolution are different
            # capabilities; neither silently substitutes for the other.
            "temporal": {
                "search": {"mode": "current_consolidation_only",
                           "valid_at": "unsupported", "known_at": "unsupported",
                           "on_temporal_argument": "reject_422_unsupported_temporal_request",
                           "historical_alternative": "norms/{id}/at?date="},
                "norm_history": {"operations": ["norms/{id}/log", "norms/{id}/at",
                                                "norms/{id}/diff", "norms/{id}/blame"],
                                 "valid_at": "verified_intervals_only; otherwise temporally_unresolved",
                                 "known_at": "as_of (RFC3339 with timezone)",
                                 "jurisdictions": {"de/bund": "complete GII states + verified BGBl intervals",
                                                   "de/by": "archived official BAYERN.RECHT pages, per norm, incomplete",
                                                   "eu": "not_in_corpus (metadata only)"}},
                "unknown_arguments": "rejected on every route (invalid_argument or unsupported_temporal_request)",
            },
            "materials": {"operation": "materials", "roles": ["entwurf", "begruendung", "stellungnahme_bundesrat",
                                                               "gegenaeusserung_bundesregierung", "beschlussempfehlung", "bericht"],
                          "relations": {"explains": "target norms of the fragment heading only",
                                        "mentions": "every other reference"}},
            "operations": ["search", "acts/{id}/markdown", "acts/{id}/markdown.json", "acts/{id}/history", "changes", "decisions",
                           "decision-passages", "decisions/{id}/passages/{passage_id}", "decisions/{id}/relations",
                           "norms/resolve", "norms/{id}/log", "norms/{id}/at", "norms/{id}/diff", "norms/{id}/blame",
                           "norms/{id}/materials", "materials"]}


# git.json lane index → jurisdiction (0=EU, 1=Bund, 2=Bayern, 3=Länder)
LANES = ["EU", "Bund", "Bayern", "Länder"]

_SGB_ROMAN = {
    1: "i", 2: "ii", 3: "iii", 4: "iv", 5: "v", 6: "vi", 7: "vii",
    8: "viii", 9: "ix", 10: "x", 11: "xi", 12: "xii", 13: "xiii",
    14: "xiv",
}
_SGB_ARABIC = {roman: number for number, roman in _SGB_ROMAN.items()}


def _exact_act_keys(value: object) -> set[str]:
    """Return closed exact aliases for an act id/abbreviation.

    The official GII catalogue spells the social codes as ``SGB 8`` while
    lawyers and readers normally write ``SGB VIII``.  Accept both without
    opening a fuzzy-match boundary in evidence-bearing endpoints.
    """
    key = " ".join(str(value or "").casefold().split())
    keys = {key}
    match = re.fullmatch(r"sgb\s+(\d+|[ivx]+)", key)
    if not match:
        return keys
    token = match.group(1)
    if token.isdigit():
        roman = _SGB_ROMAN.get(int(token))
        if roman:
            keys.add(f"sgb {roman}")
    else:
        number = _SGB_ARABIC.get(token)
        if number:
            keys.add(f"sgb {number}")
    return keys

# in-process cache of the (static per deploy) data plane
_CACHE: dict[str, object] = {}
_SEARCH_ENGINE: SearchEngine | None = None
_SEARCH_LOCK = threading.RLock()
_GII_CATALOG_INDEX: GiiCatalogIndex | None = None
_GII_CATALOG_LOCK = threading.RLock()
_PROCEDURE_SEARCH_INDEX: ProcedureSearchIndex | None = None
_PROCEDURE_SEARCH_LOCK = threading.RLock()
_RETROSPECTIVE_MANIFEST: dict[str, object] | None = None
_RETROSPECTIVE_SOURCE: object | None = None
_RETROSPECTIVE_LOCK = threading.RLock()


def _load(name: str) -> object:
    """Read and cache one top-level web/data JSON file (e.g. 'summary')."""
    if name not in _CACHE:
        path = DATA_DIR / f"{name}.json"
        if not path.exists():
            raise HTTPException(
                503, f"data file missing: {path.name} — run "
                     "tools/build_web_data.py")
        with path.open(encoding="utf-8") as fh:
            _CACHE[name] = json.load(fh)
    return _CACHE[name]


def _cached(payload: object) -> JSONResponse:
    """The data plane is static per deploy, so let clients cache it too."""
    return JSONResponse(payload, headers={"Cache-Control": "public, max-age=3600"})


def _retrospective_manifest(*, optional: bool = False) -> dict | None:
    """Return the validated, immutable retrospective data-plane manifest."""
    path = DATA_DIR / "retrospective_history.json"
    if not path.is_file():
        if optional:
            return None
        raise HTTPException(
            503, "retrospective history is not built — run "
                 "tools/build_web_data.py")
    raw = _load("retrospective_history")
    global _RETROSPECTIVE_MANIFEST, _RETROSPECTIVE_SOURCE
    with _RETROSPECTIVE_LOCK:
        if _RETROSPECTIVE_MANIFEST is None or _RETROSPECTIVE_SOURCE is not raw:
            try:
                _RETROSPECTIVE_MANIFEST = validate_retrospective_manifest(raw)
            except RetrospectiveIntegrityError as exc:
                raise HTTPException(
                    503, f"retrospective history integrity failure: {exc}") \
                    from exc
            _RETROSPECTIVE_SOURCE = raw
        return _RETROSPECTIVE_MANIFEST


def _norm_history_manifest() -> dict | None:
    """Validated manifest for norm history; integrity failure stays typed."""
    try:
        return _retrospective_manifest(optional=True)
    except HTTPException as exc:
        raise EnvelopeError(
            "integrity_check_failed", str(exc.detail),
            failed_components=[{"component": "retrospective_history",
                                "reason": "integrity_check_failed"}]) from exc


def _knowledge_value(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise HTTPException(422, "as_of must include a timezone")
    return value.isoformat()


def _history_envelope(manifest: dict, history: dict) -> dict:
    """Attach manifest-level semantics without duplicating the state catalog."""
    return {
        "schema_version": manifest.get("schema_version"),
        "kind": manifest.get("kind"),
        "built_at": manifest.get("built_at"),
        "state_identity": manifest.get("state_identity"),
        "date_semantics": manifest.get("date_semantics"),
        "source_policy": manifest.get("source_policy"),
        **history,
    }


def _gii_catalog_index(rows: list[dict]) -> GiiCatalogIndex:
    """Return the process-wide normalized GII discovery index."""
    global _GII_CATALOG_INDEX
    with _GII_CATALOG_LOCK:
        if (_GII_CATALOG_INDEX is None
                or _GII_CATALOG_INDEX.source_rows is not rows):
            _GII_CATALOG_INDEX = GiiCatalogIndex(rows)
        return _GII_CATALOG_INDEX


def _procedure_search_index(hierarchy: object) -> ProcedureSearchIndex:
    global _PROCEDURE_SEARCH_INDEX
    with _PROCEDURE_SEARCH_LOCK:
        if (_PROCEDURE_SEARCH_INDEX is None
                or _PROCEDURE_SEARCH_INDEX.source_hierarchy is not hierarchy):
            _PROCEDURE_SEARCH_INDEX = ProcedureSearchIndex(hierarchy)
        return _PROCEDURE_SEARCH_INDEX


def warm_search_indexes() -> None:
    """Pay immutable index setup costs during startup, not on first input."""
    try:
        payload = _load("gii_catalog")
    except HTTPException:
        return
    rows = payload.get("acts") if isinstance(payload, dict) else []
    if isinstance(rows, list):
        _gii_catalog_index(rows)
    try:
        hierarchy = _load("hierarchy")
    except HTTPException:
        return
    _procedure_search_index(hierarchy)


# Uvicorn imports this module once per worker.  Warming here is compatible
# with both old and current FastAPI versions (the latter removed the former
# ``add_event_handler`` convenience method) and keeps the first user query
# fast without adding a framework-specific lifecycle dependency.
warm_search_indexes()


# --------------------------------------------------------------- service

@app.get("/health")
def health():
    """Liveness + validated data-plane check (used by deploys/monitors).

    The atomic publisher treats this endpoint as its commit gate.  Validate
    the retrospective manifest here as well as ``summary.json`` so a release
    with internally inconsistent bitemporal assertions is rolled back before
    it becomes visible, rather than failing on the first history request.
    """
    try:
        summary = _load("summary")
    except HTTPException as exc:
        raise HTTPException(503, exc.detail)
    retrospective = _retrospective_manifest(optional=True)
    return {"status": "ok", "built_at": summary.get("built_at"),
            "data_dir": str(DATA_DIR),
            "retrospective": ({
                "status": "ok",
                "built_at": retrospective.get("built_at"),
                "counts": retrospective.get("counts", {}),
            } if retrospective is not None else {"status": "not_built"})}


@app.get("/version")
def version():
    """Build/version + built_at from summary.json."""
    summary = _load("summary")
    return {"dataset": "Lexgraph", "version": app.version,
            "built_at": summary.get("built_at"),
            "source": "https://github.com/SNTIQ-Team/lexgraph"}


@app.get("/stats")
def stats():
    """The summary.json counts (acts, patches, vorgaenge, EU, graph, …)."""
    return _load("summary")


@app.get("/data-policy")
def data_policy():
    """Machine-readable public-build/quarantine policy and exclusions."""
    return _cached(_load("data_policy"))


@app.get("/federal-history")
def federal_history(
        act: str | None = Query(None, min_length=1),
        tier: str | None = Query(
            None, pattern="^(exact|current_text_correspondence|metadata_only)$"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0)):
    """Evidence-bound federal history built only from official GII/BGBl/DIP.

    Buzer is not an input to this public file. ``exact`` means two captured
    official GII states; it does not silently infer their legal effective day.
    """
    data = _load("verified_federal_events")
    events = data.get("events") or []
    if act:
        needle = act.casefold().strip()
        events = [row for row in events
                  if str(row.get("act") or "").casefold() == needle]
    if tier:
        events = [row for row in events
                  if row.get("verification") == tier]
    return {
        "schema_version": data.get("schema_version"),
        "built_at": data.get("built_at"),
        "source_policy": data.get("source_policy"),
        "tiers": data.get("tiers"),
        "total": data.get("total"),
        "matched": len(events),
        "offset": offset,
        "limit": limit,
        "events": events[offset:offset + limit],
    }


@app.get("/official-states")
def official_states(
        act: str | None = Query(
            None, min_length=1, description="act id or exact jurabk"),
        limit: int = Query(250, ge=1, le=1000),
        offset: int = Query(0, ge=0)):
    """Immutable complete GII states indexed by exact retrieval date.

    The observations are evidence that GII served a complete parsed state on
    that day, not a claim about when its amendments entered into force.  The
    full body is retrieved through ``/acts/{id}/markdown?at=...`` so the same
    integrity-checked path serves both whole acts and individual norms.
    """
    data = _load("official_federal_states")
    rows = list(data.get("observations") or [])
    if act:
        needle = act.casefold().strip()
        rows = [row for row in rows if needle in {
            str(row.get("act_id") or "").casefold(),
            str(row.get("jurabk") or "").casefold(),
        }]
    rows.sort(key=lambda row: (
        str(row.get("observed_at") or ""),
        str(row.get("act_id") or "")), reverse=True)
    return {
        "schema_version": data.get("schema_version"),
        "state_identity": data.get("state_identity"),
        "source_policy": data.get("source_policy"),
        "total_states": data.get("total_states"),
        "total_observations": data.get("total_observations"),
        "matched": len(rows),
        "offset": offset,
        "limit": limit,
        "observations": rows[offset:offset + limit],
    }


@app.get("/verified-reconstructions")
def verified_reconstructions(
        act: str | None = Query(
            None, min_length=1, description="exact act id or abbreviation")):
    """Reviewed full derived states whose inverse/forward replay is proven.

    These rows are body-complete but ``source_exact:false``: the historical
    bytes were reconstructed from final BGBl commands and an official later
    GII anchor, then forward-replayed to that anchor byte-for-byte.
    """
    data = _load("verified_reconstructions")
    rows = list(data.get("reconstructions") or [])
    if act:
        needles = _exact_act_keys(act)
        rows = [row for row in rows if needles.intersection(
            _exact_act_keys(row.get("act_id")) |
            _exact_act_keys(row.get("jurabk")))]
    return _cached({
        **{key: value for key, value in data.items()
           if key not in {"reconstructions"}},
        "total": len(rows),
        "reconstructions": rows,
    })


@app.get("/official-transition-reviews")
def official_transition_reviews(
        act: str | None = Query(
            None, min_length=1, description="act id or exact jurabk"),
        procedure_id: str | None = Query(None, min_length=1),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0)):
    """Legal dates accepted by the final BGBl + DIP + GII state gate."""
    data = _load("official_transition_reviews")
    rows = list(data.get("reviews") or [])
    if act:
        needle = act.casefold().strip()
        rows = [row for row in rows if needle in {
            str(row.get("act_id") or "").casefold(),
            str(row.get("jurabk") or "").casefold(),
        }]
    if procedure_id:
        rows = [row for row in rows if str(
            row.get("procedure_id") or "") == procedure_id]
    rows.sort(key=lambda row: str(row.get("effective_at") or ""),
              reverse=True)
    return {
        "schema_version": data.get("schema_version"),
        "built_at": data.get("built_at"),
        "source_policy": data.get("source_policy"),
        "total": data.get("total"),
        "matched": len(rows),
        "offset": offset,
        "limit": limit,
        "reviews": rows[offset:offset + limit],
    }


@app.get("/feed")
def feed(limit: int = Query(100, ge=1, le=600)):
    """The realtime event feed (feed.json), newest first, capped at `limit`."""
    events = _load("feed")
    return {"total": len(events), "limit": limit, "events": events[:limit]}


@app.get("/acts")
def acts():
    """The act index (wiki.json): id, jurabk, juris, title, norms, changes."""
    return _cached(_load("wiki"))


@app.get("/acts/{act_id}")
def act_detail(act_id: str):
    """One full act (acts/<id>.json): temporal, patches, versions, norms, …."""
    return _cached(_load_act(act_id))


def _load_act(act_id: str) -> dict:
    """Read one act safely; act files stay uncached to keep helpers simple."""
    # guard against path traversal — ids are flat slugs like fed_asylblg
    if "/" in act_id or "\\" in act_id or act_id.startswith("."):
        raise HTTPException(404, f"unknown act '{act_id}'")
    path = DATA_DIR / "acts" / f"{act_id}.json"
    if not path.exists():
        raise HTTPException(404, f"unknown act '{act_id}'")
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _archive_head_fallback() -> object:
    """Deployment-wide source snapshot boundary from summary.json."""
    return _load("summary").get("built_at")


@app.get("/acts/{act_id}/archive")
def act_archive(
        act_id: str,
        as_of: datetime | None = Query(
            None, description="RFC3339 knowledge-time slice")):
    """Selectable state dates, norm designators, and known coverage gaps.

    HEAD is the exact consolidated snapshot.  Earlier entries distinguish
    source-exact official captures, body-complete replay-verified states, and
    genuinely partial reconstructions; those evidence classes are never
    collapsed into one generic ``historical`` label.
    """
    try:
        payload = build_archive_index(
            _load_act(act_id), fallback_head=_archive_head_fallback())
    except ArchiveRequestError as exc:
        raise HTTPException(422, str(exc)) from exc
    manifest = _retrospective_manifest(optional=True)
    knowledge = _knowledge_value(as_of)
    if manifest is None:
        payload["retrospective"] = {
            "available": False,
            "as_of": knowledge,
            "reason": "retrospective history is not built",
        }
    else:
        try:
            history = resolve_act_history(
                manifest, act_id, as_of=knowledge)
        except RetrospectiveNotFound as exc:
            if act_id in (manifest.get("acts") or {}):
                raise HTTPException(404, str(exc)) from exc
            payload["retrospective"] = {
                "available": False,
                "as_of": knowledge or manifest.get("built_at"),
                "reason": "no verified retrospective intervals for this act",
            }
        except (RetrospectiveIntegrityError, RetrospectiveAmbiguity) as exc:
            raise HTTPException(
                503, f"retrospective history integrity failure: {exc}") from exc
        else:
            payload["retrospective"] = {
                "available": True,
                "schema_version": manifest.get("schema_version"),
                "built_at": manifest.get("built_at"),
                "as_of": history.get("as_of"),
                "history_start": history.get("history_start"),
                "intervals": history.get("intervals") or [],
                "events": history.get("events") or [],
                "observations": history.get("observations") or [],
                "gaps": history.get("gaps") or [],
                "coverage": history.get("coverage") or {},
                "date_semantics": manifest.get("date_semantics") or {},
            }
    return _cached(payload)


@app.get("/acts/{act_id}/history")
def retrospective_history(
        act_id: str,
        as_of: datetime | None = Query(
            None, description="RFC3339 knowledge-time slice")):
    """Bitemporal state/event history for one act."""
    manifest = _retrospective_manifest()
    assert manifest is not None
    try:
        history = resolve_act_history(
            manifest, act_id, as_of=_knowledge_value(as_of))
    except RetrospectiveNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RetrospectiveIntegrityError, RetrospectiveAmbiguity) as exc:
        raise HTTPException(
            503, f"retrospective history integrity failure: {exc}") from exc
    return _cached(_history_envelope(manifest, history))


@app.get("/changes")
def changes(
        q: str | None = Query(
            None, min_length=1,
            description="text in titles, article headings or commands"),
        act: str | None = Query(
            None, min_length=1, description="exact act id or abbreviation"),
        norm: str | None = Query(
            None, min_length=1, description="target §/Art. designator"),
        from_date: date | None = Query(
            None, alias="from", description="first event/effective date"),
        to_date: date | None = Query(
            None, alias="to", description="last event/effective date"),
        future: bool | None = Query(
            None, description="only future (true) or non-future (false) effects"),
        as_of: datetime | None = Query(
            None, description="RFC3339 knowledge-time slice"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0)):
    """Official BGBl amendment commands across the curated federal corpus.

    Rows preserve publication and effective dates separately.  A command is
    not presented as a complete historical consolidation unless an exact
    state interval also exists in ``/acts/{id}/history``.
    """
    manifest = _retrospective_manifest()
    assert manifest is not None
    try:
        payload = resolve_changes(
            manifest, as_of=_knowledge_value(as_of), act=act, norm=norm,
            from_date=(from_date.isoformat() if from_date else None),
            to_date=(to_date.isoformat() if to_date else None), future=future,
            query=q)
    except RetrospectiveNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RetrospectiveIntegrityError, RetrospectiveAmbiguity) as exc:
        # A broken immutable generation is not the caller's fault and must
        # not look like a rejected query.
        raise HTTPException(
            503, f"retrospective history integrity failure: {exc}") from exc
    events = payload["events"]
    return _cached({
        **payload,
        "matched": len(events),
        "offset": offset,
        "limit": limit,
        "events": events[offset:offset + limit],
    })


@app.get("/amending-acts/{document_id}")
def amending_act(
        document_id: str,
        as_of: datetime | None = Query(
            None, description="RFC3339 knowledge-time slice")):
    """One final BGBl document grouped by article and affected act."""
    manifest = _retrospective_manifest()
    assert manifest is not None
    try:
        payload = resolve_amending_act(
            manifest, document_id, as_of=_knowledge_value(as_of))
    except RetrospectiveNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RetrospectiveIntegrityError, RetrospectiveAmbiguity) as exc:
        raise HTTPException(
            503, f"retrospective history integrity failure: {exc}") from exc
    return _cached(payload)


def _changes_atom(payload: dict, *, act_id: str | None = None,
                  limit: int = 100) -> Response:
    """Render a deterministic Atom 1.0 feed from visible change events."""
    namespace = "http://www.w3.org/2005/Atom"
    ET.register_namespace("", namespace)
    tag = lambda name: f"{{{namespace}}}{name}"
    feed = ET.Element(tag("feed"))
    title = "Lexgraph — amtliche Änderungen"
    if act_id:
        title += f" — {act_id}"
    canonical = (f"https://api.sntiq.com/lex/acts/{act_id}/changes.atom"
                 if act_id else "https://api.sntiq.com/lex/changes.atom")
    ET.SubElement(feed, tag("title")).text = title
    ET.SubElement(feed, tag("id")).text = canonical
    ET.SubElement(feed, tag("updated")).text = str(payload["built_at"])
    ET.SubElement(feed, tag("link"), {
        "rel": "self", "href": canonical,
        "type": "application/atom+xml",
    })
    ET.SubElement(feed, tag("link"), {
        "rel": "alternate", "href": "https://sntiq.com/dt/lexgraph",
        "type": "text/html",
    })
    author = ET.SubElement(feed, tag("author"))
    ET.SubElement(author, tag("name")).text = "SNTIQ Lexgraph"
    for row in list(payload.get("events") or [])[:limit]:
        entry = ET.SubElement(feed, tag("entry"))
        entry_id = str(row.get("id") or row.get("event_id") or "")
        ET.SubElement(entry, tag("id")).text = f"urn:lexgraph:{entry_id}"
        heading = str(row.get("article_heading") or "Änderung")
        ET.SubElement(entry, tag("title")).text = (
            f"{row.get('jurabk') or row.get('act_id')}: {heading}")
        ET.SubElement(entry, tag("updated")).text = str(
            row.get("knowledge_from") or payload["built_at"])
        ET.SubElement(entry, tag("published")).text = (
            f"{row['published_at']}T00:00:00Z")
        document_id = str(row.get("document_id") or "")
        ET.SubElement(entry, tag("link"), {
            "rel": "alternate",
            "href": f"https://api.sntiq.com/lex/amending-acts/{document_id}",
            "type": "application/json",
        })
        if row.get("official_html_url"):
            ET.SubElement(entry, tag("link"), {
                "rel": "related", "href": str(row["official_html_url"]),
                "type": "text/html",
            })
        effective = row.get("effective_at") or "Datum noch nicht aufgelöst"
        norms = ", ".join(row.get("affected_norms") or []) or "Akt-Ebene"
        ET.SubElement(entry, tag("summary"), {"type": "text"}).text = (
            f"Verkündet {row['published_at']}; Wirkung {effective}; "
            f"betroffen: {norms}; Befehle: {len(row.get('commands') or [])}.")
    xml = ET.tostring(feed, encoding="utf-8", xml_declaration=True)
    return Response(xml, media_type="application/atom+xml",
                    headers={"Cache-Control": "public, max-age=1800"})


@app.get("/changes.atom", response_class=Response)
def changes_atom(limit: int = Query(100, ge=1, le=500)):
    """Atom monitor for the newest official BGBl amendment events."""
    manifest = _retrospective_manifest()
    assert manifest is not None
    return _changes_atom(resolve_changes(manifest), limit=limit)


@app.get("/acts/{act_id}/changes.atom", response_class=Response)
def act_changes_atom(act_id: str, limit: int = Query(100, ge=1, le=500)):
    """Per-act Atom monitor; suitable for a twice-daily feed reader."""
    manifest = _retrospective_manifest()
    assert manifest is not None
    payload = resolve_changes(manifest, act=act_id)
    if not payload["events"] and act_id not in manifest.get("acts", {}):
        raise HTTPException(404, f"no retrospective history for {act_id}")
    return _changes_atom(payload, act_id=act_id, limit=limit)


@app.get("/acts/{act_id}/diff")
def retrospective_diff(
        act_id: str,
        from_date: date = Query(..., alias="from",
                                description="first legal date (YYYY-MM-DD)"),
        to_date: date = Query(..., alias="to",
                              description="second legal date (YYYY-MM-DD)"),
        as_of: datetime | None = Query(
            None, description="RFC3339 knowledge-time slice"),
        norm: str | None = Query(
            None, description="optional §/Art. designator")):
    """Diff two complete CAS states at one knowledge-time slice."""
    manifest = _retrospective_manifest()
    assert manifest is not None
    try:
        payload = diff_intervals(
            manifest, act_id, from_date.isoformat(), to_date.isoformat(),
            DATA_DIR / "federal_states",
            as_of=_knowledge_value(as_of), norm=norm)
    except RetrospectiveNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except (RetrospectiveIntegrityError, RetrospectiveAmbiguity) as exc:
        raise HTTPException(
            503, f"retrospective history integrity failure: {exc}") from exc
    return _cached(payload)


@app.get("/acts/{act_id}/markdown", response_class=Response)
def act_markdown(
        act_id: str,
        at: date | None = Query(
            None, description="legal date (YYYY-MM-DD); omit for HEAD"),
        as_of: datetime | None = Query(
            None, description="RFC3339 knowledge time; requires at"),
        norm: str | None = Query(
            None, description="§/Art. designator; omit for the entire act"),
        download: bool = Query(False, description="send as a .md attachment"),
):
    """Full act or one §/Art. as raw ``text/markdown``.

    Response headers distinguish an exact source snapshot, a complete
    replay-verified reconstruction, and a partial reconstruction. Historical
    gaps are also embedded at the top of the Markdown.
    """
    if as_of is not None and at is None:
        raise HTTPException(422, "as_of requires an explicit legal date in at")
    requested_at = at.isoformat() if at is not None else None
    knowledge = _knowledge_value(as_of)
    try:
        act = _load_act(act_id)
        retrospective_interval = None
        retrospective_state = None
        manifest = _retrospective_manifest(optional=True)
        if requested_at is not None and manifest is not None:
            try:
                retrospective_interval = resolve_interval(
                    manifest, act_id, requested_at, as_of=knowledge)
                retrospective_state = load_interval_state(
                    retrospective_interval, DATA_DIR / "federal_states")
            except RetrospectiveNotFound:
                if knowledge is not None:
                    raise
            except (RetrospectiveIntegrityError,
                    RetrospectiveAmbiguity) as exc:
                raise HTTPException(
                    503, f"retrospective history integrity failure: {exc}") \
                    from exc
        elif knowledge is not None:
            raise HTTPException(503, "retrospective history is not built")
        observed_state = (None if retrospective_state is not None else
                          load_observed_state(DATA_DIR, act, requested_at))
        result = render_markdown_snapshot(
            act, requested_at=requested_at, norm=norm,
            fallback_head=_archive_head_fallback(),
            observed_state=observed_state,
            retrospective_state=retrospective_state,
            retrospective_interval=retrospective_interval)
    except UnknownNormError as exc:
        raise HTTPException(404, str(exc)) from exc
    except RetrospectiveNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except OfficialStateError as exc:
        # Integrity failure is a broken immutable data generation, not a bad
        # user date.  Fail closed instead of falling back to a reconstruction.
        raise HTTPException(503, str(exc)) from exc
    except ArchiveRequestError as exc:
        raise HTTPException(422, str(exc)) from exc

    headers = {
        "Cache-Control": "public, max-age=3600",
        "X-Lexgraph-Requested-Date": result["requested_at"],
        "X-Lexgraph-Resolved-Date": result["resolved_at"],
        "X-Lexgraph-Head-Date": result["head_date"],
        "X-Lexgraph-Exact": str(result["exact"]).lower(),
        "X-Lexgraph-Archive-Status": result["archive_status"],
        "X-Lexgraph-Complete": str(result["complete"]).lower(),
        "X-Lexgraph-Source-Exact": str(result["source_exact"]).lower(),
        "X-Lexgraph-Verified-Reconstruction": str(
            result["verified_reconstruction"]).lower(),
        "X-Lexgraph-Missing-Transitions": str(len(result["gaps"])),
    }
    if result.get("date_basis"):
        headers["X-Lexgraph-Date-Basis"] = str(result["date_basis"])
    if result.get("state_sha256"):
        headers["X-Lexgraph-State-SHA256"] = str(result["state_sha256"])
    if result.get("source_url"):
        headers["X-Lexgraph-Source-URL"] = str(result["source_url"])
    if result.get("legal_effect_verified") is True:
        headers["X-Lexgraph-Legal-Effect-Verified"] = "true"
    if result.get("published_at"):
        headers["X-Lexgraph-Published-Date"] = str(result["published_at"])
    if result.get("effective_at"):
        headers["X-Lexgraph-Effective-Date"] = str(result["effective_at"])
    if result.get("review_id"):
        headers["X-Lexgraph-Review-ID"] = str(result["review_id"])
    if result.get("procedure_id"):
        headers["X-Lexgraph-Procedure-ID"] = str(result["procedure_id"])
    retrospective_headers = {
        "X-Lexgraph-As-Of": "as_of",
        "X-Lexgraph-Effective-From": "effective_from",
        "X-Lexgraph-Effective-To": "effective_to",
        "X-Lexgraph-Knowledge-From": "knowledge_from",
        "X-Lexgraph-Knowledge-To": "knowledge_to",
        "X-Lexgraph-Observed-Date": "observed_at",
        "X-Lexgraph-Text-Status": "text_status",
        "X-Lexgraph-Date-Status": "date_status",
        "X-Lexgraph-Verification": "verification",
        "X-Lexgraph-Verified-Through-Observed-Date":
            "verified_through_observed_at",
    }
    for header, field in retrospective_headers.items():
        if result.get(field) is not None:
            headers[header] = str(result[field])
    # Keep the raw Markdown response self-describing for browser clients.  Cap
    # this header to a compact summary; the Markdown body still lists every
    # selected-snapshot gap in full.
    header_gaps = list(result["gaps"][:8])
    if len(result["gaps"]) > len(header_gaps):
        header_gaps.append({
            "reason": "additional_gaps",
            "label": f"{len(result['gaps']) - len(header_gaps)} more gaps",
        })
    headers["X-Lexgraph-Archive-Gaps"] = json.dumps(
        header_gaps, ensure_ascii=True, separators=(",", ":"))
    if download:
        headers["Content-Disposition"] = (
            f'attachment; filename="{markdown_filename(result)}"')
    return Response(result["markdown"], media_type="text/markdown",
                    headers=headers)


def parse_front_matter(markdown: str) -> tuple[dict, str]:
    """Split Lexgraph's Markdown front matter into typed fields.

    The exporter writes one ``key: value`` per line with JSON scalars; bare
    ISO dates and booleans are accepted too.  Unknown syntax is kept as the
    raw string rather than dropped.
    """
    match = re.match(r"\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)",
                     markdown, re.S)
    if not match:
        return {}, markdown
    fields: dict = {}
    for line in match.group(1).splitlines():
        key, sep, raw = line.partition(":")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            continue
        value = raw.strip()
        try:
            fields[key.strip()] = json.loads(value)
        except ValueError:
            fields[key.strip()] = value
    return fields, markdown[match.end():]


@app.get("/acts/{act_id}/markdown.json")
def act_markdown_json(
        act_id: str,
        at: date | None = Query(
            None, description="legal date (YYYY-MM-DD); omit for HEAD"),
        as_of: datetime | None = Query(
            None, description="RFC3339 knowledge time; requires at"),
        norm: str | None = Query(
            None, description="§/Art. designator; omit for the entire act")):
    """The Markdown snapshot with its provenance as a structured object.

    Markdown is a representation, not the whole contract: an adapter that
    forwards only the body erases exactness, reconstruction and temporal
    qualifications.  This variant keeps the complete Markdown (front matter
    included) *and* normalises the front matter into typed fields, repeating
    the consequential limitations as warnings.
    """
    rendered = act_markdown(act_id=act_id, at=at, as_of=as_of, norm=norm,
                            download=False)
    markdown = rendered.body.decode("utf-8")
    front, body = parse_front_matter(markdown)
    headers = {key.lower(): value for key, value in rendered.headers.items()
               if key.lower().startswith("x-lexgraph-")}
    warnings = []
    if front.get("source_exact") is False:
        warnings.append("text is not an exact official source capture"
                        + (" (replay-verified reconstruction)"
                           if front.get("verified_reconstruction") else ""))
    if front.get("complete") is False:
        warnings.append("text is incomplete for the requested date; see gaps")
    if "retrieval" in str(front.get("date_basis") or ""):
        warnings.append("the date is a retrieval observation, not a legal "
                        "effective date")
    gaps = int(front.get("coverage_gaps") or 0)
    if gaps:
        warnings.append(f"{gaps} known coverage gap(s) in this snapshot")
    return JSONResponse({
        "schema": "lexgraph-evidence-envelope/1",
        "status": "ok" if front.get("complete", True) else "partial",
        "operation": "act_markdown",
        "request_scope": {"act_id": act_id, "at": at.isoformat() if at else None,
                          "as_of": as_of.isoformat() if as_of else None,
                          "norm": norm},
        "snapshot_id": corpus_snapshot(),
        "warnings": warnings,
        "provenance": front,
        "response_headers": headers,
        "quality": {"source_exact": front.get("source_exact"),
                    "complete": front.get("complete"),
                    "reconstruction_status": (
                        "verified_reconstruction"
                        if front.get("verified_reconstruction") else
                        "partial_reconstruction"
                        if front.get("complete") is False else
                        "not_reconstructed")},
        "markdown": markdown,
        "body": body,
    }, headers={"Cache-Control": "public, max-age=3600"})


@app.get("/retrospective-history.sqlite", response_class=FileResponse)
def retrospective_sqlite():
    """Download the portable bitemporal SQLite database built with the API."""
    path = DATA_DIR / "retrospective_history.sqlite"
    if not path.is_file():
        raise HTTPException(404, "retrospective SQLite database is not built")
    return FileResponse(
        path, media_type="application/vnd.sqlite3",
        filename="lexgraph-retrospective-history.sqlite",
        headers={"Cache-Control": "public, max-age=3600"})


@app.get("/git")
def git(lane: int | None = Query(None, ge=0, le=3),
        limit: int = Query(100, ge=1, le=1000)):
    """Laws-as-Git event log, optionally filtered by lane, newest first.

    Git is a navigation metaphor: legal effect is determined by each row's
    official source and status. Lane: 0=EU, 1=Bund, 2=Bayern, 3=Länder.
    """
    data = _load("git")
    commits = data["commits"]
    if lane is not None:
        commits = [c for c in commits if c.get("lane") == lane]
    return {"lanes": data.get("lanes", LANES),
            "lane": lane, "total": len(commits),
            "commits": commits[:limit]}


@app.get("/graph")
def graph():
    """The QFS arena export (graph.json): nodes/edges/beliefs/ticks/worlds."""
    return _cached(_load("graph"))


@app.get("/hierarchy")
def hierarchy():
    """Competence-aware legal layers (hierarchy.json), not a total ranking."""
    return _cached(_load("hierarchy"))


@app.get("/citations")
def citations(
        act: str | None = Query(
            None, min_length=1, description="exact act id or JurAbk"),
        norm: str | None = Query(
            None, min_length=1, description="exact §/Art. designator"),
        direction: str = Query("out", pattern="^(in|out)$"),
        kind: str | None = Query(None, pattern="^(self|cross_act)$"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0)):
    """Current-text statutory citations or reverse backlinks.

    Extraction is mechanical and limited to explicit norm designators.  An
    unqualified reference stays inside its source act; cross-act resolution
    uses the closed exact-alias registry only.  Unresolved rows are retained
    rather than fuzzily linked.  Snapshot dates are observations, not asserted
    legal-effective dates, and citation edges make no interpretation claim.
    """
    data = _load("citations")
    try:
        result = query_citations(
            DATA_DIR / "citations.sqlite", act=act, norm=norm,
            direction=direction, kind=kind, limit=limit, offset=offset)
    except CitationStoreError as exc:
        raise HTTPException(503, str(exc)) from exc
    return {
        "schema_version": data.get("schema_version"),
        "built_at": data.get("built_at"),
        "machine_extracted": data.get("machine_extracted"),
        "current_state_only": data.get("current_state_only"),
        "legal_interpretation": data.get("legal_interpretation"),
        "source_policy": data.get("source_policy"),
        "source_snapshots": data.get("source_snapshots"),
        "counts": data.get("counts"),
        "direction": direction,
        "storage": data.get("storage"),
        "total": (data.get("counts") or {}).get("total", 0),
        "matched": result["matched"],
        "offset": offset,
        "limit": limit,
        "citations": result["citations"],
    }


@app.get("/eu-index")
def eu_index(q: str | None = Query(None, min_length=1),
             kind: str | None = Query(None, pattern="^(DIR|REG)$"),
             limit: int = Query(100, ge=1, le=500),
             offset: int = Query(0, ge=0)):
    """All in-force directives (incl. delegated/implementing) and all basic
    regulations as metadata — CELEX, type, date, title. Search plus offset/
    limit pagination keeps responses small while leaving the full ~8k-row
    index enumerable. 404 until the index has been fetched once.
    """
    path = DATA_DIR / "eu_index.json"
    if not path.exists():
        raise HTTPException(404, "eu index not built yet")
    data = _load("eu_index")
    rows = data["instruments"]
    if kind:
        rows = [r for r in rows if r["type"].startswith(kind)]
    if q:
        needle = q.strip().casefold()
        rows = [r for r in rows
                if needle in r["celex"].casefold()
                or needle in r["title"].casefold()]
    return {"built_at": data["built_at"], "total": data["total"],
            "matched": len(rows), "offset": offset, "limit": limit,
            "instruments": rows[offset:offset + limit]}


@app.get("/procedures/watched")
def watched_procedures():
    """Persistent tracked procedures, including immutable terminal history.

    DIP and EUR-Lex stages are reported as observed.  In particular, an EU
    political agreement remains active. Even OJ publication stays active as
    ``pending_final_review`` until the final Article 2 has been compared with
    the tracked proposal and that review is persisted.
    """
    return _cached(_load("watched_procedures"))


@app.get("/amendment-fates")
def amendment_fates(
        procedure_id: str | None = Query(
            None, description="official DIP procedure id"),
        validation_id: str | None = Query(
            None, description="Lexgraph validation record id"),
):
    """Reviewed parliamentary document chains and current-law checks."""
    data = _load("amendment_fates")
    if not procedure_id and not validation_id:
        return _cached(data)
    rows = data.get("records") or []
    if procedure_id:
        rows = [row for row in rows
                if str(row.get("procedure_id")) == procedure_id]
    if validation_id:
        rows = [row for row in rows
                if str(row.get("id")) == validation_id]
    return _cached({
        "schema_version": data.get("schema_version"),
        "built_at": data.get("built_at"),
        "total": len(rows),
        "validated": sum(bool((row.get("validation") or {}).get("passed"))
                         for row in rows),
        "records": rows,
    })


def _append_gii_catalog(result: dict, query: str, limit: int) -> dict:
    """Append metadata-only breadth matches after deep corpus results."""
    try:
        payload = _load("gii_catalog")
        rows = payload.get("acts") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ValueError("invalid GII catalogue representation")
    except (HTTPException, ValueError):
        # Unavailable breadth discovery is not "zero catalogue matches".
        result["catalog_total"] = None
        result["catalog_matches"] = []
        _mark_component(result, "catalog", {"status": "unavailable",
                                            "reason": "source_unavailable"})
        return result
    deep_act_ids = {
        str(row.get("act_id") or row.get("id") or "")
        for row in result.get("act_matches") or []
    }
    deep_act_ids.update(
        str(row.get("act_id") or "")
        for row in result.get("norm_matches") or [])
    # The final section is breadth discovery, not a second rendering of the
    # curated corpus.  Exclude every catalogue row that already has a local
    # deep act, including deep hits beyond the current result-page limit.
    deep_act_ids.update(str(row.get("act_id")) for row in rows or []
                        if row.get("act_id"))
    matches, total = _gii_catalog_index(rows or []).search(
        query, limit=limit, exclude_act_ids=deep_act_ids)
    result["catalog_total"] = total
    result["catalog_matches"] = matches
    _mark_component(result, "catalog", {"status": "ok"}, add=total)
    return result


def _mark_component(result: dict, name: str, status: dict,
                    add: int | None = None) -> None:
    """Record one component and keep the totals honest.

    ``result_total`` stays a number only while every consulted component
    answered completely.  Otherwise it becomes ``None`` and the known part
    is kept separately as ``result_total_lower_bound``.
    """
    components = {**result.get("components", {}), name: status}
    result["components"] = components
    lower = int(result.get("result_total_lower_bound")
                if result.get("result_total_lower_bound") is not None
                else result.get("result_total") or 0) + int(add or 0)
    partial = any(c.get("status") != "ok" for c in components.values())
    result["status"] = "partial" if partial else "ok"
    result["result_total_is_partial"] = partial
    result["result_total_lower_bound"] = lower
    result["result_total"] = None if partial else lower
    result["total_semantics"] = (
        "result_total is null because a component was unavailable; "
        "result_total_lower_bound counts only the components that answered"
        if partial else "complete count over every consulted component")


def _append_temporal_search(result: dict, query: str,
                            change_limit: int = 25,
                            decision_limit: int = 20) -> dict:
    """Add amendment history and judgments to the unified search envelope."""
    statuses = {}
    changes = {"matched": 0, "events": []}
    try:
        manifest = _retrospective_manifest(optional=True)
        if manifest is None:
            # The archive was not consulted at all: unavailable, not "no
            # matching changes".
            statuses["changes"] = {"status": "unavailable", "reason": "not_built"}
        else:
            changes = resolve_changes(manifest, query=query)
            statuses["changes"] = {"status": "ok"}
    except RetrospectiveIntegrityError:
        statuses["changes"] = {"status": "unavailable", "reason": "integrity_check_failed"}
    except RetrospectiveNotFound:
        statuses["changes"] = {"status": "unavailable", "reason": "not_in_corpus"}
    except (HTTPException, OSError, ValueError) as exc:
        reason = "integrity_check_failed" if isinstance(exc.__cause__, RetrospectiveIntegrityError) else "source_unavailable"
        statuses["changes"] = {"status": "unavailable", "reason": reason}
    result["change_total"] = int(changes.get("matched") or 0) if statuses["changes"]["status"] == "ok" else None
    result["change_matches"] = list(changes.get("events") or [])[:change_limit]

    try:
        decision_rows = _load("decisions")
        if not isinstance(decision_rows, list) or any(not isinstance(r, dict) for r in decision_rows):
            raise ValueError("invalid decision representation")
        statuses["decisions"] = {"status": "ok"}
    except (HTTPException, KeyError, OSError, ValueError):
        decision_rows = []
        statuses["decisions"] = {"status": "unavailable", "reason": "source_unavailable"}
    needle = query.strip().casefold()

    def decision_hit(row: dict) -> bool:
        values = [
            row.get("az"), row.get("court"), row.get("court_short"),
            row.get("title"), *((row.get("summary") or {}).values()),
            *(effect.get("jurabk") for effect in row.get("effects") or []),
            *(effect.get("norm") for effect in row.get("effects") or []),
        ]
        return any(needle in str(value).casefold()
                   for value in values if value)

    decision_matches = [row for row in decision_rows if decision_hit(row)]
    result["decision_total"] = len(decision_matches) if statuses["decisions"]["status"] == "ok" else None
    result["decision_matches"] = decision_matches[:decision_limit]
    _mark_component(result, "changes", statuses["changes"],
                    add=result["change_total"])
    _mark_component(result, "decisions", statuses["decisions"],
                    add=result["decision_total"])
    return result


@app.get("/search")
def search(q: str = Query(..., min_length=1),
           limit: int = Query(25, ge=1, le=200),
           norm_limit: int = Query(50, ge=1, le=200),
           procedure_limit: int = Query(20, ge=1, le=100),
           catalog_limit: int = Query(25, ge=1, le=100)):
    """Ranked Unicode full-text search over acts and current norms.

    ``matches`` and ``total`` retain the original act-search contract.
    Enriched act results live in ``act_matches``; norm results include their
    act, §/Art., a plain-text snippet, provenance, score, and detail link.
    Synonyms (including multilingual Ukraine/temporary-protection terms) are
    data-driven and embedded into the built ``search.sqlite`` artifact.

    If an old deployment has no FTS artifact yet, the endpoint degrades to the
    previous title/abbreviation substring search instead of failing.  Complete
    GII catalogue matches are appended as metadata-only discovery results and
    never masquerade as locally indexed full text.
    """
    rows = _load("wiki")
    # DIP has only a few hundred current procedures.  Count the full match set
    # first so ``procedure_total`` follows the act/norm total contract, then
    # expose only the requested page.
    all_procedure_matches = _procedure_search_index(
        _load("hierarchy")).search(q, 10_000)
    procedure_total = len(all_procedure_matches)
    procedure_matches = all_procedure_matches[:procedure_limit]
    index_path = DATA_DIR / "search.sqlite"
    if not index_path.is_file():
        needle = normalize_search_text(q)
        all_matches = [r for r in rows
                       if needle in normalize_search_text(r.get("id"))
                       or needle in normalize_search_text(r.get("jurabk"))
                       or needle in normalize_search_text(r.get("title"))]
        matches = all_matches[:limit]
        # Without the FTS artifact the norm texts were not searched at all:
        # report the component as unavailable instead of "0 norm hits".
        result = {
            "query": q, "total": len(all_matches), "matches": matches,
            "result_total": len(all_matches) + procedure_total,
            "act_total": len(all_matches),
            "norm_total": None, "act_matches": matches,
            "norm_matches": [],
            "procedure_total": procedure_total,
            "procedure_matches": procedure_matches,
            "temporal_scope": _SEARCH_TEMPORAL_SCOPE,
        }
        _mark_component(result, "acts", {"status": "ok",
                                         "mode": "substring_fallback"})
        _mark_component(result, "norms", {"status": "unavailable",
                                          "reason": "index_not_built"})
        result = _append_temporal_search(result, q)
        return _append_gii_catalog(result, q, catalog_limit)

    global _SEARCH_ENGINE
    with _SEARCH_LOCK:
        if _SEARCH_ENGINE is None or _SEARCH_ENGINE.path != index_path:
            if _SEARCH_ENGINE is not None:
                _SEARCH_ENGINE.close()
            _SEARCH_ENGINE = SearchEngine(index_path, rows)
        # There are only dozens of curated acts.  Ask the engine for all act
        # candidates so the legacy substring union below has an exact count,
        # then apply the public limit after de-duplication.
        result = _SEARCH_ENGINE.search(q, act_limit=max(limit, len(rows)),
                                       norm_limit=norm_limit)

    # Preserve the old arbitrary-substring behavior for act abbreviations
    # (FTS prefix matching cannot find a query in the middle of one token).
    needle = normalize_search_text(q)
    legacy = [r for r in rows
              if needle and (needle in normalize_search_text(r.get("id"))
                             or needle in normalize_search_text(
                                 r.get("jurabk"))
                             or needle in normalize_search_text(
                                 r.get("title")))]
    seen = {r["id"] for r in result["act_matches"]}
    for row in legacy:
        if row["id"] in seen:
            continue
        enriched = dict(row)
        enriched.update({
            "score": 0.0,
            "snippet": row.get("title") or row.get("jurabk") or "",
            "matched_fields": ["legacy_substring"],
            "source": "gii" if row.get("juris") == "DE"
            else "bayern_recht",
            "url": f"/acts/{row['id']}",
        })
        result["act_matches"].append(enriched)
        seen.add(row["id"])
    result["act_matches"] = result["act_matches"][:limit]
    result["matches"] = [
        {key: value for key, value in row.items()
         if key not in {"score", "snippet", "matched_fields", "source",
                        "url"}}
        for row in result["act_matches"]]
    result["act_total"] = len(seen)
    result["total"] = len(seen)
    result["procedure_total"] = procedure_total
    result["procedure_matches"] = procedure_matches
    result["result_total"] = (result["act_total"] + result["norm_total"]
                              + result["procedure_total"])
    result["temporal_scope"] = _SEARCH_TEMPORAL_SCOPE
    for component in ("acts", "norms", "procedures"):
        _mark_component(result, component, {"status": "ok"})
    result = _append_temporal_search(result, q)
    return _append_gii_catalog(result, q, catalog_limit)


# Every search result states that it answers for the current consolidation
# only, so a current hit is never mistaken for historical text.
_SEARCH_TEMPORAL_SCOPE = {
    "valid_at": None,
    "mode": "current_consolidation",
    "historical_search": "unsupported",
    "changes": "recorded amendment events (event-only, not historical text)",
    "historical_text": "/norms/{id}/at?date=YYYY-MM-DD",
}


@app.get("/decisions")
def decisions(q: str | None = Query(None, min_length=1),
              act: str | None = Query(None, min_length=1),
              limit: int = Query(50, ge=1, le=200)):
    """Court decisions (decisions.json), newest first, optionally filtered.

    `q` matches case-insensitively in az, court, court_short, title, every
    summary language, and effects[].jurabk; `act` filters by effects[].act_id
    (an act index id like fed_asylblg).
    """
    rows = _load("decisions")
    if act:
        rows = [d for d in rows
                if any(e.get("act_id") == act
                       for e in d.get("effects") or [])]
    if q:
        needle = q.strip().casefold()

        def hit(d: dict) -> bool:
            hay = [d.get("az"), d.get("court"), d.get("court_short"),
                   d.get("title"),
                   *(d.get("summary") or {}).values(),
                   *(e.get("jurabk") for e in d.get("effects") or [])]
            return any(needle in str(h).casefold() for h in hay if h)

        rows = [d for d in rows if hit(d)]
    return {"query": q, "act": act, "total": len(rows),
            "decisions": rows[:limit]}


@app.get("/decisions/{decision_id}")
def decision_detail(decision_id: str):
    """One exported decision row by id; 404 if unknown."""
    for d in _load("decisions"):
        if d.get("id") == decision_id:
            return _cached(d)
    raise HTTPException(404, f"unknown decision '{decision_id}'")


@app.get("/digest")
def digest():
    """The LLM digest (digest.json): {generated_at, model, llm, periods}.

    Read fresh per request, NOT via _load(): the file is rewritten by every
    refresh (tools/build_digest.py) and is legitimately absent when no
    OPENROUTER_API_KEY is configured — that must stay a 404, not a
    permanently cached copy or a 503. The file is tiny, so no cache needed.
    """
    path = DATA_DIR / "digest.json"
    if not path.exists():
        raise HTTPException(404, "no digest available")
    with path.open(encoding="utf-8") as fh:
        return _cached(json.load(fh))
