#!/usr/bin/env bash
# Rebuild the pre-2023 BGBl amendment candidates on the workstation against
# the complete archive, sign an attestation and install it on the VPS.
#
# After ANY change to the legacy parser (pipeline/backfill_legacy_bgbl_history.py,
# backfill_bgbl_history.py, patch_parser.py, fetch_bgbl_documents.py,
# api/page_furniture.py), once the code is on main and deployed:
#
#     deploy/legacy-rebuild-push.sh
#
# It pulls the newest sources, rebuilds with this checkout's parser, verifies
# the result, pushes it and checks the VPS accepts it.  The VPS build then
# serves exactly the attested rows; until this runs it keeps serving the
# previous attestation (the build log names both parser hashes) — a parser
# change can no longer break the daily refresh.
#
#     deploy/legacy-rebuild-push.sh --dry-run   # rebuild + attest locally only
#
# (--if-changed, --no-pull and --receipt R are used by deploy/archive-pull.sh)
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ARCHIVE="${LEXGRAPH_ARCHIVE:-$HOME/.local/share/sntiq-legal/lexgraph-archive}"
PY="${LEXGRAPH_PYTHON:-$REPO/.venv/bin/python}"
REMOTE="${LEXGRAPH_REMOTE:-sntiq}"
REMOTE_ROOT="${LEXGRAPH_REMOTE_ROOT:-/srv/sntiq-lexgraph}"
REMOTE_PY="$REMOTE_ROOT/venv/bin/python"
DRY=0; IF_CHANGED=0; PULL=1; RECEIPT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY=1 ;;
        --if-changed) IF_CHANGED=1 ;;
        --no-pull) PULL=0 ;;
        --receipt) RECEIPT="$2"; shift ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done
source "$REPO/deploy/workstation/archive-signing-key.sh"
export LEXGRAPH_ARCHIVE_SIGNERS="$ARCHIVE/offload/allowed_signers"
mkdir -p "$ARCHIVE/offload"
exec 7>"${XDG_RUNTIME_DIR:-/tmp}/lexgraph-legacy-rebuild.lock"
flock -w 3600 7 || { echo "legacy rebuild: lock busy" >&2; exit 75; }

if [ "$PULL" = 1 ]; then
    # A pull already in progress (exit 75) must finish before rebuilding.
    until "$REPO/deploy/archive-pull.sh" --no-rebuild --no-prune; do
        [ $? -eq 75 ] || exit 1
        sleep 30
    done
    RECEIPT=$(ls "$ARCHIVE/offload/receipts/" | grep -E '\.receipt\.json$' | tail -1)
fi

inputs=$("$PY" - "$ARCHIVE" "$REPO" <<'PY'
import hashlib, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]) / "pipeline"))
from archive_receipts import parser_identity, sha256_file
archive = Path(sys.argv[1])
print(json.dumps({
    "checkpoint": sha256_file(archive / "data/legacy_bgbl_history/checkpoint.json"),
    "parser": parser_identity(Path(sys.argv[2]))["sha256"]}, sort_keys=True))
PY
)
state="$ARCHIVE/offload/last-attestation.json"
if [ "$IF_CHANGED" = 1 ] && [ -f "$state" ] && [ "$(cat "$state")" = "$inputs" ]; then
    echo "legacy rebuild: sources and parser unchanged since the last attestation"
    exit 0
fi

stage=$(mktemp -d "$ARCHIVE/rebuild.XXXXXX")
trap 'rm -rf -- "$stage"' EXIT
echo "==> rebuild against $ARCHIVE/data/legacy_bgbl_history (parser of $(git -C "$REPO" rev-parse --short HEAD))"
TZ=UTC LEXGRAPH_SNAPSHOTS="$ARCHIVE/data/snapshots" \
    "$PY" "$REPO/pipeline/backfill_legacy_bgbl_history.py" --rebuild \
    --archive-root "$ARCHIVE/data/legacy_bgbl_history" \
    --snapshot-root "$stage/out" --max-archive-mib 0 --min-free-mib 1024 \
    --start 1976-01-01 \
    --wahlperiode 8 --wahlperiode 9 --wahlperiode 10 --wahlperiode 11 \
    --wahlperiode 12 --wahlperiode 13 --wahlperiode 14 --wahlperiode 15 \
    --wahlperiode 16 --wahlperiode 17 --wahlperiode 18 --wahlperiode 19 --wahlperiode 20 \
    > "$stage/rebuild.log"
snap=$(find "$stage/out" -mindepth 1 -maxdepth 1 -type d | sort | tail -1)
"$PY" "$REPO/tools/archive_offload.py" attest --snapshot-dir "$snap" --key "$KEY" \
    ${RECEIPT:+--receipt "$RECEIPT"} --git-commit "$(git -C "$REPO" rev-parse HEAD)"
"$PY" - "$snap" "$REPO" <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[2]) / "pipeline"))
from archive_receipts import verify_attestation
value = verify_attestation(Path(sys.argv[1]))
print(f"    verified locally: {value['candidate_count']} candidate rows from "
      f"{value['verified_documents']} verified documents, parser "
      f"{value['parser']['sha256'][:12]}")
PY
if [ "$DRY" = 1 ]; then
    echo "dry run: attested rebuild left in $snap (not pushed)"
    trap - EXIT
    exit 0
fi

incoming="$REMOTE_ROOT/data/archive-offload/incoming/$(date -u +%Y%m%dT%H%M%SZ)"
echo "==> push + install on the VPS"
ssh "$REMOTE" "mkdir -p $incoming"
rsync -a "$snap/" "$REMOTE:$incoming/"
ssh "$REMOTE" "cd $REMOTE_ROOT && $REMOTE_PY tools/archive_offload.py install-legacy $incoming"
ssh "$REMOTE" "cd $REMOTE_ROOT && $REMOTE_PY tools/archive_offload.py status"
printf '%s' "$inputs" > "$state"
