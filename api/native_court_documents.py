"""Read a single native decision from its verified range in the private court archive."""
from __future__ import annotations

from contextlib import closing
from http.client import HTTPException as HTTPProtocolError
import json
import os
from pathlib import Path
import re
import sqlite3
from threading import BoundedSemaphore
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import zlib

from fastapi import APIRouter, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse

from api.native_court_search import authorize
from api.rii_archive import MAX_FRAME
from tools.read_court_archive import unpack, verify

PATH = "/courts/decisions/document"
PRIVATE = {"Cache-Control": "private, no-store", "Vary": "Authorization"}
router = APIRouter(tags=["Court decisions"])
# Whole records can be large even when their compressed ranges are small.
_readers = BoundedSemaphore(2)


class HubRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        host = target.hostname or ""
        if (target.scheme != "https" or target.port not in (None, 443)
                or target.username is not None or target.password is not None
                or not (host == "huggingface.co" or host.endswith((".hf.co", ".huggingface.co")))):
            raise ValueError("Unexpected Hub redirect")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        # The bearer is an unredirected header. Restore it only for a second
        # Hugging Face resolver request. Its signed CDN URLs need no credential.
        if (redirected is not None and host == "huggingface.co"
                and urlsplit(req.full_url).hostname == host and req.has_header("Authorization")):
            redirected.add_unredirected_header("Authorization", req.get_header("Authorization"))
        return redirected


def read_range(url, start, length, total):
    token_file = os.environ["KERF_NATIVE_DOCUMENT_TOKEN_FILE"]
    with open(token_file, "rb") as source:
        token = source.read(512)
    if not re.fullmatch(rb"hf_[A-Za-z0-9_-]{20,200}", token.strip()):
        raise ValueError("Court archive credential unavailable")
    end = start + length - 1
    req = Request(url, headers={"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"})
    req.add_unredirected_header("Authorization", "Bearer " + token.strip().decode("ascii"))
    with build_opener(HubRedirects()).open(req, timeout=15) as response:
        if (response.status != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{total}"
                or response.headers.get("Content-Encoding", "identity").lower() != "identity"):
            raise ValueError("Unexpected court archive range")
        return response.read(length + 1)


def read_document(identifier: str) -> Response:
    revision = os.environ.get("KERF_NATIVE_DOCUMENT_REVISION", "")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("Court archive revision unavailable")
    repo = os.environ["KERF_NATIVE_DOCUMENT_REPO"]
    release = os.environ["KERF_NATIVE_DOCUMENT_RELEASE"]
    if (not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", repo)
            or not re.fullmatch(r"releases/[A-Za-z0-9_-]+", release)):
        raise ValueError("Invalid court archive location")
    path = Path(os.environ["KERF_NATIVE_DOCUMENT_INDEX"]).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        row = db.execute("""
            SELECT l.shard, l.start, l.length, l.raw_length, l.sha256, s.size
            FROM locations AS l LEFT JOIN shards AS s ON s.path=l.shard WHERE l.id=?
        """, (identifier,)).fetchone()
    if row is None:
        raise HTTPException(404, "No decision matches the requested identifier.", headers=PRIVATE)
    shard, start, length, raw_size, digest, total = row
    if (not isinstance(shard, str) or not re.fullmatch(r"packs/[a-zA-Z0-9_-]+-\d{4}\.pack", shard)
            or not all(type(n) is int for n in (start, length, raw_size, total))
            or not 0 < length <= MAX_FRAME or not 0 < raw_size <= MAX_FRAME
            or start < 0 or start + length > total or not isinstance(digest, str)
            or not re.fullmatch(r"[a-f0-9]{64}", digest)):
        raise ValueError("Invalid native document location")
    url = f"https://huggingface.co/datasets/{repo}/resolve/{revision}/{release}/{shard}"
    packed = read_range(url, start, length, total)
    verify(packed, length, digest)
    raw = unpack(packed, raw_size)
    if len(raw) != raw_size:
        raise ValueError("Native decision size mismatch")
    record = json.loads(raw)
    headers = {**PRIVATE, "X-Kerf-Court-Revision": revision}
    if record["status"] == "integrity_check_failed" and record.get("decision_id") == identifier:
        # Only publish the identity hold. Its source notes may contain text.
        return JSONResponse({"status": "integrity_check_failed", "decision_id": identifier,
                             "reason": "source_identity_mismatch"}, headers=headers)
    decision = record.get("decision")
    if (record["status"] != "ok" or not isinstance(decision, dict) or decision.get("id") != identifier
            or not isinstance(decision.get("paragraphs"), list)):
        raise ValueError("Native decision identity mismatch")
    return Response(raw, media_type="application/json", headers=headers)


@router.get(PATH)
def document(identifier: str = Query(..., min_length=1, max_length=1024),
             authorization: str | None = Header(None)):
    try:
        authorize(authorization)
    except HTTPException as exc:
        raise HTTPException(exc.status_code, exc.detail, headers={**(exc.headers or {}), **PRIVATE}) from exc
    unavailable = HTTPException(503, "The decision archive is temporarily unavailable.",
                                headers={**PRIVATE, "Retry-After": "30"})
    if not _readers.acquire(timeout=1):
        raise unavailable
    try:
        return read_document(identifier)
    except (KeyError, ValueError, TypeError, AttributeError, RecursionError, OSError,
            HTTPProtocolError, sqlite3.Error, zlib.error) as exc:
        raise unavailable from exc
    finally:
        _readers.release()
