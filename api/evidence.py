"""Typed evidence envelope shared by Lexgraph's norm-level read operations.

Every read result says what was searched, what was applied and what could
not be established.  A failure never looks like an empty success:

* ``total`` is ``0`` only after a successful, complete search;
* ``total`` is ``None`` whenever a consulted component was unavailable;
* unknown or ignored arguments are rejected, never silently dropped.

The envelope is deliberately plain ``dict`` data so the same domain results
can be served over REST, written into the Git export and wrapped by an MCP
adapter without a framework-specific model layer.
"""
from __future__ import annotations

from typing import Any, Iterable

from fastapi.responses import JSONResponse

ENVELOPE_SCHEMA = "lexgraph-evidence-envelope/1"

STATUSES = (
    "ok",
    "partial",
    "unavailable",
    "not_in_corpus",
    "not_found_in_searched_scope",
    "ambiguous_identity",
    "temporally_unresolved",
    "unsupported_temporal_request",
    "stale_snapshot",
    "integrity_check_failed",
    "upstream_representation_unusable",
    "invalid_argument",
)

# HTTP status is a transport hint; the typed ``status`` field is the contract.
HTTP_STATUS = {
    "ok": 200,
    "partial": 200,
    "temporally_unresolved": 200,
    "ambiguous_identity": 200,
    "not_in_corpus": 404,
    "not_found_in_searched_scope": 404,
    "invalid_argument": 422,
    "unsupported_temporal_request": 422,
    "stale_snapshot": 409,
    "unavailable": 503,
    "integrity_check_failed": 503,
    "upstream_representation_unusable": 502,
}

# Transport-only cache keys deployed clients append (``?t=<ms>``); they
# never select content and are disclosed in X-Lexgraph-Ignored-Arguments.
CACHE_KEY_ARGUMENTS = frozenset({"t", "_", "cb"})

TEMPORAL_ARGUMENTS = frozenset({
    "at", "as_of", "valid_at", "known_at", "date", "from", "to",
    "valid_from", "valid_until", "effective_at", "stichtag",
})


class EnvelopeError(Exception):
    """Raised inside domain code to end a request with a typed status."""

    def __init__(self, status: str, message: str, *,
                 searched: Iterable[dict[str, Any]] | None = None,
                 warnings: Iterable[str] | None = None,
                 failed_components: Iterable[dict[str, Any]] | None = None,
                 candidates: Iterable[dict[str, Any]] | None = None):
        if status not in STATUSES or status in {"ok", "partial"}:
            raise ValueError(f"not an error status: {status}")
        super().__init__(message)
        self.status = status
        self.message = message
        self.searched = list(searched or [])
        self.warnings = list(warnings or [])
        self.failed_components = list(failed_components or [])
        self.candidates = list(candidates or [])


def envelope(*, status: str = "ok", operation: str,
             request_scope: dict[str, Any] | None = None,
             applied_constraints: dict[str, Any] | None = None,
             snapshot_id: str | None = None,
             searched: Iterable[dict[str, Any]] | None = None,
             evidence: Iterable[dict[str, Any]] | None = None,
             warnings: Iterable[str] | None = None,
             failed_components: Iterable[dict[str, Any]] | None = None,
             total: int | None = None,
             ignored_arguments: Iterable[str] | None = None,
             **extra: Any) -> dict[str, Any]:
    """Build one envelope; ``extra`` holds operation-specific payload."""
    if status not in STATUSES:
        raise ValueError(f"unknown envelope status: {status}")
    searched_rows = list(searched or [])
    failed = list(failed_components or [])
    # A component that did not answer makes any count a lower bound at best.
    if failed or any(row.get("status") not in {"ok", None}
                     for row in searched_rows):
        total = None
        if status == "ok":
            status = "partial"
    result = {
        "schema": ENVELOPE_SCHEMA,
        "status": status,
        "operation": operation,
        "request_scope": dict(request_scope or {}),
        "applied_constraints": dict(applied_constraints or {}),
        "ignored_arguments": list(ignored_arguments or []),
        "snapshot_id": snapshot_id,
        "searched": searched_rows,
        "evidence": list(evidence or []),
        "warnings": list(warnings or []),
        "failed_components": failed,
        "total": total,
    }
    result.update(extra)
    return result


def error_envelope(exc: EnvelopeError, *, operation: str,
                   request_scope: dict[str, Any] | None = None,
                   snapshot_id: str | None = None) -> dict[str, Any]:
    payload = envelope(
        status=exc.status, operation=operation, request_scope=request_scope,
        snapshot_id=snapshot_id, searched=exc.searched,
        warnings=[exc.message, *exc.warnings],
        failed_components=exc.failed_components, total=None)
    payload["status"] = exc.status  # never downgraded to ``partial``
    payload["error"] = {"status": exc.status, "message": exc.message}
    if exc.candidates:
        payload["candidates"] = exc.candidates
    return payload


def response(payload: dict[str, Any], *,
             cache_control: str = "no-cache") -> JSONResponse:
    code = HTTP_STATUS.get(str(payload.get("status")), 200)
    return JSONResponse(payload, status_code=code,
                        headers={"Cache-Control": cache_control})


def unknown_argument_payload(*, route: str, unsupported: list[str],
                             repeated: list[str], allowed: Iterable[str],
                             snapshot_id: str | None = None
                             ) -> dict[str, Any]:
    """Strict runtime schema: undeclared inputs are rejected, not ignored."""
    temporal = sorted(set(unsupported) & TEMPORAL_ARGUMENTS)
    status = ("unsupported_temporal_request" if temporal
              else "invalid_argument")
    message = (f"{route} does not support temporal argument(s) "
               f"{', '.join(temporal)}; it would otherwise be silently "
               "answered for another date" if temporal else
               f"{route} rejects undeclared or repeated argument(s)")
    payload = envelope(
        status=status, operation=route, snapshot_id=snapshot_id,
        warnings=[message], total=None,
        ignored_arguments=[],
        unsupported_arguments=sorted(unsupported),
        repeated_arguments=sorted(repeated),
        allowed_arguments=sorted(allowed))
    payload["status"] = status
    return payload
