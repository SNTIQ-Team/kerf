"""relations.detail without derivable keys (schema v4).

A key is dropped only when its value equals what the rule below derives from the row's own columns, so
:func:`expand_detail` restores the v3 JSON exactly.  Readers that need a key missing from ``detail`` apply
the same rule (SCHEMA.md, "relations.detail").

Norm citations (``target_kind = 'norm'``):

==================  ===============================================================================
``act_prefix``      ``target_id`` up to (excluding) its first ``/para/`` or ``/art/``; NULL when
                    ``target_id`` is NULL
``unit_type``       ``art`` if ``target_id`` contains ``/art/`` (unresolved: ``target_raw`` starts
                    with "Art"), else ``para``
``act_resolution``  ``act_index`` when ``target_id`` is set, ``unresolved`` when it is NULL
``lexgraph_act_id`` ``fed_<slug>`` for ``act_prefix`` ``de/bund/<slug>``, else NULL
``chain``           ``target_raw``
``chain_no``        1
==================  ===============================================================================

Decision citations: ``court_id`` = ``target_court_id``, ``date`` = ``target_date``, ``case_key`` =
``target_case_key`` - only when that column is not NULL (an ECLI-only citation has none of the three keys).
"""
from __future__ import annotations

import json

_MISSING = object()


def _act_prefix(target_id: str | None) -> str | None:
    if not target_id:
        return None
    cut = [target_id.index(u) for u in ("/para/", "/art/") if u in target_id]
    return target_id[:min(cut)] if cut else target_id


def derive(key: str, row) -> object:
    """The value a missing ``key`` stands for; ``_MISSING`` if the key has no rule for this row."""
    kind, target_id = row["target_kind"], row["target_id"]
    if kind == "norm":
        if key == "act_prefix":
            return _act_prefix(target_id)
        if key == "unit_type":
            if target_id:
                return "art" if "/art/" in target_id else "para"
            return "art" if (row["target_raw"] or "").lstrip().startswith("Art") else "para"
        if key == "act_resolution":
            return "act_index" if target_id else "unresolved"
        if key == "lexgraph_act_id":
            prefix = _act_prefix(target_id)
            return f"fed_{prefix.split('/')[2]}" if prefix and prefix.startswith("de/bund/") else None
        if key == "chain":
            return row["target_raw"]
        if key == "chain_no":
            return 1
    elif kind == "decision":
        if key == "court_id":
            return row["target_court_id"]
        if key == "date":
            return row["target_date"]
        if key == "case_key":
            return row["target_case_key"]
    return _MISSING


RULE_KEYS = {"norm": ("act_prefix", "unit_type", "act_resolution", "lexgraph_act_id", "chain", "chain_no"),
             "decision": ("court_id", "date", "case_key")}


def _restores(key: str, row) -> object:
    value = derive(key, row)
    if row["target_kind"] == "decision" and value is None:
        return _MISSING  # decision keys are restored only from a non-NULL column
    return value


def trim_detail(detail: dict, row) -> str:
    """The v4 ``detail`` JSON: derivable keys dropped, compact separators, sorted keys."""
    kept = {k: v for k, v in detail.items() if _restores(k, row) is _MISSING or _restores(k, row) != v}
    return json.dumps(kept, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def expand_detail(detail: str | dict, row) -> dict:
    """The full (v3) detail: the stored keys plus every derivable key the rules restore."""
    out = json.loads(detail) if isinstance(detail, str) else dict(detail)
    for key in RULE_KEYS.get(row["target_kind"], ()):
        if key not in out:
            value = _restores(key, row)
            if value is not _MISSING:
                out[key] = value
    return out
