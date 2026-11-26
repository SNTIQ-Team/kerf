"""deploy/refresh-procedure-watch.sh: a stale EUR-Lex fallback degrades, it does not fail.

The production timer runs this script every four hours. When EUR-Lex refuses the
server, fetch_eu_watch keeps the last evidence and exits EX_TEMPFAIL (75). Like
refresh.sh, the script then leaves the watch state alone (fallback evidence must
never be diffed against live evidence) and ends successfully; a real failure of the
watcher still fails the run.
"""
import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / 'deploy' / 'refresh-procedure-watch.sh'

# Stands in for the Python interpreter: logs every invocation, lets the EUR-Lex
# watcher exit with STUB_EU_RC, and reports "no evidence change" to the publish check.
STUB = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$*" in
  *pipeline/fetch_eu_watch.py*) exit "${STUB_EU_RC:-0}" ;;
  *"-m tools.watch_publish_state"*) exit 3 ;;
esac
exit 0
"""


def run_script(tmp_path: Path, eu_rc: int) -> tuple[subprocess.CompletedProcess, str]:
    stub = tmp_path / 'python-stub'
    stub.write_text(STUB)
    stub.chmod(0o755)
    log = tmp_path / 'calls.log'
    env = {
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
        'KERF_PYTHON': str(stub),
        'KERF_REFRESH_LOCK': str(tmp_path / 'refresh.lock'),
        'STUB_LOG': str(log),
        'STUB_EU_RC': str(eu_rc),
    }
    done = subprocess.run(['bash', str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    return done, log.read_text() if log.exists() else ''


def test_stale_fallback_ends_the_run_successfully_without_advancing_state(tmp_path):
    done, calls = run_script(tmp_path, eu_rc=75)
    assert done.returncode == 0, done.stdout + done.stderr
    assert 'stale fallback' in done.stdout + done.stderr
    assert 'tools/update_procedure_watch.py' not in calls


@pytest.mark.parametrize('eu_rc', [1, 2])
def test_a_real_watcher_failure_still_fails_the_run(tmp_path, eu_rc):
    done, calls = run_script(tmp_path, eu_rc=eu_rc)
    assert done.returncode == eu_rc
    assert 'tools/update_procedure_watch.py' not in calls


def test_a_successful_poll_advances_the_watch_state(tmp_path):
    done, calls = run_script(tmp_path, eu_rc=0)
    assert done.returncode == 0, done.stdout + done.stderr
    assert 'tools/update_procedure_watch.py' in calls
