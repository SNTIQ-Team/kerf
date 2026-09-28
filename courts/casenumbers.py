"""Aktenzeichen: split combined dockets, keep the raw string, match keys."""
from __future__ import annotations

import re

# Separators between dockets.  A slash only separates when surrounded by
# whitespace, because "1065/25" is part of a single Aktenzeichen.
_SPLIT = re.compile(
    r"\s*(?:;|,|\s/\s|\s\+\s|\s+und\s+|\s+u\.\s+(?=[A-Z0-9])|\s+sowie\s+|\s+bzw\.\s+|\n)\s*")
# "19 U 104/90 (vormals: 19 U 147/86)": the annotated former docket is kept as its own item.
# A parenthesis is an annotation only if it holds another docket (a digit);
# "8 AZR 194/25 (F)" keeps its suffix, which is part of the BAG ECLI.
_ANNOTATION = re.compile(r"\(\s*(?:(?:vormals|früher|frueher|alt|jetzt|neu|vorher)\s*:?\s*)?([^()]*\d[^()]*)\)",
                         re.I)
_PREFIX = re.compile(r"^(?:Az\.?|Aktenzeichen|AZ)\s*:?\s*", re.I)
_TRAILER = re.compile(r"\s*(?:u\.\s*a\.?|ua\.?|et al\.?)\s*$", re.I)
_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-",
                         "—": "-", " ": " "})


def clean(value: str) -> str:
    return " ".join(value.translate(_DASHES).split()).strip(" .")


def split_case_numbers(raw: str) -> list[str]:
    """Split a published docket string into distinct Aktenzeichen (order kept).

    "L 7 AS 1065/25 B ER / L 7 AS 1066/25 B" -> two items;
    "1 B 19/16, 1 B 19/16, 1 PKH 55/16" -> ["1 B 19/16", "1 PKH 55/16"].
    """
    text = _PREFIX.sub("", clean(raw or ""))
    text = _ANNOTATION.sub(lambda m: ", " + m.group(1), text)
    items: list[str] = []
    for part in _SPLIT.split(text):
        part = clean(_TRAILER.sub("", _PREFIX.sub("", part)))
        if not part or not re.search(r"\d", part):
            continue
        if part not in items:
            items.append(part)
    return items


def case_key(value: str) -> str:
    """Matching key: case/space-insensitive; BVerwG "19.16" equals "19/16"."""
    text = clean(value).upper()
    text = re.sub(r"(?<=\d)\.(?=\d{2}(?:\D|$))", "/", text)
    return re.sub(r"\s+", "", text)


def id_component(value: str) -> str:
    """Az as a path component of a synthetic decision id (no spaces, no '/')."""
    return re.sub(r"\s+", "_", clean(value)).replace("/", "-")
