# Expanded History Release

Date: 2026-09-21 UTC

Status: the bounded history/corpus release is deployed. Production data was
published at 22:12 UTC and the final mobile layout fix at 22:27 UTC. These
results are not a claim of complete legal history.

## Corpus And Reader

- The prior 125 federal acts are retained and 75 verified GII paths are added.
- The generated public corpus contains 200 federal and 12 Bavarian acts,
  24,183 indexed norms in total; 23,267 belong to the federal corpus.
- All 200 federal act and reader API responses passed source identity and
  canonical-text/hash checks locally and on production. Optional layouts cover
  23,013 norms. Other norms retain canonical plaintext.
- StAG, AZRG-DV and VWDG-DV keep their stable public IDs while using the real
  GII source paths `stag`, `azrg-dv` and `vwdg-dv`.
- All 212 public IDs have an explicit topic/subtopic in all four UI languages.
- The history summary separates amendment evidence from complete archived
  bodies. Event-only dates are not promoted to complete editions.
- A reproduced navigation bug is fixed: opening a reader share URL from the
  history view also switches the visible module back to reading.
- API reads now revalidate the browser cache, including previously fresh law
  indexes, so publication of new data does not leave a stale catalogue visible.
- The synopsis view switch uses three bounded grid tracks. A reproduced 320 px
  internal overflow (295 px panel / 346 px content) is fixed without hiding
  overflow; labels can wrap and the panel retains vertical scrolling.

## Deeper Historical Evidence

- 939 verified official BGBl PDFs yield 2,564 amendment records for 103 acts,
  spanning publications from 1998-03-27 to 2022-12-30. Discovery covered 3,522
  DIP procedures and selected 1,501 source documents.
- Full bytes, extracted text and source metadata are pinned by SHA-256. Exact
  publication header and article-heading checks gate the public projection.
  Production rebuilt all 2,564 records from the copied archive under the
  shared refresh lock; checksum comparison against the local source was clean.
- The former name Asylverfahrensgesetz now joins to AsylG without matching
  similarly named separate acts. The regression test was observed failing
  before the fix. Reusing existing source bytes added 12 historical joins.
- Public SGB XII history now has 63 events starting 2006-07-25; SGB III has 161
  from 1998-04-14; AsylG has 35 from 2001-12-28. These are event counts, not
  counts of available consolidated editions.
- The importer reconstructed zero historical full texts. The initial batch
  leaves 535 selected documents pending and 27 failed source checks. Earlier
  scanned sources and unresolved descriptors remain explicit coverage gaps.

## Preserved Source State

- A complete local production-data backup was made before compaction at
  `scratchpad/prod-before-history-20260921/data/`; deployed code was also copied.
- Production observations missing locally and local new-law states missing
  remotely were merged without replacing an existing immutable object.
- The merged official store contains 8,747 observations, 294 official objects
  and 56 observed transitions. One separately verified reconstructed body makes
  295 objects in the public catalogue.
- The merged watch journal has 58 distinct events. Production's latest watch
  state and retrospective knowledge assertions were retained before rebuilding.

## Compaction And Budget

Production compaction preserved and verified all 1,497 original files:

| Measure | Bytes |
| --- | ---: |
| Original historical JSONL bytes | 2,388,427,016 |
| Compressed unique objects | 258,857,486 |
| Free filesystem space before compaction | 1,126,477,824 |
| Free space after compaction, before new release staging | 3,249,401,856 |

The production verifier checked 682 unique objects. The local merged archive
independently verified 1,497 files / 684 objects / 259,069,535 compressed bytes.
Different pre-existing same-day snapshot bytes explain the two stores' distinct
object totals; the local `.compact` store was not copied over production.

The latest two snapshots per source remain raw. Historical reads transparently
verify/decompress originals, and a restore command is available. Recurring raw
GII pruning was removed. Refresh wrappers pass their held lock into compaction.

Recurring cumulative allocations are 300 MiB for the compact object store
(including the existing approximately 247 MiB baseline), 300 MiB for the new
legacy archive and its inventories, and 64 MiB for resumable GII checkpoints.
Thus new source archival growth is bounded below the agreed 500 MiB allowance.
Current working snapshots, reader layouts, derived web indexes and rollback
releases are separate working artifacts, not hidden inside this allowance.

The new legacy archive plus both inventory snapshots occupies 231,893,140 bytes
(approximately 221 MiB). The GII resumable cache occupies 9,049,571 bytes. The
recurring importer is configured for bounded batches of 12 new documents,
80 requests and 20 MiB transfer, with a 1 GiB free-space guard. Discovery starts
in 1976; this is not a claim that source texts back to 1976 were captured.

After the final code release the production filesystem had 1,961,684,992 bytes
free. The late frontend staging directory used hardlinks for unchanged files
and rsync temporary-file replacement for changed files, without `--inplace`.
Rollback releases and the private letter store were retained.

## Verification Before Publication

- Backend: `python -m pytest -q tests`: **384 passed**, one existing
  Starlette/httpx deprecation warning.
- Frontend Kerf tests: **83 passed**.
- Next.js production build, TypeScript and lint: passed. Existing `::highlight`
  optimizer and Node loader deprecation warnings remain.
- Both repositories' tracked-file `git diff --check`: passed.
- Public browser history checks: 1440, 390 and 320 px, light/dark; SGB XII section 11,
  bounded coverage strip, HGB section 1 with separate source paragraphs,
  same-page reader navigation and archive/HEAD edition roundtrip; no page errors.
- Graph checks on production: all six viewport
  and theme combinations; nonblank themed canvas, pan, animated zoom, SGB XII
  selection, transition into reading and preserved selection on return.
- The reader regression suite passed all 13 checks on staging, including
  independent pane filters, long-scroll anchors, consumed navigation commands,
  keyboard tab closing and canonical fallback for invalid layout sidecars.
- The 320 px regression failed before the layout fix and passed after it.
  All four UI languages were checked in both themes for panel and button text
  overflow, both on staging and production.

## Production Release

- API generation: `web-data.release-20260921T221250Z-3284502`;
  `built_at=2026-09-21T22:08:00+00:00`.
- Frontend build: `Il5HUDAXVMbpyBh657WgU`.
- Public API checks covered all 200 federal act/reader responses, not a sample.
  Health reports 3,236 current event records, 558 with verified effective dates,
  8,747 observations and 295 state objects. Superseded assertions are retained
  separately from current records.
- API service remained active with `NRestarts=0`; measured physical memory
  was 209,354,752 bytes and peak 210,874,368 bytes after corpus checks.
- Web health and authenticated letter-creation configuration passed the
  activation gate. No letter contents or credentials were changed.
- Refresh/watch timers are active. Updated importer dependencies, flags and
  inherited refresh locks were checked, but a complete nightly refresh cycle
  has not been rerun after this deployment.

## Evidence Files

- Frontend `.tmp/history-qa.cjs`, `.tmp/history-public-qa/`.
- Frontend `.tmp/corpus-history-qa.cjs`, `.tmp/corpus-history-public-qa.json`.
- Frontend `.tmp/history-public-graph-qa/`.
- Frontend `.tmp/synopse-language-qa.cjs`, `.tmp/synopse-public-language-qa/`.
- Legacy source inventory `data/snapshots/bgbl_legacy_history_backfill/2026-09-22/`.
- `2026-09-21-compaction-implementation.md` and
  `2026-09-21-history-storage-review.md` in this directory.
- `2026-09-21-legacy-history-implementation.md` records the source gates,
  initial probe and scanned-source limitations.

## Remaining Coverage Boundary

Pre-2023 capture supplies source-linked amendment evidence, not automatically
reconstructed consolidated editions or verified article-wide effective dates.
Missing DIP descriptors, scanned PDFs, mismatched publication headers and
unrecognized article headings remain explicit gaps. No Buzer input is enabled
in the public data generation.
