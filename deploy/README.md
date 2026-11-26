# Kerf refresh jobs

Production runs two complementary refresh paths:

- `refresh-server.sh` executes the full daily corpus and history pipeline.
- `refresh-procedure-watch.sh` runs the live DIP procedure snapshot, explicitly configured EUR-Lex watches, persistent watch state and history, and the web-data build. `kerf-procedure-watch.timer` schedules this path at 08:17 and 20:17 UTC with up to five minutes of randomized delay.

Both server scripts acquire `/run/lock/kerf-refresh.lock`. On the workstation, `refresh-and-push.sh` uploads into a unique incoming directory and acquires that same **remote** lock before publishing. When a delayed timer overlaps another refresh, the later job exits successfully without modifying snapshots or published data. This prevents concurrent `build_web_data.py` and publish runs on the small production host. The next scheduled run records the following observation.

Publishing never writes directly into the live API directory. `publish-web-data.sh` copies into a versioned release, parses the required JSON, runs SQLite `quick_check`, verifies act files, atomically switches the `web-data` symlink, and restarts the API. Three complete generations remain on disk for rollback. On its first run, the script migrates the legacy mutable directory while the API is briefly stopped.

The watch lifecycle is evidence-based. An unchanged check updates `last_checked` without appending a duplicate history event. Terminal records remain archived in `data/procedure_watch_state.json` and `data/procedure_watch_history.jsonl`, and the EUR-Lex fetcher excludes them from subsequent network polls. EU political agreement alone leaves a watch open: the watcher waits for the adopted CELEX identifier and Official Journal publication evidence, then stays in `pending_final_review` until a persisted review compares the final Article 2 with the tracked Commission proposal.

## Install the production timer

Run as root on the production host after synchronizing the repository:

```bash
install -m 0755 deploy/refresh-procedure-watch.sh \
  /srv/sntiq-kerf/deploy/refresh-procedure-watch.sh
install -m 0755 deploy/publish-web-data.sh \
  /srv/sntiq-kerf/deploy/publish-web-data.sh
install -m 0644 deploy/kerf-procedure-watch.service \
  /etc/systemd/system/kerf-procedure-watch.service
install -m 0644 deploy/kerf-procedure-watch.timer \
  /etc/systemd/system/kerf-procedure-watch.timer
systemctl daemon-reload
systemctl enable --now kerf-procedure-watch.timer
systemctl list-timers kerf-procedure-watch.timer
```

Trigger and inspect one run without waiting for the timer:

```bash
systemctl start kerf-procedure-watch.service
systemctl status kerf-procedure-watch.service
journalctl -u kerf-procedure-watch.service -n 100 --no-pager
```

The service enforces resource controls (`Nice`, CPU and IO weights, and a 450 MiB memory limit) to limit the resources available to a refresh job. `EnvironmentFile=-/etc/sntiq/kerf.env` is optional.

## Diagnose delayed source downloads

An IPv6 connection may stall in `SYN-SENT` while IPv4 succeeds. In operation, this presents as a hung crawler or package upload. Verify both address families before debugging an individual fetcher:

```bash
timeout 5 curl -6 -fsS https://huggingface.co >/dev/null || echo ipv6-failed
timeout 5 curl -4 -fsS https://huggingface.co >/dev/null && echo ipv4-ok
```

Prefer IPv4 while keeping IPv6 enabled by activating the RFC 3484 rule in `/etc/gai.conf`:

```text
precedence ::ffff:0:0/96  100
```

Keep a backup of `gai.conf` and verify that `getent ahosts huggingface.co` lists IPv4 first. This rule is a host-level workaround for the provider network; remove it once outbound IPv6 works reliably.

## Bounded frequent observations and compact releases (2026-09-27)

The watched-procedure timer runs every four hours. Fetching and checkpointing record each observation, while `tools.watch_publish_state` skips the corpus build when watched evidence, watch configuration, and transition history are unchanged. Failed publications remain pending and retry on subsequent runs. The full daily refresh continues to cover the rest of the corpus.

`tools/stage_release.py` stages new generations using checksummed hardlinks to unchanged files in the previous immutable sibling. Changed files receive new inodes. `rsync` never runs with `--inplace`. Source symlinks are rejected. Before staging, the script checks a conservative full-copy budget plus a default 2 GiB free-space reserve (configured via `KERF_PUBLISH_RESERVE_MIB`). Three logical generations stay available while unchanged content shares disk blocks. Existing releases and source archives remain untouched.

Bulk historical collection belongs on the workstation under the collector's request, byte, archive, and free-space budgets. Production receives only validated public indexes and history. Frequent checks on the small VPS stay scoped to watched procedures and avoid full historical crawls.

## Workstation archive (2026-09-28)

The VPS retains only recent data. Older source bytes live on the workstation under `~/.local/share/sntiq-legal/kerf-archive/`, covering `data/legacy_bgbl_history`, `data/snapshots` (including the `.compact` archive), and `offload/` (manifests, receipts, and the allowed-signers copy).

Archive pull, legacy rebuild, and history bundle jobs read the signing key path from `KERF_ARCHIVE_KEY` or the checkout's Git configuration. Before updating an existing workstation, register its current key path once from the checkout root:

```sh
git config --local kerf.archiveSigningKey /absolute/path/to/your/signing-key
```

The distributed workstation service expects a stable checkout symlink. Create it from the checkout root before installing the service:

```sh
mkdir -p "$HOME/.local/share"
ln -sT "$PWD" "$HOME/.local/share/kerf-checkout"
```

- **Daily pull:** User timer `kerf-archive-pull.timer` (09:30, `Persistent=true`; units in `deploy/workstation/`) runs `deploy/archive-pull.sh`. The VPS lists every unreceipted archived file with its SHA-256 digest (`tools/archive_offload.py manifest`). `rsync` pulls files append-only and never passes `--delete`. The workstation re-hashes each file and returns a receipt signed with the configured key to `data/archive-offload/receipts/`.
- **VPS retention:** `tools/archive_offload.py prune --apply` runs after the pull and at refresh step 21a. It deletes a file only when a verified receipt matches both path and hash and the refresh no longer reads it: pre-2023 BGBl PDFs and texts once the latest legacy candidate snapshot is attested; compacted snapshot objects older than 14 days (retaining GII days the refresh still archives and the newest two days per family); and superseded legacy rebuilds older than 14 days. Signatures are verified against `/etc/sntiq/kerf-archive.allowed_signers`.
- **Compaction:** The compactor processes the oldest files that fit within its configured size limits. It defers the rest until more space is available. Verified archive receipts allow pruning on the VPS.
- **Pre-2023 BGBl history:** Refresh step 8b runs on the VPS with `--capture-only` (`KERF_LEGACY_CAPTURE_ONLY=1` in `refresh-server.sh`), capturing new sources only. The workstation re-verifies every source, rebuilds the candidates, and signs an attestation. The VPS build admits only attested rows and logs which parser produced them.

### After a legacy parser change

Once the change is on `main` and deployed, run on the workstation:

```bash
deploy/legacy-rebuild-push.sh            # pull, rebuild, attest, push, verify
deploy/legacy-rebuild-push.sh --dry-run  # rebuild + attest locally only
```

Until this runs, the VPS continues serving the previous attested rows. The build log records both parser hashes, and the refresh completes without error.

## Law history: versions and change records (2026-09-28)

`law_history.sqlite` is built into every web-data generation by `tools/build_law_history.py` during refresh step 23b and every procedure-watch publication. It serves `/norms/{id}/log|at|diff|blame|changes`, `/acts/{id}/changes|versions`, and the `change_summary` blocks. It merges the VPS's daily GII states and BGBl events with a compact **history bundle** from the workstation, installed at `data/history_bundle/current` with a signed manifest under namespace `kerf-history-bundle` in `/etc/sntiq/kerf-archive.allowed_signers`:

- `corpus.sqlite`: parsed Wayback/GII and NeuRIS captures (raw bytes stay in `~/.local/share/sntiq-legal/kerf-archive/history`).
- `change_dates.jsonl.gz`: adoption, promulgation, and commencement dates per amendment item, read from the printed BGBl (`tools/resolve_change_dates.py`).
- `bavarian.jsonl.gz`: BAYERN.RECHT versions and GVBl-dated changes (`tools/bavarian_history.py build`).

Refresh the bundle on the workstation (captures are resumable and polite):

```bash
python3 pipeline/fetch_history_wayback.py            # new Wayback captures
python3 tools/parse_history_captures.py              # parse into R/parsed
python3 tools/resolve_change_dates.py --data-root <root> --out-dir <dates> --all-acts
deploy/history-bundle-push.sh --change-dates <dates> \
    --bavarian ~/.local/share/sntiq-legal/kerf-archive/history/derived/bavaria \
    [--publish-now]
```

If the bundle is absent or its signature fails verification, the builder still produces history from the official daily states alone. Amendment discovery for 2023 onward also reads the recht.bund.de issue index (`pipeline/index_bgbl_issues.py`, refresh step 7a, `data/bgbl_documents/index/`).

### Compact court index

The court API can keep full-text search, passage addresses, citations and public observations on the server and fetch original files from a published Hub revision. Build the four court files in a new directory:

```sh
python3 tools/compact_court_index.py /path/to/full/web-data /path/to/compact-court "$HUB_COMMIT"
```

Supply the 40-character commit of the Hugging Face dataset that mirrors this exact court database. The command checks the catalogue against every retained original. Assemble a new web-data generation with the remaining files from the same full release and the four generated files in place of its court database, court manifest and observation feed. The normal publisher can then check and switch generations. A full court database takes precedence when present.

The November 2026 sample of 768 decisions used 40,177,664 bytes of search SQLite, down from 85,409,792. After loading the catalogue, the original-file endpoint reads one byte range and checks both SHA-256 digests. If Hugging Face is unavailable the original-file request returns HTTP 503. Search and passage lookups run on the local index.
