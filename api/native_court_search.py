"""Search a compact native decision index. Only original-matched RII records are public."""
from __future__ import annotations

from contextlib import closing
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import urlencode

from fastapi import APIRouter, Header, HTTPException, Query, Response

router = APIRouter(prefix="/courts/decisions", tags=["Court decisions"])
FORMAT = "kerf-native-court-search/1"


def terms_for(query: str) -> str:
    words = list(dict.fromkeys(re.findall(r"[^\W_]+", query.lower(), re.UNICODE)))
    if not 1 <= len(words) <= 12 or any(len(w) > 96 for w in words):
        raise HTTPException(422, "Enter between one and twelve search terms")
    return " AND ".join(f'"{word}"' for word in words)


def authorize(authorization: str | None) -> None:
    file = os.environ.get("KERF_NATIVE_COURT_TOKEN_FILE")
    try:
        expected = Path(file).read_bytes().strip() if file else b""
    except OSError:
        expected = b""
    scheme, _, token = (authorization or "").partition(" ")
    valid = len(expected) >= 32 and scheme.casefold() == "bearer"
    if not valid or not secrets.compare_digest(token.encode(), expected):
        raise HTTPException(401, "Operator authentication required",
                            headers={"WWW-Authenticate": "Bearer", "Cache-Control": "private, no-store"})


def search(scope: str, query: str, after: str | None, limit: int) -> dict:
    match = terms_for(query)
    directory = os.environ.get("KERF_NATIVE_COURT_INDEX")
    unavailable = HTTPException(503, "Decision search index unavailable",
                                headers={"Retry-After": "60", "Cache-Control": "no-store"})
    if not directory:
        raise unavailable
    path = (Path(directory) / f"{scope}.sqlite").resolve()
    if not path.is_file():
        raise unavailable
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            meta = dict(db.execute("SELECT name,value FROM metadata"))
            if meta.get("format") != FORMAT or meta.get("scope") != scope:
                raise unavailable
            revision = meta["revision"]
            offset = 0
            if after:
                found = re.fullmatch(r"([0-9a-f]{20})\.(\d{1,10})", after)
                if not found:
                    raise HTTPException(422, "Invalid search cursor")
                if found[1] != revision:
                    raise HTTPException(409, "Search index has changed. Start again from the first page")
                offset = int(found[2])
            db.row_factory = sqlite3.Row
            rows = db.execute("""
                SELECT d.* FROM terms CROSS JOIN documents AS d ON d.ordinal=terms.rowid
                WHERE terms MATCH ? AND terms.rowid>? ORDER BY terms.rowid LIMIT ?
            """, (match, offset, limit + 1)).fetchall()
    except (sqlite3.Error, KeyError) as exc:
        raise unavailable from exc
    result = []
    for row in rows[:limit]:
        entry = dict(row)
        del entry["ordinal"]
        if scope == "public":
            entry["document_path"] = "/kerf/rii/decisions/" + entry["document"]
        else:
            entry["document_path"] = "/kerf/courts/decisions/document?" + urlencode({"identifier": entry["id"]})
        result.append(entry)
    next_page = f'{revision}.{rows[limit-1]["ordinal"]}' if len(rows) > limit else None
    return {"revision": revision, "scope": scope, "indexed_decisions": int(meta["decisions"]),
            "items": result, "next": next_page}


@router.get("/search")
def public_search(q: str = Query(..., min_length=1, max_length=240), after: str | None = Query(None, max_length=48),
                  limit: int = Query(20, ge=1, le=100)) -> dict:
    return search("public", q, after, limit)


@router.get("/search/operator")
def operator_search(response: Response, q: str = Query(..., min_length=1, max_length=240),
                    after: str | None = Query(None, max_length=48), limit: int = Query(20, ge=1, le=100),
                    authorization: str | None = Header(None)) -> dict:
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["Vary"] = "Authorization"
    authorize(authorization)
    return search("operator", q, after, limit)
