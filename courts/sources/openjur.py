"""openJur (openjur.de): recorded as a source that is NOT crawled (policy check 2026-09-28).

The operator's robots-ignore decision (sources.base.OPERATOR_DECISION_BASIS) does not cover openJur: its
robots.txt and Crawl-delay are respected.  What the check found:

* ``robots.txt`` (fetched 2026-09-28): ``User-agent: *`` may not fetch ``/suche/`` (the search, i.e. every
  listing) nor the decision PDFs/print/export views (``/u/*.pdf$``, ``*.print$``, ``*.ris$`` …), and a
  dedicated ``Disallow: /`` names the AI agents - ``anthropic-ai``, ``ClaudeBot``, ``Claude-Web``, ``GPTBot``,
  ``ChatGPT-User``, ``OAI-SearchBot``, ``PerplexityBot``, ``cohere-ai``, ``CCBot``, ``meta-externalagent`` … -
  and ``ia_archiver`` (the Wayback Machine).  This corpus is built by such an agent, so the whole site is
  treated as disallowed;
* the first page request answers with an interactive CAPTCHA ("rotate the inner part of the image" - a
  human check), which is never bypassed;
* the terms of use sit behind that CAPTCHA, so whether they forbid automated access could not be read.

``python -m courts sync --source openjur`` therefore makes no request at all: it records the source as
``blocked`` with this basis (a typed ``robots_disallowed`` failure).  Deduplication, editorial sections and the
``numbering_source: openjur`` rule would apply only if the operator obtained openJur's permission.
"""
from __future__ import annotations

import sqlite3

from ..errors import UpstreamFailure
from ..http import PoliteHttp
from .base import Adapter, Capture, Listing

POLICY_CHECKED_AT = "2026-09-28"
POLICY_BASIS = (
    "openjur.de policy check 2026-09-28: robots.txt disallows the AI agents (anthropic-ai, ClaudeBot, Claude-Web, "
    "GPTBot, ChatGPT-User, …) the whole site and every agent /suche/ and the PDF/print/export views; the site "
    "answers with an interactive CAPTCHA (never bypassed); the terms of use are behind it. The operator's "
    "robots-ignore decision does not cover openJur. Not crawled: no request is made.")


class OpenJurAdapter(Adapter):
    source_id = "openjur"
    label = "openJur (openjur.de) - not crawled: robots.txt disallows AI agents, CAPTCHA (policy 2026-09-28)"
    base_url = "https://openjur.de"
    land = None
    adapter_name = "openjur-policy-block"
    robots_policy = "respect"
    policy_decided_at = POLICY_CHECKED_AT
    policy_basis = POLICY_BASIS
    min_interval = 10.0

    def list_items(self, http: PoliteHttp, conn: sqlite3.Connection, *, since, limit, courts) -> Listing:
        raise UpstreamFailure("robots_disallowed", POLICY_BASIS, url=f"{self.base_url}/robots.txt")

    def fetch(self, http: PoliteHttp, item) -> Capture:
        raise UpstreamFailure("robots_disallowed", POLICY_BASIS, url=item["url"])

    def parse(self, capture: Capture, item):
        raise UpstreamFailure("robots_disallowed", POLICY_BASIS, url=capture.url)


def adapters() -> dict[str, Adapter]:
    return {"openjur": OpenJurAdapter()}
