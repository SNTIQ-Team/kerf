#!/usr/bin/env bash
# Lightweight production refresh for explicitly watched legislative projects.
#
# Unlike refresh-server.sh, this deliberately avoids the corpus/history
# crawlers.  It refreshes the current Bundestag procedure snapshot and the
# narrow EUR-Lex watch list, advances the persistent watch state/history,
# rebuilds the static API data plane and publishes it.  The systemd timer runs
# it every four hours; terminal procedures remain in the archive but the narrow EU
# fetcher stops polling them after their terminal observation.
set -euo pipefail
cd "$(dirname "$0")/.."

LOCK_FILE="${KERF_REFRESH_LOCK:-/run/lock/kerf-refresh.lock}"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "procedure-watch: another Kerf refresh holds $LOCK_FILE; skipping"
    exit 0
fi

if [ -n "${KERF_PYTHON:-}" ]; then
    PYTHON="$KERF_PYTHON"
elif [ -x ./venv/bin/python ]; then
    PYTHON=./venv/bin/python
else
    PYTHON=python3
fi

# Freshness observations are independent of whether changed evidence requires a
# corpus publication. Export on ordinary success, no-change and failed fetches.
finish_freshness() {
    local status=$?
    trap - EXIT
    if [ -n "${KERF_FRESHNESS:-}" ]; then
        if ! "$PYTHON" tools/source_freshness.py export --output "$KERF_FRESHNESS"; then
            echo "[warn] operational freshness export failed" >&2
            [ "$status" -ne 0 ] || status=1
        fi
    fi
    exit "$status"
}
trap finish_freshness EXIT

fetch_source() {
    local source="$1"
    shift
    "$PYTHON" tools/source_freshness.py run --source "$source" -- "$@"
}

echo "==> [1/5] Bundestag DIP procedures"
fetch_source dip "$PYTHON" pipeline/fetch_dip.py

echo "==> [2/5] active EUR-Lex procedure watches"
# 75 (EX_TEMPFAIL): EUR-Lex was unreachable and the watch kept its last evidence;
# source freshness has recorded the failed fetch. As in refresh.sh, the watch state
# is then not advanced (diffing fallback against live evidence produced false
# evidence_changed rebuilds) and the run ends without failing. Any other non-zero
# status is a real failure and still fails the run.
eu_status=0
fetch_source eu_watch "$PYTHON" pipeline/fetch_eu_watch.py || eu_status=$?
if [ "$eu_status" -eq 75 ]; then
    echo "[warn] EUR-Lex unreachable, stale fallback retained — watch state not advanced"
    exit 0
elif [ "$eu_status" -ne 0 ]; then
    exit "$eu_status"
fi

echo "==> [3/5] persistent watch state + change-only history"
"$PYTHON" tools/update_procedure_watch.py

# A polling timestamp alone must not trigger the full corpus rebuild.
if "$PYTHON" -m tools.watch_publish_state needed; then
    :
else
    check_status=$?
    if [ "$check_status" -eq 3 ]; then
        echo "OK — no evidence change; existing publication retained"
        exit 0
    fi
    exit "$check_status"
fi

echo "==> [4/5] rebuild API data plane"
"$PYTHON" tools/build_web_data.py
test -s web/data/summary.json
# Keep the change history's pending/in-force states current (same inputs as
# the daily refresh; a failure keeps the previous law_history.sqlite).
history_args=(--data-dir web/data)
if [ -f data/history_bundle/current/manifest.json ]; then
    history_args+=(--bundle data/history_bundle/current)
    signers="${KERF_ARCHIVE_SIGNERS:-/etc/sntiq/kerf-archive.allowed_signers}"
    [ -f "$signers" ] && history_args+=(--bundle-signers "$signers")
fi
"$PYTHON" tools/build_law_history.py "${history_args[@]}" \
    || echo "[warn] law history rebuild failed — previous file kept"

echo "==> [5/5] publish -> /srv/sntiq-kerf-api/data/web-data"
deploy/publish-web-data.sh web/data
"$PYTHON" -m tools.watch_publish_state published

built=$(curl -fsS http://127.0.0.1:8002/health \
  | "$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["built_at"])')
echo "OK — procedure watch published; live built_at: $built"
