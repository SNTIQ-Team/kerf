"""Sachsen: robots.txt Crawl-delay 3600 -> recorded as blocked without any request."""
import pytest

from courts import http as http_module
from courts.errors import UpstreamFailure
from courts.http import PoliteHttp
from courts.sources import get_adapter
from courts.sources.sn import BLOCK_DETAIL, SachsenAdapter
from courts.store import connect, init_db
from courts.sync import sync_source

ROBOTS = b"User-agent: *\nCrawl-delay: 3600\n"   # www.justiz.sachsen.de/robots.txt as fetched 2026-09-28


class FakeResponse:
    def __init__(self, status, body, headers, url):
        self.status_code, self.content, self.headers, self.url = status, body, headers, url


class RecordingSession:
    def __init__(self):
        self.headers, self.calls = {}, []

    def get(self, url, timeout=None, allow_redirects=True):          # robots.txt
        self.calls.append(url)
        return FakeResponse(200, ROBOTS, {"Content-Type": "text/plain"}, url)

    def request(self, method, url, **kw):
        self.calls.append(url)
        body = ("<html><head><title>Entscheidung</title></head><body><p>Aktenzeichen: 2 B 267/25</p>"
                + "<p>Gründe</p>" * 400 + "</body></html>").encode()
        return FakeResponse(200, body, {"Content-Type": "text/html; charset=utf-8"}, url)


def test_registered():
    assert isinstance(get_adapter("sn"), SachsenAdapter)


def test_listing_is_refused_without_any_request():
    session = RecordingSession()
    http = PoliteHttp(session=session)
    with pytest.raises(UpstreamFailure) as err:
        SachsenAdapter().list_items(http, None, since=None, limit=1, courts=None)
    assert err.value.kind == "robots_disallowed" and err.value.detail == BLOCK_DETAIL
    assert session.calls == [] and http.requests_made == 0


def test_sync_records_blocked_source(tmp_path):
    conn = connect(tmp_path / "c.sqlite")
    init_db(conn)
    session = RecordingSession()
    result = sync_source(SachsenAdapter(), conn, PoliteHttp(session=session), limit=1)
    assert result["status"] == "blocked" and result["requests"] == 0 and session.calls == []
    source = conn.execute("SELECT status, status_reason FROM sources WHERE source_id='sn'").fetchone()
    assert source["status"] == "blocked" and "Crawl-delay 3600" in source["status_reason"]
    failure = conn.execute("SELECT kind, url FROM failures WHERE source_id='sn'").fetchone()
    assert failure["kind"] == "robots_disallowed" and failure["url"].endswith("/robots.txt")


def test_fetch_is_paced_by_the_crawl_delay(monkeypatch):
    waits = []
    monkeypatch.setattr(http_module.time, "sleep", lambda s: waits.append(s))
    session = RecordingSession()
    http = PoliteHttp(session=session)
    url = "https://www.justiz.sachsen.de/ovgentschweb/document.phtml?id=7717"
    capture = SachsenAdapter().fetch(http, {"source_doc_id": "7717", "url": url})
    assert capture.media_type == "text/html"
    assert session.calls == ["https://www.justiz.sachsen.de/robots.txt", url]
    assert http.crawl_delay(url) == 3600.0
    assert waits and max(waits) > 3590          # the document request waited for the Crawl-delay
    with pytest.raises(UpstreamFailure) as err:
        SachsenAdapter().parse(capture, {"source_doc_id": "7717", "url": url})
    assert err.value.kind == "parse_error"
