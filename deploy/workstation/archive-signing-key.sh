# Sourced by the workstation archive jobs after they set REPO.
KEY="${KERF_ARCHIVE_KEY:-}"
if [[ -z "$KEY" ]]; then
    KEY=$(git -C "$REPO" config --path --get kerf.archiveSigningKey) || KEY=""
fi
if [[ ! -f "$KEY" || ! -r "$KEY" ]]; then
    echo "Configure a readable signing key with KERF_ARCHIVE_KEY or git config kerf.archiveSigningKey." >&2
    exit 1
fi
