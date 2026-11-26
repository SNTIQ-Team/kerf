"""Read-only resolver for published content-addressed official states.

The pipeline hashes the canonical, uncompressed JSON bytes and publishes the
same bytes as deterministic gzip objects.  The API verifies that digest again
before trusting a historical body.  An observation is resolved only for its
exact retrieval date: choosing the latest earlier observation would silently
turn a crawl timestamp into a legal point-in-time assertion.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from collections.abc import Iterable
from typing import Any


class OfficialStateError(ValueError):
    """A published observation/object pair failed its integrity contract."""


_DIGEST = re.compile(r"[0-9a-f]{64}")


@lru_cache(maxsize=1)
def _projection_review() -> tuple[frozenset[str], str, str]:
    path = Path(__file__).parent / "data" / "gii_projection_review.json"
    review = json.loads(path.read_bytes())
    return (frozenset(row["sha256"] for row in review["legacy_projections"]),
            review["preceding_projection_sha256"], review["corrected_projection_sha256"])


def incomplete_projection(row: dict[str, Any]) -> bool:
    """An immutable digest identifies both the act and its captured norm inventory."""
    return (row.get("state_sha256") or row.get("state_digest")) in _projection_review()[0]


def with_projection_coverage(row: dict[str, Any]) -> dict[str, Any]:
    if incomplete_projection(row):
        return {**row, "projection_status": "partial", "body_complete": False}
    return row


def _same_source_corrections(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """When both parses of the identical source were recorded the same day, select the complete one."""
    _, earlier, corrected = _projection_review()
    dates = {(r.get("act_id"), r.get("observed_at") or r.get("date"))
             for r in rows if (r.get("state_sha256") or r.get("state_digest")) == corrected}
    return [r for r in rows if (r.get("state_sha256") or r.get("state_digest")) != earlier
            or (r.get("act_id"), r.get("observed_at") or r.get("date")) not in dates]


def unordered_observation_days(
        observations: Iterable[dict[str, Any]]) -> set[tuple[str | None, str]]:
    """Identify act/date pairs with distinct complete source states and no retrieval order."""
    first = {}
    ambiguous = set()
    for row in _same_source_corrections([r for r in observations if isinstance(r, dict)]):
        if row.get("source_store") == "bavarian_states":
            continue
        day = str(row.get("observed_at") or row.get("date") or "")[:10]
        digest = str(row.get("state_sha256") or row.get("state_digest") or "")
        if not day or not _DIGEST.fullmatch(digest):
            continue
        key = (row.get("act_id"), day)
        if first.setdefault(key, digest) != digest:
            ambiguous.add(key)
    return ambiguous


def single_state_observations(
        observations: Iterable[dict[str, Any]], *, allow_partial: bool = False) -> list[dict[str, Any]]:
    """Select unambiguous observations. A partial capture proves only its retained text."""
    rows = _same_source_corrections([r for r in observations if isinstance(r, dict)])
    ambiguous = unordered_observation_days(rows)
    return [row for row in rows if (allow_partial or not incomplete_projection(row)) and
            (row.get("act_id"), str(row.get("observed_at") or row.get("date") or "")[:10]) not in ambiguous]


def observation_on(act: dict[str, Any], requested_at: str | None
                   ) -> dict[str, Any] | None:
    """Return the one exact official retrieval observation for a date."""
    if not requested_at:
        return None
    matches = []
    for row in act.get("official_states") or []:
        if not isinstance(row, dict):
            continue
        observed = str(row.get("observed_at") or row.get("date") or "")[:10]
        digest = str(row.get("state_sha256") or row.get("state_digest") or "")
        if observed == requested_at and _DIGEST.fullmatch(digest):
            matches.append({**row, "observed_at": observed,
                            "state_sha256": digest})
    matches = single_state_observations(matches, allow_partial=True)
    if not matches:
        return None
    return max(matches, key=lambda row: (
        str(row.get("builddate") or ""), row["state_sha256"]))


@lru_cache(maxsize=256)
def _read_object(path_string: str, digest: str) -> dict[str, Any]:
    path = Path(path_string)
    try:
        with gzip.open(path, "rb") as handle:
            payload = handle.read()
    except (OSError, EOFError) as exc:
        raise OfficialStateError(f"cannot read official state object {digest}") \
            from exc
    actual = hashlib.sha256(payload).hexdigest()
    if actual != digest:
        raise OfficialStateError(
            f"official state object hash mismatch: expected {digest}, got {actual}")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OfficialStateError(
            f"official state object {digest} is not canonical JSON") from exc
    if not isinstance(value, dict) or not isinstance(value.get("norms"), list):
        raise OfficialStateError(
            f"official state object {digest} has no norm list")
    return value


def load_observed_state(data_dir: Path, act: dict[str, Any],
                        requested_at: str | None
                        ) -> dict[str, Any] | None:
    """Load and verify a complete state captured on exactly requested_at."""
    observation = observation_on(act, requested_at)
    if observation is None:
        return None
    digest = observation["state_sha256"]
    store = observation.get("source_store") or "federal_states"
    providers = {"federal_states": "GII", "bavarian_states": "BAYERN.RECHT"}
    if not isinstance(store, str) or store not in providers or (observation.get("source") or "GII") != providers[store]:
        raise OfficialStateError("unapproved or inconsistent official source store")
    path = (data_dir / store / "objects" / "sha256"
            / digest[:2] / f"{digest}.json.gz")
    if not path.is_file():
        raise OfficialStateError(
            f"official state object is missing for observation {requested_at}")
    state = _read_object(str(path), digest)
    expected_act_id = str(act.get("id") or "")
    expected_jurabk = str(act.get("jurabk") or "")
    object_act_id = state.get("act_id") or state.get("id")
    if object_act_id != expected_act_id or str(
            state.get("jurabk") or "") != expected_jurabk:
        raise OfficialStateError(
            f"official state object {digest} belongs to another act")
    expected_count = observation.get("norm_count")
    if expected_count is not None and len(state["norms"]) != int(expected_count):
        raise OfficialStateError(
            f"official state object {digest} has an unexpected norm count")
    # Provenance can describe the CAS body, never replace its verified text,
    # heading, or identity with unhashed observation metadata.
    metadata_keys = {
        "observed_at", "date", "state_sha256", "state_digest", "norm_count",
        "builddate", "source", "source_store", "source_url", "original_url",
        "archive_url", "archive_timestamp", "captured_at", "source_html_sha256",
        "date_basis", "verification", "retrieved_at",
    }
    return {
        **state,
        **{key: value for key, value in observation.items() if key in metadata_keys},
        "source": observation.get("source") or "GII",
        "date_basis": observation.get("date_basis")
        or "retrieval_observation_not_effective_date",
        "verification": observation.get("verification") or "exact",
    }


def load_state_digest(state_root: Path, digest: str, *,
                      act_id: str | None = None,
                      jurabk: str | None = None) -> dict[str, Any]:
    """Load one immutable state by digest for bitemporal history queries.

    ``state_root`` is the directory containing ``objects/sha256``.  Unlike
    :func:`load_observed_state`, this lookup is anchored by a separately
    validated legal interval rather than by a retrieval-day observation.
    Optional act identities prevent a valid object from being attached to the
    wrong retrospective interval.
    """
    if not _DIGEST.fullmatch(str(digest or "")):
        raise OfficialStateError("official state digest is invalid")
    path = (Path(state_root) / "objects" / "sha256" / digest[:2]
            / f"{digest}.json.gz")
    if not path.is_file():
        raise OfficialStateError(
            f"official state object is missing for digest {digest}")
    state = _read_object(str(path), digest)
    object_act_id = str(state.get("act_id") or state.get("id") or "")
    object_jurabk = str(state.get("jurabk") or "")
    if act_id is not None and object_act_id != act_id:
        raise OfficialStateError(
            f"official state object {digest} belongs to another act")
    if jurabk is not None and object_jurabk != jurabk:
        raise OfficialStateError(
            f"official state object {digest} has another jurabk")
    return state


def clear_cache() -> None:
    """Test/deployment hook; generations are otherwise immutable."""
    _read_object.cache_clear()
