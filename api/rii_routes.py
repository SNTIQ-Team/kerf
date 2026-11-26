"""Read exact decisions, editorial or court passages, and original RII source bytes."""
from contextlib import contextmanager
from http.client import HTTPException as HTTPProtocolError
from hashlib import sha256
import os
import zipfile
import zlib

from fastapi import APIRouter, HTTPException, Query, Response

from api.rii_archive import MissingRecord, document_id, open_archive, passage_for, select_passages

router = APIRouter(prefix="/rii/decisions", tags=["RII decisions"])


@contextmanager
def verified(response):
    try:
        archive = open_archive(os.environ.get("KERF_RII_REVISION", ""), os.environ.get("KERF_RII_MANIFEST_SHA256", ""))
        # The Hub revision can change independently of the local edge-cache generation.
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Kerf-RII-Revision"] = archive.revision
        yield archive
    except MissingRecord as exc:
        raise HTTPException(404, "Document or passage absent from this RII release",
                            headers={"Cache-Control": "no-store", "X-Kerf-RII-Revision": archive.revision}) from exc
    except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPProtocolError, zipfile.BadZipFile, zlib.error) as exc:
        raise HTTPException(503, "The pinned RII release could not be verified",
                            headers={"Cache-Control": "no-store", "Retry-After": "30"}) from exc


@router.get("/{document}")
def decision(document: str, response: Response):
    with verified(response) as archive:
        return {"revision": archive.revision, "metadata": archive.decision(document)["metadata"]}


@router.get("/{document}/passages")
def passages(document: str, response: Response, rn: str | None = Query(None, min_length=1, max_length=12),
             q: str | None = Query(None, min_length=1, max_length=250),
             offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=250)):
    with verified(response) as archive:
        return {"revision": archive.revision, "document": document_id(document),
                **select_passages(archive.decision(document), rn, q, offset, limit)}


@router.get("/{document}/passages/{anchor}")
def passage(document: str, anchor: str, response: Response):
    with verified(response) as archive:
        return {"revision": archive.revision, "passage": passage_for(archive.decision(document), anchor)}


@router.get("/{document}/source")
def source(document: str):
    response = Response()
    with verified(response) as archive:
        raw, extension = archive.source(document)
        return Response(raw, media_type="application/"+extension,
                        headers={"Cache-Control": "no-store", "X-Kerf-RII-Revision": archive.revision,
                                 "X-Kerf-Source-Sha256": sha256(raw).hexdigest(),
                                 "Content-Disposition": f'attachment; filename="{document_id(document)}.{extension}"'})
