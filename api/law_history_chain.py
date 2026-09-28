"""Adapt ``law_history.sqlite`` rows to the norm Git operations.

The log/at/diff/blame operations in ``api.law_routes`` work on
``NormVersion``/``StateVersion`` objects.  For acts covered by the law
history store, one *per-norm* chain is built from indexed rows (no whole-act
chain in memory): every version gets one synthetic state that carries its
legal interval (when evidenced), its observation interval and its source
role.  Change records are exposed both in the new ``changes`` shape and in
the historic ``events`` shape so existing clients keep working.
"""
from __future__ import annotations

from typing import Any

from api.law_history import (
    HistoryVersion,
    LawHistoryStore,
    change_effective_date,
    change_state,
    summarize,
)
from api.norm_history import (
    ActChain,
    NormVersion,
    StateVersion,
    _act_summary,
    bgbl_reference,
    norm_content_hash,
)
from api.norm_identity import act_slug

ROLE_LABEL = {
    "official_consolidation": "official_consolidation",
    "archived_capture": "archived_capture",
    "official_promulgation": "official_promulgation",
    "reconstruction": "reconstruction",
}


def _amendment(record: dict[str, Any], changes: dict[str, dict[str, Any]]
               ) -> dict[str, Any]:
    temporal = record.get("temporal") or {}
    ids = temporal.get("change_ids") or []
    chosen = [changes[i] for i in ids if i in changes]
    if not chosen:
        interval = temporal.get("interval_amendment") or \
            record.get("bavarian_amendment")
        return dict(interval) if interval else {}
    chosen.sort(key=lambda c: change_effective_date(c) or "")
    change = chosen[-1]
    act = change.get("amending_act") or {}
    dates = change.get("dates") or {}
    return {
        "act": act.get("title") or act.get("citation"),
        "citation": act.get("citation"),
        "document_id": act.get("document_id"),
        "reference": (act.get("gazette") or {}).get("reference")
        or bgbl_reference(act.get("document_id")),
        "article": act.get("article"),
        "items": act.get("items"),
        "url": act.get("url"),
        "published_at": (dates.get("promulgated") or {}).get("date"),
        "adopted_at": (dates.get("adopted") or {}).get("date"),
        "effective_at": change_effective_date(change),
        "commencement_basis": (dates.get("in_force") or {}).get("basis"),
        "basis": "law_history_attribution",
        "attribution": (record.get("temporal") or {}).get("attribution"),
        "change_ids": ids,
        "legal_effect_verified": False,
    }


def norm_chain(store: LawHistoryStore, resolved: dict[str, Any],
               act: dict[str, Any]) -> tuple[ActChain, list[NormVersion],
                                             list[dict[str, Any]]]:
    """(chain, versions, change records) for one resolved norm."""
    norm_id = resolved["norm_id"]
    act_id = str(act.get("id"))
    rows: list[HistoryVersion] = store.versions(norm_id)
    changes = store.changes(norm_id=norm_id)
    by_id = {c["change_id"]: c for c in changes}
    bavarian = act_id.startswith("by_")
    states: list[StateVersion] = []
    versions: list[NormVersion] = []
    for row in rows:
        record = row.record or {}
        temporal = record.get("temporal") or {}
        attestations = record.get("attestations") or []
        first_url = next((s.get("url") for a in attestations
                          for s in a.get("samples") or [] if s.get("url")),
                         act.get("source_url"))
        state = StateVersion(
            act_id=act_id, key=f"{norm_id}@v{row.vseq}", index=row.vseq,
            first_observed_at=row.known_from, last_observed_at=row.known_until,
            text_status=("derived_verified" if row.source_role ==
                         "reconstruction" else "official_exact"),
            source_role=row.source_role, source_url=first_url,
            valid_from=temporal.get("valid_from"),
            valid_from_status=str(temporal.get("valid_from_status")
                                  or "unresolved"),
            valid_from_basis=temporal.get("valid_from_basis"),
            valid_until=temporal.get("valid_until"),
            valid_until_status=str(temporal.get("valid_until_status")
                                   or "unresolved"),
            valid_until_basis=temporal.get("valid_until_basis"),
            amendment=_amendment(record, by_id),
            knowledge_from=row.known_from,
            derived=row.source_role == "reconstruction")
        states.append(state)
        norm_row = {"enbez": row.enbez, "titel": row.titel, "text": row.text}
        # verified intervals of consolidated states that carry this wording
        # (the wording may be older than the interval's start)
        carried = [StateVersion(
            act_id=act_id, key=str(item.get("state")),
            index=row.vseq, first_observed_at=row.known_from,
            last_observed_at=row.known_until,
            source_role=row.source_role,
            valid_from=item.get("valid_from"),
            valid_from_status=str(item.get("valid_from_status")
                                  or "official_verified"),
            valid_from_basis=item.get("valid_from_basis"),
            valid_until=item.get("valid_until"),
            valid_until_status=str(item.get("valid_from_status")
                                   or "official_verified"),
            valid_until_basis="retrospective_interval",
            amendment={}, derived=False)
            for item in record.get("state_intervals") or []
            if item.get("valid_from")]
        versions.append(NormVersion(
            norm_id=norm_id, label=row.enbez, title=row.titel,
            text=row.text, content_sha256=norm_content_hash(norm_row),
            first_state=state, last_state=state, states=[state, *carried],
            bavarian=bavarian,
            source_capture={"law_history": {
                "vseq": row.vseq, "identity": row.identity,
                "attestations": attestations,
                "completeness": record.get("completeness"),
                "verification": record.get("verification"),
                "source_role": row.source_role}}))
    for version, following in zip(versions, versions[1:]):
        bound = (following.first_state.first_observed_at or
                 following.first_state.valid_from)
        for state in version.states:
            if state.valid_until is None and bound:
                state.superseded_by_observation = bound
    juris, slug = act_slug(act)
    info = store.acts().get(act_id) or {}
    chain = ActChain(
        act=_act_summary(act), prefix=f"{juris}/{slug}", states=states,
        norms={}, events=[], history_start=info.get("event_history_start"),
        bavarian=bavarian, warnings=[], retrospective_status="ok",
        snapshot={"law_history": True})
    chain.law_history = info  # type: ignore[attr-defined]
    return chain, versions, changes


def events_from_changes(changes: list[dict[str, Any]]
                        ) -> list[dict[str, Any]]:
    """Historic ``events`` shape (newest first) from change records."""
    rows = []
    for change in changes:
        act = change.get("amending_act") or {}
        dates = change.get("dates") or {}
        in_force = dates.get("in_force") or {}
        rows.append({
            "event_id": act.get("event_id") or change.get("change_id"),
            "change_id": change.get("change_id"),
            "document_id": act.get("document_id"),
            "reference": (act.get("gazette") or {}).get("reference"),
            "citation": act.get("citation"),
            "amending_article": act.get("article"),
            "items": act.get("items"),
            "procedure_title": act.get("title"),
            "procedure_id": act.get("procedure_id"),
            "adopted_at": (dates.get("adopted") or {}).get("date"),
            "published_at": (dates.get("promulgated") or {}).get("date"),
            "effective_at": in_force.get("date"),
            "date_status": ("official_verified" if in_force.get("date")
                            else "unknown"),
            "date_basis": in_force.get("basis") or in_force.get("reason"),
            "text_status": "event_only",
            "url": act.get("url"),
            "verification": change.get("verification"),
            "legal_effect_verified": False,
            "change_type": change.get("change_type"),
            "commands": [{**c, "text": " ".join(str(c.get("raw") or "")
                                                .split())}
                         for c in change.get("commands") or []],
        })
    rows.sort(key=lambda row: (str(row.get("effective_at")
                                   or row.get("published_at") or ""),
                               str(row.get("document_id") or "")),
              reverse=True)
    return rows


def enrich_evidence(item: dict[str, Any], version: NormVersion,
                    changes_by_id: dict[str, dict[str, Any]],
                    as_of: str) -> dict[str, Any]:
    """Add the multi-source contract fields to one version evidence item."""
    info = (version.source_capture or {}).get("law_history")
    if not info:
        return item
    role = info.get("source_role") or "official_consolidation"
    attestations = info.get("attestations") or []
    item["source_role"] = role
    item["attestations"] = attestations
    item["completeness"] = info.get("completeness")
    item["verification"] = info.get("verification")
    source = item.get("source") or {}
    source["role"] = role
    source["capture"] = None
    archived = [s for a in attestations for s in a.get("samples") or []
                if s.get("archive_url")]
    if archived:
        source["archive_url"] = archived[0]["archive_url"]
    item["source"] = source
    temporal = item.get("temporal") or {}
    temporal["known_at"] = version.first_state.first_observed_at
    temporal["known_at_basis"] = (
        "first capture or retrieval in which any source showed this exact "
        "wording (not a legal date)")
    item["temporal"] = temporal
    quality = item.get("quality") or {}
    quality["source_exact"] = role != "reconstruction"
    quality["complete"] = (info.get("completeness") or {}).get(
        "status") in {None, "complete"}
    quality["reconstruction_status"] = ("reconstruction" if role ==
                                        "reconstruction" else
                                        "not_reconstructed")
    item["quality"] = quality
    ids = ((version.first_state.amendment or {}).get("change_ids") or [])
    change = next((changes_by_id[i] for i in reversed(ids)
                   if i in changes_by_id), None)
    item["change"] = compact(change, as_of) if change else None
    return item


def compact(change: dict[str, Any], as_of: str) -> dict[str, Any]:
    from api.law_history import compact_change
    return compact_change(change, as_of)


def history_block(chain: ActChain, versions: list[NormVersion]
                  ) -> dict[str, Any]:
    info = getattr(chain, "law_history", {}) or {}
    roles: dict[str, int] = {}
    for version in versions:
        role = (version.source_capture or {}).get("law_history", {}).get(
            "source_role", "unknown")
        roles[role] = roles.get(role, 0) + 1
    known = [v.first_state.first_observed_at for v in versions
             if v.first_state.first_observed_at]
    return {
        "text_history_start": min(known) if known else None,
        "act_text_history_start": info.get("earliest_text"),
        "amendment_event_history_start": info.get("event_history_start"),
        "consolidated_versions": info.get("states"),
        "act_states_by_source": info.get("states_by_source"),
        "norm_versions": len(versions),
        "versions_by_role": roles,
        "verified_legal_starts": sum(
            1 for v in versions if v.first_state.valid_from and
            v.first_state.valid_from_status != "unresolved"),
        "note": ("Versions merge official consolidations (GII daily states, "
                 "NeuRIS), archived copies of official pages (known_at = "
                 "capture date) and labelled reconstructions, de-duplicated "
                 "by exact wording. A legal start (valid_from) is set only "
                 "when the text change is attributed to an amendment whose "
                 "in-force date is evidenced by its commencement clause."),
    }


def norm_summary(store: LawHistoryStore, act_id: str, norm_id: str,
                 changes: list[dict[str, Any]], versions: list[NormVersion],
                 as_of: str, current: NormVersion | None = None
                 ) -> dict[str, Any]:
    latest = store.latest_state(act_id) or {}
    notes = latest.get("notes") or {}
    info = store.acts().get(act_id) or {}
    role = None
    if current is not None:
        role = (current.source_capture or {}).get("law_history", {}).get(
            "source_role")
    return summarize(
        changes, scope="norm", subject=norm_id, as_of=as_of,
        versions=[{"source_role": (v.source_capture or {}).get(
            "law_history", {}).get("source_role")} for v in versions],
        version_role=role, stand_notes=notes,
        notes_observed_at=latest.get("known_until") or latest.get(
            "known_from"),
        completeness=_completeness(store, act_id, norm_id, info, versions))


def act_summary(store: LawHistoryStore, act_id: str, as_of: str
                ) -> dict[str, Any]:
    latest = store.latest_state(act_id) or {}
    info = store.acts().get(act_id) or {}
    changes = store.changes(act_id=act_id)
    summary = summarize(
        changes, scope="act", subject=act_id, as_of=as_of,
        stand_notes=latest.get("notes") or {},
        notes_observed_at=latest.get("known_until") or latest.get(
            "known_from"),
        completeness=_completeness(store, act_id, None, info, None))
    summary["counts"]["versions"] = info.get("norm_versions")
    summary["counts"]["act_states"] = info.get("states")
    summary["counts"]["act_states_by_source"] = info.get("states_by_source")
    return summary


def _completeness(store: LawHistoryStore, act_id: str, norm_id: str | None,
                  info: dict[str, Any], versions: list[NormVersion] | None
                  ) -> dict[str, Any]:
    findings = store.findings(norm_id=norm_id) if norm_id else []
    kinds: dict[str, int] = {}
    for finding in findings:
        kinds[finding["kind"]] = kinds.get(finding["kind"], 0) + 1
    unattributed = kinds.get("unattributed_text_change", 0)
    act_gaps = (info.get("findings") or {})
    missing_docs = act_gaps.get("stand_amending_act_not_in_event_store", 0)
    return {
        "event_history": ("partial" if unattributed or missing_docs
                          else "complete_for_observed_text_changes"),
        "event_history_start": info.get("event_history_start"),
        "text_history_start": (min((v.first_state.first_observed_at
                                    for v in versions
                                    if v.first_state.first_observed_at),
                                   default=None) if versions else
                               info.get("earliest_text")),
        "gaps": ([{"kind": kind, "count": count}
                  for kind, count in sorted(kinds.items())] +
                 ([{"kind": "stand_amending_act_not_in_event_store",
                    "count": missing_docs, "scope": "act"}]
                  if missing_docs else [])),
    }


def brief(change: dict[str, Any] | None) -> dict[str, Any] | None:
    if not change:
        return None
    dates = change.get("dates") or {}
    act = change.get("amending_act") or {}
    return {"change_id": change.get("change_id"),
            "norm_label": change.get("norm_label"),
            "citation": act.get("citation"),
            "document_id": act.get("document_id"),
            "gazette": (act.get("gazette") or {}).get("reference"),
            "title": act.get("title"),
            "in_force": (dates.get("in_force") or {}).get("date"),
            "promulgated": (dates.get("promulgated") or {}).get("date"),
            "adopted": (dates.get("adopted") or {}).get("date"),
            "state": change.get("state")}


def index_entry(full: dict[str, Any]) -> dict[str, Any]:
    """Compact act summary for the /acts index (landing tiles)."""
    return {
        "schema": full["schema"], "scope": "act", "as_of": full["as_of"],
        "last_change": brief(full.get("last_change")),
        "next_pending_change": brief(full.get("next_pending_change")),
        "counts": {k: full["counts"].get(k) for k in (
            "versions", "act_states", "events", "amending_acts",
            "events_pending", "events_date_unknown")},
        "documentation_notes": len(full.get("documentation_notes") or []),
    }


__all__ = ["norm_chain", "events_from_changes", "enrich_evidence",
           "history_block", "norm_summary", "act_summary", "change_state"]
