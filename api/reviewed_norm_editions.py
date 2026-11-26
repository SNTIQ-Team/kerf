"""Validated editions of one provision, independently bounded by primary legal sources."""
from __future__ import annotations

from datetime import date, datetime
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit


FILENAME = "reviewed_norm_editions.json"
BASIS = "reviewed_norm_edition"
DIGEST = re.compile(r"[0-9a-f]{64}")


class ReviewedEditionError(ValueError):
    pass


def _day(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("invalid legal date")
    return date.fromisoformat(value).isoformat()


def _instant(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("knowledge timestamp requires a timezone")
    return instant


def validate(package: dict) -> dict:
    from api.norm_identity import split_norm_id

    if package["schema"] != "kerf-reviewed-norm-editions/1":
        raise ValueError("unknown reviewed norm edition schema")
    for norm_id, record in package["norms"].items():
        juris, slug, unit, number, sub = split_norm_id(norm_id)
        expected = {"para": f"§ {number}", "art": f"Art. {number}"}
        if (juris != "de/bund" or sub or expected.get(unit) != record["label"]
                or record["act_id"] != f"fed_{slug}" or not record["title"]):
            raise ValueError("inconsistent reviewed norm identity")
        first_review = _instant(record["known_from"])
        through = _day(record["verified_through"])
        previous_end = None
        editions = record["editions"]
        if not editions:
            raise ValueError("reviewed norm has no editions")
        for index, edition in enumerate(editions):
            if "known_from" in edition and _instant(edition["known_from"]) < first_review:
                raise ValueError("edition precedes its provision review")
            start = _day(edition["valid_from"])
            end = edition["valid_until_exclusive"]
            if start > through or (index and start != previous_end):
                raise ValueError("invalid or noncontiguous reviewed norm intervals")
            if end is not None and _day(end) <= start:
                raise ValueError("empty reviewed norm interval")
            previous_end = end
            body = edition["text"]
            if not body or hashlib.sha256(body.encode()).hexdigest() != edition["text_sha256"]:
                raise ValueError("reviewed norm text digest mismatch")
            sources = edition["sources"]
            if not sources or sources[0]["role"] != "wording":
                raise ValueError("reviewed norm is missing its wording source")
            for src in sources:
                url = urlsplit(src["url"])
                if (url.scheme != "https" or not url.hostname or not src["locator"]
                        or not DIGEST.fullmatch(src["sha256"])):
                    raise ValueError("invalid reviewed norm source")
    return package["norms"]


@lru_cache(maxsize=4)
def _read(path: Path, mtime: int, size: int) -> dict:
    try:
        return validate(json.loads(path.read_bytes()))
    except (OSError, TypeError, AttributeError, ValueError, KeyError) as exc:
        raise ReviewedEditionError(f"Invalid reviewed norm editions: {exc}") from exc


def load(data_dir: Path) -> dict:
    path = (data_dir / FILENAME).resolve()
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {}
    return _read(path, stat.st_mtime_ns, stat.st_size)


def available(record: dict, as_of: str | None) -> bool:
    return as_of is None or _instant(as_of) >= _instant(record["known_from"])


def _known_from(record: dict, edition: dict) -> str:
    return edition.get("known_from", record["known_from"])


def edition_at(record: dict, on: str, as_of: str | None = None) -> dict | None:
    if on > record["verified_through"]:
        return None
    for edition in record["editions"]:
        end = edition["valid_until_exclusive"]
        if edition["valid_from"] <= on and (end is None or on < end):
            if as_of and _instant(as_of) < _instant(_known_from(record, edition)):
                return None
            return edition
    return None


def provenance(record: dict, edition: dict) -> dict[str, Any]:
    return {"date_basis": BASIS, "effective_from": edition["valid_from"],
            "effective_to": edition["valid_until_exclusive"],
            "knowledge_from": _known_from(record, edition),
            "verified_through_observed_at": record["verified_through"],
            "text_sha256": edition["text_sha256"],
            "source_url": edition["sources"][0]["url"],
            "sources": edition["sources"]}


def versions_for(norm_id: str, record: dict, as_of: str | None = None):
    from api.norm_history import NormVersion, StateVersion, norm_content_hash

    result = []
    for index, edition in enumerate(record["editions"]):
        known = _known_from(record, edition)
        if as_of and _instant(as_of) < _instant(known):
            continue
        src = edition["sources"][0]
        state = StateVersion(
            act_id=record["act_id"], key=f"section:{edition['text_sha256']}", index=index,
            source_role=BASIS, source_url=src["url"],
            valid_from=edition["valid_from"], valid_from_status=BASIS, valid_from_basis=BASIS,
            valid_until=edition["valid_until_exclusive"],
            valid_until_status=BASIS if edition["valid_until_exclusive"] else "open",
            valid_until_basis=BASIS if edition["valid_until_exclusive"] else None,
            knowledge_from=known, first_observed_at=known[:10])
        row = {"enbez": record["label"], "titel": record["title"], "text": edition["text"]}
        result.append(NormVersion(norm_id, record["label"], record["title"], edition["text"],
                                  norm_content_hash(row), state, state, [state],
                                  source_capture={BASIS: provenance(record, edition)}))
    return result


def merge_versions(norm_id: str, record: dict, versions: list, selector: str | None, as_of: str | None = None):
    """Keep observations outside the reviewed span and make existing version IDs addressable."""
    reviewed = versions_for(norm_id, record, as_of)
    if not reviewed:
        return versions
    if selector and re.fullmatch(r"\d{4}-\d{2}-\d{2}", selector):
        if edition_at(record, selector, as_of) is None:
            return versions
        return reviewed
    if selector and selector.lower() != "head":
        suffix = selector.split("#")[-1].lower()
        if not any(selector == v.version_id or (len(suffix) >= 8 and
                   v.content_sha256.startswith(suffix)) for v in reviewed):
            return versions
    outside = []
    reviewed_ids = {version.version_id for version in reviewed}
    for version in versions:
        day = version.valid_from or version.first_state.first_observed_at
        if day is None or edition_at(record, day, as_of) is None:
            if version.version_id not in reviewed_ids:
                outside.append(version)
    return sorted([*outside, *reviewed], key=lambda v: v.valid_from or v.first_state.first_observed_at or "")
