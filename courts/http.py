"""Polite HTTP: per-host pacing, robots.txt, conditional requests, typed errors."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .config import MIN_INTERVAL_SECONDS, USER_AGENT
from .detect import classify_page, ensure_not_blocked, looks_like_html
from .errors import UpstreamFailure


@dataclass
class Response:
    url: str
    status: int
    body: bytes
    headers: dict[str, str]
    elapsed: float
    not_modified: bool = False

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


@dataclass
class _Host:
    last: float = 0.0
    interval: float = MIN_INTERVAL_SECONDS
    robots: RobotFileParser | None = None
    robots_state: str = "unknown"   # ok | missing | unreachable
    robots_detail: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)


class PoliteHttp:
    """One session; every host is paced to >= MIN_INTERVAL_SECONDS.

    ``validators`` is an optional mapping-like store {url: (etag, last_modified)}
    used to send If-None-Match / If-Modified-Since.
    """

    def __init__(self, *, min_interval: float = MIN_INTERVAL_SECONDS,
                 timeout: float = 60.0, retries: int = 2,
                 respect_robots: bool = True, session: requests.Session | None = None):
        if min_interval < 1.0:
            raise ValueError("politeness floor is 1 request per second per host")
        self.min_interval = min_interval
        self.timeout = timeout
        self.retries = retries
        self.respect_robots = respect_robots
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.5",
        })
        self._hosts: dict[str, _Host] = {}
        self._guard = threading.Lock()
        self.requests_made = 0

    def _host(self, url: str) -> _Host:
        netloc = urlsplit(url).netloc.lower()
        with self._guard:
            return self._hosts.setdefault(netloc, _Host())

    def _pace(self, host: _Host) -> None:
        wait = max(self.min_interval, host.interval) - (time.monotonic() - host.last)
        if wait > 0:
            time.sleep(wait)
        host.last = time.monotonic()

    # robots.txt -----------------------------------------------------------
    def _load_robots(self, url: str, host: _Host) -> None:
        parts = urlsplit(url)
        robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
        parser = RobotFileParser()
        self._pace(host)
        try:
            response = self.session.get(robots_url, timeout=30, allow_redirects=True)
            self.requests_made += 1
        except requests.RequestException:
            host.robots, host.robots_state = None, "unreachable"
            return
        body = response.content or b""
        content_type = response.headers.get("content-type", "")
        if response.status_code >= 500:
            # RFC 9309 2.3.1.4: server errors mean "assume complete disallow".
            host.robots, host.robots_state = None, "unreachable"
            verdict = classify_page(body, status=response.status_code) if looks_like_html(body) else None
            host.robots_detail = f"robots.txt answered HTTP {response.status_code}" + (
                f" with a {verdict[0]} page ({verdict[1]})" if verdict else "")
        elif response.status_code >= 400 or "html" in content_type.lower():
            parser.parse([])  # no robots.txt (or an HTML landing page): allow
            host.robots, host.robots_state = parser, "missing"
        else:
            parser.parse(body.decode("utf-8", errors="replace").splitlines())
            host.robots, host.robots_state = parser, "ok"
            delay = parser.crawl_delay(USER_AGENT)
            if delay:
                # Crawl-delay is honoured even when it is far slower than our 1 req/s floor.
                host.interval = max(host.interval, float(delay))

    def crawl_delay(self, url: str) -> float:
        return self._host(url).interval

    def check_robots(self, url: str) -> None:
        if not self.respect_robots:
            return
        host = self._host(url)
        with host.lock:
            if host.robots_state == "unknown":
                self._load_robots(url, host)
        if host.robots_state == "unreachable":
            kind = "challenge" if " challenge page" in host.robots_detail else "robots_disallowed"
            raise UpstreamFailure(kind, "robots.txt unreachable or 5xx: complete disallow (RFC 9309); "
                                  + (host.robots_detail or "no response"), url=url)
        if host.robots is not None and not host.robots.can_fetch(USER_AGENT, url):
            raise UpstreamFailure("robots_disallowed", "path disallowed by robots.txt", url=url)

    # requests -------------------------------------------------------------
    def request(self, method: str, url: str, *, validators: tuple[str | None, str | None] | None = None,
                headers: dict | None = None, detect: bool = True, **kwargs) -> Response:
        self.check_robots(url)
        host = self._host(url)
        send = dict(headers or {})
        if validators:
            etag, last_modified = validators
            if etag:
                send["If-None-Match"] = etag
            if last_modified:
                send["If-Modified-Since"] = last_modified
        last_error: UpstreamFailure | None = None
        for attempt in range(self.retries + 1):
            with host.lock:
                self._pace(host)
                started = time.monotonic()
                try:
                    raw = self.session.request(method, url, headers=send,
                                               timeout=kwargs.pop("timeout", self.timeout),
                                               **kwargs)
                    self.requests_made += 1
                except requests.RequestException as exc:
                    last_error = UpstreamFailure("network", type(exc).__name__ + ": " + str(exc)[:200], url=url)
                    time.sleep(5 * (attempt + 1))
                    continue
            response = Response(url=raw.url, status=raw.status_code, body=raw.content or b"",
                                headers={k.lower(): v for k, v in raw.headers.items()},
                                elapsed=time.monotonic() - started,
                                not_modified=raw.status_code == 304)
            if response.not_modified:
                return response
            if raw.status_code == 429:
                retry_after = raw.headers.get("Retry-After", "")
                delay = int(retry_after) if retry_after.isdigit() else 30 * (attempt + 1)
                last_error = UpstreamFailure("rate_limited", f"HTTP 429 (retry-after {retry_after or '-'})",
                                             url=url, status=429)
                time.sleep(min(delay, 120))
                continue
            if detect:
                # A challenge served with 403/503 is a challenge, not a retryable 5xx.
                ensure_not_blocked(response.body, url=url, status=raw.status_code,
                                   content_type=response.content_type)
            if raw.status_code in (500, 502, 503, 504):
                last_error = UpstreamFailure("http_error", f"HTTP {raw.status_code}", url=url,
                                             status=raw.status_code)
                time.sleep(10 * (attempt + 1))
                continue
            if raw.status_code >= 400:
                raise UpstreamFailure("http_error", f"HTTP {raw.status_code}", url=url,
                                      status=raw.status_code)
            return response
        assert last_error is not None
        raise last_error

    def get(self, url: str, **kwargs) -> Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> Response:
        return self.request("POST", url, **kwargs)
