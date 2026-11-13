"""Source fetch outcomes and retained-snapshot freshness, separate from integrity."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile

# Daily source jobs and explicitly weekly text refreshes. Archive/backfill
# completion is not called freshness: these may intentionally be incomplete.
SOURCE_CADENCE_DAYS = {
    'dip': 2, 'eu_watch': 2, 'bgbl_events': 2, 'gii': 2,
    'bgbl_documents': 2, 'neuris_changelog': 2,
    'bayern_recht': 2, 'gvbl_events': 2, 'bay_landtag': 2,
    'eu_layer': 2, 'eu_index': 2,
    'eu_texts': 14, 'emrk': 14, 'bayern_recht_archive': 14,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('fetch timestamps require a timezone')
    return parsed.astimezone(timezone.utc)


def record_attempt(path: Path, source: str, exit_code: int, at: str) -> None:
    """Atomic metadata update; source bytes and prior success are never changed."""
    if source not in SOURCE_CADENCE_DAYS:
        raise ValueError('unknown source freshness family')
    instant = _instant(at)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Daily refresh and procedure-watch may finish in separate processes.
    # Atomic replacement alone cannot protect their read/modify/write cycle.
    with path.with_name(path.name + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _record_attempt_locked(path, source, exit_code, at, instant)


def _record_attempt_locked(path: Path, source: str, exit_code: int,
                           at: str, instant: datetime) -> None:
    body = json.loads(path.read_text()) if path.exists() else {'schema_version': 1, 'sources': {}}
    if not isinstance(body, dict) or body.get('schema_version') != 1 or not isinstance(body.get('sources'), dict):
        raise ValueError('invalid source freshness state')
    previous = body['sources'].get(source, {})
    if not isinstance(previous, dict):
        raise ValueError('invalid source freshness row')
    # A journal-backed record may arrive after a later fetch. Preserve event
    # order rather than treating the CLI invocation order as source truth.
    previous_at = (_instant(previous['last_attempt_at'])
                   if previous.get('last_attempt_at') else None)
    previous_success = (_instant(previous['last_successful_fetch_at'])
                        if previous.get('last_successful_fetch_at') else None)
    row = dict(previous)
    if previous_at == instant:
        codes = set(previous.get('conflicting_exit_codes') or
                    [previous.get('last_exit_code')]) | {exit_code}
        if len(codes) > 1:
            # Two real observations can share a coarse imported timestamp.
            # Neither arrival order certifies recovery; retain the conflict.
            row.update(last_result='conflicting', last_exit_code=None,
                       conflicting_exit_codes=sorted(codes))
    elif previous_at is None or instant > previous_at:
        row.update(last_attempt_at=at, last_exit_code=exit_code,
                   last_result='ok' if exit_code == 0 else 'failed')
        row.pop('conflicting_exit_codes', None)
    if exit_code == 0 and (previous_success is None or instant > previous_success):
        row['last_successful_fetch_at'] = at
    body['sources'][source] = row
    write_json_atomic(path, body)


def write_json_atomic(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(body, handle, sort_keys=True)
            handle.write('\n')
        os.chmod(temporary, 0o644)  # public operational metadata only
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def source_health(report: dict | None, checked_at: str | None = None) -> dict:
    """Re-evaluate age at request time: an old healthy build cannot stay fresh."""
    at = checked_at or utc_now()
    now = _instant(at)
    if not isinstance(report, dict) or not isinstance(report.get('sources'), dict):
        return {'schema_version': 1, 'status': 'unknown', 'checked_at': at,
                'sources': {}, 'reasons': ['fetch_tracking_unavailable']}
    sources = {}
    for source in sorted(set(SOURCE_CADENCE_DAYS) | set(report['sources'])):
        raw = report['sources'].get(source)
        row = dict(raw) if isinstance(raw, dict) else {}
        reasons = []
        if source not in SOURCE_CADENCE_DAYS:
            reasons.append('unknown_source_family')
        if row.get('last_result') == 'failed':
            reasons.append('last_fetch_failed')
        if row.get('last_result') == 'conflicting':
            reasons.append('conflicting_fetch_outcomes')
        if row.get('last_result') not in ('ok', 'failed', 'conflicting'):
            reasons.append('fetch_tracking_unavailable')
        success = row.get('last_successful_fetch_at')
        if success:
            try:
                age = (now - _instant(success)).total_seconds() / 86400
                if age < 0:
                    reasons.append('future_fetch_timestamp')
                elif age > SOURCE_CADENCE_DAYS.get(source, 2):
                    reasons.append('refresh_overdue')
                row['days_since_successful_fetch'] = round(max(0, age), 3)
            except (AttributeError, TypeError, ValueError):
                reasons.append('invalid_fetch_timestamp')
        else:
            reasons.append('no_successful_fetch_recorded')
        observed = row.get('observed_on')
        if observed is None or observed == '':
            reasons.append('no_retained_snapshot')
        else:
            try:
                if not isinstance(observed, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', observed):
                    raise ValueError('invalid observation date')
                # Directory names are local calendar days, with no retained
                # timezone. Allow today's date anywhere (up to UTC+14), not
                # an invented UTC instant for a source observation.
                if date.fromisoformat(observed) > (now + timedelta(hours=14)).date():
                    reasons.append('future_snapshot_date')
            except (TypeError, ValueError):
                reasons.append('invalid_snapshot_date')
        row.update(status='degraded' if reasons else 'ok', reasons=reasons)
        sources[source] = row
    return {'schema_version': 1, 'status': 'degraded' if not sources or any(
                row['status'] != 'ok' for row in sources.values()) else 'ok',
            'checked_at': at, 'sources': sources,
            'semantics': 'fetch freshness only; retained data integrity is checked separately'}


def build_freshness(snapshots: Path, attempts_path: Path, built_at: str) -> dict:
    try:
        attempts = json.loads(attempts_path.read_text())
        if attempts.get('schema_version') != 1 or not isinstance(attempts.get('sources'), dict):
            attempts = {}
    except (OSError, ValueError, AttributeError):
        attempts = {}
    sources = {}
    for source, cadence in SOURCE_CADENCE_DAYS.items():
        base = snapshots / source
        days = sorted(p.name for p in base.iterdir()
                      if p.is_dir() and re.fullmatch(r'\d{4}-\d{2}-\d{2}', p.name)) if base.is_dir() else []
        observed = days[-1] if days else None
        if observed:
            try:
                date.fromisoformat(observed)
            except ValueError:
                observed = None
        raw = (attempts.get('sources') or {}).get(source)
        row = dict(raw) if isinstance(raw, dict) else {}
        row.update(observed_on=observed, observed_on_basis='latest_retained_snapshot_directory',
                   stale_after_days=cadence)
        sources[source] = row
    return {**source_health({'sources': sources}, built_at),
            'operational_as_of': built_at,
            'observation_scope': 'retained pipeline snapshots; not a published corpus selection'}
