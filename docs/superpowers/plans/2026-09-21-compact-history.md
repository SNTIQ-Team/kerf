# Compact History Implementation Plan

**Goal:** Extend usable legal history and current full texts while limiting new archive data to 500 MiB.

**Approved design:** User approved compact, deduplicated archival storage; broader laws; deeper history; edition comparisons and visible coverage gaps. Prioritize social, migration and procedure law. Never equate capture dates with legal commencement or amendment events with historical full bodies.

**Architecture:** Preserve the existing snapshot readers, verified state objects and retrospective API. Compact inactive source snapshots reversibly with byte-integrity checks and retain recent raw working snapshots. Extend existing official imports and publish through the current atomic data-release gate.

**Tech Stack:** Python, JSONL, deterministic gzip, SHA-256, SQLite, FastAPI, Next.js.

## Constraints

- Work in the user's existing dirty checkout; preserve all prior changes. No commits or branch changes requested.
- Only the root agent deploys or mutates production. Shared refresh lock is mandatory.
- No loss of source observations, provenance, unique bytes or current serving data.
- 500 MiB hard budget for new source archive objects; resumable checkpoints and disk-free guard.
- Historical bodies need independent complete-source evidence; metadata-only history stays explicitly labeled.
- Existing external-source controls remain explicit; using a source never makes it official evidence.

## Task 1: Reversible Snapshot Compaction

- [x] Add failing roundtrip, duplicate, corruption, symlink and recent-snapshot tests.
- [x] Implement compact storage and transparent historical JSONL reads, without changing latest raw snapshot behavior.
- [x] Verify every compacted file before replacing the original; provide verify/restore and dry-run commands.
- [x] Run focused and dependent archival tests; compact locally and prove a complete web rebuild still works.

## Task 2: Deeper Official History

- [x] Extend official historical ingestion below the current 2023 boundary, retaining exact publication references and unresolved legal dates.
- [x] Add tests for pre-2023 source resolution, byte limits, resumability and no invented historical bodies.
- [x] Import a bounded initial batch with priority for SGB and migration/procedure acts.
- [x] Integrate event evidence into existing retrospective builders and publish coverage counts.

## Task 3: Broader Current Corpus and Coverage UI

- [x] Expand the verified GII practice corpus without losing configured laws on partial refresh.
- [x] Fetch and archive new full texts within the remaining budget; update derived search and graph data.
- [x] Expose history depth, available bodies and event-only gaps in the existing reader/history interface; preserve edition comparison.
- [x] Test parser, API and reader behavior with old and new laws.

## Task 4: Verification and Release

- [x] Review compaction and new historical joins independently.
- [x] Run full backend tests and frontend type/build tests for touched code.
- [x] Preserve local copies of production source snapshots before production compaction.
- [x] Under the refresh lock, compact production, verify archive restoration hashes, and deploy the new data generation atomically.
- [x] Exercise public history, current text, comparison and narrow/mobile browser flows. Record actual storage, coverage and remaining gaps.

## Execution Record

- Initial production disk: 1.1 GiB free on 20 GiB. Existing source snapshots about 2.4 GiB.
- Read-only local compression estimate: 5249 files, 3237 unique, 2348.7 MiB raw, 260.8 MiB unique gzip.
- Task boundaries: storage owns common I/O and compactor; history owns legacy import. Root owns corpus expansion, UI, integration and deployment.
- Completed bounded release: 200 federal acts; 2,564 additional legacy amendment records from 939 official PDFs; source compaction preserved all 1,497 originals. Full historical text reconstruction remains outside the evidence actually captured.
- Final deployment, storage figures and verification are recorded in `docs/verification/2026-09-21-expanded-history-release.md`.
