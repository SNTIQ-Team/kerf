#!/usr/bin/env bash
# Lexgraph realtime refresh: pull the live legislative state across
# Bund / Bayern / EU / Länder and rebuild the arena. Run on cron.
#
# Fetch steps degrade gracefully (a flaky source must not kill the
# arena rebuild); only the final build is fatal. Fetchers refuse to
# overwrite a good same-day snapshot with empty output on their own.
set -euo pipefail
cd "$(dirname "$0")"

step() { echo "==> [$1] $2"; shift 2; "$@" || echo "[warn] step degraded — continuing"; }

# Record fetch outcomes independently of the release timestamp. Never capture
# command output or credentials; a failed fetch keeps the last complete data.
fetch_source() {
    local source="$1"
    shift
    python3 tools/source_freshness.py run --source "$source" -- "$@"
}

WATCH_SOURCES_OK=1
echo "==> [ 1/24] DIP legislative pipeline (Bund, intraday)"
fetch_source dip python3 pipeline/fetch_dip.py || {
    echo "[warn] DIP refresh degraded — watch state will not be advanced"
    WATCH_SOURCES_OK=0
}
echo "==> [ 2/24] explicit EUR-Lex procedure watches"
fetch_source eu_watch python3 pipeline/fetch_eu_watch.py || {
    echo "[warn] EUR-Lex watch refresh degraded — watch state will not be advanced"
    WATCH_SOURCES_OK=0
}
if [ "$WATCH_SOURCES_OK" -eq 1 ]; then
    step " 3/24" "persistent procedure state + change-only history" \
        python3 tools/update_procedure_watch.py
else
    echo "==> [ 3/24] watch state skipped (one official refresh failed)"
fi
step " 4/24" "BGBl promulgation events (daily feed)"       fetch_source bgbl_events python3 pipeline/fetch_bgbl_events.py
step " 5/24" "GII corpus HEAD (lags days-weeks)"           fetch_source gii python3 pipeline/fetch_gii.py --budget-mib 64
step " 6/24" "archive complete official GII states"       python3 tools/archive_gii_states.py
step " 7/24" "final BGBl documents + integrity/text"       fetch_source bgbl_documents python3 pipeline/fetch_bgbl_documents.py
# Complete recht.bund.de issue index (FNA of every amended act): the 2023+
# inventory also captures amendments that were never an act's latest
# "zuletzt geändert" note.  Daily: the current year's tail (+ previous year
# in January); the backlog is bounded by --max-new-index per run.
INDEX_YEARS="$(date -u +%Y)"
if [ "$(date -u +%m)" = "01" ]; then INDEX_YEARS="$(( $(date -u +%Y) - 1 )),$INDEX_YEARS"; fi
step "7a/24" "recht.bund.de BGBl I issue index (FNA)"       python3 pipeline/index_bgbl_issues.py --years "$INDEX_YEARS" --max-requests 60
step " 8/24" "2023+ official BGBl retrospective inventory" python3 pipeline/backfill_bgbl_history.py --max-new-index "${LEXGRAPH_FNA_NEW_PER_RUN:-40}"
# On the VPS (LEXGRAPH_LEGACY_CAPTURE_ONLY=1, set by deploy/refresh-server.sh)
# this only captures new sources: the workstation archive holds every older
# source byte, re-verifies all of them, rebuilds the candidates and pushes a
# signed attestation (deploy/archive-pull.sh, deploy/legacy-rebuild-push.sh).
LEGACY_MODE_ARGS=()
if [ "${LEXGRAPH_LEGACY_CAPTURE_ONLY:-0}" = "1" ]; then
    LEGACY_MODE_ARGS+=(--capture-only)
fi
step "8b/24" "bounded resumable pre-2023 official BGBl history" \
    python3 pipeline/backfill_legacy_bgbl_history.py \
        --start 1976-01-01 \
        --wahlperiode 8 --wahlperiode 9 --wahlperiode 10 --wahlperiode 11 \
        --wahlperiode 12 --wahlperiode 13 --wahlperiode 14 --wahlperiode 15 \
        --wahlperiode 16 --wahlperiode 17 --wahlperiode 18 --wahlperiode 19 --wahlperiode 20 \
        --limit 12 --max-requests 80 --max-download-mib 20 \
        --max-archive-mib 300 --min-free-mib 1024 --delay 0.8 "${LEGACY_MODE_ARGS[@]}"
echo "==> [ 9/24] Federal case law intake"
if [ "${LEXGRAPH_ENABLE_RII:-0}" = "1" ]; then
    # RII permits reuse of decisions, but its robots/TDM headers conflict
    # with automated ZIP polling.  Keep this legacy intake explicit until
    # it is replaced by the documented NeuRIS bulk/changelog API.
    python3 pipeline/fetch_rii.py || echo "[warn] step degraded — continuing"
else
    echo "    skipped by data policy (use NeuRIS; legacy RII is explicit opt-in)"
fi
step "9b/24" "retained official decision sources"          python3 pipeline/fetch_decision_sources.py
step "10/24" "NeuRIS changelog + expiring ZIP capture"     fetch_source neuris_changelog python3 pipeline/fetch_neuris_changelog.py
step "10b/24" "bounded resumable NeuRIS archive backfill" \
    python3 pipeline/fetch_neuris_changelog.py --backfill-archive \
        --limit 25 --max-downloads 25 --max-bytes 134217728 --delay 0.75

echo "==> [11/24] private back-history source"
if [ "${LEXGRAPH_ENABLE_BUZER:-0}" != "1" ]; then
    echo "    skipped: independent official-source history is active; private QA cache is explicit opt-in"
elif [ -d "data/snapshots/buzer/$(date +%F)" ]; then
    echo "    explicit opt-in enabled; today's snapshot exists, skipping"
else
    python3 pipeline/fetch_buzer.py || echo "[warn] step degraded — continuing"
fi

# extract first: it writes the DIP text cache that br_texts scans for
# cover letters; if br_texts then fetched NEW substantive texts, run the
# extraction again so BR patches land in THIS cycle, not the next
step "12/24" "PatchInstruction extraction (writes dip_text cache)" python3 pipeline/extract_patches.py
BRLOG="$(mktemp)"
echo "==> [13/24] Bundesrat texts (cache-first; 30s crawl-delay for new)"
python3 pipeline/fetch_br_texts.py | tee "$BRLOG" || echo "[warn] step degraded — continuing"
if grep -q "requests=0 " "$BRLOG"; then
    echo "==> [14/24] re-extraction skipped (no new BR texts)"
else
    step "14/24" "PatchInstruction re-extraction (absorb new BR texts)" python3 pipeline/extract_patches.py
fi
rm -f "$BRLOG"

step "15/24" "BAYERN.RECHT corpus HEAD + BayRS chains"     fetch_source bayern_recht python3 pipeline/fetch_bayern_recht.py
step "16/24" "GVBl/BayMBl promulgation events (RSS)"       fetch_source gvbl_events python3 pipeline/fetch_gvbl_events.py
step "17/24" "Bayerischer Landtag WP19 pipeline"           fetch_source bay_landtag python3 pipeline/fetch_bay_landtag.py
step "18/24" "EU layer: curated corpus + transpositions"   fetch_source eu_layer python3 pipeline/fetch_eu_layer.py
step "19/24" "EU breadth index (all directives + basic regulations)" fetch_source eu_index python3 pipeline/fetch_eu_index.py
# Article texts of the curated EU corpus, the EMRK and the Bavarian acts that
# BAYERN.RECHT's challenge now blocks (archived official copies).  These texts
# change rarely: refresh at most every LEXGRAPH_TEXT_REFRESH_DAYS days (7).
stale() { [ -z "$(find "data/snapshots/$1" -mindepth 1 -maxdepth 1 -type d -mtime "-${LEXGRAPH_TEXT_REFRESH_DAYS:-7}" 2>/dev/null)" ]; }
if stale eu_texts; then
    step "19a/24" "EU article texts (CELLAR Formex/XHTML)"    fetch_source eu_texts python3 pipeline/fetch_eu_texts.py
fi
if stale emrk; then
    step "19b/24" "EMRK German text (archived CoE PDF)"      fetch_source emrk python3 pipeline/fetch_emrk.py
fi
if stale bayern_recht_archive; then
    step "19c/24" "Bavarian acts from archived official copies" fetch_source bayern_recht_archive python3 pipeline/fetch_bayern_archive.py
fi
echo "==> [20/24] Länder discovery monitor"
if [ "${LEXGRAPH_ENABLE_PARLAMENTSSPIEGEL:-0}" = "1" ]; then
    python3 pipeline/fetch_parlamentsspiegel.py || echo "[warn] step degraded — continuing"
else
    echo "    skipped by data policy (origin-Landtag verification required before publication)"
fi
echo "==> [21/24] broad Länder discovery index"
if [ "${LEXGRAPH_ENABLE_PARLAMENTSSPIEGEL_BULK:-0}" = "1" ]; then
    python3 pipeline/fetch_laender_bills.py || echo "[warn] step degraded — continuing"
else
    echo "    quarantined by data policy (no republication permission for the portal database)"
fi

if [ -n "${LEXGRAPH_FRESHNESS:-}" ]; then
    step "21c/24" "publish operational source metadata (no corpus mutation)" \
        python3 tools/source_freshness.py export --output "$LEXGRAPH_FRESHNESS"
fi

# Preserve original bytes before aging snapshots out of the raw working set.
# A failed compaction leaves those originals in place; it never triggers pruning.
COMPACT_LOCK_ARGS=()
if [ -n "${LEXGRAPH_REFRESH_LOCK_FD:-}" ]; then
    COMPACT_LOCK_ARGS+=(--lock-file "$LEXGRAPH_REFRESH_LOCK" --lock-fd "$LEXGRAPH_REFRESH_LOCK_FD")
fi
# Archive-and-prune first (VPS: LEXGRAPH_ARCHIVE_OFFLOAD=1): objects older
# than the 14-day window whose exact bytes a signed workstation receipt holds
# leave the VPS, so compaction has room.  Over its budget, compaction defers
# the newest files instead of failing.
if [ "${LEXGRAPH_ARCHIVE_OFFLOAD:-0}" = "1" ]; then
    step "21a/24" "offload receipted archive bytes (14-day window)" \
        python3 tools/archive_offload.py prune --apply "${COMPACT_LOCK_ARGS[@]}"
fi
step "21b/24" "reversible snapshot compaction (newest two remain raw)" \
    python3 tools/compact_snapshots.py compact --apply --keep 2 \
        --max-new-mib 64 --max-archive-mib 300 --min-free-mib 1024 "${COMPACT_LOCK_ARGS[@]}"

echo "==> [22/24] build arena"
python3 tools/build_qfs.py
VIS="/home/echo0x22/Documents/Projects/03_PROJECTS_HOBBY/qfs_visualizer/public"
[ -d "$VIS" ] && cp data/lexgraph_de_wp21.qfs "$VIS/" && echo "deployed to qfs_visualizer"

echo "==> [23/24] export web data (Wiki/Realtime/Hierarchie/Graph)"
python3 tools/build_web_data.py
# Multi-source norm versions + change history (law_history.sqlite).  The
# workstation bundle (archived captures, legacy date resolutions) is optional;
# without it the history is built from the daily official states.
HISTORY_BUNDLE_ARGS=()
if [ -f data/history_bundle/current/manifest.json ]; then
    HISTORY_BUNDLE_ARGS+=(--bundle data/history_bundle/current)
    SIGNERS="${LEXGRAPH_ARCHIVE_SIGNERS:-/etc/sntiq/lexgraph-archive.allowed_signers}"
    if [ -f "$SIGNERS" ]; then HISTORY_BUNDLE_ARGS+=(--bundle-signers "$SIGNERS"); fi
fi
step "23b/24" "law history: versions + change records"      python3 tools/build_law_history.py --data-dir web/data "${HISTORY_BUNDLE_ARGS[@]}"

step "24/24" "LLM digest (skips without OPENROUTER_API_KEY)" python3 tools/build_digest.py
echo "OK — serve with: python3 -m http.server -d web 8777"
