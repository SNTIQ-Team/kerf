"""Norm-level Git semantics: log, at, diff, blame (evidence envelopes).

    GET /norms/resolve?q=                    citation → canonical identity
    GET /norms/{id}/log                      versions + amendment evidence
    GET /norms/{id}/at?date=YYYY-MM-DD       the version valid on a date
    GET /norms/{id}/diff?from=&to=           sentence-aware diff
    GET /norms/{id}/blame[?date=]            per sentence: introducing version

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

from api.evidence import EnvelopeError, envelope, error_envelope, response
from api.law_structure import (
    STRUCTURE_PARSER_VERSION,
    human_locator,
    iter_units,
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
from api.norm_identity import load_gii_toc

BUNDLED_TOC = Path(__file__).resolve().parent / "data" / "gii_toc.json"
_ALLOWED = {
    "log": {"as_of", "limit"},
    "at": {"date", "as_of", "structure"},
    "diff": {"from", "to", "as_of"},
    "blame": {"date", "as_of"},
    "resolve": {"q", "act", "norm"},
    "materials": {"relation"},
}


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
            resolved, chain, versions = _context(store(), norm_id, as_of)
            sub = resolved["sub"]
            shown = _collapse_by_sub(versions, sub, chain) if sub else versions
            unit, number = resolved["norm_id"].split("/")[-2:]
            events = amendment_events(chain, unit, number)
            evidence = [version_evidence(chain, v, toc(), include_text=False)
                        for v in reversed(shown)][:limit]
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
                history=_history(chain, versions))
        return run("log", request, norm_id, handler)

    @router.get("/norms/{norm_id:path}/at")
    def norm_at(norm_id: str, request: Request):
        def handler():
            params = request.query_params
            on = _date(params.get("date"), "date", required=True)
            as_of = _as_of(params.get("as_of"))
            structure = _bool(params.get("structure"), True)
            resolved, chain, versions = _context(store(), norm_id, as_of)
            status, version, resolution = select_version(versions, on, chain)
            warnings = _warnings(chain)
            if resolution.get("note"):
                warnings.append(resolution["note"])
            if on > today_iso():
                warnings.append(
                    "the requested date is in the future; later amendments "
                    "not yet recorded may change the answer")
            if version is None and status == "not_in_corpus":
                raise EnvelopeError(
                    "not_in_corpus",
                    f"Lexgraph holds no text of {resolved['norm_id']} for "
                    f"{on}; its text history starts "
                    f"{resolution.get('text_history_start')}",
                    searched=_searched(chain))
            evidence = []
            if version is not None:
                item = version_evidence(chain, version, toc(),
                                        include_structure=structure)
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
                resolution=resolution)
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
            resolved, chain, versions = _context(store(), norm_id, as_of)
            left, left_res = _pick(versions, params["from"], chain)
            right, right_res = _pick(versions, params["to"], chain)
            statuses = {left_res["status"], right_res["status"]}
            status = "ok" if statuses == {"ok"} else "temporally_unresolved"
            ops = sentence_diff(left.text, right.text,
                                bavarian=chain.bavarian)
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
                evidence=[version_evidence(chain, left, toc(),
                                           include_text=False),
                          version_evidence(chain, right, toc(),
                                           include_text=False)],
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
            resolved, chain, versions = _context(store(), norm_id, as_of)
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
            events = amendment_events(chain, unit, number)
            lines = blame(versions, target, events, bavarian=chain.bavarian)
            if resolved["sub"]:
                lines = [row for row in lines
                         if _locator_in_sub(row["locator"], resolved["sub"])]
            attributed = sum(1 for row in lines
                             if row["status"] == "introduced_within_history")
            warnings = _warnings(chain)
            if lines and attributed < len(lines):
                warnings.append(
                    f"{len(lines) - attributed} of {len(lines)} sentences "
                    "predate Lexgraph's text history for this norm; their "
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
            result = materials_for_norm(data_dir(), ident)
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
            return envelope(
                status=status, operation="law_materials",
                total=len(evidence),
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
    unknown = sorted(set(params) - allowed)
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


def _context(store: NormHistoryStore, norm_id: str, as_of: str | None
             ) -> tuple[dict[str, Any], ActChain, list[NormVersion]]:
    resolved = store.resolve(norm_id)
    chain = store.chain(str(resolved["act_id"]), as_of=as_of)
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
    structure = parse_norm_structure(version.text, bavarian=chain.bavarian)
    for unit in iter_units(structure):
        short = re.sub(r"/satz/\d+(?=/nr/)", "", unit["locator"])
        if unit["locator"] == sub or (unit["type"] in {
                "nummer", "buchstabe", "doppelbuchstabe"} and short == sub):
            # The marker ("3.", "(2)") is a locator, not wording.
            return unit["text"]
    return None


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


def _searched(chain: ActChain) -> list[dict[str, Any]]:
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
    if chain.bavarian:
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
                 "and Lexgraph's daily snapshots; gaps are not filled."),
    }
