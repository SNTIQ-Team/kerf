"""Publish reviewed historical federal editions as ordinary Kerf state objects.

The reviewed German Basic Law chain pins the complete GII anchor, earlier
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
        if label in labels:
            raise ReconstructionError(f"duplicate edition change {label}")
        labels.add(label)
        if "previous_norm" in edit:
            row = edit["previous_norm"]
            left = [i for i, norm in enumerate(earlier["norms"]) if norm["enbez"] == edit["after"]]
            if (positions or len(left) != 1 or left[0] + 1 >= len(earlier["norms"])
                    or earlier["norms"][left[0] + 1]["enbez"] != edit["before"]
                    or row.get("enbez") != label or not isinstance(row.get("text"), str)
                    or not row["text"].strip()):
                raise ReconstructionError(f"invalid prior article or position for {label}")
            index = left[0] + 1
            earlier["norms"].insert(index, copy.deepcopy(row))
            restored.append((index, None, False))
            continue
        if len(positions) != 1:
            raise ReconstructionError(f"non-unique edition article {label}")
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
        if row is None:
            forward["norms"].pop(index)
        elif inserted:
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
    event = {
        "document_id": edition["document_id"], "published_at": edition["published_at"],
        "effective_at": edition["effective_from"], "official_html_url": gazette["url"],
        "pdf_sha256": gazette["pdf_sha256"], "amending_article": edition.get("amending_article", "1"),
    }
    if edition.get("companion_gazettes"):
        event["companion_gazettes"] = [
            {"official_html_url": sources[key]["url"], "pdf_sha256": sources[key]["pdf_sha256"]}
            for key in edition["companion_gazettes"]]
    return event


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
    for edition, boundary in zip(editions, boundaries):
        published = date.fromisoformat(edition["published_at"])
        if "expiry_last_day" in edition:
            final_day = date.fromisoformat(edition["expiry_last_day"])
            if published > final_day or edition.get("amending_article") != "2":
                raise ReconstructionError("invalid expiry provision")
        else:
            final_day = published
        if final_day + timedelta(days=1) != boundary:
            raise ReconstructionError("edition boundary disagrees with its published clause")
    state = states[anchor_hash]
    if (state["id"] != act_id or state["jurabk"] != review["jurabk"]
            or digest(state) != anchor_hash or editions[-1]["state_sha256"] != anchor_hash):
        raise ReconstructionError("edition anchor identity or digest mismatch")

    evidence = [{"source": "GII", "url": anchor["source_url"],
                 "state_sha256": anchor_hash, "observed_at": observed}]
    evidence.extend({k: s[k] for k in ("source", "url", "pdf_sha256")}
                    for s in review["sources"].values())
    evidence.extend({k: source[k] for k in ("source", "url", "html_sha256")}
                    for source in review.get("expiry_confirmations", {}).values())
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


def validate_published_editions(root: Path, manifest: dict, artifact: dict) -> set[str]:
    """Replay each edition in the upload against the installed review and its GII anchor."""
    from official_states import load_state_verified
    metadata = artifact["object_metadata"]
    supplied = {key for key, value in metadata.items() if isinstance(value, dict)
                and value.get("origin") == "reviewed_federal_edition_replay"}
    rows = [r for r in artifact["reconstructions"] if isinstance(r, dict)
            and (r.get("state_sha256") in supplied or str(r.get("id") or "").startswith("federal-edition:"))]
    if not supplied and not rows:
        return set()
    review = json.loads(GG_REVIEW.read_bytes())
    anchor = review["anchor"]["state_sha256"]
    if anchor not in manifest["objects"]:
        raise ReconstructionError("federal edition anchor missing from the official ledger")
    state = load_state_verified(root / "federal_states", anchor)
    replay = {"built_at": artifact["built_at"], "state_objects": {},
              "object_metadata": {}, "reconstructions": []}
    add_reviewed_editions(replay, manifest, {anchor: state})
    expected = {r["state_sha256"]: r for r in replay["reconstructions"]}
    if supplied != set(expected):
        # Older complete review packages have their own fixed boundaries and
        # evidence lists. An arbitrary suffix is insufficient for publication.
        original = {"2022-2478", "2024-439", "2025-94", "21843", "23183",
                    "bundestag-amendments-since-1990"}
        extended = original | {"17609", "gazette-61", "gazette-62", "gazette-63",
                               "gazette-64", "gazette-65", "gazette-66"}
        for start, count, sources, expiry in [
                ("2022-12-24", 2, original, False), ("2017-07-20", 8, extended, True)]:
            older = {key: row for key, row in expected.items() if row["effective_from"] >= start}
            if len(older) == count and supplied == set(older):
                expected = older
                urls = {review["sources"][key]["url"] for key in sources}
                if expiry:
                    urls.update(s["url"] for s in review["expiry_confirmations"].values())
                break
        else:
            raise ReconstructionError("incomplete or unknown federal edition chain")
        for r in expected.values():
            r["evidence"] = [s for s in r["evidence"] if s["source"] == "GII" or s["url"] in urls]
    by_digest = {r.get("state_sha256"): r for r in rows}
    if len(rows) != len(expected) or by_digest != expected:
        raise ReconstructionError("federal edition dates, events or source evidence differ from replay")
    for key in supplied:
        if any(metadata[key].get(k) != v for k, v in replay["object_metadata"][key].items()):
            raise ReconstructionError("federal edition object metadata differs from replay")
    return supplied
