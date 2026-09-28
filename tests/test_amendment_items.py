"""Positions and change types of amendment commands (real BGBl articles).

tests/fixtures/amendment_articles.json holds verbatim prefixes of amending
articles (``text_sha256``/``start``/``end`` identify the source text).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from amendment_items import (  # noqa: E402
    CHANGE_TYPES, annotate_commands, classify_change,
    itemise_amendment_commands,
)
from backfill_bgbl_history import parse_amendment_commands  # noqa: E402

ARTICLES = {(row["document_id"], row["article"]): row for row in json.loads(
    (Path(__file__).parent / "fixtures" / "amendment_articles.json")
    .read_text(encoding="utf-8"))}


def section(document_id: str, article: str) -> dict:
    row = ARTICLES[(document_id, article)]
    return {"article": row["article"], "heading": row["heading"],
            "text": row["text"]}


def leaves(document_id: str, article: str) -> dict[str, dict]:
    return {leaf["item_label"]: leaf for leaf in itemise_amendment_commands(
        section(document_id, article))}


def test_positions_down_to_doppelbuchstabe():
    items = leaves("bgbl-1-2023-217", "1")
    assert list(items)[:5] == [
        "Nummer 1 Buchstabe a", "Nummer 1 Buchstabe b",
        "Nummer 1 Buchstabe c", "Nummer 1a", "Nummer 2"]
    leaf = items["Nummer 7 Buchstabe a Doppelbuchstabe bb"]
    assert leaf["item"] == {"absatz": None, "nummer": "7", "buchstabe": "a",
                            "doppelbuchstabe": "bb",
                            "dreifachbuchstabe": None}
    # The container "§ 18c wird wie folgt geändert: a) Absatz 1 …" supplies
    # the parent address; the leaf adds its own Nummer.
    assert leaf["ref"] == {"para": "18c", "absatz": "1", "nummer": "2"}
    assert leaf["raw"].startswith("In Nummer 2 wird die Angabe")


@pytest.mark.parametrize("label,change_type,unit,norm_change", [
    ("Nummer 1 Buchstabe a", "eingefügt", "inhaltsuebersicht", "geändert"),
    ("Nummer 1 Buchstabe b", "neugefasst", "inhaltsuebersicht", "geändert"),
    ("Nummer 1a", "geändert", "text", "geändert"),        # Wörter gestrichen
    ("Nummer 2", "geändert", "text", "geändert"),         # Angabe ersetzt
    ("Nummer 3 Buchstabe a", "neugefasst", "satz", "geändert"),
    ("Nummer 3 Buchstabe b", "eingefügt", "satz", "geändert"),
    ("Nummer 4 Buchstabe b", "neugefasst", "absatz", "geändert"),
    ("Nummer 5", "neugefasst", "norm", "neugefasst"),     # § 18a gefasst
    ("Nummer 8", "eingefügt", "norm", "eingefügt"),       # §§ 18g–18i
])
def test_change_types_from_the_command_verb(label, change_type, unit,
                                            norm_change):
    leaf = leaves("bgbl-1-2023-217", "1")[label]
    assert (leaf["change_type"], leaf["target_unit"],
            leaf["norm_change_type"]) == (change_type, unit, norm_change)
    assert leaf["change_type"] in CHANGE_TYPES


def test_new_units_of_an_insertion():
    leaf = leaves("bgbl-1-2023-217", "1")["Nummer 8"]
    assert leaf["new_units"] == [{"kind": "§§", "first": "18g",
                                  "last": "18i"}]


def test_repeal_and_renumbering_in_legacy_bürgergeld_text():
    items = leaves("bgbl-1-2022-s2328", "1")
    assert items["Nummer 3 Buchstabe b"]["change_type"] == "aufgehoben"
    assert items["Nummer 3 Buchstabe b"]["target_unit"] == "nummer"
    assert items["Nummer 3 Buchstabe c"]["change_type"] == "geändert"
    assert items["Nummer 4"]["change_type"] == "neugefasst"
    assert items["Nummer 4"]["norm_change_type"] == "neugefasst"
    # Hyphenated line breaks stay verbatim in raw.
    assert "Grundsiche -\nrung" in items["Nummer 1"]["raw"]
    assert items["Nummer 2 Buchstabe e"]["ref"] == {"toc": True}


def test_items_before_one():
    # BGBl. 2023 I Nr. 408, Art. 10 numbers its first commands 0, 0a, 0b.
    labels = [leaf["item"]["nummer"] for leaf in itemise_amendment_commands(
        section("bgbl-1-2023-408", "10"))]
    assert labels[:5] == ["0", "0a", "0a", "0a", "0b"]
    items = leaves("bgbl-1-2023-408", "10")
    assert items["Nummer 0"]["target_unit"] == "ueberschrift"
    assert items["Nummer 0b"]["ref"] == {"para": "8", "absatz": "2",
                                         "satz": "1"}


def test_letter_spaced_and_figure_space_markers():
    # BGBl. I 2011 S. 453, Art. 2: " 1. Die Inhaltsübersicht …" and
    # "b ) D i eA n g a b ez u§7w i r dw i ef o l g tg e f a s s t :".
    items = leaves("bgbl-1-2011-s453", "2")
    assert "Nummer 1 Buchstabe b" in items
    leaf = items["Nummer 1 Buchstabe b"]
    assert leaf["raw"].startswith("D i eA n g a b ez u§7")   # verbatim
    assert leaf["change_type"] == "neugefasst"
    assert leaf["ref"]["para"] == "7"


def test_kerned_item_markers_and_abbreviations():
    # BGBl. I 2008 S. 444, Art. 1: "2 .§1 0 Abs. 1 Satz 2 wird …".
    items = leaves("bgbl-1-2008-s444", "1")
    assert items["Nummer 2"]["ref"] == {"para": "10", "absatz": "1",
                                        "satz": "2"}
    assert items["Nummer 2"]["change_type"] == "neugefasst"
    assert items["Nummer 7"]["change_type"] == "ersetzt"  # durch folgende Sätze
    assert items["Nummer 8 Buchstabe b"]["change_type"] == "eingefügt"


def test_existing_parser_output_is_unchanged_and_annotation_adds_fields():
    article = section("bgbl-1-2023-217", "1")
    commands, affected = parse_amendment_commands(article)
    before = json.loads(json.dumps(commands))
    annotated = annotate_commands(commands, article)
    assert commands == before                       # not mutated
    for original, row in zip(commands, annotated):
        assert {key: row[key] for key in original} == original
        assert set(row) - set(original) == {
            "position", "position_status", "change_type", "target_unit",
            "norm_change_type", "leaf_count"}
    first = annotated[0]
    assert first["position"]["nummer"] == "1"
    assert first["position_status"] == "aligned"
    assert first["change_type"] == "geändert"       # "wie folgt geändert"
    assert first["leaf_count"] == 3
    assert [row["position"]["nummer"] for row in annotated[:4]] == [
        "1", "1a", "2", "3"]


def test_collective_subsection_position():
    # A Folgeänderungen subsection is addressed as Absatz.
    article = section("bgbl-1-2023-217", "1")
    leaf = itemise_amendment_commands(article, absatz="7")[0]
    assert leaf["item"]["absatz"] == "7"
    assert leaf["item_label"].startswith("Absatz 7 Nummer 1")


@pytest.mark.parametrize("raw,expected", [
    ("§ 5 wird aufgehoben.", ("aufgehoben", "norm", "aufgehoben")),
    ("In § 7 Absatz 1 Satz 1 werden die Wörter „x“ gestrichen.",
     ("geändert", "text", "geändert")),
    ("Absatz 3 wird durch die folgenden Absätze 3 und 4 ersetzt: „…“",
     ("ersetzt", "absatz", "geändert")),
    ("§ 9 wird durch den folgenden § 9 ersetzt: „§ 9 …“",
     ("ersetzt", "norm", "ersetzt")),
    ("Dem § 44 Absatz 2 wird folgender Satz angefügt: „…“",
     ("eingefügt", "satz", "geändert")),
    ("§ 12 wird wie folgt geändert:", ("geändert", "norm", "geändert")),
])
def test_classify_change_on_real_command_shapes(raw, expected):
    change = classify_change(raw)
    assert (change["change_type"], change["target_unit"],
            change["norm_change_type"]) == expected
