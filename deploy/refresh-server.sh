#!/usr/bin/env bash
# Daily realtime retrieval ON the production VPS (systemd:
# kerf-refresh.timer, 04:37 UTC). Runs the full pipeline (peak RSS
# ~100 MB — fits the 1 GB host at idle-priority), publishes the fresh
# web data plane to the API and restarts it (data is cached in-process).
#
# OPENROUTER_API_KEY (via /etc/sntiq/kerf.env) enables the LLM
# digest step inside refresh.sh; without it the digest is skipped.
set -euo pipefail
cd "$(dirname "$0")/.."

# The full daily refresh and the twice-daily watched-procedure refresh both
# rebuild/publish web/data.  A shared non-blocking lock keeps a delayed timer
# or manual run from racing the other job.  The timer will try again at its
# next scheduled observation; no half-built data plane is published.
LOCK_FILE="${KERF_REFRESH_LOCK:-/run/lock/kerf-refresh.lock}"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "full refresh: another Kerf refresh holds $LOCK_FILE; skipping"
    exit 0
fi
export KERF_REFRESH_LOCK="$LOCK_FILE"
export KERF_REFRESH_LOCK_FD=9
# Capture new pre-2023 sources and use signed receipts for archive offload.
export KERF_LEGACY_CAPTURE_ONLY=1
export KERF_ARCHIVE_OFFLOAD=1

./refresh.sh

echo "==> publish web data -> /srv/sntiq-kerf-api/data/web-data"
deploy/publish-web-data.sh web/data

built=$(curl -fsS http://127.0.0.1:8002/health \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["built_at"])')
echo "OK — live built_at: $built"
