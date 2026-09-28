from datetime import date
from fastapi.testclient import TestClient
from api import main
from api.retrospective_store import RetrospectiveNotFound, RetrospectiveIntegrityError
from tools.archived_reader_store import ArchivedReaderError


def configure(monkeypatch):
    act = {'id': 'fed_testg'}
    monkeypatch.setattr(main, '_load_act', lambda aid: act)
    monkeypatch.setattr(main, '_retrospective_manifest', lambda **kw: {'fixture': True})
    return act


def test_reader_uses_same_retrospective_state_before_observation(monkeypatch):
    act = configure(monkeypatch)
    state = {'raw_cas': True}
    monkeypatch.setattr(main, 'resolve_interval', lambda *a, **kw: {'state_sha256': 'a' * 64})
    monkeypatch.setattr(main, 'load_interval_state', lambda *a: state)
    monkeypatch.setattr(main, 'load_observed_state', lambda *a: (_ for _ in ()).throw(AssertionError('observation must not override interval')))
    def load(directory, current, selected, *, requested_at):
        assert current is act and selected == {**state, 'state_sha256': 'a' * 64} and requested_at == '2026-07-16'
        assert 'state_sha256' not in state
        return {'act_id': current['id'], 'state_sha256': selected['state_sha256'], 'requested_at': requested_at}
    monkeypatch.setattr(main, 'load_archived_reader', load)
    response = TestClient(main.app).get('/acts/fed_testg/reader?at=2026-07-16')
    assert response.status_code == 200
    assert response.json()['state_sha256'] == 'a' * 64


def test_reader_missing_interval_falls_back_to_exact_observation(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(main, 'resolve_interval', lambda *a, **kw: (_ for _ in ()).throw(RetrospectiveNotFound('missing')))
    monkeypatch.setattr(main, 'load_observed_state', lambda *a: {'observation': True})
    monkeypatch.setattr(main, 'load_archived_reader', lambda d, a, s, **kw: {'observed': s['observation']})
    assert TestClient(main.app).get('/acts/fed_testg/reader?at=2026-07-16').json() == {'observed': True}


def test_reader_does_not_fall_back_after_state_integrity_failure(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(main, 'resolve_interval', lambda *a, **kw: (_ for _ in ()).throw(RetrospectiveIntegrityError('broken CAS')))
    monkeypatch.setattr(main, 'load_observed_state', lambda *a: (_ for _ in ()).throw(AssertionError('unsafe fallback')))
    assert TestClient(main.app).get('/acts/fed_testg/reader?at=2026-07-16').status_code == 503


def test_reader_missing_historical_structure_does_not_return_head(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(main, 'resolve_interval', lambda *a, **kw: {'state_sha256': 'a' * 64})
    monkeypatch.setattr(main, 'load_interval_state', lambda *a: {'state': True})
    monkeypatch.setattr(main, 'load_archived_reader', lambda *a, **kw: None)
    monkeypatch.setattr(main, 'load_sidecar', lambda *a: (_ for _ in ()).throw(AssertionError('HEAD must not masquerade as history')))
    client = TestClient(main.app)
    assert client.get('/acts/fed_testg/reader?at=2026-07-16').status_code == 404
    assert client.get('/acts/fed_testg/reader?at=invalid').status_code == 422
    assert client.get('/acts/fed_testg/reader?as_of=2026-07-16T00:00:00Z').status_code == 422


def test_reader_invalid_structure_fails_closed(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(main, 'resolve_interval', lambda *a, **kw: {'state_sha256': 'a' * 64})
    monkeypatch.setattr(main, 'load_interval_state', lambda *a: {'state': True})
    monkeypatch.setattr(main, 'load_archived_reader', lambda *a, **kw: (_ for _ in ()).throw(ArchivedReaderError('text differs')))
    assert TestClient(main.app).get('/acts/fed_testg/reader?at=2026-07-16').status_code == 503
