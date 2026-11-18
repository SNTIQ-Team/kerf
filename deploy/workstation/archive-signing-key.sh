# Sourced by the archive worker archive jobs after they set REPO.
KEY="${LEXGRAPH_ARCHIVE_KEY:-}"
if [[ -z "$KEY" ]]; then
    KEY=$(git -C "$REPO" config --path --get lexgraph.archiveSigningKey) || KEY=""
fi
if [[ ! -f "$KEY" || ! -r "$KEY" ]]; then
    echo "Configure a readable signing key with LEXGRAPH_ARCHIVE_KEY or git config lexgraph.archiveSigningKey." >&2
    exit 1
fi
