# Shared conformance suite

Every module that implements a legal concern (citation parsing, norm identity, court identity, the Rn and
paragraph model, relation classification, page and challenge detection, legislative materials, norm structure)
runs these cases:

- Python: `tests/test_conformance.py`, which runs `courts` and `lexgraph` cases through `conformance/runner.py`.
- The Node MCP: loads the same JSON files and implements the same `kind`s.

A fix in one place is proven in every implementation by one shared expected output.

## Layout

```
conformance/
  cases/<module>/<case-id>.json     one case per file (module: courts | lexgraph)
  fixtures/...                      inputs used only by cases (prefer referencing existing test fixtures)
  runner.py                         Python implementation of every kind (dispatch table KINDS)
```

## Case format (version 1)

```json
{
  "format": "sntiq-conformance/1",
  "id": "courts.rn.bverwg-1-b-19-16-rn10",
  "module": "courts",
  "kind": "quote_to_paragraph",
  "description": "Rn 10 has three blocks; the quote is in the third block",
  "origin": "spec: Identity/Paragraph; paper §6",
  "status": "active",
  "input": { "...": "kind-specific; file paths are relative to the repository root" },
  "expected": { "...": "kind-specific" },
  "match": "subset"
}
```

- `status`: `active` cases must pass. An `open` case records a known error that is not fixed yet (usually a
  correction from the corrections ledger); runners report it as expected-to-fail until it passes, and it then
  becomes `active`.
- `match`: `subset` (default) means every key in `expected` must be present in the actual output with an equal
  value; lists compare element by element and dicts recursively, and extra keys in the actual output are
  ignored. `exact` means whole-value equality.
- Operators on list values: `{"$contains": [..]}` requires the items to be present, and `{"$excludes": [..]}` requires them to be absent (negative cases, e.g. "no `applies` proposal"). On string values the same operators take a substring: `{"$contains": "1. des Schriftsatzes"}`.
- Input files are addressed relative to the repository root. A path ending in `.gz` is decompressed before use
  unless the kind expects compressed bytes.

## Kinds

| kind | input | output (compared against `expected`) |
|---|---|---|
| `paragraphs_from_capture` | `adapter`, `fixture`, `item` (listing row), optional `url` | `{numbering, paragraph_ids: [suffix after '#'], rn_blocks: {rn: block_count}, sections: [{name, authorship}]}` |
| `quote_to_paragraph` | as above + `quote` (may contain `…`) | `{paragraph_ids: [suffix after '#']}` |
| `decision_metadata` | as above | `{ecli, court_id, decision_date, decision_type, case_numbers_raw, case_numbers}` |
| `case_numbers` | `raw` | `{case_numbers: [...]}` |
| `page_classification` | `fixture`, `status`, optional `headers` (response headers; default `content-type: text/html`) | `{kind: challenge\|login\|error_page\|null}`; response headers count (AWS WAF `x-amzn-waf-action`, HTTP 202 with an empty HTML body) |
| `norm_citations` | `text`, optional `land` | `{targets: [norm id or null in order], relations: [...]}` |
| `decision_citations` | `text`, `own_court_id`, optional `own_case_numbers` | `{citations: [{relation, target_id}]}` |
| `relation_assessments` | `text`, `own_court_id`, optional `land` | `{relations: [parser relation types], assessments: [{assessment, target_id}]}` (proposals from the rules stage) |
| `act_resolution` | `abbreviation`, optional `land` | `{prefix, basis, in_corpus}` (canonical Lexgraph resolver) |
| `norm_structure` | `fixture` (JSON) + `key` [+ `field`, default `text`] or `text`; optional `bavarian`, `annex`, `eu` (EU/treaty text: Unterabsätze, direct points, counted subparagraphs) | `{parser_version, mode, numbered_absaetze, text_sha256, counts: {absatz, satz, nummer, buchstabe, doppelbuchstabe, text_block[, unterabsatz]}, locators: {node type: [locator]}, texts: {locator: text}, flags: {locator: [flag]}, sentences: [{locator, items: [short locator]}]}` (`api.law_structure`) |
| `material_fragments` | `fixture` (directory with `meta.json` + page texts; unlisted pages are unextracted), `drucksache`, optional `sources` (default `data/materials_sources.json`: `act_slugs` + document entry, else `meta.json` `document_meta`) | `{document: {id, parser_version, extracted_pages, pages_missing_count, warnings}, sections: {role: first pdf page}, fragment_ids: [id suffix], fragments: {id suffix: {role, section_path, heading, heading_source (raw page slice of the heading span), body, page_start, page_end, complete, target_norms: [id], mentions: [id], relations: [{type, target, derived_from, confidence, legal_effect_verified}], government_position, notes}}}` (`api.legislative_materials.segment_drucksache`) |
| `norm_url` | `slug`, `queries: [{unit, number}]`, and `toc_html` (captured GII `index.html`) or `toc_json` (captured TOC file) or neither (no TOC) | `{results: [{key, url, act_url, status: resolved\|norm_not_in_toc\|act_not_in_toc\|toc_unavailable}], toc_norms: {unit/number: href} or null}` (`pipeline.fetch_gii_toc.parse_toc` + `api.norm_identity.gii_norm_url`) |
| `page_furniture` | `fixture` (JSON) + `key` or `where` [+ `field`, default `raw`], or `text` | `{spans: [{kind, start, end, text}], clean}` (`api.page_furniture.strip_page_furniture`) |
| `amendment_commands` | `fixture` (JSON) + `key` of a row `{article, heading, text}` (one final BGBl article) | `{command_count, norms, commands: {position from 1: {operation, ref, raw, parent_scope}}}` (`pipeline/backfill_bgbl_history.parse_amendment_commands`) |

Identifiers in expected outputs are canonical: norms `de/bund/<GII slug>/para/<n>/abs/<n>…` (the Lexgraph
resolver), decisions `ECLI…` or `court:<court>/<date>/<Az>`.
