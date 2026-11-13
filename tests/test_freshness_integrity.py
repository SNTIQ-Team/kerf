"""Partial/corrupt operational metadata must not certify healthy coverage."""
import json
import fcntl
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from api.source_freshness import SOURCE_CADENCE_DAYS, record_attempt, source_health

NOW = '2026-09-30T12:00:00Z'


def healthy_report():
    return {'schema_version': 1, 'sources': {
        name: {'last_attempt_at': '2026-09-30T10:00:00Z',
               'last_successful_fetch_at': '2026-09-30T10:00:00Z',
               'last_result': 'ok', 'last_exit_code': 0,
               'observed_on': '2026-09-01'}
        for name in SOURCE_CADENCE_DAYS}}


def test_a_partial_sources_map_cannot_certify_the_entire_pipeline():
    report = healthy_report()
    del report['sources']['bayern_recht']
    result = source_health(report, NOW)
    assert result['status'] == 'degraded'
    assert set(result['sources']) == set(SOURCE_CADENCE_DAYS)
    assert 'fetch_tracking_unavailable' in result['sources']['bayern_recht']['reasons']


@pytest.mark.parametrize('observed', ['not-a-date', '2026-02-31', '2027-01-01', 123, ['2026-09-01']])
def test_invalid_retained_observation_does_not_certify_a_source(observed):
    report = healthy_report()
    report['sources']['gii']['observed_on'] = observed
    result = source_health(report, NOW)
    assert result['status'] == 'degraded'
    assert result['sources']['gii']['status'] == 'degraded'


def test_complete_report_accepts_old_unchanged_content_after_recent_poll():
    result = source_health(healthy_report(), NOW)
    assert result['status'] == 'ok'
    assert all(row['status'] == 'ok' for row in result['sources'].values())


def test_observation_calendar_day_allows_source_local_timezone():
    report = healthy_report()
    report['sources']['gii']['observed_on'] = '2026-10-01'
    result = source_health(report, '2026-09-30T23:30:00Z')
    assert result['sources']['gii']['status'] == 'ok'


def test_late_record_of_older_success_cannot_hide_a_newer_failure(tmp_path):
    path = tmp_path / 'attempts.json'
    record_attempt(path, 'gii', 1, '2026-09-30T10:00:00Z')
    record_attempt(path, 'gii', 0, '2026-09-29T10:00:00Z')
    row = json.loads(path.read_text())['sources']['gii']
    assert row['last_result'] == 'failed'
    assert row['last_exit_code'] == 1
    assert row['last_attempt_at'] == '2026-09-30T10:00:00Z'
    assert row['last_successful_fetch_at'] == '2026-09-29T10:00:00Z'


def test_late_record_of_older_success_never_regresses_success_time(tmp_path):
    path = tmp_path / 'attempts.json'
    record_attempt(path, 'gii', 0, '2026-09-30T10:00:00Z')
    record_attempt(path, 'gii', 0, '2026-09-29T10:00:00Z')
    row = json.loads(path.read_text())['sources']['gii']
    assert row['last_attempt_at'] == row['last_successful_fetch_at'] == '2026-09-30T10:00:00Z'


def test_attempt_order_uses_instants_not_lexical_timezone_strings(tmp_path):
    path = tmp_path / 'attempts.json'
    record_attempt(path, 'gii', 0, '2026-09-30T10:30:00+02:00')
    record_attempt(path, 'gii', 1, '2026-09-30T09:00:00Z')
    row = json.loads(path.read_text())['sources']['gii']
    assert row['last_result'] == 'failed'
    assert row['last_attempt_at'] == '2026-09-30T09:00:00Z'


@pytest.mark.parametrize('first,second', [(1, 0), (0, 1)])
def test_conflicting_outcomes_at_one_instant_remain_degraded(tmp_path, first, second):
    path = tmp_path / 'attempts.json'
    record_attempt(path, 'gii', first, '2026-09-30T10:00:00Z')
    record_attempt(path, 'gii', second, '2026-09-30T12:00:00+02:00')
    # Replaying either record is not evidence that the conflict was resolved.
    record_attempt(path, 'gii', first, '2026-09-30T10:00:00Z')
    row = json.loads(path.read_text())['sources']['gii']
    assert row['last_result'] == 'conflicting'
    assert row['last_exit_code'] is None
    assert row['conflicting_exit_codes'] == [0, 1]
    assert row['last_successful_fetch_at']
    report = healthy_report()
    report['sources']['gii'].update(row)
    result = source_health(report, NOW)
    assert result['status'] == 'degraded'
    assert 'conflicting_fetch_outcomes' in result['sources']['gii']['reasons']
    record_attempt(path, 'gii', 0, '2026-09-30T10:00:00.000001Z')
    recovered = json.loads(path.read_text())['sources']['gii']
    assert recovered['last_result'] == 'ok'
    assert 'conflicting_exit_codes' not in recovered


def test_attempt_update_waits_for_other_process_state_lock(tmp_path):
    path = tmp_path / 'attempts.json'
    lock = path.with_name(path.name + '.lock')
    started, completed = Event(), Event()

    def update():
        started.set()
        record_attempt(path, 'gii', 0, NOW)
        completed.set()

    with ThreadPoolExecutor(max_workers=1) as pool, lock.open('a') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        future = pool.submit(update)
        try:
            assert started.wait(2)
            assert not completed.wait(0.1), 'state write bypassed the publication lock'
            # Simulate the other job completing while this update is queued.
            path.write_text(json.dumps({'schema_version': 1, 'sources': {
                'dip': {'last_attempt_at': NOW, 'last_result': 'failed', 'last_exit_code': 7}}}))
        finally:
            fcntl.flock(held, fcntl.LOCK_UN)
        future.result(timeout=2)
    sources = json.loads(path.read_text())['sources']
    assert sources['dip']['last_exit_code'] == 7
    assert sources['gii']['last_result'] == 'ok'
