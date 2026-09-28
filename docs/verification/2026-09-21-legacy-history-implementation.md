# Legacy Federal History Import

Status: implemented and tested locally. The root agent owns the broader run,
public-data integration and production release. No production changes or commits
were made by the history agent.

## Implementation

- `pipeline/backfill_legacy_bgbl_history.py` discovers promulgated DIP legislation
  and requires DIP's exact official `www.bgbl.de` archive URL. Legacy document IDs
  use publication year and starting page, not the issue number shared by several
  laws. The default discovery range is 2006-2022, electoral periods 16-20.
- The final official PDF must agree with DIP's year, part, issue, publication
  date and page. The PDF, extracted text and pinned document metadata have
  SHA-256 addressed objects. Old Xaver PDFs do not provide the modern advertised
  MD5; `publisher_checksum_available=false` and the explicit `integrity_basis`
  preserve that distinction.
- Event targeting requires an exact GII/DIP name and the complete final amendment
  heading. Wrapped headings and spaces inserted inside PDF glyph runs are handled
  without dropping title qualifiers or accepting substrings. The generic modern
  article/command parser is reused only after that stricter legacy target gate.
- All candidates have `candidate_only=true`,
  `historical_text_reconstructed=false`, `effective_at=null` and
  `effective_date_status=unresolved_legacy_commencement_review`. DIP's raw
  commencement entries are retained as evidence. No historical consolidated
  body is generated.
- `data/legacy_bgbl_history/` retains immutable PDF/text/DIP response objects and
  one atomic checkpoint. The checkpoint resumes pagination and document capture;
  already captured PDFs and text are verified and reused. Expanded GII corpus
  matches refresh document metadata without downloading its PDF again.
- The cumulative archive budget includes that directory plus
  `data/snapshots/bgbl_legacy_history_backfill/`. The ceiling is 400 MiB, including
  checkpoint/inventory writes and temporary files required for atomic replacement.
  A separate free-space guard, request limit, response size bound and per-run
  transfer quota stop intake. Symlink traversal and corrupt immutable objects
  fail closed.
- Missing PDF crypto support aborts before network discovery rather than becoming
  a persistent source failure. `requirements.txt` now requires `pypdf[crypto]`.
  The generated source archive is excluded by `.gitignore`.
- A response exceeding its own 16 MiB PDF cap is recorded as `ResponseTooLarge`
  and skipped. Exhaustion of the separate run-wide transfer allowance pauses
  resumably. Declared oversized responses are rejected before body download.
  Alias ordering is deterministic across Python hash seeds, avoiding repeated
  metadata-only objects on unchanged resumes.

## Verified Initial Probe

The successful eight-document run completed at 2026-09-21T21:07:30Z. A stricter
heading regression discovered during inspection was fixed and rebuilt offline at
2026-09-21T21:11:19Z. Only the corrected inventory counts below are valid.

| Measurement | Initial corrected result |
| --- | --- |
| Official DIP procedures retained for the 2006 query | 206 |
| Official pre-2023 documents selected for the expanded corpus | 45 |
| Final PDFs captured and hash/header verified | 8 |
| Selected documents pending after this probe | 37 |
| Verified amendment events after the strict heading gate | 27 |
| Corpus acts with those events | 20 |
| Earliest / latest event publication | 2006-03-30 / 2006-07-20 |
| Historical consolidated bodies reconstructed | 0 |
| Article-wide commencement dates resolved | 0 |
| PDF bytes retained | 555,985 |
| Text bytes retained | 278,483 |
| Total legacy archive including metadata/snapshot at handoff | 1,236,526 bytes |
| Successful capture run HTTP requests / bytes | 25 / 713,582 |

The probe and diagnostic retries together remained below the authorized 20 MiB
transfer limit. Diagnostic retries first exposed missing venv crypto, then the
old PDF header's extracted `T eil` spelling. Both have focused regression tests.
No failed response or header-mismatched PDF became a published source object.

`pdfinfo`, `pdffonts`, extracted text, and a rasterized first page of BGBl I 2006
p. 558 were inspected. The rendered page confirms Article 1 is the SGB II
amendment and the publication date is 30 March 2006. The extracted typography
`Soz ialgesetzbuch` is now normalized only for complete-heading matching.

The inspection also found and eliminated false matches of StGB inside the title
of StGBEG, BAfoeG inside the title of a separate regulation, and corpus law names
inside repealed amendment-act titles. The initial broad parser's 35-event count
is superseded and must not be cited as verified coverage.

## Commands

Bounded initial deeper batch, resumable by repeating the same command:

```sh
.venv/bin/python pipeline/backfill_legacy_bgbl_history.py \
  --limit 220 --max-requests 800 --max-download-mib 100 \
  --max-archive-mib 400 --delay 0.8
```

Offline CAS verification and inventory rebuild:

```sh
.venv/bin/python pipeline/backfill_legacy_bgbl_history.py --rebuild
```

Historical failures are retained explicitly and skipped on ordinary resume;
`--retry-failures` requests a bounded retry. Earlier DIP periods can be selected
with an earlier `--start` and one or more explicit `--wahlperiode` arguments.
Coverage before 2006 was not validated by this initial probe.

## Integration Contract

Load `bgbl_legacy_history_backfill/candidates.jsonl` in addition to the modern
inventory. For `source=gii+dip+bgbl.de`, call `validate_legacy_candidate()` before
accepting the official legacy URLs. The validator rechecks pinned metadata,
source PDF/text digests, publication identity and the exact generated candidate.
Keep the existing separately reviewed 2022 SGB XII amendment independent.

The root agent implemented the loader and `_public_event` adapter. Independent
review found its origin exception correctly follows full legacy validation;
events remain `event_only`, legal dates remain unresolved, and no historical
body becomes selectable merely because an old event exists. Recommended public
provenance fields are `integrity_basis`, `publisher_checksum_available`,
`publication_page`, `publication_issue` and `fundstelle`.

## Verification

Tests were written before implementation, observed failing, and then made green.

```text
.venv/bin/python -m pytest -q tests/test_backfill_legacy_bgbl_history.py \
  tests/test_backfill_bgbl_history.py tests/test_legacy_bgbl_history.py
52 passed in 0.12s

.venv/bin/python -m pytest -q tests/test_backfill_legacy_bgbl_history.py \
  tests/test_legacy_event_projection.py tests/test_retrospective_history.py
43 passed in 0.13s
```

The focused tests cover year/page identity, wrong official origins, byte/request
limits, cumulative quotas across restart, CAS corruption and symlinks, retained
pagination cursors, document reuse, expanded-corpus joins, dependency failure
classification, conservative date/body status, wrapped complete headings and
misleading law names inside other official titles.

After the first larger run exposed a per-document versus per-run byte-limit
classification bug, a further test-first correction was verified:

```text
.venv/bin/python -m pytest -q tests/test_backfill_legacy_bgbl_history.py \
  tests/test_backfill_bgbl_history.py tests/test_legacy_bgbl_history.py \
  tests/test_legacy_event_projection.py tests/test_retrospective_history.py
79 passed in 0.50s
```

An offline rebuild at 2026-09-21T21:22:13Z retained 2,380 discovered procedures,
1,083 selected documents and 1,046 pending documents across 2006-2022. At that
checkpoint, 37 PDFs supported 94 events across 39 acts. The cumulative source
archive before that publication was 9,268,312 bytes. Discovery/selection counts
are now reconstructed from cached pages during offline rebuild, and summaries
also expose pending/failed counts and publication-year distributions.

## Older Discovery Feasibility

A read-only sample queried the first 100 promulgated procedures in each of
electoral periods 8-15. This was nine HTTP requests and 2,819,238 transferred
bytes, with no archive writes.

| Electoral period | DIP promulgated total | Exact-corpus selections in first 100 |
| --- | --- | --- |
| 8 | 337 | 20 |
| 9 | 134 | 24 |
| 10 | 320 | 32 |
| 11 | 366 | 25 |
| 12 | 493 | 32 |
| 13 | 552 | 22 |
| 14 | 547 | 38 |
| 15 | 384 | 36 |

The eight totals sum to 3,133 procedures; the first-page samples yielded 229
document selections. They are samples, not complete imported history. Official
links include SGB administrative procedure legislation (BGBl I 1980 p. 1469),
asylum procedure legislation (BGBl I 1982 p. 946), and social-administration
simplification (BGBl I 2005 p. 818).

A narrowed WP8 query found BGBl I 1978 p. 333 (Pfändungsfreigrenzen). Its final
official PDF was 3,622,853 bytes and yielded no text layer. It therefore cannot
pass the current exact-article evidence gate. This is an explicit scanned-source
gap; admitting OCR-derived events would require a separately reviewed source
and matching workflow. The read-only PDF probe made no source objects.

A second read-only probe sampled three previously discovered official URLs in
nine requests and 129,060 bytes total:

| Source | Text extraction result |
| --- | --- |
| BGBl I 1994 p. 2323, Beratungshilfe amendment | No text layer; excluded by the current gate |
| BGBl I 1998 p. 1606, 1. BABAnpG | 7,655 PDF bytes, 3 pages, 7,495 extracted characters; exact Article 1 heading matches SGB III |
| BGBl I 2002 p. 2074, rent legislation | 15,040 PDF bytes, 2 pages, 5,311 extracted characters; exact Article 2 heading matches SGB VI |

Thus direct, text-based pre-2006 ingestion is feasible at least for the sampled
1998 and 2002 publications. These feasibility checks did not publish candidates
or add source objects; the full publication/provenance capture gate still applies
when the root agent imports them.

## Remaining Boundaries

- This is an amendment-event inventory, not complete legal history. Exact DIP
  descriptors, readable PDF text and an exact amendment heading are required;
  missing descriptors, scanned sources, collective headings and unrecognized
  typography remain gaps.
- Automated command parsing is discovery metadata. It does not certify that
  every amendment instruction or affected norm in an older PDF was recovered.
- The larger 2006-2022 batch, final corpus coverage and production release require
  the root agent's separate verification record. These initial probe numbers
  are deliberately not updated while that batch is running.
