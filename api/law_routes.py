"""Norm-level Git semantics: log, at, diff, blame (evidence envelopes).

    GET /norms/resolve?q=                    citation → canonical identity
    GET /norms/{id}/log                      versions + amendment evidence
    GET /norms/{id}/at?date=YYYY-MM-DD       the version valid on a date
    GET /norms/{id}/diff?from=&to=           sentence-aware diff
    GET /norms/{id}/blame[?date=]            per sentence: introducing version
    GET /norms/{id}/changes                  amendment change history (norm)
    GET /acts/{id}/changes                   amendment change history (act)

``{id}`` is a canonical norm id such as ``de/bund/sgb_14/para/37`` or a
sub-unit ``de/bund/sgb_14/para/37/abs/2/nr/3``.  Every response is a typed
evidence envelope (``api.evidence``); legal validity (``valid_*``) and source
observation (``observed_*``) are separate fields and never substituted.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Request

from api.evidence import (CACHE_KEY_ARGUMENTS, EnvelopeError, envelope,
                          error_envelope, response)
from api.law_structure import (
    STRUCTURE_PARSER_VERSION,
    find_unit,
    human_locator,
    parse_norm_structure,
)
from api.norm_history import (
    HISTORY_SCHEMA,
    ActChain,
    NormHistoryStore,
    NormVersion,
    amendment_events,
    blame,
    select_version,
    sentence_diff,
    today_iso,
    version_evidence,
)
from api.law_history import LawHistoryStore, open_store
from api.law_history_chain import (
    act_summary,
    compact,
    enrich_evidence,
    events_from_changes,
    history_block,
    norm_chain,
    norm_summary,
)
from api.norm_identity import load_gii_toc
from api import reviewed_norm_editions as reviewed

BUNDLED_TOC = Path(__file__).resolve().parent / "data" / "gii_toc.json"
_ALLOWED = {
    "log": {"as_of", "limit"},
    "at": {"date", "version", "as_of", "structure"},
    "diff": {"from", "to", "as_of"},
    "blame": {"date", "as_of"},
    "resolve": {"q", "act", "norm"},
    "materials": {"relation"},
    "changes": {"date", "limit", "offset", "state"},
    "act_changes": {"date", "limit", "offset", "state", "norm"},
    "act_versions": {"limit", "offset", "source", "include_partial"},
}
CHANGE_STATES = {"in_force", "pending", "incorporated_date_unknown",
                 "date_unknown", "not_yet_promulgated"}


def create_router(data_dir: Callable[[], Path],
                  manifest: Callable[[], dict | None],
                  snapshot: Callable[[], str | None]) -> APIRouter:
    router = APIRouter()
    stores: dict[Path, NormHistoryStore] = {}

    def store() -> NormHistoryStore:
        root = data_dir()
        if root not in stores:
            stores.clear()
            stores[root] = NormHistoryStore(
                root, toc_paths=(root / "gii_toc.json", BUNDLED_TOC),
                manifest_loader=manifest)
        return stores[root]

    def toc() -> dict[str, Any] | None:
        root = data_dir()
        return load_gii_toc(root / "gii_toc.json", BUNDLED_TOC)

    def history() -> LawHistoryStore | None:
        try:
            return open_store(data_dir())
        except Exception as exc:  # a broken store is typed, never empty
            raise EnvelopeError(
                "integrity_check_failed", f"law history unreadable: {exc}",
                failed_components=[{"component": "law_history",
                                    "reason": "integrity_check_failed"}]
            ) from exc

    def context(norm_id: str, as_of: str | None, selector: str | None = None, *, wording: bool = True):
        index = store()
        if not wording:
            return _context(index, norm_id, as_of, history(), selector=selector)
        try:
            records = reviewed.load(data_dir())
        except reviewed.ReviewedEditionError as exc:
            raise EnvelopeError("integrity_check_failed", str(exc)) from exc
        if not records:
            return _context(index, norm_id, as_of, history(), selector=selector)
        resolved = index.resolve(norm_id)
        record = records.get(resolved["norm_id"])
        if record is None or not reviewed.available(record, as_of):
            return _context(index, norm_id, as_of, history(), selector=selector, resolved=resolved)
        try:
            _, chain, versions = _context(index, norm_id, as_of, history(),
                                          selector=selector, resolved=resolved)
        except EnvelopeError as exc:
            if exc.status not in {"temporally_unresolved", "not_found_in_searched_scope", "not_in_corpus"}:
                raise
            # A reviewed section can have an earlier legal date or a later knowledge
            # horizon than the optional full-act archive in this same generation.
            versions = reviewed.merge_versions(resolved["norm_id"], record, [], selector)
            if not versions:
                raise
            chain = ActChain(act=resolved["act"], prefix=resolved["norm_id"].rsplit("/", 2)[0],
                             states=[v.first_state for v in versions], norms={}, events=[],
                             history_start=record["editions"][0]["valid_from"], bavarian=False,
                             snapshot={"reviewed_norm_editions": True})
            return resolved, chain, versions
        return resolved, chain, reviewed.merge_versions(resolved["norm_id"], record, versions, selector)

    def run(operation: str, request: Request, norm_id: str | None,
            handler: Callable[..., dict[str, Any]]):
        scope = {"norm": norm_id, **dict(request.query_params)}
        try:
            _strict(request, operation)
            payload = handler()
        except EnvelopeError as exc:
            payload = error_envelope(exc, operation=f"law_{operation}",
                                     request_scope=scope,
                                     snapshot_id=snapshot())
            return response(payload)
        payload["request_scope"] = scope
        payload["snapshot_id"] = snapshot()
        return response(payload, cache_control="public, max-age=600")

    @router.get("/norms/resolve")
    def resolve_norm(request: Request):
        def handler():
            params = request.query_params
            if params.get("act") or params.get("norm"):
                if params.get("q") or not (params.get("act") and
                                           params.get("norm")):
                    raise EnvelopeError(
                        "invalid_argument",
                        "use either q=<citation|id> or act=<act id> with "
                        "norm=<designator>")
                resolved = store().resolve_act_norm(params["act"],
                                                    params["norm"])
            else:
                resolved = store().resolve(params.get("q") or "")
            act = resolved["act"]
            ident = resolved["norm_id"] + (
                "/" + resolved["sub"] if resolved["sub"] else "")
            return envelope(
                operation="law_resolve", total=1,
                searched=[{"source": "act_index", "status": "ok",
                           "count": 1}],
                applied_constraints={"match": "exact_alias"},
                evidence=[{"id": ident, "canonical_id": ident,
                           "act_id": act.get("id"),
                           "jurabk": act.get("jurabk"),
                           "title": act.get("title")}])
        return run("resolve", request,
                   request.query_params.get("q") or
                   request.query_params.get("norm"), handler)

    @router.get("/norms/{norm_id:path}/log")
    def norm_log(norm_id: str, request: Request):
        def handler():
            params = request.query_params
            as_of = _as_of(params.get("as_of"))
            limit = _int(params.get("limit"), 100, 1, 500)
            resolved, chain, versions = context(norm_id, as_of)
            sub = resolved["sub"]
            shown = _collapse_by_sub(versions, sub, chain) if sub else versions
            unit, number = resolved["norm_id"].split("/")[-2:]
            changes = _changes_of(chain)
            events = (events_from_changes(changes) if _is_lh(chain) else
                      amendment_events(chain, unit, number))
            today = today_iso()
            by_id = {c["change_id"]: c for c in changes or []}
            evidence = [enrich_evidence(
                version_evidence(chain, v, toc(), include_text=False), v,
                by_id, today) for v in reversed(shown)][:limit]
            if sub:
                for item, version in zip(evidence, reversed(shown)):
                    item["pinpoint"]["sub_unit"] = sub
                    item["pinpoint"]["sub_unit_label"] = human_locator(sub)
                    item["sub_unit_text"] = _sub_text(version, sub, chain)
            return envelope(
                operation="law_log", total=len(shown),
                searched=_searched(chain),
                applied_constraints=_applied(chain, as_of, resolved),
                evidence=evidence, warnings=_warnings(chain),
                norm=_norm_meta(chain, resolved, versions),
                order="newest_first",
                events=events,
                history=(history_block(chain, versions) if _is_lh(chain)
                         else _history(chain, versions)),
                change_summary=_summary(history(), chain, resolved, versions,
                                        today, versions[-1]))
        return run("log", request, norm_id, handler)

    @router.get("/norms/{norm_id:path}/at")
    def norm_at(norm_id: str, request: Request):
        def handler():
            params = request.query_params
            if params.get("version") and params.get("date"):
                raise EnvelopeError(
                    "invalid_argument",
                    "use either date=<YYYY-MM-DD> (the version valid on that "
                    "date) or version=<version_id> (that exact version)")
            as_of = _as_of(params.get("as_of"))
            structure = _bool(params.get("structure"), True)
            resolved, chain, versions = context(norm_id, as_of, params.get("date") or params.get("version"))
            if params.get("version"):
                version, picked = _pick(versions, params["version"], chain)
                on = (version.first_state.valid_from or
                      version.first_state.first_observed_at or today_iso())
                status, resolution = "ok", {"resolution": "exact_version",
                                            **picked}
            else:
                on = _date(params.get("date"), "date", required=True)
                status, version, resolution = select_version(versions, on,
                                                             chain)
            warnings = _warnings(chain)
            if resolution.get("note"):
                warnings.append(resolution["note"])
            if on > today_iso():
                warnings.append(
                    "the requested date is in the future; later amendments "
                    "not yet recorded may change the answer")
            if version is None and status == "not_in_corpus":
                if resolution.get("resolution") == "absent_from_verified_edition":
                    detail = (f"The complete edition for {on} contains no "
                              f"{resolved['norm_id']}.")
                else:
                    detail = (f"Kerf holds no text of {resolved['norm_id']} for {on}. "
                              f"Its text history starts {resolution.get('text_history_start')}")
                raise EnvelopeError(
                    "not_in_corpus", detail,
                    searched=_searched(chain))
            evidence = []
            changes = _changes_of(chain)
            by_id = {c["change_id"]: c for c in changes or []}
            if version is not None:
                item = enrich_evidence(
                    version_evidence(chain, version, toc(),
                                     include_structure=structure),
                    version, by_id, on)
                item["temporal"]["resolution"] = resolution
                if resolved["sub"]:
                    item["pinpoint"]["sub_unit"] = resolved["sub"]
                    item["pinpoint"]["sub_unit_label"] = human_locator(
                        resolved["sub"])
                    sub_text = _sub_text(version, resolved["sub"], chain)
                    if sub_text is None:
                        raise EnvelopeError(
                            "not_found_in_searched_scope",
                            f"{human_locator(resolved['sub'])} does not exist "
                            f"in {version.version_id}",
                            searched=_searched(chain))
                    item["sub_unit_text"] = sub_text
                evidence.append(item)
            return envelope(
                status=status, operation="law_text_at",
                total=len(evidence) if status == "ok" else None,
                searched=_searched(chain),
                applied_constraints={**_applied(chain, as_of, resolved),
                                     "valid_at": on},
                evidence=evidence, warnings=warnings,
                norm=_norm_meta(chain, resolved, versions),
                resolution=resolution,
                change_summary=_summary(history(), chain, resolved, versions,
                                        on, version))
        return run("at", request, norm_id, handler)

    @router.get("/norms/{norm_id:path}/diff")
    def norm_diff(norm_id: str, request: Request):
        def handler():
            params = request.query_params
            as_of = _as_of(params.get("as_of"))
            if not params.get("from") or not params.get("to"):
                raise EnvelopeError(
                    "invalid_argument",
                    "diff needs from= and to= (YYYY-MM-DD, a version id, a "
                    "content hash prefix, or 'head')")
            resolved, chain, versions = context(norm_id, as_of, params["from"])
            left, left_res = _pick(versions, params["from"], chain)
            _, right_chain, right_versions = context(norm_id, as_of, params["to"])
            right, right_res = _pick(right_versions, params["to"], right_chain)
            statuses = {left_res["status"], right_res["status"]}
            status = "ok" if statuses == {"ok"} else "temporally_unresolved"
            ops = sentence_diff(left.text, right.text,
                                bavarian=chain.bavarian, annex=left.annex,
                                eu=chain.eu)
            sub = resolved["sub"]
            if sub:
                ops = [op for op in ops if _op_in_sub(op, sub)]
            counts: dict[str, int] = {}
            for op in ops:
                counts[op["op"]] = counts.get(op["op"], 0) + 1
            warnings = _warnings(chain)
            for side, res in (("from", left_res), ("to", right_res)):
                if res.get("note"):
                    warnings.append(f"{side}: {res['note']}")
            return envelope(
                status=status, operation="law_diff",
                total=sum(v for k, v in counts.items() if k != "equal"),
                searched=_searched(chain),
                applied_constraints={**_applied(chain, as_of, resolved),
                                     "from": left_res, "to": right_res,
                                     "granularity": "sentence",
                                     "structure_parser":
                                         STRUCTURE_PARSER_VERSION},
                evidence=[enrich_evidence(version_evidence(
                              chain, v, toc(), include_text=False), v,
                              {c["change_id"]: c for c in
                               _changes_of(chain) or []}, today_iso())
                          for v in (left, right)],
                warnings=warnings,
                norm=_norm_meta(chain, resolved, versions),
                identical=left.content_sha256 == right.content_sha256,
                summary=counts, changes=ops,
                title_change=(None if left.title == right.title else
                              {"old": left.title, "new": right.title}))
        return run("diff", request, norm_id, handler)

    @router.get("/norms/{norm_id:path}/blame")
    def norm_blame(norm_id: str, request: Request):
        def handler():
            params = request.query_params
            as_of = _as_of(params.get("as_of"))
            resolved, chain, versions = context(norm_id, as_of, params.get("date"))
            status = "ok"
            resolution: dict[str, Any] = {"resolution": "latest_version"}
            target = versions[-1]
            if params.get("date"):
                on = _date(params.get("date"), "date", required=True)
                status, picked, resolution = select_version(versions, on,
                                                            chain)
                if picked is None:
                    raise EnvelopeError(
                        status if status != "ok" else "not_in_corpus",
                        f"no version of {resolved['norm_id']} for {on}",
                        searched=_searched(chain))
                target = picked
            unit, number = resolved["norm_id"].split("/")[-2:]
            events = (events_from_changes(_changes_of(chain))
                      if _is_lh(chain) else
                      amendment_events(chain, unit, number))
            lines = blame(versions, target, events, bavarian=chain.bavarian,
                          eu=chain.eu)
            if resolved["sub"]:
                lines = [row for row in lines
                         if _locator_in_sub(row["locator"], resolved["sub"])]
            attributed = sum(1 for row in lines
                             if row["status"] == "introduced_within_history")
            warnings = _warnings(chain)
            if lines and attributed < len(lines):
                warnings.append(
                    f"{len(lines) - attributed} of {len(lines)} sentences "
                    "predate Kerf's text history for this norm; their "
                    "introducing amendment is not attributed (only "
                    "parser-matched candidates are listed)")
            return envelope(
                status=status, operation="law_blame", total=len(lines),
                searched=_searched(chain),
                applied_constraints={**_applied(chain, as_of, resolved),
                                     "target": resolution},
                evidence=[version_evidence(chain, target, toc())],
                warnings=warnings,
                norm=_norm_meta(chain, resolved, versions),
                lines=lines,
                summary={"sentences": len(lines),
                         "introduced_within_history": attributed,
                         "predates_text_history": len(lines) - attributed})
        return run("blame", request, norm_id, handler)

    @router.get("/norms/{norm_id:path}/changes")
    def norm_changes(norm_id: str, request: Request):
        def handler():
            params = request.query_params
            on = _date(params.get("date"), "date", required=False) or \
                today_iso()
            limit = _int(params.get("limit"), 100, 0, 500)
            offset = _int(params.get("offset"), 0, 0, 100000)
            state = _state_filter(params.get("state"))
            resolved, chain, versions = context(norm_id, None, wording=False)
            if not _is_lh(chain):
                raise EnvelopeError(
                    "unavailable",
                    "the change history store (law_history) does not cover "
                    f"{chain.act.get('id')}",
                    failed_components=[{"component": "law_history",
                                        "reason": "source_unavailable"}])
            changes = _changes_of(chain) or []
            rows = [dict(compact(c, on), commands=c.get("commands"),
                         evidence=c.get("evidence"),
                         diff_url=_diff_url(resolved["norm_id"], c, versions))
                    for c in reversed(changes)]
            if state:
                rows = [row for row in rows if row["state"] in state]
            return envelope(
                operation="law_changes", total=len(rows),
                searched=_searched(chain),
                applied_constraints={**_applied(chain, None, resolved),
                                     "evaluated_on": on,
                                     "state": sorted(state) if state else
                                     "all",
                                     "order": "newest_first"},
                evidence=rows[offset:offset + limit] if limit else [],
                offset=offset, limit=limit,
                warnings=_warnings(chain),
                norm=_norm_meta(chain, resolved, versions),
                change_summary=_summary(history(), chain, resolved, versions,
                                        on, versions[-1]))
        return run("changes", request, norm_id, handler)

    @router.get("/acts/{act_id}/changes")
    def act_changes(act_id: str, request: Request):
        def handler():
            params = request.query_params
            on = _date(params.get("date"), "date", required=False) or \
                today_iso()
            limit = _int(params.get("limit"), 100, 0, 1000)
            offset = _int(params.get("offset"), 0, 0, 100000)
            state = _state_filter(params.get("state"))
            lh = history()
            if not re.fullmatch(r"[a-z0-9_]+", act_id or ""):
                raise EnvelopeError("invalid_argument", "invalid act id")
            if lh is None or not lh.covers(act_id):
                known = any(row.get("id") == act_id
                            for row in store().act_index())
                raise EnvelopeError(
                    "unavailable" if known else "not_in_corpus",
                    f"no change history for {act_id}",
                    failed_components=[{"component": "law_history",
                                        "reason": "source_unavailable"}]
                    if known else [])
            changes = lh.changes(act_id=act_id)
            norm = params.get("norm")
            if norm:
                resolved = store().resolve_act_norm(act_id, norm)
                changes = [c for c in changes
                           if c.get("norm_id") == resolved["norm_id"]]
            groups: dict[tuple[str, str], dict[str, Any]] = {}
            for change in reversed(changes):
                row = compact(change, on)
                if state and row["state"] not in state:
                    continue
                key = (str((change.get("amending_act") or {}).get(
                    "document_id")), str(
                    (change.get("amending_act") or {}).get("article")))
                group = groups.setdefault(key, {
                    "amending_act": row["amending_act"],
                    "dates": row["dates"], "state": row["state"],
                    "pending": row["pending"], "norms": []})
                group["norms"].append({
                    "change_id": row["change_id"],
                    "norm_id": row["norm_id"],
                    "norm_label": row["norm_label"],
                    "change_type": row["change_type"],
                    "dates": row["dates"], "state": row["state"],
                    "text_effect": row["text_effect"]})
            rows = list(groups.values())
            info = lh.acts().get(act_id) or {}
            return envelope(
                operation="law_act_changes", total=len(rows),
                searched=[{"source": "law_history", "status": "ok",
                           "count": len(changes)}],
                applied_constraints={"act_id": act_id, "evaluated_on": on,
                                     "grouping": "amending act and article",
                                     "state": sorted(state) if state else
                                     "all", "norm": norm,
                                     "order": "newest_first"},
                evidence=rows[offset:offset + limit] if limit else [],
                offset=offset, limit=limit,
                act={k: info.get(k) for k in ("act_id", "jurabk", "title",
                                              "states", "states_by_source",
                                              "earliest_text", "norms",
                                              "norm_versions")},
                change_summary=_stored_act_summary(lh, act_id, on))
        return run("act_changes", request, act_id, handler)

    @router.get("/acts/{act_id}/versions")
    def act_versions(act_id: str, request: Request):
        """Consolidated versions of a whole act as the sources showed them."""
        def handler():
            params = request.query_params
            limit = _int(params.get("limit"), 200, 0, 1000)
            offset = _int(params.get("offset"), 0, 0, 100000)
            source = params.get("source")
            partial = _bool(params.get("include_partial"), False)
            lh = history()
            if not re.fullmatch(r"[a-z0-9_]+", act_id or ""):
                raise EnvelopeError("invalid_argument", "invalid act id")
            if lh is None or not lh.covers(act_id):
                raise EnvelopeError(
                    "not_in_corpus", f"no version history for {act_id}")
            rows = []
            skipped = 0
            for state in reversed(lh.states(act_id)):
                if source and state["source"] != source:
                    continue
                notes = state.get("notes") or {}
                if notes.get("capture_kind") == "norm_html" and not partial:
                    skipped += 1   # single-norm pages attest norms, not acts
                    continue
                rows.append({
                    "seq": state["seq"], "state": state["state_key"],
                    "source": state["source"],
                    "source_role": state["source_role"],
                    "known_from": state["known_from"],
                    "known_until": state["known_until"],
                    "url": state["url"], "archive_url": state["archive_url"],
                    "sha256": state["sha256"],
                    "complete": bool(state["complete"]),
                    "valid_from": state["valid_from"],
                    "valid_from_status": state["valid_from_status"],
                    "valid_until_exclusive": state["valid_until"],
                    "act_stand_note": notes.get("act_stand_note"),
                    "documentation_notes": notes.get("documentation_notes")
                    or [],
                    "date_axes_note": (
                        "known_* is when the source showed this "
                        "consolidation (capture/retrieval), never a legal "
                        "date; valid_* only where verified"),
                })
            info = lh.acts().get(act_id) or {}
            return envelope(
                operation="law_act_versions", total=len(rows),
                searched=[{"source": "law_history", "status": "ok",
                           "count": len(rows)}],
                applied_constraints={"act_id": act_id,
                                     "source": source or "all",
                                     "include_partial": partial,
                                     "single_norm_pages_omitted": skipped,
                                     "order": "newest_first"},
                evidence=rows[offset:offset + limit] if limit else [],
                offset=offset, limit=limit,
                act={k: info.get(k) for k in (
                    "act_id", "jurabk", "title", "states", "states_by_source",
                    "earliest_text", "latest_text", "norms",
                    "norm_versions")})
        return run("act_versions", request, act_id, handler)

    @router.get("/norms/{norm_id:path}/materials")
    def norm_materials(norm_id: str, request: Request):
        def handler():
            resolved = store().resolve(norm_id)
            try:
                from api.materials_routes import materials_for_norm
            except ImportError as exc:  # optional component
                raise EnvelopeError(
                    "unavailable", "legislative materials are not deployed",
                    failed_components=[{"component": "materials",
                                        "reason": "source_unavailable"}]
                ) from exc
            ident = resolved["norm_id"] + (
                "/" + resolved["sub"] if resolved["sub"] else "")
            result = materials_for_norm(data_dir(), ident,
                                        limit_per_relation=50)
            relation = request.query_params.get("relation")
            if relation and relation not in {"explains", "mentions", "drafts"}:
                raise EnvelopeError("invalid_argument",
                                    "relation must be explains, mentions or "
                                    "drafts")
            status = str(result.get("status") or "unavailable")
            if status not in {"ok", "partial"}:
                # A missing or broken materials store is never "no material".
                raise EnvelopeError(
                    status if status in {"unavailable", "invalid_argument",
                                         "integrity_check_failed",
                                         "not_in_corpus"} else "unavailable",
                    "; ".join(result.get("warnings") or [])
                    or "legislative materials unavailable",
                    searched=result.get("searched") or [],
                    failed_components=[{"component": "materials",
                                        "reason": status}])
            groups = {kind: result.get(kind) or []
                      for kind in ("explains", "mentions", "drafts")}
            evidence = [item for kind, rows in groups.items()
                        if relation in (None, kind) for item in rows]
            totals = result.get("totals") or {}
            return envelope(
                status=status, operation="law_materials",
                total=(sum(int(totals.get(kind) or 0) for kind in groups
                           if relation in (None, kind))
                       if totals else len(evidence)),
                truncated=bool(result.get("truncated")),
                relation_totals=totals,
                searched=result.get("searched") or [],
                warnings=result.get("warnings") or [],
                applied_constraints={"canonical_id": ident,
                                     "relation": relation or "all",
                                     "relation_semantics": {
                                         "explains": "fragment whose heading "
                                         "targets this norm (draft numbering; "
                                         "correspondence to the enacted norm "
                                         "unverified)",
                                         "mentions": "any other reference; "
                                         "never an official justification",
                                         "drafts": "the draft wording itself"}},
                evidence=evidence, **groups)
        return run("materials", request, norm_id, handler)

    return router


# ----------------------------------------------------------------- helpers

def _strict(request: Request, operation: str) -> None:
    allowed = _ALLOWED[operation]
    params = request.query_params
    unknown = sorted(set(params) - allowed - CACHE_KEY_ARGUMENTS)
    repeated = sorted(k for k in params if len(params.getlist(k)) > 1)
    if unknown or repeated:
        temporal = {"at", "valid_at", "known_at", "stichtag"} & set(unknown)
        raise EnvelopeError(
            "unsupported_temporal_request" if temporal else
            "invalid_argument",
            f"unsupported argument(s) {', '.join(unknown + repeated)}; "
            f"allowed: {', '.join(sorted(allowed))}")


def _date(value: str | None, name: str, *, required: bool) -> str:
    if not value:
        if required:
            raise EnvelopeError("invalid_argument", f"{name}= is required "
                                "(YYYY-MM-DD)")
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise EnvelopeError("invalid_argument",
                            f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise EnvelopeError("invalid_argument",
                            f"{name} is not a calendar date") from exc


def _as_of(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EnvelopeError("invalid_argument",
                            "as_of must be an RFC3339 instant") from exc
    if parsed.tzinfo is None:
        raise EnvelopeError("invalid_argument",
                            "as_of must include a timezone")
    return parsed.isoformat()


def _int(value: str | None, default: int, low: int, high: int) -> int:
    if value is None:
        return default
    if not re.fullmatch(r"\d+", value) or not low <= int(value) <= high:
        raise EnvelopeError("invalid_argument",
                            f"limit must be an integer {low}–{high}")
    return int(value)


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    if value.casefold() in {"1", "true", "yes"}:
        return True
    if value.casefold() in {"0", "false", "no"}:
        return False
    raise EnvelopeError("invalid_argument", "boolean must be true or false")


def _context(store: NormHistoryStore, norm_id: str, as_of: str | None,
             history: LawHistoryStore | None = None, *, selector: str | None = None,
             resolved: dict | None = None
             ) -> tuple[dict[str, Any], ActChain, list[NormVersion]]:
    resolved = resolved or store.resolve(norm_id)
    act_id = str(resolved["act_id"])
    if history is not None and as_of is None and history.covers(act_id):
        if (selector and re.fullmatch(r"\d{4}-\d{2}-\d{2}", selector)
                and history.has_verified_federal_state(act_id, selector)):
            dated = store.chain(act_id, as_of=as_of)
            if any(state.interval_id and not state.gaps and state.valid_from
                   and state.valid_from_status == "official_verified" and state.valid_from <= selector
                   and (not state.valid_until or selector < state.valid_until)
                   for state in dated.states):
                source_versions = dated.versions_of(resolved["norm_id"])
                if source_versions:
                    return resolved, dated, source_versions
                raise EnvelopeError("not_in_corpus",
                                    f"The complete edition for {selector} contains no {resolved['norm_id']}.",
                                    searched=_searched(dated))
        chain, versions, changes = norm_chain(history, resolved,
                                              resolved["act"])
        chain.change_records = changes  # type: ignore[attr-defined]
        if versions:
            return resolved, chain, versions
        raise EnvelopeError(
            "not_found_in_searched_scope",
            f"{resolved['norm_id']} does not occur in any stored version of "
            f"{chain.act.get('jurabk')}",
            searched=_searched(chain))
    chain = store.chain(act_id, as_of=as_of)
    versions = chain.versions_of(resolved["norm_id"])
    if not versions:
        raise EnvelopeError(
            "not_found_in_searched_scope",
            f"{resolved['norm_id']} does not occur in any stored version of "
            f"{chain.act.get('jurabk')}",
            searched=_searched(chain))
    return resolved, chain, versions


def _pick(versions: list[NormVersion], token: str, chain: ActChain
          ) -> tuple[NormVersion, dict[str, Any]]:
    value = token.strip()
    if value.casefold() == "head":
        return versions[-1], {"status": "ok", "selector": "head",
                              "version_id": versions[-1].version_id}
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        on = _date(value, "date", required=True)
        status, version, res = select_version(versions, on, chain)
        if version is None:
            raise EnvelopeError(
                status if status != "ok" else "not_in_corpus",
                f"no stored version for {on}: {res.get('resolution')}",
                searched=_searched(chain))
        return version, {"status": status, "selector": on,
                         "version_id": version.version_id, **res}
    digest = value.split("#")[-1].casefold()
    matches = [v for v in versions if v.version_id == value or (
        len(digest) >= 8 and v.content_sha256.startswith(digest))]
    if len(matches) != 1:
        raise EnvelopeError(
            "invalid_argument" if not matches else "ambiguous_identity",
            f"selector {token!r} matches {len(matches)} versions")
    return matches[0], {"status": "ok", "selector": "version",
                        "version_id": matches[0].version_id}


def _locator_in_sub(locator: str, sub: str) -> bool:
    return locator == sub or locator.startswith(sub + "/") or \
        sub.startswith(locator + "/") and "/nr/" in sub[len(locator):]


def _op_in_sub(op: dict[str, Any], sub: str) -> bool:
    for side in ("old", "new"):
        loc = op.get(side) or {}
        locator = loc.get("locator") or ""
        if _locator_in_sub(locator, sub):
            return True
        if any(item.get("locator") == sub or item.get("short_locator") == sub
               for item in loc.get("items") or []):
            return True
    return False


def _sub_text(version: NormVersion, sub: str, chain: ActChain) -> str | None:
    structure = parse_norm_structure(version.text, bavarian=chain.bavarian,
                                     annex=version.annex, eu=chain.eu)
    # The marker ("3.", "(2)") is a locator, not wording.
    unit = find_unit(structure, sub)
    return unit["text"] if unit is not None else None


def _collapse_by_sub(versions: list[NormVersion], sub: str,
                     chain: ActChain) -> list[NormVersion]:
    shown: list[NormVersion] = []
    previous: str | None | object = object()
    for version in versions:
        text = _sub_text(version, sub, chain)
        if text != previous:
            shown.append(version)
            previous = text
    return shown


def _stored_act_summary(lh: LawHistoryStore, act_id: str, on: str
                        ) -> dict[str, Any]:
    stored = (lh.acts().get(act_id) or {}).get("act_change_summary")
    if stored and stored.get("as_of") == on:
        return stored
    return act_summary(lh, act_id, on)


def _is_lh(chain: ActChain) -> bool:
    return bool((chain.snapshot or {}).get("law_history"))


def _changes_of(chain: ActChain) -> list[dict[str, Any]] | None:
    return getattr(chain, "change_records", None)


def _state_filter(value: str | None) -> set[str]:
    if not value:
        return set()
    states = {part.strip() for part in value.split(",") if part.strip()}
    unknown = states - CHANGE_STATES
    if unknown:
        raise EnvelopeError(
            "invalid_argument",
            f"unknown state(s) {', '.join(sorted(unknown))}; use "
            f"{', '.join(sorted(CHANGE_STATES))}")
    return states


def _diff_url(norm_id: str, change: dict[str, Any],
              versions: list[NormVersion]) -> str | None:
    effect = change.get("text_effect") or {}
    before, after = effect.get("before_version"), effect.get("after_version")
    if before is None or after is None or not (
            0 <= before < len(versions) and 0 <= after < len(versions)):
        return None
    from urllib.parse import quote
    return (f"/norms/{norm_id}/diff?from="
            f"{quote(versions[before].version_id, safe='')}"
            f"&to={quote(versions[after].version_id, safe='')}")


def _summary(history: LawHistoryStore | None, chain: ActChain,
             resolved: dict[str, Any], versions: list[NormVersion],
             on: str, current: NormVersion | None) -> dict[str, Any] | None:
    if history is None or not _is_lh(chain):
        return None
    return norm_summary(history, str(chain.act.get("id")),
                        resolved["norm_id"], _changes_of(chain) or [],
                        versions, on, current)


def _searched(chain: ActChain) -> list[dict[str, Any]]:
    if chain.snapshot.get("reviewed_norm_editions"):
        return [{"source": "reviewed_norm_editions", "status": "ok", "count": len(chain.states)}]
    if _is_lh(chain):
        info = getattr(chain, "law_history", {}) or {}
        return [
            {"source": "law_history", "status": "ok",
             "count": len(chain.states)},
            {"source": "law_history_changes", "status": "ok",
             "count": len(_changes_of(chain) or [])},
            {"source": "act_states", "status": "ok",
             "count": info.get("states"),
             "by_source": info.get("states_by_source")},
        ]
    if chain.bavarian:
        return [{"source": "bayern_recht_archive", "status": "ok",
                 "count": len(chain.states)}]
    return [
        {"source": "federal_states", "status": "ok",
         "count": len(chain.states)},
        {"source": "retrospective_history",
         "status": ("ok" if chain.retrospective_status in {"ok",
                                                           "not_in_corpus"}
                    else chain.retrospective_status),
         "count": (len(chain.events) if chain.retrospective_status == "ok"
                   else 0 if chain.retrospective_status == "not_in_corpus"
                   else None)},
    ]


def _applied(chain: ActChain, as_of: str | None,
             resolved: dict[str, Any]) -> dict[str, Any]:
    return {"canonical_id": resolved["norm_id"] + (
                "/" + resolved["sub"] if resolved["sub"] else ""),
            "act_id": chain.act.get("id"),
            "known_at": as_of or "latest_published_knowledge",
            "history_schema": HISTORY_SCHEMA}


def _warnings(chain: ActChain) -> list[str]:
    warnings = list(chain.warnings)
    if chain.bavarian and not _is_lh(chain):
        warnings.append(
            "Bavarian history is per norm from archived official pages; it is "
            "incomplete where no archived page exists (missing versions are "
            "not reconstructed)")
    return warnings


def _norm_meta(chain: ActChain, resolved: dict[str, Any],
               versions: list[NormVersion]) -> dict[str, Any]:
    latest = versions[-1]
    return {
        "canonical_id": resolved["norm_id"],
        "sub_unit": resolved["sub"] or None,
        "label": latest.label,
        "title": latest.title,
        "act": chain.act,
        "versions": len(versions),
        "present_in_latest_state": chain.present_in_last_state(
            resolved["norm_id"]),
    }


def _history(chain: ActChain, versions: list[NormVersion]) -> dict[str, Any]:
    observed = [s.first_observed_at for s in chain.states
                if s.first_observed_at]
    verified = [s for s in chain.states if s.valid_from and
                s.valid_from_status != "unresolved"]
    return {
        "text_history_start": min(observed) if observed else None,
        "amendment_event_history_start": chain.history_start,
        "consolidated_versions": len(chain.states),
        "verified_legal_starts": len(verified),
        "note": ("Versions are complete official consolidations (or "
                 "replay-verified reconstructions); a valid_from is shown "
                 "only when verified against the final BGBl command and its "
                 "commencement clause. Amendment events before the text "
                 "history are listed without reconstructed text."
                 if not chain.bavarian else
                 "Versions come from archived official BAYERN.RECHT pages "
                 "and Kerf's daily snapshots; gaps are not filled."),
    }
