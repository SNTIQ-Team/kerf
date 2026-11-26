"""Publish reviewed historical federal editions as ordinary Kerf state objects.

The reviewed German Basic Law chain pins the complete GII anchor, both earlier
projections, the preceding text of each changed article and the source documents.
Its full-edition comparison is reproducible with tools/audit_gg_editions.py.
"""
from __future__ import annotations

import copy
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any

from official_states import canonical_json_bytes
from verified_reconstruction import ReconstructionError

GG_REVIEW = Path(__file__).resolve().parents[1] / "data" / "gg_edition_review.json"


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _inverse(later: dict, earlier_edition: dict, later_edition: dict) -> dict:
    earlier = copy.deepcopy(later)
    restored = []
    labels = set()
    for edit in later_edition["changes_from_previous"]:
        label = edit["enbez"]
        positions = [i for i, row in enumerate(earlier["norms"])
                     if row["enbez"] == label]
        if len(positions) != 1 or label in labels:
            raise ReconstructionError(f"non-unique edition article {label}")
        labels.add(label)
        index = positions[0]
        current = earlier["norms"][index]
        if digest(current) != edit["current_norm_sha256"]:
            raise ReconstructionError(f"edition article digest mismatch for {label}")
        restored.append((index, copy.deepcopy(current), edit["previous_text"] is None))
        if edit["previous_text"] is None:
            if (index == 0 or index + 1 == len(earlier["norms"])
                    or earlier["norms"][index - 1]["enbez"] != edit["after"]
                    or earlier["norms"][index + 1]["enbez"] != edit["before"]):
                raise ReconstructionError(f"unexpected insertion position for {label}")
            earlier["norms"].pop(index)
        elif isinstance(edit["previous_text"], str) and edit["previous_text"].strip():
            current["text"] = edit["previous_text"]
        else:
            raise ReconstructionError(f"missing reviewed prior wording for {label}")
    earlier.update(norm_count=len(earlier["norms"]), stand=earlier_edition["projection_stand"],
                   build=earlier_edition["projection_build"])
    if digest(earlier) != earlier_edition["state_sha256"]:
        raise ReconstructionError("historical edition does not reproduce its reviewed state hash")

    forward = copy.deepcopy(earlier)
    for index, row, inserted in reversed(restored):
        if inserted:
            forward["norms"].insert(index, row)
        else:
            forward["norms"][index] = row
    forward.update(norm_count=len(forward["norms"]), stand=later_edition["projection_stand"],
                   build=later_edition["projection_build"])
    if forward != later or digest(forward) != later_edition["state_sha256"]:
        raise ReconstructionError("forward edition replay disagrees with its successor")
    return earlier


def _event(edition: dict, sources: dict) -> dict:
    gazette = sources[edition["gazette"]]
    return {
        "document_id": edition["document_id"], "published_at": edition["published_at"],
        "effective_at": edition["effective_from"], "official_html_url": gazette["url"],
        "pdf_sha256": gazette["pdf_sha256"], "amending_article": "1",
    }


def add_reviewed_editions(artifact: dict, manifest: dict, states: dict, *,
                          review_path: Path = GG_REVIEW) -> None:
    """Append complete, consecutively reviewed intervals to a reconstruction build."""
    review = json.loads(review_path.read_text(encoding="utf-8"))
    if review["schema_version"] != 1:
        raise ReconstructionError("unknown federal edition review schema")
    act_id = review["act_id"]
    observations = [o for o in manifest.get("observations", []) if o["act_id"] == act_id]
    if not observations or max(o["observed_at"] for o in observations) < review["reviewed_on"]:
        return
    anchor = review["anchor"]
    anchor_hash = anchor["state_sha256"]
    # Only an exact capture of the reviewed projection can anchor the history.
    # Later observations of a different text do not extend its legal-time ceiling.
    matching = [o for o in observations if o.get("verification") == "exact"
                and o.get("date_basis") == "retrieval_observation_not_effective_date"
                and o.get("jurabk") == review["jurabk"]
                and all(o.get(k) == v for k, v in anchor.items())]
    if not matching or anchor_hash not in states:
        raise ReconstructionError(f"missing reviewed edition anchor for {act_id}")
    at_or_after_review = [o["observed_at"] for o in matching if o["observed_at"] >= review["reviewed_on"]]
    observed = min(at_or_after_review) if at_or_after_review else max(o["observed_at"] for o in matching)
    # A return to an identical export after another observed state does not
    # prove uninterrupted legal validity across the intervening change.
    changes = [o["observed_at"] for o in observations
               if o["observed_at"] >= observed and o["state_sha256"] != anchor_hash]
    first_change = min(changes, default=None)
    if first_change == observed:
        raise ReconstructionError("contradictory edition anchors on the same observation day")
    ceiling = max(o["observed_at"] for o in matching
                  if first_change is None or o["observed_at"] < first_change)
    editions = review["editions"]
    if len(editions) < 2 or editions[0]["changes_from_previous"]:
        raise ReconstructionError("invalid initial edition")
    boundaries = [date.fromisoformat(e["effective_from"]) for e in editions]
    if any(a >= b for a, b in zip(boundaries, boundaries[1:])):
        raise ReconstructionError("edition dates must strictly increase")
    # Each of the reviewed promulgating acts expressly uses the day after publication.
    if any(date.fromisoformat(e["published_at"]) + timedelta(days=1) != d
           for e, d in zip(editions, boundaries)):
        raise ReconstructionError("edition boundary disagrees with its commencement clause")
    state = states[anchor_hash]
    if (state["id"] != act_id or state["jurabk"] != review["jurabk"]
            or digest(state) != anchor_hash or editions[-1]["state_sha256"] != anchor_hash):
        raise ReconstructionError("edition anchor identity or digest mismatch")

    evidence = [{"source": "GII", "url": anchor["source_url"],
                 "state_sha256": anchor_hash, "observed_at": observed}]
    evidence.extend({k: s[k] for k in ("source", "url", "pdf_sha256")}
                    for s in review["sources"].values())
    pending_states = {}
    pending_meta = {}
    pending_rows = []
    for i in range(len(editions) - 2, -1, -1):
        edition, following = editions[i:i + 2]
        state = _inverse(state, edition, following)
        state_hash = edition["state_sha256"]
        pending_states[state_hash] = state
        pending_meta[state_hash] = {
            "state_sha256": state_hash, "canonical_bytes": len(canonical_json_bytes(state)),
            "origin": "reviewed_federal_edition_replay", "anchor_state_sha256": anchor_hash,
            "source_exact": False,
        }
        row = {
            "id": f"federal-edition:{act_id}:{edition['effective_from']}",
            "schema_version": 1, "act_id": act_id, "jurabk": review["jurabk"],
            "state_sha256": state_hash, "anchor_state_sha256": anchor_hash,
            "text_status": "derived_verified", "body_complete": True, "source_exact": False,
            "reverse_replay_verified": True, "date_status": "official_verified",
            "date_basis": "promulgating_gazettes_and_bundestag_amendment_sequence",
            "verification": "complete_federal_editions_and_canonical_state_replay",
            "effective_from": edition["effective_from"], "effective_to": following["effective_from"],
            "published_at": edition["published_at"], "observed_at": observed,
            "knowledge_from": artifact["built_at"], "knowledge_to": None, "retroactive": False,
            "incoming_event": _event(edition, review["sources"]),
            "outgoing_event": _event(following, review["sources"]),
            "changes_reversed": [{"norm": e["enbez"]} for e in following["changes_from_previous"]],
            "evidence": evidence, "gaps": [],
        }
        if following["state_sha256"] != anchor_hash:
            row["successor_state_sha256"] = following["state_sha256"]
        else:
            row["anchor_verified_through"] = ceiling
        pending_rows.append(row)
    artifact["state_objects"].update(pending_states)
    artifact["object_metadata"].update(pending_meta)
    artifact["reconstructions"].extend(reversed(pending_rows))
