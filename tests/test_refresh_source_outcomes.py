"""A retained fallback is publishable data, never a successful source refresh."""
import importlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipeline'))


def test_eu_watch_main_reports_failure_while_preserving_stale_evidence(tmp_path, monkeypatch):
    watcher = importlib.import_module('fetch_eu_watch')
    watchlist, state = tmp_path / 'watchlist.json', tmp_path / 'state.json'
    watchlist.write_text(json.dumps({'procedures': {'fixture': {'source': 'EUR-Lex', 'procedure': '2026/0000/COD'}}}))
    state.write_text(json.dumps({'procedures': {'fixture': {'procedure': '2026/0000/COD', 'status': 'pending', 'stage': 'pending'}}}))
    monkeypatch.setattr(watcher, 'WATCHLIST', watchlist)
    monkeypatch.setattr(watcher, 'WATCH_STATE', state)
    monkeypatch.setattr(watcher, 'Http', lambda **kw: object())
    def failed(*args, **kwargs):
        raise watcher.requests.RequestException('synthetic upstream failure')
    monkeypatch.setattr(watcher, 'fetch_watch', failed)
    monkeypatch.setattr(watcher, '_cellar_journal', lambda *args: [])
    monkeypatch.setattr(watcher, 'snapshot_dir', lambda *args: tmp_path)
    # Not 0 (source freshness records a failed fetch), and not 1 either: callers must
    # be able to tell a handled fallback (EX_TEMPFAIL) from a crash.
    assert watcher.main() == watcher.EX_TEMPFAIL == 75
    rows = [json.loads(line) for line in (tmp_path / 'procedures.jsonl').read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['source_stale'] is True
    assert rows[0]['status'] == 'pending'


def test_bavarian_archive_main_reports_failure_while_retaining_old_row(tmp_path, monkeypatch):
    archive = importlib.import_module('fetch_bayern_archive')
    previous = tmp_path / 'previous'
    previous.mkdir()
    retained = {'key': 'fixture', 'jurabk': 'Fixture', 'retained_fixture': True}
    (previous / 'acts.jsonl').write_text(json.dumps(retained) + '\n')
    monkeypatch.setattr(sys, 'argv', ['fetch_bayern_archive.py'])
    monkeypatch.setattr(archive, 'ACTS', {'fixture': {}})
    def failed(*args, **kwargs):
        raise RuntimeError('synthetic upstream failure')
    monkeypatch.setattr(archive, 'fetch_act', failed)
    monkeypatch.setattr(archive, 'latest_snapshot', lambda *args: previous)
    monkeypatch.setattr(archive, 'snapshot_dir', lambda *args: tmp_path)
    assert archive.main() == 1
    assert json.loads((tmp_path / 'acts.jsonl').read_text()) == retained


def test_failed_live_archive_index_cannot_masquerade_as_fresh_using_cached_index(tmp_path, monkeypatch):
    archive = importlib.import_module('fetch_bayern_archive')
    monkeypatch.setattr(archive, 'CACHE', tmp_path)
    cache = tmp_path / 'fixture.cdx.json'
    cache.write_text(json.dumps([['20200101000000', 'digest', '1']]))
    monkeypatch.setattr(archive, '_get', lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match='archive index refresh failed'):
        archive.zip_captures('fixture', offline=False)
    assert archive.zip_captures('fixture', offline=True) == [
        {'timestamp': '20200101000000', 'digest': 'digest'}]
    assert json.loads(cache.read_text()) == [['20200101000000', 'digest', '1']]
    monkeypatch.setattr(archive, '_get', lambda *args, **kwargs:
                        b'[["timestamp","digest","length","original"]]')
    with pytest.raises(RuntimeError, match='no captures'):
        archive.zip_captures('fixture', offline=False)
    assert json.loads(cache.read_text()) == [['20200101000000', 'digest', '1']]


@pytest.mark.parametrize('status,body', [
    (403, b'<html><body>Access denied</body></html>'),
    (200, b'<html><body>Maintenance</body></html>'),
    (200, b'<rss><channel/></rss>'),
])
def test_bgbl_failed_or_nonfeed_response_cannot_replace_retained_events(tmp_path, monkeypatch, status, body):
    import requests
    feed = importlib.import_module('fetch_bgbl_events')
    response = requests.Response()
    response.status_code = status
    response._content = body
    target = tmp_path / 'events.jsonl'
    target.write_text('{"retained_fixture":true}\n')
    class Http:
        def get(self, *args, **kwargs):
            return response
    monkeypatch.setattr(feed, 'Http', lambda **kwargs: Http())
    monkeypatch.setattr(feed, 'snapshot_dir', lambda *args: tmp_path)
    with pytest.raises((requests.HTTPError, RuntimeError)):
        feed.main()
    assert target.read_text() == '{"retained_fixture":true}\n'


def test_bgbl_valid_feed_remains_successful(tmp_path, monkeypatch):
    import requests
    feed = importlib.import_module('fetch_bgbl_events')
    response = requests.Response()
    response.status_code = 200
    response._content = b'<rss><channel><item><title>BGBl fixture</title><link>https://www.recht.bund.de/eli/bund/bgbl-1/2026/1</link><pubDate>Wed, 30 Sep 2026 00:00:00 GMT</pubDate></item></channel></rss>'
    class Http:
        def get(self, *args, **kwargs):
            return response
    monkeypatch.setattr(feed, 'Http', lambda **kwargs: Http())
    monkeypatch.setattr(feed, 'snapshot_dir', lambda *args: tmp_path)
    assert feed.main() == 0
    row = json.loads((tmp_path / 'events.jsonl').read_text())
    assert row['time'] == '2026-09-30'
