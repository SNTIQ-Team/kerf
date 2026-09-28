# Court corpus – SQLite schema v3

The corpus is local only (not deployed to the VPS). The SNTIQ Legal MCP (Node, `node:sqlite`)
reads it directly and read-only. This file is the contract between them.

## Location and versioning

| Item | Value |
|---|---|
| Root | `~/.local/share/sntiq-legal/courts/` (override: `SNTIQ_COURTS_HOME`) |
| Database | `courts.sqlite` (WAL mode; writers are `python -m courts sync` processes) |
| Raw captures | Packed: `packs/<source_id>/<source_id>-<NNNN>.pack`, append-only, rotated at 512 MiB (O(100) files in total, not one file per capture; per-file storage exhausted btrfs metadata). Indexed by table `captures`. |
| Schema version | `PRAGMA user_version` = `3`, also `meta.schema_version`. |
| Parser version | `meta.parser_version` and `decisions.parser_version` (e.g. `courts-parser/1.4.0`). |

Readers must check `PRAGMA user_version == 3` and refuse any other value. Within a version, changes are additive only (new tables, new nullable columns, new views). Removing or renaming a column, or changing identity or storage rules, bumps the version. v1 → v2 (2026-09-28): added `captures` and the `sources` policy columns; `decisions.capture_path` became a pack reference. `init_db` upgrades v1 in place, and `python -m courts migrate-captures --delete-legacy` moves v1 `raw/*.gz` files into packs, verifying every hash before anything is deleted. v2 → v3 (2026-09-28): `decisions.court_id` became the Amtsgraph court id (`court:de:<XJustiz-ID>`, `court:eu:<ECLI code>`) where one is mapped, and the corpus slug moved to the new column `decisions.court_slug`. Added the tables `relation_assessments`, `corrections`, `verification_runs`, `verification_checks`, `verification_findings`, `amtsgraph_courts` and `court_map`, all documented below. `init_db` upgrades v1/v2 in place.

### Pack record format
Each record is a header followed by a payload; the header makes a pack self-describing, so the index can be rebuilt by scanning:

```
header  = "SCAP1\n" (6 bytes) | sha256 (32 bytes, raw) | codec (1 byte: 1 = zstd, 2 = gzip) | raw_size (u64 BE) | stored_size (u64 BE)
payload = stored_size bytes (one zstd frame for codec 1)
```

To read a capture: `SELECT pack, offset, stored_size, codec FROM captures WHERE sha256 = :content_hash`, then read `stored_size` bytes at byte `offset` of `<root>/<pack>` (`offset` points at the payload; the header is the 55 bytes before it). Decompress (Node: `zlib.zstdDecompressSync`, Node ≥ 22.15) and verify `sha256(bytes) == content_hash`. The bytes are the exact HTTP response body (ZIP, HTML, PDF, JSON). Appends are fsynced before their index row is written, so a crash can only leave unindexed bytes at a pack's tail.

Open the database read-only: `new DatabaseSync(path, { readOnly: true })`, or the URI `file:…?mode=ro`.

## Identity

| Object | Identifier |
|---|---|
| Decision | ECLI when published (`ECLI:DE:BVerwG:2016:160316B1B19.16.0`), otherwise `court:<court_id>/<YYYY-MM-DD>/<first Az>`. In the Az component, whitespace becomes `_` and `/` becomes `-`, e.g. `court:vgh-by/2026-06-16/10_ZB_24.2079`. |
| Paragraph | `<decision_id>#rn-<n>` for an explicit Randnummer. Other forms: `#<section>-<k>` (positional, for sections without Rn such as `tenor`, `leitsatz`, `titel`); `#<section>-u<k>` (unnumbered blocks before a section's first Rn); `#<section>-rn-<n>` (a Rn number that repeats, e.g. a dissent restarting at 1); `#p-<n>` (decision without any Rn: `decisions.numbering = 'none'`). |
| Court | `<type>-<place>`: federal courts are bare (`bverwg`, `bsg`, …); Land-level courts use ISO 3166-2 codes (`lsg-nw`, `ovg-nw`, `vgh-by`, `lsg-bebb`, `lsg-nihb`); local courts use the seat (`vg-muenchen`, `sg-dortmund`). |
| Norm | Lexgraph's canonical identity, from `api.norm_identity.ActResolver` (the same implementation as `/norms/resolve`; courts keeps no table of its own): `de/bund/<GII slug>/<para|art>/<n>[/abs/<n>][/satz/<n>][/nr/<n>][/buchst/<x>]` (e.g. `de/bund/sgb_14/para/37/abs/2/nr/3`, `de/bund/asylvfg_1992/para/24`, `de/bund/s_g/para/5` for SÜG). Bavarian acts are `de/by/<slug>` and other Länder `de/<land>/<key>`; EU acts are `eu/<CELEX>/art/<n>/…` and treaties `int/emrk/art/<n>`. `relations.detail.act_resolution` records the basis (`act_index`, `alias`, `gii_catalog`, `land_affix`, `land_context`, `eu`, `eu_citation`, `international`, `ambiguous`, `unresolved`). An unresolved or ambiguous act gives `target_id` NULL; the raw text is kept and nothing is guessed. |

Paragraph rules: a Randnummer runs until the next explicitly numbered block. Unnumbered blocks continue the previous Rn of the same section. Page breaks never create or split a Rn. Block boundaries are preserved: `paragraphs.blocks` is a JSON array, and `paragraphs.text` joins the blocks with `\n\n`.

## Tables

### `decisions`
One row per decision. Keys: `id` PK, `UNIQUE(source_id, source_doc_id)`.

| Column | Meaning |
|---|---|
| `ecli` | ECLI as published (can be NULL). |
| `source_id`, `source_doc_id` | Portal and its document id (e.g. `rii` / `jb-WBRE201600193`, `nrw` / `sgs/lsg_nrw/j2026/…`). |
| `court_id`, `court_slug`, `court_name`, `court_type`, `jurisdiction`, `land`, `chamber` | `court_id` is the Amtsgraph court id `court:de:<XJustiz-ID>` (federal courts via their ECLI code, e.g. BVerwG `court:de:U8572`; EuGH `court:eu:C`) when `court_map` has one, otherwise the corpus slug. `court_slug` is always the corpus slug (`lsg-nw`, `vg-muenchen`, `bverwg`); it is the alias used in synthetic decision ids and in `relations.target_court_id`. Filters accept either. `jurisdiction` ∈ `verfassung, ordentlich, verwaltung, sozial, finanz, arbeit, patent`; `land` ∈ `de` (federal) or an ISO code. |
| `decision_type` | As published (`Beschluss`, `Urteil`, `Gerichtsbescheid`, …). |
| `decision_date` | ISO date. This is **not** the date of the applicable law (see paper §5.2). |
| `case_numbers_raw` | Published docket string, verbatim (`L 9 SO 202/26 B ER und L 9 SO 203/26 B`). |
| `case_numbers` | JSON array of distinct Aktenzeichen, split from the raw string in order. |
| `numbering` | `rn` (the decision has explicit Randnummern) or `none`. |
| `source_url`, `content_hash`, `capture_path`, `media_type` | Provenance of the raw capture. `content_hash` (sha256 of the exact bytes) is the key into `captures`; `capture_path` is informational (`packs/<source>/<file>.pack@<offset>`). |
| `text_hash` | sha256 over all `paragraph id + text` pairs; changes whenever the extracted text changes. |
| `parser_version`, `first_seen`, `observed_at` | `first_seen` is kept across re-parses; `observed_at` is the last fetch. |
| `metadata` | JSON: editorial `norms`, `vorinstanz_raw`, keywords, `rn_without_own_text`, `preamble_omitted`, … |

### `case_numbers`
`(decision_id, position, case_number, case_key)`. `case_key` is uppercase and spaceless, with BVerwG-style `19.16` normalised to `19/16`. Look up an Aktenzeichen through `case_key`, never by `LIKE` on the raw string.

### `sections`
`(decision_id, position, name, authorship, authorship_basis, heading)`.
- `name` ∈ `titel, leitsatz, orientierungssatz, tenor, tatbestand, entscheidungsgruende, gruende, abweichende_meinung, sonstiger_langtext`.
- `authorship` ∈ `court | editorial | unknown`; `authorship_basis` says why. RII `<leitsatz>` is court-authored; RII `<sonstosatz>` (Orientierungssatz) and `<titelzeile>` are editorial.

### `paragraphs` and `paragraphs_fts`

| Column | Meaning |
|---|---|
| `rowid` | FTS key. |
| `id` | Paragraph id (unique). |
| `decision_id`, `section`, `authorship`, `ordinal` | `ordinal` is the document order. |
| `rn` | Explicit Randnummer label, or NULL. |
| `numbering` | `rn` or `none`. |
| `block_count`, `blocks` | `blocks` is JSON `[{start, end, locator, kind}]`: code-point offsets into `text` (text = the block texts joined by `\n\n`), so the block text is `text[start:end]`; `locator` points into the raw capture (XPath / CSS / PDF page+line). Block text is not stored twice. |
| `text`, `text_hash` | `text_hash` = sha256(text). Readers verify it before quoting. |

`paragraphs_fts` is an external-content FTS5 table (`tokenize='unicode61 remove_diacritics 2'`), kept in sync by triggers. Query it with `paragraphs_fts MATCH ?`. Build the MATCH string from quoted literals only (`"Untätigkeitsklage" AND "Mitteilung"`, phrase: `"sondern lediglich"`) so user input can never inject FTS syntax. Rank with `bm25(paragraphs_fts)`.

### `relations`
Typed citation statements. Never a generic "cites".

| Column | Meaning |
|---|---|
| `decision_id` | Citing decision. |
| `relation` | Parser rows: `mentions`, `quotes`, `procedural_reference` only (enforced by CHECK). Stronger relations (`applies`, `interprets`, …) may only come from `derived_by = 'model'` with `legal_effect_verified = 0`, or from `'user'`. |
| `target_kind` | `norm` or `decision`. |
| `target_id` | Norm id, decision id (ECLI or `court:` synthetic id), or NULL when unresolved. |
| `target_decision_id` | Corpus decision the citation resolves to (filled by the relink step: ECLI, or court + date + case_key). |
| `target_court_id`, `target_date`, `target_case_key` | Decision citations only; used for relinking. |
| `target_raw` | Normalised citation text (`§ 166 Abs. 1 Satz 1 VwGO`, `BVerwG, Beschluss vom … - 1 B 1.14`). |
| `paragraph_id`, `span_start`, `span_end`, `quoted_context` | Evidence. Spans are Unicode code-point offsets into `paragraphs.text`. `quoted_context` is stored for `quotes` rows (the quoted passage) and for metadata rows. For other text rows it is stored as `''`, and readers derive it as `paragraphs.text[max(0, span_start-160) : span_end+160]` with whitespace collapsed. |
| `origin` | `decision_text` (parsed from court-authored paragraphs), `editorial_metadata` (e.g. RII `<norm>`, `<vorinstanz>`) or `source_metadata` (e.g. NRWE "Vorinstanz" field). Metadata rows have `paragraph_id` NULL. |
| `derived_by`, `legal_effect_verified`, `confidence` | `derived_by` ∈ `parser, model, user`. `legal_effect_verified` is 0 for everything the parser writes. |
| `detail` | JSON: act, act_key, act_resolution (`gii_catalog, alias, land_affix, eu, eu_citation, international, unresolved`), chain, `in_conjunction_with` (i.V.m.), `following` (`f`/`ff`), `via_range`, `pinpoint_extra` (Alt./Hs.), court/date/case_number for decisions. |

Norm prefix query (segment-exact, index-friendly): `target_id = :n OR (target_id >= :n || '/' AND target_id < :n || '0')`. This matches `…/para/75` and `…/para/75/abs/1`, but not `…/para/750`.

### `captures`
`(sha256 PK, source_id, extension, pack, offset, stored_size, raw_size, codec, stored_at)`: one row per distinct capture. It includes captures that failed to parse, which remain available for re-parsing.

### `relation_assessments`
Proposals from the separate polarity stage (`courts/polarity.py`, classifier `courts-polarity-rules/1.0.0`) and user decisions. Parser rows in `relations` are never changed.

| Column | Meaning |
|---|---|
| `id` | PK |
| `relation_id` | `relations.id` of the parser relation assessed. Rule rows are recomputed whenever the decision is stored or re-parsed, so relation ids are not stable across re-parses. For user rows it is NULL: join those on `paragraph_id` + `target_id`. |
| `decision_id`, `paragraph_id`, `target_kind` (`norm`/`decision`), `target_id`, `span_start`, `span_end` | The parser relation's identity and citation span. |
| `assessment` | `applies`, `interprets`, `holds_inapplicable`, `rejects_application_of`, `follows`, `departs_from`, `overrules` or `distinguishes`. |
| `polarity` | `positive` (applies, follows), `neutral` (interprets) or `negative` (holds_inapplicable, rejects_application_of, departs_from, overrules, distinguishes). Views of "supporting" case law must exclude `negative`. |
| `cue`, `cue_start`, `cue_end`, `evidence_text` | The cue phrase, its offsets in the paragraph text, and the clause it was found in. |
| `rule_id`, `confidence`, `classifier_version` | E.g. `norm.keine_anwendung`, 0.8. |
| `derived_by`, `legal_effect_verified` | `derived_by` ∈ `rules`, `model`, `user`. `legal_effect_verified` is 1 only for `user` rows (CHECK). |
| `status`, `created_at` | `status` ∈ `proposed`, `confirmed`, `rejected`. Ledger corrections set it; rejected proposals must not be shown as findings. |

### `corrections` (append-only ledger)
Columns: `id` PK, `target` (decision id, or paragraph id `<decision>#rn-10`), `kind` ∈ `decision_field, case_numbers, relation_assessment, paragraph_boundary, relation, other`, `field` (decision field name; for `relation_assessment`/`relation` the `target_id`; for `paragraph_boundary` the quote), `old` (JSON), `new` (JSON), `evidence`, `author`, `created_at`, `fixture` (path of the conformance case written for it). UPDATE and DELETE raise errors (triggers). `decision_field`, `case_numbers` and `relation_assessment` corrections are applied as an overlay after every store or re-parse. The other kinds are recorded, and their conformance case stays `status: open` until the parser is fixed. CLI: `python -m courts correct …`, `python -m courts corrections [--target]`.

### Verification (`verification_runs`, `verification_checks`, `verification_findings`)
- `verification_runs`: `id`, `started_at`, `finished_at`, `sample`, `pairs`, `agreeing`, `findings`, `errors`, `verifier_version`.
- `verification_checks` (one row per compared pair): `id`, `run_id`, `decision_id`, `representation_a` (e.g. `stored:nrw`), `representation_b` (`nrwe-pdf`, `bverwg-html`, `rii-xml`, `capture:<source>`), `outcome` ∈ `agree, disagree, error`, `detail` (JSON), `checked_at`.
- `verification_findings` (one row per disagreement): `id`, `run_id`, `decision_id`, `kind` ∈ `numbering_mismatch, rn_count_mismatch, rn_boundary_mismatch, text_mismatch, case_numbers_mismatch, representation_error`, `severity` ∈ `major, minor`, `representation_a`, `representation_b`, `locator` (`rn-<n>` or `body`), `value_a`, `value_b` (JSON), `detail` (JSON: similarity, normalised text hashes, next Rn), `created_at`, `status` ∈ `open, explained, corrected`.
- The stored decision is never changed by verification. CLI: `python -m courts verify --sample N [--source S] [--offline]`.

### Court identity (`amtsgraph_courts`, `court_map`)
- `amtsgraph_courts`: a copy of Amtsgraph `/v1/courts/mapping` with `court_id` PK, `official_name`, `xjustiz_id`, `ecli_court_code`, `kind`, `land_code` (AGS), `status`, `unit_type`, `parent_court_id`, `aliases` (JSON), `snapshot_id`, `fetched_at`.
- `court_map`: `court_slug` PK, `court_id` (NULL if unmapped or ambiguous), `basis` ∈ `ecli_code, name_active, name_dissolved, seat_in_land, ambiguous, unmapped` (`seat_in_land`: same court type and Land, with the only such court in the Land or the same first seat word, e.g. "VG Neustadt (Weinstraße)" → "Verwaltungsgericht Neustadt an der Weinstraße"), `candidates` (JSON), `snapshot_id`. Names are matched through the canonical `courts.courtids.resolve`. An ambiguous name, e.g. "Amtsgericht Fürth" (BY and HE), is settled per decision by its Land, never by guessing.
- CLI: `python -m courts courts-map [--file payload.json]`.

### Sync bookkeeping

| Table | Content |
|---|---|
| `sources` | One row per portal. `status` ∈ `ok, partial, unavailable, blocked, never_synced`; `status_reason`; `upstream_listed`; `listing_complete` (1 only after an unbounded run listed the whole upstream archive); access policy `robots_policy` ∈ `respect, ignore_by_operator_decision` with `policy_decided_at` (date) and `policy_basis` (text), and `min_interval` (seconds between requests per host); last sync timestamps. `ignore_by_operator_decision` means that robots.txt, Crawl-delay and TDM reservations were deliberately not applied for that source on the operator's (user's) decision. Challenges, CAPTCHAs and logins are never bypassed; 429/503 and Retry-After are honoured. Coverage output must show this flag. |
| `source_items` | The incremental frontier: every listed upstream document with `state` ∈ `pending, stored, failed, skipped` (`skipped` + `failure_kind = duplicate_identity`: the same decision is already stored from another official source, e.g. RII and the court's own site); `priority` (0 = social/administrative courts first); `listed_modified` versus `fetched_modified` (a change re-queues the item); typed `failure_kind`. |
| `sync_runs` | One row per run: counts, request total, status; `notes = 'listing incomplete…'` marks runs that cannot serve as an incremental baseline. |
| `failures` | Typed failures (`challenge, login, error_page, http_error, rate_limited, network, robots_disallowed, unexpected_representation, parse_error, identity_mismatch, empty_document, duplicate_identity`). A failure is never stored as a decision. |
| `http_validators` | ETag / Last-Modified per URL, for conditional requests. |
| `source_coverage` (view) | Per source: status, `listing_complete`, `robots_policy`, `policy_decided_at`, `policy_basis`, `min_interval`, decisions, date range, pending/failed item counts, last sync. |

## Tool mapping and envelope rules (mirror `courts/read.py`)

- **`decisions_search`** (`source` may be one id or a list; scoping to available sources yields a definite total)
  1. Count matches per source with `GROUP BY d.source_id` over the full filter (FTS + court, date, `case_key`, ECLI, norm/relation via `EXISTS (SELECT 1 FROM relations r WHERE r.paragraph_id = p.id …)`).
  2. `searched` has one entry per in-scope source. `count` is NULL for sources whose status is `blocked`, `unavailable` or `never_synced`.
  3. If any in-scope source is unavailable: `status: "partial"`, `total: null`, and the source is listed in `failed_components`. **Never return a silent 0.**
  4. Otherwise `total` is the sum. A source is *complete* only if `listing_complete = 1` **and** it has no pending items. If any in-scope source is incomplete (a bounded or unfinished backfill): `total == 0` gives `status: "not_found_in_searched_scope"`, and `total > 0` gives `status: "partial"`, each with a warning naming the local and upstream counts. Only a complete scope yields `status: "ok"`.
- **`decision_get`**: resolve the argument as `id`, then `ecli`, then `source_id:source_doc_id`, then `case_numbers.case_key`. More than one hit returns `ambiguous_identity` with the candidates.
- **`decision_paragraph`**: select by `paragraphs.id`, verify `sha256(text) == text_hash` (otherwise `integrity_check_failed`), and return `blocks` and the paragraph's relations.
- **`citing_decisions`**: select `relations` by norm prefix or by `target_id` / `target_decision_id`. Always state which relation types were included. Parser relations say that a decision *mentions/quotes* a target, never that it applies it.
- **`courts_coverage`**: `SELECT * FROM source_coverage`, plus the latest `sync_runs` row and failure kinds per source.

## Example

```sql
-- quote → Rn: BVerwG 1 B 19.16, Rn. 10 has three blocks
SELECT p.id, p.rn, p.block_count
FROM paragraphs_fts f JOIN paragraphs p ON p.rowid = f.rowid
WHERE paragraphs_fts MATCH '"Streitgegenstand" AND "Untätigkeitsklage" AND "Mitteilung"'
  AND p.decision_id = 'ECLI:DE:BVerwG:2016:160316B1B19.16.0';
-- → ECLI:DE:BVerwG:2016:160316B1B19.16.0#rn-10 | 10 | 3
```

## Operations

- `python -m courts space`: the btrfs guard. Bulk writes (sync, reparse, migrate-captures) start only if "Device unallocated" ≥ 10 GiB and metadata usage ≤ 90 %. Metadata usage = used / (allocated + unallocated / metadata ratio). A running sync re-checks every 25 items and stops (run status `failed`, reason in `sync_runs.notes`) if metadata usage exceeds 90 % or unallocated falls below 4 GiB. Items stay `pending`.
- `python -m courts status`: coverage per source, including the robots policy and the space guard verdict.
- `python -m courts check-norm-ids`: every federal norm id in `relations` must be a canonical Lexgraph id (a GII slug known to the resolver). It exits with 1 and lists examples otherwise, e.g. stale `de/bund/sgb14/…`.
- `python -m courts compact`: FTS `optimize` plus `VACUUM INTO` a new file in the NOCOW corpus directory, then an atomic swap. It needs all source locks, so no sync may be running.
- One sync process per source (lock file `locks/<source>.lock`); a second one exits with code 3.
