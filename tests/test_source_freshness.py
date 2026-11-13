"""Retained complete source data may be served, but failed refreshes stay visible."""
import json
import subprocess
import sys
from pathlib import Path

from api import main
from fastapi.testclient import TestClient


def test_health_reports_degraded_sources_without_failing_integrity_gate(tmp_path, monkeypatch):
    (tmp_path / 'summary.json').write_text(json.dumps({
        'built_at': '2026-09-30T05:00:00Z',
        'source_freshness': {'schema_version': 1, 'sources': {
            'bayern_recht': {'observed_on': '2026-09-17',
                'last_attempt_at': '2026-09-30T04:00:00Z',
                'last_result': 'failed', 'last_exit_code': 1,
                'last_successful_fetch_at': '2026-09-17T04:00:00Z',
                'stale_after_days': 2}}}}))
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, '_CACHE', {})
    monkeypatch.setattr(main, '_RETROSPECTIVE_SOURCE', None)
    monkeypatch.setattr(main, '_RETROSPECTIVE_MANIFEST', None)
    response = TestClient(main.app).get('/health')
    assert response.status_code == 200  # complete retained corpus is publishable
    body = response.json()
    assert body['status'] == 'degraded'
    assert body['data_integrity'] == 'ok'
    source = body['source_freshness']['sources']['bayern_recht']
    assert source['status'] == 'degraded'
    assert 'last_fetch_failed' in source['reasons']
    assert source['observed_on'] == '2026-09-17'


def test_failed_fetch_retains_last_success_and_snapshot(tmp_path):
    from api.source_freshness import build_freshness, record_attempt
    snapshots = tmp_path / 'snapshots'
    retained = snapshots / 'bayern_recht' / '2026-09-17'
    retained.mkdir(parents=True)
    state = tmp_path / 'attempts.json'
    record_attempt(state, 'bayern_recht', 0, '2026-09-17T04:00:00Z')
    record_attempt(state, 'bayern_recht', 1, '2026-09-30T04:00:00Z')
    body = build_freshness(snapshots, state, '2026-09-30T05:00:00Z')
    source = body['sources']['bayern_recht']
    assert body['status'] == 'degraded'
    assert source['last_successful_fetch_at'] == '2026-09-17T04:00:00Z'
    assert source['last_exit_code'] == 1
    assert source['observed_on'] == '2026-09-17'
    assert retained.is_dir()


def test_health_age_uses_successful_fetch_not_build_time(tmp_path):
    from api.source_freshness import build_freshness, record_attempt, source_health
    snapshots = tmp_path / 'snapshots'
    (snapshots / 'bayern_recht' / '2026-09-17').mkdir(parents=True)
    state = tmp_path / 'attempts.json'
    record_attempt(state, 'bayern_recht', 0, '2026-09-30T04:00:00Z')
    report = build_freshness(snapshots, state, '2026-09-30T05:00:00Z')
    assert report['sources']['bayern_recht']['status'] == 'ok'
    aged = source_health(report, '2026-10-04T05:00:00Z')
    assert aged['sources']['bayern_recht']['status'] == 'degraded'
    assert 'refresh_overdue' in aged['sources']['bayern_recht']['reasons']


def test_missing_attempt_tracking_is_unknown_not_fresh(tmp_path):
    from api.source_freshness import build_freshness
    snapshots = tmp_path / 'snapshots'
    (snapshots / 'bayern_recht' / '2026-09-17').mkdir(parents=True)
    result = build_freshness(snapshots, tmp_path / 'absent.json', '2026-09-30T05:00:00Z')
    assert result['status'] == 'degraded'
    assert 'fetch_tracking_unavailable' in result['sources']['bayern_recht']['reasons']


def test_fetch_wrapper_preserves_exit_code_and_records_outcome(tmp_path):
    target = tmp_path / 'fetches.json'
    script = Path(__file__).resolve().parents[1] / 'tools/source_freshness.py'
    result = subprocess.run([sys.executable, str(script), '--state', str(target),
        'run', '--source', 'bayern_recht', '--', sys.executable, '-c', 'raise SystemExit(7)'])
    assert result.returncode == 7
    row = json.loads(target.read_text())['sources']['bayern_recht']
    assert row['last_result'] == 'failed' and row['last_exit_code'] == 7


def test_operational_sidecar_discloses_observations_without_inventing_success(tmp_path, monkeypatch):
    from api.source_freshness import build_freshness
    snapshots = tmp_path / 'snapshots'
    (snapshots / 'bayern_recht' / '2026-09-17').mkdir(parents=True)
    report = build_freshness(snapshots, tmp_path / 'absent.json', '2026-09-30T05:00:00Z')
    path = tmp_path / 'operations.json'
    path.write_text(json.dumps(report))
    (tmp_path / 'summary.json').write_text(json.dumps({'built_at': '2026-09-29T05:00:00Z'}))
    monkeypatch.setattr(main, 'FRESHNESS_PATH', path)
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, '_CACHE', {})
    response = TestClient(main.app).get('/health')
    body = response.json()
    assert response.status_code == 200 and body['status'] == 'degraded'
    assert body['built_at'] == '2026-09-29T05:00:00Z'
    report = body['source_freshness']
    assert report['origin'] == 'operational_sidecar'
    assert report['operational_as_of'] == '2026-09-30T05:00:00Z'
    assert report['sources']['bayern_recht']['observed_on'] == '2026-09-17'
    assert 'last_successful_fetch_at' not in report['sources']['bayern_recht']


def test_metadata_exporter_does_not_fetch_or_build_corpus(tmp_path):
    snapshots = tmp_path / 'snapshots'
    (snapshots / 'gii' / '2026-09-29').mkdir(parents=True)
    target = tmp_path / 'report.json'
    script = Path(__file__).resolve().parents[1] / 'tools/source_freshness.py'
    subprocess.run([sys.executable, str(script), '--state', str(tmp_path / 'absent.json'),
        'export', '--snapshots', str(snapshots), '--output', str(target)], check=True)
    body = json.loads(target.read_text())
    assert body['sources']['gii']['observed_on'] == '2026-09-29'
    assert 'last_successful_fetch_at' not in body['sources']['gii']
    assert body['operational_as_of']


def test_invalid_operational_sidecar_keeps_summary_and_reports_tracking_failure(tmp_path, monkeypatch):
    path = tmp_path / 'operations.json'
    path.write_text(json.dumps({'schema_version': 1, 'sources': {}, 'operational_as_of': 'invalid'}))
    (tmp_path / 'summary.json').write_text(json.dumps({'built_at': 'retained'}))
    monkeypatch.setattr(main, 'FRESHNESS_PATH', path)
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(main, '_CACHE', {})
    response = TestClient(main.app).get('/health')
    assert response.status_code == 200
    body = response.json()
    assert body['built_at'] == 'retained' and body['status'] == 'degraded'
    assert body['source_freshness']['operational_tracking'] == 'unavailable'
