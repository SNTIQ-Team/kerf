"""Whole-act date roles from CELLAR's annotated legal-date assertions."""
from __future__ import annotations

import re


def general_date(rows: list[dict], role: str | None) -> str | None:
    """Select a unique whole-act date. Scope can be encoded in the role or comment."""
    candidates = set()
    for row in rows:
        comments = re.findall(r"\{([^|}]+)\|", row.get("dateComment", ""))
        types = re.findall(r"\{([^|}]+)\|", row.get("dateRole", ""))
        if any("PART" in code.split("/") for code in comments + types):
            continue
        if role is None or role in types:
            candidates.add(row["date"])
    # Older works such as the Charter have one unannotated entry-into-force date.
    if (role == "EV" and not candidates
            and all(not r.get("dateRole") and not r.get("dateComment") for r in rows)):
        candidates = {r["date"] for r in rows}
    if len(candidates) != 1:
        return None
    day = candidates.pop()
    return None if day in {"1001-01-01", "9999-12-31"} else day
