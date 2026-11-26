#!/usr/bin/env bash
# Run a full refresh and upload the validated generation for publication.
set -euo pipefail
cd "$(dirname "$0")/.."

# Serialize manual/user-timer runs on this host.  Publication also takes the
# production /run/lock lock below; a local lock alone cannot serialize with a
# timer running on the VPS.
LOCK_FILE="${KERF_REFRESH_LOCK:-${XDG_RUNTIME_DIR:-/tmp}/kerf-refresh.lock}"
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "refresh-and-push: another Kerf refresh holds $LOCK_FILE; skipping"
    exit 0
fi
export KERF_REFRESH_LOCK="$LOCK_FILE"
export KERF_REFRESH_LOCK_FD=9

./refresh.sh

stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
remote_stage="/srv/sntiq-kerf-api/data/web-data.incoming-$stamp"
cleanup_remote() {
    ssh sntiq "rm -rf -- '$remote_stage'" >/dev/null 2>&1 || true
}
trap cleanup_remote EXIT

echo "==> upload complete generation -> sntiq:$remote_stage"
upload_args=(-az --delete)
# Reuse unchanged bytes from the immutable serving release while the incoming
# generation is uploaded. The publisher independently checks its disk reserve.
if ssh sntiq 'test -L /srv/sntiq-kerf-api/data/web-data && test -d /srv/sntiq-kerf-api/data/web-data'; then
    upload_args+=(--checksum --no-times --no-owner --no-group --no-perms
                  --link-dest=/srv/sntiq-kerf-api/data/web-data)
fi
rsync "${upload_args[@]}" web/data/ "sntiq:$remote_stage/"

echo "==> acquire production lock + atomic publish"
ssh sntiq "flock -w 1800 /run/lock/kerf-refresh.lock \
  /srv/sntiq-kerf/deploy/publish-web-data.sh '$remote_stage' --consume-upload"
cleanup_remote
trap - EXIT

built=$(curl -fsS https://api.sntiq.com/kerf/health \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["built_at"])')
echo "OK — live built_at: $built"
