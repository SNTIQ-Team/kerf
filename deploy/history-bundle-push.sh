#!/usr/bin/env bash
# Workstation → VPS: publish the compact law-history bundle.
#
# The heavy archive (Wayback/NeuRIS/BAYERN.RECHT raw bytes, legacy BGBl
# texts) stays in ~/.local/share/sntiq-legal/lexgraph-archive.  This exports
# only the parsed, de-duplicated captures, the date resolutions and the
# Bavarian records into a signed bundle, uploads it and installs it as
# /srv/sntiq-lexgraph/data/history_bundle/current (the daily refresh builds
# law_history.sqlite from it).  --publish-now also rebuilds law_history.sqlite
# into the VPS web/data and publishes a new API data generation immediately
# (under the refresh lock).
#
#   deploy/history-bundle-push.sh [--change-dates F] [--bavarian F]
#                                 [--publish-now] [--dry-run]
set -euo pipefail
cd "$(dirname "$0")/.."
REPO="$PWD"

ARCHIVE="${LEXGRAPH_HISTORY_ARCHIVE:-$HOME/.local/share/sntiq-legal/lexgraph-archive/history}"
HOST="${LEXGRAPH_VPS:-sntiq}"
REMOTE="${LEXGRAPH_VPS_ROOT:-/srv/sntiq-lexgraph}"
PYTHON="${LEXGRAPH_PYTHON:-python3}"
CHANGE_DATES="" BAVARIAN="" PUBLISH=0 DRY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --change-dates) CHANGE_DATES="$2"; shift 2 ;;
        --bavarian) BAVARIAN="$2"; shift 2 ;;
        --publish-now) PUBLISH=1; shift ;;
        --dry-run) DRY=1; shift ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
done
source "$REPO/deploy/workstation/archive-signing-key.sh"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$ARCHIVE/bundles/$STAMP"
args=(export --archive "$ARCHIVE" --out "$OUT" --key "$KEY")
[ -n "$CHANGE_DATES" ] && args+=(--change-dates "$CHANGE_DATES")
[ -n "$BAVARIAN" ] && args+=(--bavarian "$BAVARIAN")
echo "==> export bundle -> $OUT"
"$PYTHON" tools/history_bundle.py "${args[@]}"
"$PYTHON" tools/history_bundle.py verify "$OUT" \
    --signers "${LEXGRAPH_ARCHIVE_SIGNERS:-$HOME/.local/share/sntiq-legal/lexgraph-archive/offload/allowed_signers}"
du -sh "$OUT"
if [ "$DRY" = 1 ]; then echo "dry run: not uploaded"; exit 0; fi

echo "==> upload to $HOST:$REMOTE/data/history_bundle/incoming/$STAMP"
ssh "$HOST" "mkdir -p '$REMOTE/data/history_bundle/incoming'"
rsync -a --partial "$OUT/" "$HOST:$REMOTE/data/history_bundle/incoming/$STAMP/"
ssh "$HOST" "cd '$REMOTE' && venv/bin/python tools/history_bundle.py install \
    data/history_bundle/incoming/$STAMP && ls -l data/history_bundle/"

if [ "$PUBLISH" = 1 ]; then
    echo "==> rebuild law_history.sqlite and publish (refresh lock)"
    ssh "$HOST" "cd '$REMOTE' && flock -w 3600 /run/lock/lexgraph-refresh.lock \
        sh -c 'PATH=$REMOTE/venv/bin:\$PATH python3 tools/build_law_history.py \
            --data-dir web/data --bundle data/history_bundle/current \
            --bundle-signers /etc/sntiq/lexgraph-archive.allowed_signers \
            && deploy/publish-web-data.sh web/data'"
fi
echo "OK — bundle $STAMP installed"
