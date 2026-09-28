"""Challenge/login/error detection and HTTP politeness (offline, fake session)."""
import pytest

from courts import http as http_module
from courts.config import USER_AGENT
from courts.detect import classify_page, ensure_not_blocked
from courts.errors import UpstreamFailure
from courts.http import PoliteHttp


def test_real_myra_security_check_is_a_challenge(fixture):
    body = fixture("by/gesetze_bayern_security_check_503.html")
    assert classify_page(body, status=503)[0] == "challenge"
    with pytest.raises(UpstreamFailure) as err:
        ensure_not_blocked(body, url="https://www.gesetze-bayern.de/", status=503, content_type="text/html")
    assert err.value.kind == "challenge"


def test_real_page_with_javascript_banner_is_not_a_challenge(fixture):
    assert classify_page(fixture("by/vgansbach_press_index_js_notice.html"), status=200) is None


@pytest.mark.parametrize("markup,kind", [
    (b"<html><head><title>Just a moment...</title></head><body><div id='cf-chl-widget'></div></body></html>",
     "challenge"),
    (b"<html><head><title>Anmeldung</title></head><body><form><input type='password' name='pw'></form></body></html>",
     "login"),
    (b"<html><head><title>404 Not Found</title></head><body><h1>Not Found</h1></body></html>", "error_page"),
    (b"<html><head><title>Wartungsarbeiten</title></head><body>Bitte sp\xc3\xa4ter</body></html>", "error_page"),
    (b"<html><head><title>App</title></head><body><p>Bitte aktivieren Sie JavaScript.</p></body></html>",
     "challenge"),
])
def test_synthetic_non_decision_pages(markup, kind):
    assert classify_page(markup)[0] == kind


def test_long_decision_mentioning_error_words_is_not_rejected():
    text = ("<p>Der Fehler der Behörde liegt in der Anmeldung. " + "Gründe " * 800 + "</p>").encode()
    assert classify_page(b"<html><head><title>VG Berlin, 1 K 404/20</title></head><body>" + text + b"</body></html>") is None


def test_empty_body_is_typed():
    with pytest.raises(UpstreamFailure) as err:
        ensure_not_blocked(b"", url="https://x")
    assert err.value.kind == "empty_document"


class FakeResponse:
    def __init__(self, status, body=b"", headers=None, url="https://example.org/"):
        self.status_code = status
        self.content = body
        self.headers = headers or {"Content-Type": "text/plain"}
        self.url = url


class FakeSession:
    def __init__(self, robots=b"", robots_status=200, pages=None):
        self.headers = {}
        self.robots = robots
        self.robots_status = robots_status
        self.pages = pages or {}
        self.calls = []

    def get(self, url, timeout=None, allow_redirects=True):
        self.calls.append(("GET", url, {}))
        return FakeResponse(self.robots_status, self.robots, url=url)

    def request(self, method, url, headers=None, timeout=None, **kw):
        self.calls.append((method, url, dict(headers or {})))
        status, body, hdrs = self.pages.get(url, (200, b"ok", {"Content-Type": "text/plain"}))
        return FakeResponse(status, body, hdrs, url=url)


@pytest.fixture
def no_sleep(monkeypatch):
    waits = []
    monkeypatch.setattr(http_module.time, "sleep", lambda s: waits.append(s))
    return waits


def test_user_agent_is_descriptive_and_has_no_personal_data():
    assert USER_AGENT.startswith("SNTIQ-Legal-Courts/") and "@" not in USER_AGENT and "req/s" in USER_AGENT
    session = FakeSession()
    PoliteHttp(session=session)
    assert session.headers["User-Agent"] == USER_AGENT


def test_robots_disallow_all_blocks_before_any_page_request(no_sleep):
    session = FakeSession(robots=b"User-agent: *\nDisallow: /\n\nUser-agent: DG_JUSTICE_CRAWLER\n")
    http = PoliteHttp(session=session)
    with pytest.raises(UpstreamFailure) as err:
        http.get("https://www.rechtsprechung-im-internet.de/rii-toc.xml")
    assert err.value.kind == "robots_disallowed"
    assert [c[0] for c in session.calls] == ["GET"]  # only robots.txt was requested


def test_robots_5xx_means_complete_disallow(no_sleep):
    http = PoliteHttp(session=FakeSession(robots=b"", robots_status=503))
    with pytest.raises(UpstreamFailure) as err:
        http.get("https://www.gesetze-bayern.de/x")
    assert err.value.kind == "robots_disallowed"


def test_crawl_delay_is_honoured(no_sleep):
    session = FakeSession(robots=b"User-agent: *\nCrawl-delay: 30\nAllow: /\n")
    http = PoliteHttp(session=session)
    http.get("https://www.bsg.bund.de/a")
    http.get("https://www.bsg.bund.de/b")
    assert http.crawl_delay("https://www.bsg.bund.de/") == 30
    assert max(no_sleep) > 25  # the second request waited for the crawl delay


def test_politeness_floor_cannot_be_lowered():
    with pytest.raises(ValueError):
        PoliteHttp(min_interval=0.5, session=FakeSession())


def test_conditional_request_headers_and_304(no_sleep):
    url = "https://example.org/toc.xml"
    session = FakeSession(pages={url: (304, b"", {})})
    http = PoliteHttp(session=session)
    response = http.get(url, validators=('"abc"', "Mon, 28 Sep 2026 07:00:47 GMT"))
    assert response.not_modified
    sent = session.calls[-1][2]
    assert sent["If-None-Match"] == '"abc"' and sent["If-Modified-Since"].startswith("Mon, 28 Sep")


def test_challenge_served_with_503_is_not_retried_as_server_error(no_sleep, fixture):
    url = "https://www.gesetze-bayern.de/Search"
    body = fixture("by/gesetze_bayern_security_check_503.html")
    session = FakeSession(pages={url: (503, body, {"Content-Type": "text/html"})})
    http = PoliteHttp(session=session)
    with pytest.raises(UpstreamFailure) as err:
        http.get(url)
    assert err.value.kind == "challenge"
    assert len([c for c in session.calls if c[1] == url]) == 1


def test_real_aws_waf_challenge_202_is_a_challenge_not_an_empty_document(no_sleep):
    import json as _json
    from tests.courts.conftest import FIXTURES
    captured = _json.loads((FIXTURES / "eurlex_aws_waf_challenge_202.json").read_text())
    url = captured["url"]
    headers = {k.title(): v for k, v in captured["headers"].items()}
    session = FakeSession(pages={url: (202, b"", headers)})
    http = PoliteHttp(session=session, respect_robots=False)
    with pytest.raises(UpstreamFailure) as err:
        http.get(url)
    assert err.value.kind == "challenge" and "AWS WAF" in err.value.detail
    assert len([c for c in session.calls if c[1] == url]) == 1          # never retried
