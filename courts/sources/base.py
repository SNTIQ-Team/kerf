"""Adapter contract and shared parsing helpers."""
from __future__ import annotations

import html as htmlmod
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date

from ..errors import UpstreamFailure
from ..hostgov import Concurrency
from ..http import PoliteHttp
from ..model import ParsedDecision

MONTHS = {"januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5, "juni": 6, "juli": 7,
          "august": 8, "september": 9, "oktober": 10, "november": 11, "dezember": 12}


@dataclass
class Capture:
    body: bytes
    url: str
    media_type: str
    extension: str
    headers: dict = field(default_factory=dict)


@dataclass
class Listing:
    items: list[dict]
    upstream_total: int | None = None
    complete: bool = True          # False when the listing was bounded (limit/since)


ROBOTS_POLICIES = ("respect", "ignore_by_operator_decision")
OPERATOR_DECISION_DATE = "2026-09-28"
OPERATOR_DECISION_BASIS = (
    "operator decision (user), 2026-09-28: robots.txt, Crawl-delay and TDM reservations are not applied "
    "to this source; bot challenges, CAPTCHAs, JS challenges and logins are never bypassed; requests stay "
    "paced (>= min_interval per host), 429/503 and Retry-After are honoured, repeated blocks stop the source")


class Adapter:
    source_id: str = ""
    label: str = ""
    base_url: str = ""
    land: str | None = None
    adapter_name: str = ""
    # Access policy.  "respect" enforces robots.txt (incl. Crawl-delay) in PoliteHttp;
    # "ignore_by_operator_decision" skips robots.txt/TDM checks for this source only, as
    # recorded in sources.robots_policy together with the decision date and basis.
    robots_policy: str = "respect"
    policy_decided_at: str | None = None
    policy_basis: str | None = None
    # Minimum seconds between requests to one host per concurrency slot (never below 1.0; 2-5 s for small
    # court sites).  With the pipeline the host's rate is slots / min_interval (courts.hostgov).
    min_interval: float = 1.1
    # Documents in flight for the host: start, maximum (AIMD in between; operator decision 2026-09-28).
    # Court sites: at most 2.
    concurrency: Concurrency = Concurrency(1, 2)

    def operator_policy(self) -> dict:
        return {"robots_policy": self.robots_policy, "policy_decided_at": self.policy_decided_at,
                "policy_basis": self.policy_basis, "min_interval": self.min_interval}

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since: str | None,
                   limit: int | None, courts: list[str] | None) -> Listing:
        raise NotImplementedError

    def fetch(self, http: PoliteHttp, item: sqlite3.Row | dict) -> Capture:
        raise NotImplementedError

    def parse(self, capture: Capture, item: sqlite3.Row | dict) -> ParsedDecision:
        raise NotImplementedError


def iso_date(raw: str) -> str:
    """'20160316', '16.03.2016', '16. März 2016', '2016-03-16' -> '2016-03-16'."""
    text = " ".join((raw or "").split())
    m = re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})(?:T.*)?", text)
    if m:
        y, mo, d = map(int, m.groups())
    else:
        m = re.search(r"(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})", text)
        if m:
            d, mo, y = map(int, m.groups())
        else:
            m = re.search(r"(\d{1,2})\.\s?([A-Za-zäÄ]+)\s+(\d{4})", text)
            if not m or m.group(2).lower() not in MONTHS:
                raise UpstreamFailure("parse_error", f"unparseable date {raw!r}")
            d, mo, y = int(m.group(1)), MONTHS[m.group(2).lower()], int(m.group(3))
    try:
        return date(y, mo, d).isoformat()
    except ValueError as exc:
        raise UpstreamFailure("parse_error", f"invalid date {raw!r}") from exc


def squash(text: str | None) -> str:
    return " ".join(htmlmod.unescape(text or "").replace(" ", " ").split())


def decode_html(body: bytes, headers: dict | None = None) -> str:
    content_type = (headers or {}).get("content-type", "")
    m = re.search(r"charset=([\w-]+)", content_type, re.I) or re.search(
        rb"<meta[^>]+charset=[\"']?([\w-]+)", body[:4096], re.I)
    charset = m.group(1) if m else "utf-8"
    if isinstance(charset, bytes):
        charset = charset.decode("ascii", "ignore")
    try:
        return body.decode(charset)
    except (LookupError, UnicodeDecodeError):
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            return body.decode("cp1252", errors="replace")
