# Court corpus – SQLite schema v1

The corpus is local only (not deployed to the VPS). The SNTIQ Legal MCP (Node, `node:sqlite`)
reads it directly and read-only. This file is the contract between them.

## Location and versioning

| Item | Value |
|---|---|
| Root | `~/.local/share/sntiq-legal/courts/` (override: `SNTIQ_COURTS_HOME`) |
| Database | `courts.sqlite` (WAL mode; writers are `python -m courts sync` processes) |
| Raw captures | `raw/<source_id>/<sha256[:2]>/<sha256>.<ext>.gz`: exact response bytes, gzip-compressed. `sha256` is taken over the **uncompressed** bytes and equals `decisions.content_hash`. |
| Schema version | `PRAGMA user_version` = `1`, also `meta.schema_version`. |
| Parser version | `meta.parser_version` and `decisions.parser_version` (e.g. `courts-parser/1.1.0`). |

Readers must check `PRAGMA user_version == 1` and refuse any other value. Within v1, changes are additive only (new tables, new nullable columns, new views). Removing or renaming a column, or changing identity rules, bumps the version.

Open the database read-only: `new DatabaseSync(path, { readOnly: true })`, or the URI `file:…?mode=ro`.

## Identity

| Object | Identifier |
|---|---|
| Decision | ECLI when published (`ECLI:DE:BVerwG:2016:160316B1B19.16.0`), otherwise `court:<court_id>/<YYYY-MM-DD>/<first Az>`. In the Az component, whitespace becomes `_` and `/` becomes `-`, e.g. `court:vgh-by/2026-06-16/10_ZB_24.2079`. |
| Paragraph | `<decision_id>#rn-<n>` for an explicit Randnummer. Other forms: `#<section>-<k>` (positional, for sections without Rn such as `tenor`, `leitsatz`, `titel`); `#<section>-u<k>` (unnumbered blocks before a section's first Rn); `#<section>-rn-<n>` (a Rn number that repeats, e.g. a dissent restarting at 1); `#p-<n>` (decision without any Rn: `decisions.numbering = 'none'`). |
| Court | `<type>-<place>`: federal courts are bare (`bverwg`, `bsg`, …); Land-level courts use ISO 3166-2 codes (`lsg-nw`, `ovg-nw`, `vgh-by`, `lsg-bebb`, `lsg-nihb`); local courts use the seat (`vg-muenchen`, `sg-dortmund`). |
| Norm | `de/bund/<act>/<para|art>/<n>[/abs/<n>][/satz/<n>][/nr/<n>][/buchst/<x>]`; Land law is `de/<land>/<act>/…`; EU law is `eu/<CELEX>/art/<n>/…`; treaties are `int/emrk/art/<n>`. `<act>` is the folded abbreviation with SGB books in Arabic numerals (`sgb14`, `vwgo`, `asylg`). Federal acts are validated against the official GII catalogue. An unknown abbreviation stays unresolved (`target_id` NULL, raw text kept) and is never guessed. |

Paragraph rules: a Randnummer runs until the next explicitly numbered block. Unnumbered blocks continue the previous Rn of the same section. Page breaks never create or split a Rn. Block boundaries are preserved: `paragraphs.blocks` is a JSON array, and `paragraphs.text` joins the blocks with `\n\n`.

## Tables

### `decisions`
One row per decision. Keys: `id` PK, `UNIQUE(source_id, source_doc_id)`.

| Column | Meaning |
|---|---|
| `ecli` | ECLI as published (can be NULL). |
| `source_id`, `source_doc_id` | Portal and its document id (e.g. `rii` / `jb-WBRE201600193`, `nrw` / `sgs/lsg_nrw/j2026/…`). |
| `court_id`, `court_name`, `court_type`, `jurisdiction`, `land`, `chamber` | `jurisdiction` ∈ `verfassung, ordentlich, verwaltung, sozial, finanz, arbeit, patent`; `land` ∈ `de` (federal) or an ISO code. |
| `decision_type` | As published (`Beschluss`, `Urteil`, `Gerichtsbescheid`, …). |
| `decision_date` | ISO date. This is **not** the date of the applicable law (see paper §5.2). |
| `case_numbers_raw` | Published docket string, verbatim (`L 9 SO 202/26 B ER und L 9 SO 203/26 B`). |
| `case_numbers` | JSON array of distinct Aktenzeichen, split from the raw string in order. |
| `numbering` | `rn` (the decision has explicit Randnummern) or `none`. |
| `source_url`, `content_hash`, `capture_path`, `media_type` | Provenance of the raw capture. |
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
| `block_count`, `blocks` | `blocks` is JSON `[{text, locator, kind}]`; `locator` points into the raw capture (XPath / CSS / PDF page+line). |
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
| `paragraph_id`, `span_start`, `span_end`, `quoted_context` | Evidence. Spans are Unicode code-point offsets into `paragraphs.text`. |
| `origin` | `decision_text` (parsed from court-authored paragraphs), `editorial_metadata` (e.g. RII `<norm>`, `<vorinstanz>`) or `source_metadata` (e.g. NRWE "Vorinstanz" field). Metadata rows have `paragraph_id` NULL. |
| `derived_by`, `legal_effect_verified`, `confidence` | `derived_by` ∈ `parser, model, user`. `legal_effect_verified` is 0 for everything the parser writes. |
| `detail` | JSON: act, act_key, act_resolution (`gii_catalog, alias, land_affix, eu, eu_citation, international, unresolved`), chain, `in_conjunction_with` (i.V.m.), `following` (`f`/`ff`), `via_range`, `pinpoint_extra` (Alt./Hs.), court/date/case_number for decisions. |

Norm prefix query (segment-exact, index-friendly): `target_id = :n OR (target_id >= :n || '/' AND target_id < :n || '0')`. This matches `…/para/75` and `…/para/75/abs/1`, but not `…/para/750`.

### Sync bookkeeping

| Table | Content |
|---|---|
| `sources` | One row per portal. `status` ∈ `ok, partial, unavailable, blocked, never_synced`; `status_reason`; `upstream_listed`; last sync timestamps. |
| `source_items` | The incremental frontier: every listed upstream document with `state` ∈ `pending, stored, failed, skipped`; `priority` (0 = social/administrative courts first); `listed_modified` versus `fetched_modified` (a change re-queues the item); typed `failure_kind`. |
| `sync_runs` | One row per run: counts, request total, status. |
| `failures` | Typed failures (`challenge, login, error_page, http_error, rate_limited, network, robots_disallowed, unexpected_representation, parse_error, identity_mismatch, empty_document, duplicate_identity`). A failure is never stored as a decision. |
| `http_validators` | ETag / Last-Modified per URL, for conditional requests. |
| `source_coverage` (view) | Per source: status, decisions, date range, pending/failed item counts, last sync. |

## Tool mapping and envelope rules (mirror `courts/read.py`)

- **`decisions_search`**
  1. Count matches per source with `GROUP BY d.source_id` over the full filter (FTS + court, date, `case_key`, ECLI, norm/relation via `EXISTS (SELECT 1 FROM relations r WHERE r.paragraph_id = p.id …)`).
  2. `searched` has one entry per in-scope source. `count` is NULL for sources whose status is `blocked`, `unavailable` or `never_synced`.
  3. If any in-scope source is unavailable: `status: "partial"`, `total: null`, and the source is listed in `failed_components`. **Never return a silent 0.**
  4. Otherwise `total` is the sum. If `total == 0` but an in-scope source still has pending items (an incomplete backfill): `status: "not_found_in_searched_scope"`. If `total > 0` with pending items: `status: "partial"` plus a warning. Only a complete scope yields `status: "ok"`.
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
