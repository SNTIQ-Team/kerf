# Independent History Storage Review

Date: 2026-09-21

## Result

No unresolved introduced defect was found after the storage/GII findings and
follow-up refresh integration findings below were fixed and rechecked. This is a code and
temporary-fixture result, not production deployment verification.

The reviewer changed only this report. No production access, source snapshot
compaction, real-data restoration, commit, or unrelated dirty-worktree edit was
performed.

## Scope

- `pipeline/snapshot_storage.py` and `tools/compact_snapshots.py`.
- Current-turn reader/writer changes in `pipeline/common.py`,
  `tools/official_states.py`, `tools/verified_reconstruction.py`, and
  `tools/prune_gii_snapshots.py`.
- `pipeline/gii_import_cache.py`, the new cache/download handling and September
  75-law expansion in `pipeline/fetch_gii.py`, and corresponding tests.
- Read-only tracing of the existing refresh sequence, federal/Bavarian history
  readers, reconstruction inputs, and binary snapshot export paths.

## Findings Resolved During Review

### P1: Manifest-only verification could falsely approve unreadable references

Initial locations: `pipeline/snapshot_storage.py:209` and
`pipeline/snapshot_storage.py:345` before the review fix.

Reproduction: compact a three-day GII fixture, then remove
`.compact/manifest.json`. `verify_archive()` returned zero verified files and
objects successfully, although reading a compacted source failed with
`FileNotFoundError`. Removing only one `files` entry from an otherwise valid
manifest also passed verification, while that source failed with
`SnapshotArchiveError: snapshot reference is absent from manifest`.

The verifier only followed records listed in the manifest; it did not establish
that every existing reference was covered. This could approve an incomplete
archive copy before source history was used or restored.

Resolution: `pipeline/snapshot_storage.py:359` now reverse-scans the source tree
for reference files and requires a manifest record for each one. Verification
and restoration both invoke this check. The storage owner added the
missing-manifest and omitted-entry regressions at
`tests/test_compact_snapshots.py:450`.

Independent recheck: both corruptions now fail verification and restoration
with an explicit untracked-reference error. Passed.

### P2: Pruning restored files invalidated the retained byte archive

Initial location: `tools/prune_gii_snapshots.py:53` before the review fix.

Reproduction: archive a three-day GII fixture into the official state store,
compact its oldest day, restore all original files, then run ordinary GII
pruning. The oldest restored `acts.jsonl` and `norms.jsonl` were deleted because
the new exclusion recognized only reference magic. The retained compact
manifest still required those paths, so verification and subsequent compaction
failed with `archived snapshot path is missing`. Original compressed objects
remained recoverable, but the normal maintenance sequence broke the archive.

Resolution: `tools/prune_gii_snapshots.py:40` loads the byte-archive manifest,
and the exclusion at `tools/prune_gii_snapshots.py:54` retains recorded paths
even when they are restored raw files.

Independent recheck: compact -> restore -> prune deletes no tracked files;
verification succeeds, and another compaction and verification succeed. Passed.

## Fresh Verification

The following reviewer-run suite completed after the fixes:

```sh
.venv/bin/python -m pytest -q \
  tests/test_compact_snapshots.py \
  tests/test_gii_import_cache.py \
  tests/test_gii_corpus.py \
  tests/test_official_states.py \
  tests/test_prune_gii_snapshots.py \
  tests/test_verified_reconstruction.py \
  tests/test_verified_reconstruction_route.py \
  tests/test_official_state_store_api.py \
  tests/test_official_history_cli.py \
  tests/test_export_hf_official_history.py
```

Result: **66 passed in 0.93 seconds**. One Starlette/httpx deprecation warning;
no failing tests. This final run includes the storage owner's additional
planning-race regression at `tests/test_compact_snapshots.py:468`: a restored
raw source changed after initial verification is rejected by the digest check
at `pipeline/snapshot_storage.py:551` before an old reference can replace it.
The scoped tracked-file `git diff --check` also passed.

Additional independent temporary-fixture checks:

- Missing manifest and omitted manifest entry were reproduced before the fix,
  then explicitly rejected by both verification and restoration after it.
- The restore/prune failure was reproduced before the fix, then the complete
  compact/restore/prune/recompact sequence passed after it.
- A mocked two-law `fetch_gii.main()` run hit the checkpoint byte budget and
  returned status 2 without changing any previous catalog, act, or norm file.
  With a larger fixture allowance, retry reused the first law's checkpoint,
  fetched only the remaining law, and published two complete acts and norms.

The tests also exercise exact-byte round trips, source observation dates,
deduplication, corruption refusal, interrupted replacement recovery, source
changes, symlink boundaries, reserve/budget refusal, newest-two raw retention,
inherited refresh locks, common-writer atomic replacement, and archive-aware
strict readers. Corpus tests preserve the prior 125 configured laws and assert
the 75-law addition and a total of 200.

## Compatibility And Remaining Integration Work

Shared JSONL consumers now open archived originals through integrity checks.
The two separate strict historical readers were adapted. The existing binary
export path at `tools/export_hf.py:861` selects latest snapshots; its unmodified
copy behavior depends on the tested newest-two raw-retention invariant.

The source observation date stays in its existing path and manifest field; it
is not converted into a legal commencement date. Archive objects preserve the
original JSONL bytes, including whitespace and line endings. Checkpoint hashes
verify parsed GII import data; they do not independently preserve downloaded
ZIP/XML bytes or prove the live completeness of the official catalogue.

The initial review identified that the existing refresh still pruned old raw
GII files without invoking compaction. The root agent subsequently removed
that pruning step and added recurring compaction; the follow-up below reviews
that integration.

Production backup completeness, actual filesystem savings, task-wide budget
accounting across separate stores, the complete web rebuild, the production
refresh lock, and public reader flows remain root-agent checks. No conclusion
about those unperformed checks is implied by this review.

## Follow-up: Refresh Integration

Additional scope: the current-turn changes in `refresh.sh`,
`deploy/refresh-server.sh`, and `deploy/refresh-and-push.sh`. The local real-data
compaction already running in the root agent's session was not touched.

### P2 Resolved: Electoral-period arguments prevented every legacy refresh

`refresh.sh:37` initially supplied `--wahlperiode 8 9 ... 20`, while
`pipeline/backfill_legacy_bgbl_history.py:608` defines that option with
`action="append"`, accepting one integer per occurrence. Running that exact
argument list exited with status 2 and `unrecognized arguments: 9 10 ... 20`
before any network or archive work. The shell's ordinary degraded-step
handling would then continue the refresh without the new historical intake.

The root agent corrected this to repeated `--wahlperiode` options. A fixture
compiled the real parser statements and fed them the corrected command
arguments extracted from `refresh.sh`, without executing the importer. It
confirmed periods 8 through 20, 1976-01-01 through 2022-12-31, 12 documents,
80 requests, 20 MiB transfer, and a 1024 MiB reserve. Passed.

### P2 Resolved: Independent Ceilings Exceeded The Combined Budget

The initial integration set legacy capture to 350 MiB cumulative and left the
GII checkpoint default at 100 MiB. The compactor allowed 500 MiB cumulative
objects, with only a per-invocation 64 MiB limit. At the reported approximately
241.436 MiB existing compaction baseline, those ceilings permitted roughly
708.564 MiB of additional compact objects, legacy archive data, and GII
checkpoints over repeated refreshes. The free-space reserve did not enforce
the agreed combined 500 MiB allowance.

The root agent selected cumulative allocations of 300 MiB for compaction,
300 MiB for legacy capture, and 64 MiB for GII checkpoints. Relative to that
existing compaction baseline, this allows approximately 422.564 MiB of added
objects/checkpoints and leaves approximately 77.436 MiB for archive metadata
and margin. The implemented `--max-archive-mib` option is passed from
`tools/compact_snapshots.py:73` to the existing-plus-new object check at
`pipeline/snapshot_storage.py:571`, independently of the per-run allowance.
Regression cases at `tests/test_compact_snapshots.py:501` cover existing-only
overflow, existing-plus-new overflow, invalid limits, and CLI propagation.

Independent recheck: the actual refresh arguments configure GII checkpoints at
64 MiB (`refresh.sh:31`), legacy capture at 300 MiB (`refresh.sh:42`), and the
compactor at 300 MiB cumulative plus 64 MiB per run (`refresh.sh:106`). The final
legacy command passes the real parser. Both wrapper fixtures pass again with
the final compactor flags. The new cap regression tests pass. Resolved for
these added source-archive allocations; the baseline and total deployment
storage remain measurements owned by the root agent.

### Inherited Lock Verification

For each full-refresh wrapper, an isolated fixture executed the actual shell
prefix through its lock acquisition and environment exports. A child Bash then
executed the actual compaction step from `refresh.sh`, using the real compactor
and only a temporary three-day snapshot tree. No fetcher, builder, publisher,
SSH command, or real-data compaction was run.

- `deploy/refresh-server.sh`: descriptor 9 survived the child shell and reached
  the compactor; compaction completed in 0.032 seconds without a degraded step.
- `deploy/refresh-and-push.sh`: the same inheritance and compaction check passed
  in 0.032 seconds.
- After the child returned, a second independent lock attempt still failed,
  proving the wrapper retained the refresh lock through compaction.
- With another process already holding the fixture lock, each wrapper took its
  normal skip path before launching the child. No deadlock occurred.

`bash -n` and scoped `git diff --check` passed again for all three integration
files after the final changes. A source check also confirms the raw-pruning
command is absent from `refresh.sh`. The final focused suite completed with
**71 passed in 0.77 seconds**:

```sh
.venv/bin/python -m pytest -q \
  tests/test_compact_snapshots.py \
  tests/test_backfill_legacy_bgbl_history.py \
  tests/test_gii_import_cache.py tests/test_gii_corpus.py
```

That run also includes the storage owner's final small-file free-space
regression: the reservation accounts for all replacement references, since
references can be larger than tiny raw source files.
