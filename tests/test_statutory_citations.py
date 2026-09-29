from __future__ import annotations

import json
import hashlib
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.statutory_citations import (
    build_citation_index,
    citation_manifest,
    write_citation_database,
)


def _acts() -> list[dict]:
    return [{
        "id": "fed_alpha", "jurabk": "AlphaG", "juris": "DE",
        "title": "Alpha-Gesetz",
        "norms": [{
            "enbez": "§ 1", "text": (
                "Nach § 2 gilt die Regel; § 2 gilt nochmals. "
                "Die §§ 3 bis 4 bleiben unberührt. "
                "Die § 5 und § 6 BetaG sind anzuwenden. "
                "Außerdem gelten § 999 BetaG und § 1 UnknownG. "
                "Art. 1 ist hier kein Paragraph des AlphaG."
            ),
        }, {
            "enbez": "§ 2", "text": "Nach § 1 dieses Gesetzes gilt dies.",
        }, {
            "enbez": "§ 3", "text": "",
        }, {
            "enbez": "§ 4", "text": "",
        }],
    }, {
        "id": "fed_beta", "jurabk": "BetaG", "juris": "DE",
        "title": "Beta-Gesetz",
        "norms": [
            {"enbez": "§ 5", "text": ""},
            {"enbez": "§ 6", "text": ""},
        ],
    }, {
        "id": "fed_gg", "jurabk": "GG", "juris": "DE",
        "title": "Grundgesetz für die Bundesrepublik Deutschland",
        "norms": [{"enbez": "Art 1", "text": ""}],
    }]


def _rows() -> tuple[dict, list[dict]]:
    index = build_citation_index(
        _acts(), built_at="2026-07-16T12:00:00+00:00",
        source_snapshots={"DE": "2026-07-15"},
    )
    return index, index["citations"]


def _reader_fixture(directory: Path, text: str, blocks: list[dict],
                    *, digest: str | None = None) -> None:
    payload = {
        "schema_version": 1, "act_id": "fed_alpha",
        "source_url": "https://www.gesetze-im-internet.de/alpha/xml.zip",
        "retrieved_at": "2026-09-27T00:00:00Z",
        "norms": {"§ 1": {
            "text_sha256": digest or hashlib.sha256(text.encode()).hexdigest(),
            "blocks": blocks,
        }},
    }
    (directory / "fed_alpha.json").write_text(json.dumps(payload))


def _text(value: str) -> dict:
    return {"type": "text", "text": value}


def _element(tag: str, *children: dict) -> dict:
    return {"type": "element", "tag": tag, "children": list(children)}


def test_reader_blocks_stop_list_markers_becoming_citation_heads(tmp_path):
    acts = _acts()
    text = "Zuständigkeit nach § 3,2.weitere Aufgabe."
    _only_alpha_text(acts, text)
    _reader_fixture(tmp_path, text, [_element("dl",
        _element("dd", _text("Zuständigkeit nach § 3,")),
        _element("dt", _text("2.")),
        _element("dd", _text("weitere Aufgabe.")),
    )])
    index = build_citation_index(acts, built_at="2026-09-27",
                                 reader_directory=tmp_path)
    rows = index["citations"]
    assert [row["target_norm"] for row in rows] == ["§ 3"]
    assert rows[0]["citation_text"] == "§ 3"
    assert rows[0]["source_excerpt"] == text
    assert index["counts"]["structured_source_norms"] == 1
    assert index["counts"]["structure_boundaries"] == 2


def test_source_media_stops_citations_joining_across_graphics():
    from tools.statutory_citations import _structured_parse_body, parse_citation_groups

    text = "§ 3 und 4 BetaG"
    for tag in ("img", "source-file"):
        parsed, offsets, _ = _structured_parse_body(text, [
            _text("§ 3 und "), {"type": "element", "tag": tag,
                                 "attrs": {"src": "https://www.gesetze-im-internet.de/alpha/graph.png"},
                                 "children": []}, _text("4 BetaG"),
        ])
        assert offsets
        assert [spec.start for group in parse_citation_groups(parsed)
                for spec in group.specs] == ["§ 3"]


def test_reader_block_boundaries_do_not_split_inline_citations(tmp_path):
    acts = _acts()
    text = "Nach §§ 5 und 6 BetaG gilt dies."
    _only_alpha_text(acts, text)
    _reader_fixture(tmp_path, text, [_element("p",
        _text("Nach §§ 5"), _text(" und 6 "), _text("BetaG gilt dies."))])
    rows = build_citation_index(acts, built_at="2026-09-27",
                                reader_directory=tmp_path)["citations"]
    assert {(row["target_act"], row["target_norm"]) for row in rows} == {
        ("fed_beta", "§ 5"), ("fed_beta", "§ 6")}
    assert all(row["citation_text"] == "§§ 5 und 6 BetaG" for row in rows)


def test_pre_and_caption_do_not_join_adjacent_prose_into_citations(tmp_path):
    for tag in ('pre', 'caption'):
        acts = _acts()
        text = '§ 3 und 4 BetaG'
        _only_alpha_text(acts, text)
        _reader_fixture(tmp_path, text, [_text('§ 3 und '),
            _element(tag, _text('4 BetaG'))])
        rows = build_citation_index(acts, built_at='2026-09-27',
                                    reader_directory=tmp_path)['citations']
        assert [(row['target_act'], row['target_norm']) for row in rows] == [
            ('fed_alpha', '§ 3')]
        assert rows[0]['citation_text'] == '§ 3'
        assert rows[0]['source_excerpt'] == text


def test_reader_boundaries_keep_aliases_inside_their_own_blocks(tmp_path):
    acts = _acts()
    text = "§ 2(BetaG) ist die folgende Tabellenzelle."
    _only_alpha_text(acts, text)
    _reader_fixture(tmp_path, text, [_element("table", _element("tbody",
        _element("tr", _element("td", _text("§ 2")),
                 _element("td", _text("(BetaG) ist die folgende Tabellenzelle.")))))])
    rows = build_citation_index(acts, built_at="2026-09-27",
                                reader_directory=tmp_path)["citations"]
    assert len(rows) == 1
    assert rows[0]["target_act"] == "fed_alpha"
    assert rows[0]["target_norm"] == "§ 2"
    assert rows[0]["source_excerpt"] == text


def test_reader_boundaries_preserve_original_offsets_after_whitespace_folding(tmp_path):
    acts = _acts()
    text = "Vorwort. § 2,3. Noch § 4."
    _only_alpha_text(acts, text)
    _reader_fixture(tmp_path, text, [
        _element("p", _text("  Vorwort.   ")),
        _element("dl", _element("dd", _text("§ 2,")),
                 _element("dt", _text("3.")),
                 _element("dd", _text("  Noch § 4.  "))),
    ])
    rows = build_citation_index(acts, built_at="2026-09-27",
                                reader_directory=tmp_path)["citations"]
    assert {row["citation_text"] for row in rows} == {"§ 2", "§ 4"}
    assert all(row["source_excerpt"] == text for row in rows)


def test_stale_reader_structure_is_not_used_for_citation_evidence(tmp_path):
    acts = _acts()
    text = "§§ 3,4"
    _only_alpha_text(acts, text)
    _reader_fixture(tmp_path, text, [_element("p", _text("§§ 3,")),
                                    _element("p", _text("4"))], digest="0" * 64)
    index = build_citation_index(acts, built_at="2026-09-27",
                                 reader_directory=tmp_path)
    assert {row["target_norm"] for row in index["citations"]} == {"§ 3", "§ 4"}
    assert index["counts"]["structured_source_norms"] == 0


def test_source_annotations_do_not_change_the_canonical_body_citation_index(tmp_path):
    acts = _acts()
    text = 'Nach § 2 gilt dies.'
    _only_alpha_text(acts, text)
    _reader_fixture(tmp_path, text, [_element('p', _text(text))])
    before = build_citation_index(acts, built_at='2026-09-27', reader_directory=tmp_path)
    path = tmp_path/'fed_alpha.json'
    payload = json.loads(path.read_text())
    annotation = 'Amtlicher Hinweis auf § 5 BetaG und § 999 UnknownG.'
    payload['norms']['§ 1'].update(source_annotations=[_element('p', _text(annotation))],
                                  source_annotations_text=annotation,
                                  source_annotations_sha256=hashlib.sha256(annotation.encode()).hexdigest())
    path.write_text(json.dumps(payload))
    after = build_citation_index(acts, built_at='2026-09-27', reader_directory=tmp_path)
    assert after == before


def test_repeated_norm_designators_with_distinct_scope_remain_unresolved():
    acts = _acts()
    _only_alpha_text(acts, "§ 5 BetaG und §§ 5 bis 6 BetaG")
    acts[1]["norms"][0]["glied"] = "Artikel 1"
    acts[1]["norms"].append({"enbez": "§ 5", "text": "", "glied": "Artikel 2"})
    rows = build_citation_index(acts, built_at="2026-09-27")["citations"]
    assert len(rows) == 2
    assert all(row["status"] == "unresolved" for row in rows)
    assert {row["unresolved_reason"] for row in rows} == {
        "ambiguous_target_norm", "range_contains_ambiguous_target_norm"}


def test_identical_duplicate_norm_records_do_not_create_false_ambiguity():
    acts = _acts()
    _only_alpha_text(acts, "§ 5 BetaG")
    acts[1]["norms"].append(dict(acts[1]["norms"][0]))
    rows = build_citation_index(acts, built_at="2026-09-27")["citations"]
    assert len(rows) == 1
    assert rows[0]["status"] == "resolved"


def test_named_law_scope_applies_only_to_its_enclosing_source_list_item(tmp_path):
    acts = _acts()
    text = "1.aus dem BetaG:a)nach § 5,b)nach § 6.2.Nach § 2 gilt dies."
    _only_alpha_text(acts, text)
    _reader_fixture(tmp_path, text, [_element("dl",
        _element("dt", _text("1.")),
        _element("dd", _text("aus dem BetaG:"), _element("dl",
            _element("dt", _text("a)")), _element("dd", _text("nach § 5,")),
            _element("dt", _text("b)")), _element("dd", _text("nach § 6.")))),
        _element("dt", _text("2.")),
        _element("dd", _text("Nach § 2 gilt dies.")))])
    index = build_citation_index(acts, built_at="2026-09-27",
                                 reader_directory=tmp_path)
    assert {(r["target_act"], r["target_norm"]) for r in index["citations"]} == {
        ("fed_beta", "§ 5"), ("fed_beta", "§ 6"), ("fed_alpha", "§ 2")}
    assert index["counts"]["contextual_citation_groups"] == 2


def test_unknown_explicit_list_scope_cannot_become_a_same_act_reference(tmp_path):
    acts = _acts()
    text = "aus dem Ausgangsstoffgesetz:Straftaten nach § 2."
    _only_alpha_text(acts, text)
    _reader_fixture(tmp_path, text, [_element("dl", _element("dd",
        _text("aus dem Ausgangsstoffgesetz:"), _element("br"),
        _text("Straftaten nach § 2.")))])
    rows = build_citation_index(acts, built_at="2026-09-27",
                                reader_directory=tmp_path)["citations"]
    assert len(rows) == 1
    assert rows[0]["status"] == "unresolved"
    assert rows[0]["target_jurabk"] == "Ausgangsstoffgesetz"
    assert rows[0]["target_act"] is None
    assert rows[0]["citation_text"] == "§ 2"
    assert rows[0]["source_excerpt"] == text


def test_nonlegal_list_heading_does_not_invent_a_foreign_law(tmp_path):
    acts = _acts()
    text = "nach dem Inhalt der Mitteilung:§ 2 bleibt anwendbar."
    _only_alpha_text(acts, text)
    _reader_fixture(tmp_path, text, [_element("dl", _element("dd", _text(text)))])
    rows = build_citation_index(acts, built_at="2026-09-27",
                                reader_directory=tmp_path)["citations"]
    assert len(rows) == 1
    assert rows[0]["target_act"] == "fed_alpha"
    assert rows[0]["status"] == "resolved"


def test_official_sentence_ordinals_and_footnotes_bound_citation_context(tmp_path):
    acts = _acts()
    text = "Nach § 2,3Noch § 4Amtliche Anmerkung (BetaG)."
    _only_alpha_text(acts, text)
    sentence = _element("sup", _text("3"))
    sentence["attrs"] = {"role": "sentence"}
    note = _element("small", _text("Amtliche Anmerkung (BetaG)."))
    note["attrs"] = {"role": "footnote"}
    _reader_fixture(tmp_path, text, [_element("p", _text("Nach § 2,"),
                                              sentence, _text("Noch § 4"), note)])
    rows = build_citation_index(acts, built_at="2026-09-27",
                                reader_directory=tmp_path)["citations"]
    assert {(r["target_act"], r["target_norm"], r["status"]) for r in rows} == {
        ("fed_alpha", "§ 2", "resolved"), ("fed_alpha", "§ 4", "resolved")}
    assert all(r["source_excerpt"] == text for r in rows)


def test_indexed_structure_keeps_duplicate_source_norm_records_separate(tmp_path):
    acts = _acts()
    first = "Nach § 2,3.Aufgabe."
    second = "Andere Aufgabe nach § 4,3.Ende."
    _only_alpha_text(acts, first)
    acts[0]["norms"].append({"enbez": "§ 1", "text": second,
                              "glied": "Anderer Artikel"})
    _reader_fixture(tmp_path, first, [])
    path = tmp_path / "fed_alpha.json"
    payload = json.loads(path.read_text())
    payload["norms"] = {}
    payload["norms_by_index"] = {}
    for index, text, before, after in [
            (0, first, "Nach § 2,", "Aufgabe."),
            (len(acts[0]["norms"]) - 1, second, "Andere Aufgabe nach § 4,", "Ende.")]:
        payload["norms_by_index"][str(index)] = {
            "enbez": "§ 1", "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "blocks": [_element("dl", _element("dd", _text(before)),
                                _element("dt", _text("3.")),
                                _element("dd", _text(after)))],
        }
    path.write_text(json.dumps(payload))
    index = build_citation_index(acts, built_at="2026-09-27",
                                 reader_directory=tmp_path)
    assert {r["target_norm"] for r in index["citations"]} == {"§ 2", "§ 4"}
    assert index["counts"]["structured_source_norms"] == 2


def _only_alpha_text(acts: list[dict], text: str) -> None:
    for norm in acts[0]["norms"]:
        norm["text"] = ""
    acts[0]["norms"][0]["text"] = text


def test_current_citation_index_separates_self_cross_and_unresolved():
    index, rows = _rows()

    self_targets = {(row["target_norm"], row["status"])
                    for row in rows
                    if row["source_act"] == "fed_alpha"
                    and row["source_norm"] == "§ 1"
                    and row["kind"] == "self"}
    assert {("§ 2", "resolved"), ("§ 3", "resolved"),
            ("§ 4", "resolved")} <= self_targets

    beta = [row for row in rows if row["target_act"] == "fed_beta"]
    assert {(row["target_norm"], row["kind"], row["status"])
            for row in beta} == {
                ("§ 5", "cross_act", "resolved"),
                ("§ 6", "cross_act", "resolved"),
                ("§ 999", "cross_act", "unresolved"),
            }
    missing_norm = next(row for row in beta if row["target_norm"] == "§ 999")
    assert missing_norm["unresolved_reason"] == \
        "target_norm_not_in_current_corpus"

    unknown = next(row for row in rows
                   if row["target_jurabk"] == "UnknownG")
    assert unknown["target_act"] is None
    assert unknown["kind"] == "cross_act"
    assert unknown["status"] == "unresolved"
    assert unknown["unresolved_reason"] == \
        "target_act_not_in_current_corpus"

    foreign_marker = next(row for row in rows
                          if row["target_norm"] == "Art. 1")
    assert foreign_marker["status"] == "unresolved"
    assert foreign_marker["unresolved_reason"] == \
        "unqualified_foreign_marker"

    repeated = next(row for row in rows
                    if row["source_act"] == "fed_alpha"
                    and row["source_norm"] == "§ 1"
                    and row["target_act"] == "fed_alpha"
                    and row["target_norm"] == "§ 2")
    assert repeated["occurrence_count"] == 2
    assert repeated["source_excerpt"]
    assert repeated["machine_extracted"] is True
    assert repeated["current_state_only"] is True
    assert repeated["legal_interpretation"] == "not_asserted"
    assert repeated["source_snapshot"] == "2026-07-15"
    assert repeated["date_basis"] == \
        "current_consolidated_snapshot_observation_not_legal_effect"

    assert index["machine_extracted"] is True
    assert index["current_state_only"] is True
    assert index["source_policy"]["fuzzy_matching"] is False
    assert index["counts"]["total"] == len(rows)
    assert index["counts"]["unresolved"] >= 3


def test_compound_reference_retains_every_target_after_an_earlier_single_reference():
    acts = _acts()
    acts[0]['norms'].append({'enbez': '§ 2a', 'text': ''})
    single = '§ 2 Absatz 3'
    compound = '§ 2 Absatz 3, § 2a Absatz 4 Nummer 2'
    for text in (f'Nach {single}. Die Stelle ({compound}) handelt.',
                 f'Die Stelle ({compound}) handelt. Nach {single}.'):
        _only_alpha_text(acts, text)
        rows = build_citation_index(acts, built_at='2026-09-27')['citations']
        assert {(row['citation_text'], row['target_norm'], row['occurrence_count']) for row in rows} == {
            (single, '§ 2', 1), (compound, '§ 2', 1), (compound, '§ 2a', 1)}
        assert all(row['status'] == 'resolved' for row in rows)


def test_identical_compound_occurrences_aggregate_without_losing_other_group_evidence(tmp_path):
    from api.citation_store import query_citations
    acts = _acts()
    first, second = '§§ 3 und 4', '§ 3'
    _only_alpha_text(acts, f'{second} gilt; {first} gelten; nochmals {first}.')
    index = build_citation_index(acts, built_at='2026-09-27')
    rows = index['citations']
    assert {(row['citation_text'], row['target_norm'], row['occurrence_count']) for row in rows} == {
        (second, '§ 3', 1), (first, '§ 3', 2), (first, '§ 4', 2)}
    path = tmp_path/'citations.sqlite'
    write_citation_database(path, index)
    stored = query_citations(path, act='fed_alpha', norm='§ 1', direction='out', kind=None, limit=10, offset=0)
    assert {(row['citation_text'], row['target_norm'], row['occurrence_count']) for row in stored['citations']} == {
        (second, '§ 3', 1), (first, '§ 3', 2), (first, '§ 4', 2)}


def test_shared_trailing_alias_applies_to_every_explicit_head():
    acts = _acts()
    _only_alpha_text(acts, "Nach § 5 und § 6 BetaG gilt dies.")
    rows = build_citation_index(
        acts, built_at="2026-07-16T12:00:00+00:00")['citations']

    assert {(row["target_act"], row["target_norm"], row["kind"])
            for row in rows} == {
                ("fed_beta", "§ 5", "cross_act"),
                ("fed_beta", "§ 6", "cross_act"),
            }


def test_article_dot_normalization_and_exact_alias_resolution():
    acts = _acts()
    _only_alpha_text(acts, "Die Garantie aus Art. 1 GG gilt.")
    rows = build_citation_index(
        acts, built_at="2026-07-16T12:00:00+00:00")['citations']

    assert len(rows) == 1
    assert rows[0]["target_act"] == "fed_gg"
    assert rows[0]["target_norm"] == "Art. 1"
    assert rows[0]["status"] == "resolved"


def test_inflected_article_designators_resolve_exact_national_targets():
    acts = _acts()
    _only_alpha_text(acts, "Nach Artikels 1 des Grundgesetzes und Artikel 1 GG.")
    rows = build_citation_index(acts, built_at="2026-09-21")['citations']

    assert {row['citation_text'] for row in rows} == {'Artikels 1 des Grundgesetzes', 'Artikel 1 GG'}
    assert len(rows) == 2
    assert all(row["target_act"] == "fed_gg" and row["target_norm"] == "Art. 1"
               and row["occurrence_count"] == 1 for row in rows)


def test_explicit_eu_instrument_is_never_a_same_act_reference():
    acts = [{
        "id": "by_example", "jurabk": "ExampleG", "juris": "DE-BY",
        "title": "Beispielgesetz",
        "norms": [{"enbez": "Art. 1", "text": (
            "Artikel 14 Absatz 2 der Verordnung (EU) 2024/1347 und "
            "Artikels 14 Absatz 1 Buchstabe e der Verordnung (EU) 2024/1347. "
            "Art. 14 der Verordnung (EG) Nr. 1234/2007. "
            "Artikel 14 der Richtlinie 2011/95/EU."
        )}, {"enbez": "Art. 14", "text": ""}],
    }]
    rows = build_citation_index(acts, built_at="2026-09-21")['citations']

    assert len(rows) == 4
    assert all(row["kind"] == "cross_act" for row in rows)
    assert all(row["status"] == "unresolved" for row in rows)
    assert all(row["target_act"] is None for row in rows)
    assert all(row["unresolved_reason"] == "target_act_not_in_current_corpus"
               for row in rows)
    assert {row["target_jurabk"] for row in rows} == {
        "Verordnung (EU) 2024/1347", "Verordnung (EG) Nr. 1234/2007",
        "Richtlinie 2011/95/EU",
    }


def test_generic_this_regulation_remains_an_explicit_self_reference():
    acts = [{
        "id": "by_example", "jurabk": "ExampleV", "juris": "DE-BY",
        "title": "Beispielverordnung",
        "norms": [{"enbez": "Art. 1", "text": (
            "Artikel 14 dieser Verordnung gilt."
        )}, {"enbez": "Art. 14", "text": ""}],
    }]
    rows = build_citation_index(acts, built_at="2026-09-21")['citations']

    assert len(rows) == 1
    assert rows[0]["target_act"] == "by_example"
    assert rows[0]["status"] == "resolved"


def test_unrecognized_explicit_eu_identifier_does_not_become_self():
    acts = [{
        "id": "by_example", "jurabk": "ExampleG", "juris": "DE-BY",
        "title": "Beispielgesetz",
        "norms": [{"enbez": "Art. 1", "text": (
            "Artikel 14 der Verordnung (EU) 2024/99999 gilt."
        )}, {"enbez": "Art. 14", "text": ""}],
    }]
    rows = build_citation_index(acts, built_at="2026-09-21")['citations']

    assert len(rows) == 1
    assert rows[0]["kind"] == "cross_act"
    assert rows[0]["target_act"] is None
    assert rows[0]["status"] == "unresolved"


def test_short_social_book_name_does_not_link_to_same_number_in_source():
    acts = [{
        "id": "fed_sgb_12", "jurabk": "SGB 12", "juris": "DE",
        "title": "Sozialgesetzbuch Zwölftes Buch",
        "norms": [{"enbez": "§ 11", "text": (
            "Ein Persönliches Budget nach § 29 des Neunten Buches."
        )}, {"enbez": "§ 29", "text": ""}],
    }, {
        "id": "fed_sgb_9_2018", "jurabk": "SGB 9 2018", "juris": "DE",
        "title": "Sozialgesetzbuch Neuntes Buch",
        "norms": [{"enbez": "§ 29", "text": ""}],
    }]
    rows = build_citation_index(acts, built_at="2026-09-21")['citations']

    assert len(rows) == 1
    assert rows[0]["target_act"] == "fed_sgb_9_2018"
    assert rows[0]["target_norm"] == "§ 29"
    assert rows[0]["kind"] == "cross_act"


def test_short_ordinal_book_outside_social_code_context_stays_unresolved():
    acts = _acts()
    _only_alpha_text(acts, "Nach § 2 des Neunten Buches gilt dies.")
    rows = build_citation_index(acts, built_at="2026-09-21")['citations']

    assert len(rows) == 1
    assert rows[0]["status"] == "unresolved"
    assert rows[0]["target_act"] is None


def test_ambiguous_exact_alias_is_flagged_instead_of_guessed():
    acts = _acts() + [{
        "id": "by_beta", "jurabk": "BetaG", "juris": "DE-BY",
        "title": "Anderes Beta-Gesetz",
        "norms": [{"enbez": "Art. 5", "text": ""}],
    }]
    _only_alpha_text(acts, "Siehe § 5 BetaG.")
    rows = build_citation_index(
        acts, built_at="2026-07-16T12:00:00+00:00")['citations']

    assert len(rows) == 1
    assert rows[0]["target_act"] is None
    assert rows[0]["status"] == "unresolved"
    assert rows[0]["unresolved_reason"] == "ambiguous_target_act"


def test_ids_and_output_order_are_deterministic():
    first = build_citation_index(
        _acts(), built_at="2026-07-16T12:00:00+00:00")
    second = build_citation_index(
        list(reversed(_acts())), built_at="2026-07-16T12:00:00+00:00")

    assert first["citations"] == second["citations"]


def test_sqlite_is_deterministic_indexed_and_manifest_has_no_rows(
        tmp_path: Path):
    index = build_citation_index(
        _acts(), built_at="2026-07-16T12:00:00+00:00",
        source_snapshots={"DE": "2026-07-15"})
    first, second = tmp_path / "first.sqlite", tmp_path / "second.sqlite"
    write_citation_database(first, index)
    write_citation_database(second, index)

    assert first.read_bytes() == second.read_bytes()
    manifest = citation_manifest(index)
    assert "citations" not in manifest
    assert manifest["storage"] == {
        "format": "sqlite3", "file": "citations.sqlite",
        "table": "citation", "rows": index["counts"]["total"],
        "ordering": "ordinal", "read_only": True,
    }
    assert len(json.dumps(manifest)) < 5000

    with sqlite3.connect(first) as database:
        assert database.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert database.execute(
            "SELECT COUNT(*) FROM citation").fetchone()[0] == \
            index["counts"]["total"]
        indexes = {row[1] for row in database.execute(
            "PRAGMA index_list('citation')")}
        assert {"citation_source_act", "citation_source_jurabk",
                "citation_target_act", "citation_target_jurabk",
                "citation_target_pinpoint"} <= indexes
        plan = " ".join(str(part) for row in database.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM citation "
            "WHERE target_act_key=? AND target_norm_key=? ORDER BY ordinal",
            ("fed_beta", "§ 5")) for part in row)
        assert "citation_target_act" in plan


def test_citations_api_exact_direction_filters_and_stable_pagination(
        tmp_path: Path, monkeypatch):
    import pytest

    pytest.importorskip("fastapi")
    from api import main as api_main
    from fastapi.testclient import TestClient

    data = build_citation_index(
        _acts(), built_at="2026-07-16T12:00:00+00:00",
        source_snapshots={"DE": "2026-07-15"})
    manifest = citation_manifest(data)
    write_citation_database(tmp_path / "citations.sqlite", data)
    monkeypatch.setattr(api_main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(api_main, "_load", lambda name: manifest
                        if name == "citations" else None)

    inbound = api_main.citations(
        act="BetaG", norm="§ 5", direction="in", kind="cross_act",
        limit=100, offset=0)
    assert inbound["direction"] == "in"
    assert inbound["matched"] == 1
    assert inbound["citations"][0]["target_act"] == "fed_beta"
    assert inbound["citations"][0]["target_norm"] == "§ 5"

    outbound = api_main.citations(
        act="fed_alpha", norm=None, direction="out", kind=None,
        limit=1000, offset=0)
    first_page = api_main.citations(
        act="fed_alpha", norm=None, direction="out", kind=None,
        limit=2, offset=0)
    second_page = api_main.citations(
        act="fed_alpha", norm=None, direction="out", kind=None,
        limit=2, offset=2)
    assert first_page["citations"] + second_page["citations"] == \
        outbound["citations"][:4]
    assert {row["id"] for row in first_page["citations"]}.isdisjoint(
        {row["id"] for row in second_page["citations"]})

    exact = api_main.citations(
        act="fed_alpha", norm="§ 2", direction="out", kind="self",
        limit=100, offset=0)
    assert exact["matched"] == 1
    assert exact["citations"][0]["source_norm"] == "§ 2"

    client = TestClient(api_main.app)
    response = client.get("/citations", params={
        "act": "BetaG", "norm": "§ 5", "direction": "in",
        "kind": "cross_act", "limit": 1, "offset": 0,
    })
    assert response.status_code == 200
    assert response.json()["citations"][0]["target_act"] == "fed_beta"
    assert client.get("/citations?direction=sideways").status_code == 422


def _citation_fixture(text: str) -> list[dict]:
    return [{
        "id": "by_agsg", "jurabk": "AGSG", "juris": "DE-BY",
        "title": "Gesetz zur Ausführung der Sozialgesetze",
        "norms": [{"enbez": "Art. 59", "text": text}],
    }, {
        "id": "fed_bgb", "jurabk": "BGB", "juris": "DE",
        "title": "Bürgerliches Gesetzbuch",
        "norms": [{"enbez": f"§ {number}", "text": ""}
                  for number in (1, 2, 3, 4, 5, 6, 1798, 1835, 1849)],
    }, {
        "id": "fed_beta", "jurabk": "BetaG", "juris": "DE",
        "title": "Beta-Gesetz",
        "norms": [{"enbez": "§ 3", "text": ""}],
    }]


def _fixture_rows(text: str) -> list[dict]:
    return build_citation_index(_citation_fixture(text),
                                built_at="2026-09-27")["citations"]


def test_plural_sections_with_pinpoint_have_individual_target_norms():
    text = "§§ 1835 Abs. 1 und 1849 des Bürgerlichen Gesetzbuches (BGB)"
    rows = _fixture_rows(text)
    assert {(row["target_act"], row["target_norm"], row["target_pinpoint"])
            for row in rows} == {
        ("fed_bgb", "§ 1835", "§ 1835 Abs. 1"),
        ("fed_bgb", "§ 1849", None),
    }
    assert all(row["citation_text"] == text for row in rows)


def test_singular_section_keeps_unmarked_pinpoint_lists():
    rows = _fixture_rows("§ 1835 Abs. 1 und 2 BGB")
    assert len(rows) == 1
    assert rows[0]["target_norm"] == "§ 1835"
    assert rows[0]["target_pinpoint"] == "§ 1835 Abs. 1 und 2"


def test_each_plural_head_keeps_its_own_pinpoint():
    rows = _fixture_rows("§§ 3 Abs. 1, 4 Satz 2 und 5 Abs. 3 BGB")
    assert {(row["target_act"], row["target_norm"], row["target_pinpoint"])
            for row in rows} == {
        ("fed_bgb", "§ 3", "§ 3 Abs. 1"),
        ("fed_bgb", "§ 4", "§ 4 Satz 2"),
        ("fed_bgb", "§ 5", "§ 5 Abs. 3"),
    }


def test_repeated_marker_after_pinpoint_shares_trailing_law():
    rows = _fixture_rows("§ 3 Abs. 1 und § 4 Abs. 2 BGB")
    assert {(row["target_act"], row["target_norm"], row["target_pinpoint"])
            for row in rows} == {
        ("fed_bgb", "§ 3", "§ 3 Abs. 1"),
        ("fed_bgb", "§ 4", "§ 4 Abs. 2"),
    }


def test_repeated_pinpoint_labels_do_not_create_section_heads():
    rows = _fixture_rows("§§ 3 Abs. 1 und Abs. 2, 4 BGB")
    assert {(row["target_norm"], row["target_pinpoint"]) for row in rows} == {
        ("§ 3", "§ 3 Abs. 1 und Abs. 2"), ("§ 4", None),
    }


def test_buchstaben_list_in_plural_head_remains_one_pinpoint():
    rows = _fixture_rows("§§ 3 Abs. 1 Buchst. a und b, 4 BGB")
    assert {(row["target_norm"], row["target_pinpoint"]) for row in rows} == {
        ("§ 3", "§ 3 Abs. 1 Buchst. a und b"), ("§ 4", None),
    }


def test_parenthetical_alias_closes_only_its_own_parenthesis():
    rows = _fixture_rows("(nach § 3 des Bürgerlichen Gesetzbuches (BGB)).")
    assert rows[0]["citation_text"] == \
        "§ 3 des Bürgerlichen Gesetzbuches (BGB)"


def test_connected_reference_inherits_trailing_law_across_explicit_bridge():
    for bridge in ("in Verbindung mit", "i. V. m.", "i.V.m."):
        rows = _fixture_rows(
            f"§ 1798 Abs. 2 {bridge} §§ 1835 Abs. 1 und 1849 BGB")
        assert {(row["target_act"], row["target_norm"], row["target_pinpoint"])
                for row in rows} == {
            ("fed_bgb", "§ 1798", "§ 1798 Abs. 2"),
            ("fed_bgb", "§ 1835", "§ 1835 Abs. 1"),
            ("fed_bgb", "§ 1849", None),
        }


def test_trailing_law_never_overrides_own_law_or_crosses_prose():
    for text in (
        "§ 3 BetaG in Verbindung mit § 4 BGB",
        "§ 3 gilt entsprechend; anschließend § 4 BGB",
        "§ 3 für diese Zwecke und § 4 BGB",
    ):
        rows = _fixture_rows(text)
        first = next(row for row in rows if row["target_norm"].startswith("§ 3"))
        assert first["target_act"] != "fed_bgb"


def test_sentence_abbreviation_and_multiletter_buchstaben_are_complete():
    for letter in ("aa", "ii", "iaa"):
        text = f"§ 3 Abs. 2 S. 4 Buchstabe {letter} BGB"
        rows = _fixture_rows(text)
        assert len(rows) == 1
        assert rows[0]["target_act"] == "fed_bgb"
        assert rows[0]["target_pinpoint"] == text.removesuffix(" BGB")
        assert rows[0]["citation_text"] == text


def test_explicit_plural_pinpoint_label_retains_its_list():
    for label in ("Absätze", "Sätze", "Nummern"):
        rows = _fixture_rows(f"§§ 3 {label} 1 und 2 und § 4 BGB")
        assert {(row["target_norm"], row["target_pinpoint"])
                for row in rows} == {
            ("§ 3", f"§ 3 {label} 1 und 2"), ("§ 4", None),
        }


def test_trailing_head_of_plural_group_keeps_its_absatz_list():
    rows = _fixture_rows("§§ 3 und 4 Abs. 1 und 2 BGB")
    assert {(row["target_norm"], row["target_pinpoint"]) for row in rows} == {
        ("§ 3", None), ("§ 4", "§ 4 Abs. 1 und 2"),
    }


def test_plural_group_keeps_pinpoint_list_before_another_explicit_head():
    rows = _fixture_rows("§§ 3, 4 Abs. 1 und 2, § 5 Abs. 2, 3 BGB")
    assert {(row["target_norm"], row["target_pinpoint"]) for row in rows} == {
        ("§ 3", None), ("§ 4", "§ 4 Abs. 1 und 2"),
        ("§ 5", "§ 5 Abs. 2, 3"),
    }


def test_section_ranges_and_sowie_resolve_every_head():
    for text in ("§§ 1–3 BGB", "§§ 1 - 3 BGB", "§§ 1, 2 sowie 3 BGB"):
        rows = _fixture_rows(text)
        assert {(row["target_act"], row["target_norm"]) for row in rows} == {
            ("fed_bgb", "§ 1"), ("fed_bgb", "§ 2"), ("fed_bgb", "§ 3"),
        }


def test_full_date_after_section_is_not_a_range_or_second_head():
    for suffix in ("bis einschließlich 31. Dezember 2012", "bis 31.12.2012",
                   "und 1. Januar 2027", ", 01.01.2027"):
        rows = _fixture_rows(f"Nach § 3 {suffix} gilt dies.")
        assert len(rows) == 1
        assert rows[0]["citation_text"] == "§ 3"
        assert rows[0]["target_norm"] == "§ 3"


def test_letter_pinpoint_does_not_swallow_following_prose():
    for word in ("die", "der", "das", "zur", "im", "ein", "je", "sie"):
        rows = _fixture_rows(f"§ 3 Buchstabe a und {word} Regel")
        assert len(rows) == 1
        assert rows[0]["citation_text"] == "§ 3 Buchstabe a"
        assert rows[0]["target_pinpoint"] == "§ 3 Buchstabe a"


def test_range_endpoint_pinpoint_does_not_lose_trailing_law():
    rows = _fixture_rows("§§ 1 bis 3 Nummer 1 bis 4 BGB")
    assert {(row["target_act"], row["target_norm"], row["target_pinpoint"])
            for row in rows} == {
        ("fed_bgb", "§ 1", None), ("fed_bgb", "§ 2", None),
        ("fed_bgb", "§ 3", "§ 3 Nummer 1 bis 4"),
    }


def test_parallel_groups_with_article_share_explicit_trailing_law():
    rows = _fixture_rows("die §§ 1 bis 2, die §§ 3, 4 sowie die §§ 5 bis 6 BGB")
    assert {(row["target_act"], row["target_norm"]) for row in rows} == {
        ("fed_bgb", f"§ {number}") for number in range(1, 7)
    }


def test_deeper_pinpoint_after_value_list_stays_on_the_existing_head():
    for suffix in ("Satz 3", "Nummer 3", "Buchstabe aa"):
        rows = _fixture_rows(f"§§ 3 und 4 Abs. 1 und 2 {suffix} BGB")
        assert {(row["target_norm"], row["target_pinpoint"])
                for row in rows} == {
            ("§ 3", None), ("§ 4", f"§ 4 Abs. 1 und 2 {suffix}"),
        }


def test_specialized_pinpoint_labels_preserve_trailing_alias():
    text = "§ 3 Unterabsatz 1 Nrn. 2 und 3 Buchstabe a Doppelbuchstabe aa BGB"
    rows = _fixture_rows(text)
    assert len(rows) == 1
    assert rows[0]["target_act"] == "fed_bgb"
    assert rows[0]["target_pinpoint"] == text.removesuffix(" BGB")


def test_an_act_from_another_snapshot_family_names_its_own_snapshot():
    """Archived Bavarian and EU acts are not from their jurisdiction's feed:
    their rows carry the act's own snapshot (publish validation rejects NULL)."""
    acts = _acts()
    acts[0] = {**acts[0], "juris": "DE-BY", "source_snapshot": "bayern_recht_archive/2026-09-29"}
    eu = {**_acts()[0], "id": "eu_x", "juris": "EU", "jurabk": "XVO"}
    index = build_citation_index([acts[0], eu], built_at="2026-09-29T00:00:00+00:00",
                                 source_snapshots={"DE-BY": "2026-09-17", "EU": "2026-09-29"})
    snapshots = {row["source_act"]: row["source_snapshot"] for row in index["citations"]}
    assert snapshots["fed_alpha"] == "bayern_recht_archive/2026-09-29"
    assert snapshots["eu_x"] == "2026-09-29"
