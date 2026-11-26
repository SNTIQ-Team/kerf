"""Whole-act date roles from CELLAR's annotated legal-date assertions."""
from __future__ import annotations

import re

if __package__:
    from .bgbl_text import german_date_text
else:
    from bgbl_text import german_date_text


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


def application_clause(editions: list[dict], expected: list[str], article: str, paragraph: str,
                       cellar_date: str | None) -> dict:
    """Read a general application date from a designated paragraph across the fetched editions."""
    sentence = re.compile(rf"^\({re.escape(paragraph)}\) Diese Verordnung gilt ab dem "
                          r"\d{1,2}\. [^\W\d_]+ \d{4}\.(?=\s|$)", re.MULTILINE)
    sources = []
    for edition in editions:
        provision = next((a["text"] for a in edition["articles"] if a["enbez"] == article), "")
        matches = sentence.findall(provision)
        quote = matches[0] if len(matches) == 1 else None
        sources.append({"celex": edition["celex"], "quote": quote,
                        "date": german_date_text(quote) if quote else None,
                        "sources": [{"url": item["url"], "sha256": item["sha256"]} for item in edition["items"]]})
    missing = sorted(set(expected) - {s["celex"] for s in sources})
    dates = {s["date"] for s in sources}
    day = dates.pop() if not missing and len(dates) == 1 else None
    return {"article": article, "paragraph": paragraph, "date": day, "cellar_date": cellar_date,
            "editions": sources, "missing_editions": missing}
