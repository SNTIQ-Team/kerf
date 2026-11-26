# Shared conformance suite

The Python runner and Node MCP use the same JSON cases. It covers citation parsing, norm identity, court identity, the Rn and paragraph model, relation classification, page and challenge detection, legislative materials, and norm structure.

- Python: `tests/test_conformance.py` executes `courts` and `kerf` cases through `conformance/runner.py`.
- Node MCP: loads the same JSON files and implements the same `kind`s.

Each case pairs its input with the expected result.

## Layout

```
conformance/
  cases/<module>/<case-id>.json     one case per file (module: courts | kerf)
  fixtures/...                      inputs used only by cases (prefer referencing existing test fixtures)
  runner.py                         Python implementation of every kind (dispatch table KINDS)
```

### Archived court HTML fixtures

Seven BAYERN.RECHT decision pages carry empty request-verification fields. Four Saxon ESAMOS search/result pages use synthetic ASP.NET form values so offline tests exercise both postbacks with distinct state.

[The fixture manifest](fixtures/court-html-provenance.json) records SHA-256 digests over the decompressed HTML for both the capture before redaction (`capture_sha256`) and the distributed derivative (`fixture_sha256`). The corresponding court cases and Sachsen adapter tests identify the source pages, timestamps, or search parameters.

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

- `status`: `active` cases must pass. Use `open` for a known failure. After fixing it, set its status to `active` so the test suite checks the result.
- `match`: `subset` (default) requires every key in `expected` to appear in the actual output with an equal value. Lists compare element by element, dictionaries compare recursively, and extra keys in the actual output are ignored. `exact` requires whole-value equality.
- Operators on list values: `{"$contains": [..]}` requires the listed items to be present. `{"$excludes": [..]}` requires them to be absent for negative assertions, such as no `applies` proposal. On string values, both operators match a substring, as in `{"$contains": "1. des Schriftsatzes"}`.
- Input paths resolve relative to the repository root. Paths ending in `.gz` are decompressed before use unless the kind expects compressed bytes.

## Kinds

| Kind | Input | Output (compared against `expected`) |
|---|---|---|
| `paragraphs_from_capture` | `adapter`, `fixture`, `item` (listing row), optional `url` | `{numbering, paragraph_ids: [suffix after '#'], rn_blocks: {rn: block_count}, sections: [{name, authorship}]}` |
| `quote_to_paragraph` | same as `paragraphs_from_capture`, plus `quote` (may contain `…`) | `{paragraph_ids: [suffix after '#']}` |
| `decision_metadata` | same as `paragraphs_from_capture` | `{ecli, court_id, decision_date, decision_type, case_numbers_raw, case_numbers}` |
| `case_numbers` | `raw` | `{case_numbers: [...]}` |
| `page_classification` | `fixture`, `status`, optional `headers` (response headers; default `content-type: text/html`) | `{kind: challenge\|login\|error_page\|null}`. Response headers affect classification, including AWS WAF `x-amzn-waf-action` and HTTP 202 with an empty HTML body. |
| `norm_citations` | `text`, optional `land` | `{targets: [norm id or null in order], relations: [...]}` |
| `decision_citations` | `text`, `own_court_id`, optional `own_case_numbers` | `{citations: [{relation, target_id}]}` |
| `relation_assessments` | `text`, `own_court_id`, optional `land` | `{relations: [parser relation types], assessments: [{assessment, target_id}]}` (proposals from the rules stage) |
| `act_resolution` | `abbreviation`, optional `land` | `{prefix, basis, in_corpus}` (canonical Kerf resolver) |
| `norm_structure` | `fixture` (JSON) + `key` [+ `field`, default `text`] or `text`; optional `bavarian`, `annex`, `eu` (EU/treaty text: Unterabsätze, direct points, counted subparagraphs) | `{parser_version, mode, numbered_absaetze, text_sha256, counts: {absatz, satz, nummer, buchstabe, doppelbuchstabe, text_block[, unterabsatz]}, locators: {node type: [locator]}, texts: {locator: text}, flags: {locator: [flag]}, sentences: [{locator, items: [short locator]}]}` (`api.law_structure`) |
| `material_fragments` | `fixture` (directory with `meta.json` + page texts; unlisted pages are unextracted), `drucksache`, optional `sources` (default `data/materials_sources.json`: `act_slugs` + document entry, else `meta.json` `document_meta`) | `{document: {id, parser_version, extracted_pages, pages_missing_count, warnings}, sections: {role: first pdf page}, fragment_ids: [id suffix], fragments: {id suffix: {role, section_path, heading, heading_source (raw page slice of the heading span), body, page_start, page_end, complete, target_norms: [id], mentions: [id], relations: [{type, target, derived_from, confidence, legal_effect_verified}], government_position, notes}}}` (`api.legislative_materials.segment_drucksache`) |
| `norm_url` | `slug`, `queries: [{unit, number}]`, and `toc_html` (captured GII `index.html`), `toc_json` (captured TOC file), or neither (no TOC) | `{results: [{key, url, act_url, status: resolved\|norm_not_in_toc\|act_not_in_toc\|toc_unavailable}], toc_norms: {unit/number: href} or null}` (`pipeline.fetch_gii_toc.parse_toc` + `api.norm_identity.gii_norm_url`) |
| `page_furniture` | `fixture` (JSON) + `key` or `where` [+ `field`, default `raw`], or `text` | `{spans: [{kind, start, end, text}], clean}` (`api.page_furniture.strip_page_furniture`) |
| `amendment_commands` | `fixture` (JSON) + `key` of a row `{article, heading, text}` (one final BGBl article) | `{command_count, norms, commands: {position from 1: {operation, ref, raw, parent_scope}}}` (`pipeline/backfill_bgbl_history.parse_amendment_commands`) |

Expected outputs use canonical identifiers: norms follow `de/bund/<GII slug>/para/<n>/abs/<n>…` from the Kerf resolver, and decisions follow `ECLI…` or `court:<court>/<date>/<Az>`.
