"""Typed failures.  A failure is never stored or returned as a decision."""
from __future__ import annotations

FAILURE_KINDS = frozenset({
    "challenge",                 # bot/JS challenge or captcha page
    "login",                     # login / session wall
    "error_page",                # upstream HTML error page served as content
    "http_error",                # non-success HTTP status
    "rate_limited",              # HTTP 429 or equivalent
    "network",                   # DNS/TLS/connection/timeout
    "robots_disallowed",         # robots.txt forbids the path (or is unreachable)
    "unexpected_representation", # wrong media type / not the expected format
    "parse_error",               # expected format but structure not understood
    "identity_mismatch",         # document is not the listed decision
    "empty_document",            # no decision text
    "duplicate_identity",        # same decision id already stored from another source
})


class CourtsError(Exception):
    """Base class for corpus errors."""


class UpstreamFailure(CourtsError):
    """A fetch or parse that must not be recorded as a decision."""

    def __init__(self, kind: str, detail: str, *, url: str | None = None,
                 status: int | None = None) -> None:
        if kind not in FAILURE_KINDS:
            raise ValueError(f"unknown failure kind {kind!r}")
        super().__init__(f"{kind}: {detail}" + (f" ({url})" if url else ""))
        self.kind = kind
        self.detail = detail
        self.url = url
        self.status = status

    def as_dict(self) -> dict:
        return {"kind": self.kind, "detail": self.detail, "url": self.url,
                "status": self.status}
