# Reversible snapshot compaction implementation

Date: 2026-09-21

## Scope and current evidence

Implemented bounded, deduplicated byte storage for inactive dated JSONL
snapshots. No real snapshot was compacted or restored during this implementation
task. Production was not touched. The complete web rebuild and production
integration remain separate root-agent checks.

Fresh focused and dependent verification: **153 tests passed in 0.53 seconds**.
Tests cover byte restoration, source URLs and observation dates, original file
metadata, duplicate object sharing, repeated runs, partial replacement recovery,
compressed and uncompressed integrity, changed sources, traversal and symlink
boundaries, free-space and object budgets, missing-reference recovery, the shared
refresh lock including inherited descriptors, recent raw materialization, strict
state/reconstruction readers, existing GII pruning, and federal/Bavarian history
compatibility. Independent review found and drove regression fixes for missing
manifest coverage and GII pruning after restoration. Verification and restore
now reject every on-disk reference omitted from the manifest, and pruning keeps
both reference and restored raw paths recorded by the byte archive.
Additional failing-before regressions verify changes to restored files between
initial verification and planning, and reserve space for every reference file
when compacting very small originals.

The real-data read-only dry run reported:

| Measure | Result |
| --- | ---: |
| Historical JSONL files eligible | 1,453 |
| Their original bytes | 2,316,189,992 bytes / 2,208.891 MiB |
| New unique gzip objects | 253,164,050 bytes / 241.436 MiB |
| Raw bytes minus gzip object bytes | 1,967.455 MiB |
| Auxiliary files kept raw | 3,734 |
| Auxiliary bytes kept raw | 5,393,993 bytes / 5.144 MiB |
| Existing archived files | 0 |
| Actual compacted files | 0 |

Reference files and the manifest consume additional small amounts of storage,
so the raw-minus-gzip figure is not an exact filesystem free-space measurement.
The dry run retains the newest two valid dated directories per source family,
including each family's latest available date. Families with only one or two
dates stay raw. The newly imported legacy-history family was detected and stayed
raw. Non-JSONL summaries and the undated Wayback JSON cache are exclusions.

## Format and failure handling

`pipeline/snapshot_storage.py` owns the archive format. Gzip objects live under
`data/snapshots/.compact/objects/sha256/<prefix>/<original-sha256>.gz`. The
cumulative `.compact/manifest.json` pins both compressed and original hashes and
lengths, original relative paths, source family, observation date, mode, owner,
group, and nanosecond atime/mtime/ctime. Atime, mtime, mode and ownership are
restored. Original ctime is recorded as provenance; filesystem ctime is generated
by the kernel when the replacement is made.

Each compacted `.jsonl` pathname remains a regular reference file. Existing
directory iteration, globbing and existence checks remain valid. Readers using
`common.read_jsonl` receive a completely verified original stream. The strict
official-state and reconstruction readers use the same archive-aware opening
function while preserving their JSON and row validation. GII pruning skips
all byte-archive-tracked paths, including restored raw files. Latest snapshots
remain normal JSONL for binary export
and copy consumers. If newer snapshots disappear, the next compaction pass
materializes any reference that now belongs to the retained recent set.

Objects are verified before the cumulative manifest is atomically published;
the manifest is durable before the first raw source is replaced. Replacement
uses same-directory atomic rename and fsync. An interrupted run can therefore
contain both original raw files and references, all represented by the durable
manifest. Re-running continues the remaining replacements. Orphan objects from
an interruption before manifest publication are checked and reused. Their
actual disk bytes count toward the archive cap.

Both the cumulative object store and each invocation's new-object allowance are
limited to at most 500 MiB. `--max-archive-mib` configures the cumulative limit;
`--max-new-mib` independently configures each invocation's new-object allowance.
Both default to 500 MiB. The Python API accepts `max_archive_bytes`, also defaulting
to 500 MiB, without changing existing calls. Existing objects, including orphans
from interrupted runs, count toward the cumulative cap even if no new objects
are planned. Six further regression cases check the separate limits, rejection
above the hard maximum, and CLI propagation.

The default 128 MiB free-space reserve includes the
staged object batch, manifest, all replacement references and any required recent
restoration. The task-wide source budget also includes other newly fetched
objects; the integrator must subtract those when setting `--max-new-mib`.

Archive reads and mutations reject symlink ancestors, non-regular targets and
non-canonical relative paths. Source device, inode, size, timestamps, mode and
owner/group are rechecked around reads and immediately before replacement.
Commands that mutate files acquire the existing refresh lock as well as a
source-root archive writer lock. Common JSONL writes now use atomic replacement,
so a partial producer failure cannot truncate the previous file or mutate a
hardlink sibling.

Restoration verifies the entire object set before writing and rejects raw files
that changed after archival. It can recover a missing reference path from the
manifest. Restoration retains the manifest and archive objects; it performs no
garbage collection.

## Runnable commands

Run from the repository root:

```sh
# Read-only inventory and exact gzip budget calculation.
.venv/bin/python tools/compact_snapshots.py compact --dry-run --keep 2 --max-new-mib 500

# Approved integration step: archive, verify, checkpoint, then replace old raw files.
.venv/bin/python tools/compact_snapshots.py compact --apply --keep 2 --max-new-mib 500

# Integration allocation: cap this cumulative snapshot archive at 300 MiB.
.venv/bin/python tools/compact_snapshots.py compact --apply --keep 2 --max-archive-mib 300

# Verify every archived byte stream and every raw/reference pathname.
.venv/bin/python tools/compact_snapshots.py verify

# Read-only restoration preflight, including required free space.
.venv/bin/python tools/compact_snapshots.py restore --dry-run

# Restore exact original files while retaining the reversible archive.
.venv/bin/python tools/compact_snapshots.py restore --apply
```

Use `--snapshots-root /absolute/path/to/snapshots` for an isolated copy or another
checkout. `--details` includes every excluded path in the JSON report. Omitted
`--apply` always means dry-run for compact/restore. The default lock is
`$LEXGRAPH_REFRESH_LOCK` when set, otherwise `$XDG_RUNTIME_DIR/lexgraph-refresh.lock`
(or `/tmp/lexgraph-refresh.lock`) locally and `/run/lock/lexgraph-refresh.lock`
when the checkout is under `/srv`.

Inside an existing refresh transaction with an inherited, locked descriptor 9,
run the compactor with both `--lock-file /run/lock/lexgraph-refresh.lock` and
`--lock-fd 9`. The descriptor must name that exact regular lock file. The
compactor keeps the inherited lock held when it returns.

Always copy `.compact` together with reference-bearing snapshot directories.
Copying references alone does not copy their archived originals.

Focused and dependent test command used:

```sh
.venv/bin/python -m pytest -q \
  tests/test_compact_snapshots.py \
  tests/test_prune_gii_snapshots.py \
  tests/test_official_states.py \
  tests/test_verified_reconstruction.py \
  tests/test_official_history_cli.py \
  tests/test_federal_history.py \
  tests/test_retrospective_history.py \
  tests/test_export_hf_archives.py \
  tests/test_export_hf_official_history.py \
  tests/test_build_web_data_by_diffs.py \
  tests/test_build_web_data_temporal.py \
  tests/test_fetch_neuris_changelog.py \
  tests/test_fetch_bayern_wayback.py \
  tests/test_backfill_bgbl_history.py
```

`git diff --check` also passed. No commits or branch changes were made.
