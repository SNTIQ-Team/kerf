"""Recognise challenge, login and error pages before anything is parsed.

A portal that answers with a bot challenge, a login wall or an error page
often still returns HTTP 200 (or 503 with a friendly HTML body).  Such a body
must become a typed failure, never a decision (paper section 6.5).
"""
from __future__ import annotations

import html
import re

from .errors import UpstreamFailure

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.I | re.S)

# Markers that never occur in a court decision's own text.
CHALLENGE_MARKERS = (
    "cf-chl", "challenge-platform", "__cf_chl", "cf_chl_opt", "g-recaptcha",
    "h-captcha", "hcaptcha.com", "frc-captcha", "friendlycaptcha",
    "captcha-delivery", "_incapsula_resource", "px-captcha", "ddos-guard",
    "checking your browser", "verify you are human", "bot protection",
    "sicherheitsüberprüfung", "security check",
    # AWS WAF JavaScript challenge / CAPTCHA pages (EUR-Lex/CELLAR, paper §6.5)
    "awswaf", "aws-waf-token", "gokuprops", "awswafintegration",
)
# AWS WAF marks challenged responses with this header (seen live: HTTP 202, empty body, value "challenge").
WAF_ACTION_HEADER = "x-amzn-waf-action"
CHALLENGE_TITLES = re.compile(
    r"^(security check|just a moment\.*|attention required!?.*|access denied|"
    r"captcha|checking your browser.*|ddos-guard|bot verification|"
    r"sicherheitsprüfung|sicherheitsüberprüfung)$", re.I)
LOGIN_TITLES = re.compile(
    r"(anmeldung|login|log in|sign in|einloggen|sitzung abgelaufen|"
    r"session expired|zugang gesperrt)", re.I)
# A bare number is not an error signal ("1 A 406/26.A" is an Aktenzeichen):
# status codes only count next to an error word.
ERROR_TITLES = re.compile(
    r"(\b(?:http|error|fehler|status|code)\s*[:#-]?\s*[45]\d\d\b|"
    r"\b[45]\d\d\s*[-–:]?\s*(?:not found|forbidden|error|fehler|nicht gefunden|service unavailable|"
    r"bad gateway|internal server error|gone)|"
    r"not found|nicht gefunden|seite existiert nicht|service unavailable|nicht verfügbar|"
    r"wartungsarbeiten|maintenance|forbidden|zugriff verweigert|bad gateway|internal server error|"
    r"^\s*(?:fehler|error)\b)", re.I)
# Short pages are suspect; real decisions are long.  Error/login detection by
# title only applies below this visible-text length, so that a decision whose
# title happens to contain "Fehler" is not rejected.
SHORT_PAGE_CHARS = 4000
JS_SHELL_CHARS = 800


def visible_text(markup: str) -> str:
    text = _SCRIPT_RE.sub(" ", markup)
    text = _TAG_RE.sub(" ", text)
    return " ".join(html.unescape(text).split())


def _first(regex: re.Pattern[str], markup: str) -> str:
    match = regex.search(markup)
    return " ".join(visible_text(match.group(1)).split()) if match else ""


def looks_like_html(body: bytes) -> bool:
    head = body[:2048].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<html" in head \
        or b"<head" in head or b"<body" in head


# Cloudflare injects a passive bot-management beacon into normal pages
# (/cdn-cgi/challenge-platform/scripts/jsd/main.js).  It is not a challenge.
_CF_BEACON_RE = re.compile(r"<script\b[^>]*>(?:(?!</script>).)*?/cdn-cgi/challenge-platform/scripts/jsd/main\.js"
                           r"(?:(?!</script>).)*</script>", re.I | re.S)


def classify_page(body: bytes, *, status: int | None = None) -> tuple[str, str] | None:
    """Return (failure_kind, evidence) for a non-decision page, else None."""
    try:
        markup = body.decode("utf-8")
    except UnicodeDecodeError:
        markup = body.decode("latin-1")
    markup = _CF_BEACON_RE.sub(" ", markup)
    lower = markup.lower()
    title = _first(_TITLE_RE, markup)
    h1 = _first(_H1_RE, markup)
    text = visible_text(markup)
    short = len(text) < SHORT_PAGE_CHARS

    if CHALLENGE_TITLES.match(title.strip()):
        return "challenge", f"title={title!r}"
    for marker in CHALLENGE_MARKERS:
        if marker in lower and (short or marker.startswith(("cf-", "__cf", "g-re", "h-cap"))):
            return "challenge", f"marker={marker!r} title={title!r}"
    if short and ('type="password"' in lower or "type='password'" in lower
                  or LOGIN_TITLES.search(title) or LOGIN_TITLES.search(h1)):
        return "login", f"title={title!r} h1={h1!r}"
    if short and (ERROR_TITLES.search(title) or ERROR_TITLES.search(h1)):
        return "error_page", f"title={title!r} h1={h1!r}"
    # A JavaScript-only shell has (almost) no content of its own; a normal page
    # that merely shows a "please enable JavaScript" banner is not a wall.
    if len(text) < JS_SHELL_CHARS and re.search(r"(bitte aktivieren sie javascript|please enable javascript|"
                                               r"enable javascript and cookies|javascript is required)", lower):
        return "challenge", "javascript-only shell"
    if status is not None and status >= 400:
        return "error_page", f"HTTP {status} title={title!r}"
    return None


def classify_response(body: bytes, *, status: int | None = None, headers: dict | None = None
                      ) -> tuple[str, str] | None:
    """Like classify_page, but also uses response headers and the WAF challenge shape."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    action = headers.get(WAF_ACTION_HEADER)
    if action:
        return "challenge", f"AWS WAF action {action!r} (header {WAF_ACTION_HEADER})"
    content_type = headers.get("content-type", "")
    if status == 202 and not body.strip() and "html" in content_type.lower():
        return "challenge", "HTTP 202 with an empty HTML body (JavaScript challenge shape)"
    if body and ("html" in content_type.lower() or looks_like_html(body)):
        return classify_page(body, status=status)
    return None


def ensure_not_blocked(body: bytes, *, url: str, status: int | None = None,
                       content_type: str = "", headers: dict | None = None) -> None:
    """Raise UpstreamFailure when a response is a challenge/login/error page (or a WAF challenge)."""
    verdict = classify_response(body, status=status, headers=dict(headers or {}, **(
        {"content-type": content_type} if content_type else {})))
    if verdict and verdict[0] == "challenge" and (not body or verdict[1].startswith(("AWS WAF", "HTTP 202"))):
        raise UpstreamFailure("challenge", verdict[1], url=url, status=status)
    if not body:
        raise UpstreamFailure("empty_document", "empty response body", url=url,
                              status=status)
    is_html = "html" in content_type.lower() or looks_like_html(body)
    if not is_html:
        return
    verdict = classify_page(body, status=status)
    if verdict:
        kind, evidence = verdict
        raise UpstreamFailure(kind, evidence, url=url, status=status)
