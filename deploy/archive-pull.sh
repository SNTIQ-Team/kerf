#!/usr/bin/env bash
# Daily workstation pull of the Lexgraph source archives (user timer
# lexgraph-archive-pull.timer, Persistent=true: a missed day catches up).
#
#   1. the VPS lists every archived file not yet receipted, with SHA-256
#   2. rsync pulls the pre-2023 BGBl store and all dated snapshots
#      (append-only: never --delete; VPS deletions never reach this copy)
#   3. every listed file is re-hashed here; a signed receipt goes back
#   4. the legacy candidates are rebuilt + attested when inputs changed
#   5. the VPS deletes receipted bytes it no longer reads (14-day window)
#
# Options: --no-rebuild  skip step 4    --no-prune  skip step 5
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="${LEXGRAPH_ARCHIVE:-$HOME/.local/share/sntiq-legal/lexgraph-archive}"
KEY="${LEXGRAPH_ARCHIVE_KEY:-$HOME/.config/sntiq/lexgraph-archive-signing-key}"
PY="${LEXGRAPH_PYTHON:-$REPO/.venv/bin/python}"
REMOTE="${LEXGRAPH_REMOTE:-sntiq}"
REMOTE_ROOT="${LEXGRAPH_REMOTE_ROOT:-/srv/sntiq-lexgraph}"
REMOTE_PY="$REMOTE_ROOT/venv/bin/python"
REBUILD=1; PRUNE=1
for arg in "$@"; do
    case "$arg" in
        --no-rebuild) REBUILD=0 ;;
        --no-prune) PRUNE=0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

mkdir -p "$ARCHIVE/data" "$ARCHIVE/offload/manifests" "$ARCHIVE/offload/receipts"
exec 8>"${XDG_RUNTIME_DIR:-/tmp}/lexgraph-archive-pull.lock"
if ! flock -n 8; then
    echo "archive-pull: another run holds the lock; skipping"
    exit 75   # EX_TEMPFAIL; the unit treats it as success
fi
test -r "$KEY" || { echo "signing key missing: $KEY" >&2; exit 1; }

echo "==> [1/5] VPS manifest of unreceipted archive files"
info=$(ssh "$REMOTE" "cd $REMOTE_ROOT && $REMOTE_PY tools/archive_offload.py manifest")
echo "    $info"
manifest=$(printf '%s' "$info" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["manifest"])')

echo "==> [2/5] pull into $ARCHIVE/data (append-only)"
rsync -a --partial --exclude='.writing-*' --exclude='*.tmp' --exclude='.compact.lock' \
    "$REMOTE:$REMOTE_ROOT/data/legacy_bgbl_history" \
    "$REMOTE:$REMOTE_ROOT/data/snapshots" "$ARCHIVE/data/"
rsync -a "$REMOTE:$REMOTE_ROOT/data/archive-offload/manifests/$manifest" \
    "$ARCHIVE/offload/manifests/"

echo "==> [3/5] re-hash every listed file; signed receipt back to the VPS"
"$PY" "$REPO/tools/archive_offload.py" receipt \
    --manifest "$ARCHIVE/offload/manifests/$manifest" --archive "$ARCHIVE/data" \
    --key "$KEY" --out "$ARCHIVE/offload/receipts" --allow-excluded
receipt="${manifest%.json}.receipt.json"
rsync -a "$ARCHIVE/offload/receipts/$receipt" "$ARCHIVE/offload/receipts/$receipt.sig" \
    "$REMOTE:$REMOTE_ROOT/data/archive-offload/receipts/"

if [ "$REBUILD" = 1 ]; then
    echo "==> [4/5] legacy rebuild + attestation (only when inputs or parser changed)"
    "$REPO/deploy/legacy-rebuild-push.sh" --if-changed --no-pull --receipt "$receipt"
fi

if [ "$PRUNE" = 1 ]; then
    echo "==> [5/5] VPS prune of receipted bytes it no longer reads"
    ssh "$REMOTE" "cd $REMOTE_ROOT && $REMOTE_PY tools/archive_offload.py prune --apply"
fi
echo "archive-pull: done ($(du -sh "$ARCHIVE" | cut -f1) in $ARCHIVE)"
