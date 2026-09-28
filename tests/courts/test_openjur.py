"""openJur is recorded, never crawled (robots.txt disallows AI agents; CAPTCHA)."""
import pytest

from courts.errors import UpstreamFailure
from courts.sources import get_adapter
from courts.sync import is_blocking


class NoNetwork:
    def get(self, *a, **k):
        raise AssertionError("openJur must not be requested")

    post = get


def test_openjur_listing_makes_no_request_and_is_blocking():
    adapter = get_adapter("openjur")
    assert adapter.robots_policy == "respect" and "ClaudeBot" in adapter.policy_basis
    with pytest.raises(UpstreamFailure) as failure:
        adapter.list_items(NoNetwork(), None, since=None, limit=None, courts=None)
    assert failure.value.kind == "robots_disallowed" and is_blocking(failure.value)
    with pytest.raises(UpstreamFailure):
        adapter.fetch(NoNetwork(), {"url": "https://openjur.de/u/1.html"})


def test_sync_records_openjur_as_blocked():
    from courts.http import PoliteHttp
    from courts.store import connect, init_db
    from courts.sync import sync_source
    conn = connect()
    init_db(conn)
    http = PoliteHttp(min_interval=10.0, respect_robots=True)
    http.session.get = NoNetwork().get          # any request would fail the test
    result = sync_source(get_adapter("openjur"), conn, http)
    assert result["status"] == "blocked"
    row = conn.execute("SELECT status, status_reason, robots_policy, policy_basis FROM sources "
                       "WHERE source_id='openjur'").fetchone()
    assert row[0] == "blocked" and "robots_disallowed" in row[1] and row[2] == "respect" and "CAPTCHA" in row[3]
