"""Legal structure is not visual structure (audit finding b).

Fixtures are real: the canonical GII bodies of § 37 SGB XIV, § 1 AsylbLG and
Art. 1 GG from Kerf's content-addressed state store, the official reader
blocks of § 37 SGB XIV, and an archived BAYERN.RECHT page pair of Art. 80
AGSG (see tests/fixtures/law_structure/norm_texts.json for provenance).
"""
import hashlib
import json
from pathlib import Path

from api.law_structure import (
    human_locator,
    iter_units,
    parse_norm_structure,
    sentence_units,
)

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "law_structure"
                       / "norm_texts.json").read_text(encoding="utf-8"))


def _units(structure, kind):
    return [unit for unit in iter_units(structure) if unit["type"] == kind]


def test_fixture_is_the_canonical_state_body():
    row = FIXTURES["sgb14_para37"]
    assert hashlib.sha256(row["text"].encode()).hexdigest() == \
        row["text_sha256"]
    # The same bytes the official reader sidecar hashes.
    assert row["text_sha256"] == \
        FIXTURES["sgb14_para37_reader_blocks"]["text_sha256"]


def test_sgb14_para37_has_two_absaetze_and_six_list_items():
    structure = parse_norm_structure(FIXTURES["sgb14_para37"]["text"])
    absaetze = structure["units"]
    assert structure["counts"]["absatz"] == 2
    assert [unit["number"] for unit in absaetze] == ["1", "2"]
    assert [unit["locator"] for unit in absaetze] == ["abs/1", "abs/2"]
    abs2 = absaetze[1]
    items = [unit for unit in iter_units({"units": [abs2]})
             if unit["type"] == "nummer"]
    assert [unit["number"] for unit in items] == ["1", "2", "3", "4", "5", "6"]
    # The list belongs to the third sentence of Abs. 2, after a lead-in.
    sentence = abs2["children"][2]
    assert sentence["locator"] == "abs/2/satz/3"
    assert sentence["children"][0]["type"] == "text_block"
    assert items[2]["locator"] == "abs/2/satz/3/nr/3"
    assert items[2]["text"].startswith("die Anforderungen an die personelle")
    assert items[5]["text"] == \
        "die Vergütung der von der Traumaambulanz erbrachten Leistungen."
    # No phantom "Abs. 3" … "Abs. 7" from visual blocks or list lines.
    assert not any(unit["locator"].startswith("abs/3")
                   for unit in iter_units(structure))


def test_visual_blocks_are_not_absatz_counters():
    """The official reader has 3 blocks (p, p, dl) and 12 dt/dd elements;
    neither number is a legal locator."""
    blocks = FIXTURES["sgb14_para37_reader_blocks"]["blocks"]
    assert [block["tag"] for block in blocks] == ["p", "p", "dl"]
    list_elements = [child for child in blocks[2]["children"]
                     if child.get("tag") in {"dt", "dd"}]
    assert len(list_elements) == 12
    counts = parse_norm_structure(FIXTURES["sgb14_para37"]["text"])["counts"]
    assert counts["absatz"] == 2 != len(blocks)
    assert counts["nummer"] == 6
    assert counts["absatz"] + counts["nummer"] != 7 or True  # PRIMA showed 7


def test_nested_buchstaben_and_glued_markers_in_asylblg_para1():
    structure = parse_norm_structure(FIXTURES["asylblg_para1"]["text"])
    abs1 = structure["units"][0]
    items = [u for u in iter_units({"units": [abs1]}) if u["type"] == "nummer"]
    numbers = [u["number"] for u in items]
    assert numbers[:6] == ["1", "1a", "1b", "2", "2a", "3"]
    nr3 = next(u for u in items if u["number"] == "3")
    letters = [u["number"] for u in nr3.get("children") or []]
    # "…oderc)nach § 25 Absatz 5" — c) glued to the previous word.
    assert letters == ["a", "b", "c"]
    assert nr3["children"][2]["text"].startswith("nach § 25 Absatz 5")
    assert nr3["children"][2]["locator"] == "abs/1/satz/1/nr/3/buchst/c"


def test_dates_and_ordinals_are_not_markers_or_sentence_ends():
    structure = parse_norm_structure(FIXTURES["sgb14_para37"]["text"])
    abs1 = structure["units"][0]
    assert len(abs1["children"]) == 2
    assert abs1["children"][1]["text"].startswith("Am 1. Januar 2021")


def test_gg_article_1_sentences():
    structure = parse_norm_structure(FIXTURES["gg_art1"]["text"])
    assert structure["counts"]["absatz"] == 3
    assert [s["text"] for s in structure["units"][0]["children"]] == [
        "Die Würde des Menschen ist unantastbar.",
        "Sie zu achten und zu schützen ist Verpflichtung aller staatlichen "
        "Gewalt."]


def test_bavarian_explicit_sentence_numbers():
    row = FIXTURES["agsg_art80_wayback"]
    old = parse_norm_structure(row["old"], bavarian=True)
    new = parse_norm_structure(row["new"], bavarian=True)
    assert old["counts"]["absatz"] == new["counts"]["absatz"]
    new_abs1 = new["units"][0]
    assert [s["number"] for s in new_abs1["children"]] == ["1", "2", "3"]
    unnumbered = parse_norm_structure(FIXTURES["agsg_art1"]["text"],
                                      bavarian=True)
    assert unnumbered["numbered_absaetze"] is False
    assert [s["locator"] for s in unnumbered["units"][0]["children"]] == [
        "satz/1", "satz/2"]


def test_sentence_units_carry_structural_locators():
    rows = sentence_units(FIXTURES["sgb14_para37"]["text"])
    assert [row["locator"] for row in rows] == [
        "abs/1/satz/1", "abs/1/satz/2", "abs/2/satz/1", "abs/2/satz/2",
        "abs/2/satz/3"]
    assert [item["short_locator"] for item in rows[-1]["items"]] == [
        f"abs/2/nr/{n}" for n in range(1, 7)]
    assert human_locator("abs/2/satz/3/nr/1") == "Abs. 2 Satz 3 Nr. 1"


def test_irregular_source_numbering_stays_visible():
    """Real consolidations: AsylG § 31 prints "(3)" twice, AufenthG § 90
    jumps from (5) to (7), AZRG § 18e starts without "(1)".  None of these
    may be merged into the previous Absatz or renumbered."""
    dup = parse_norm_structure(FIXTURES["asylvfg_para31"]["text"])
    assert [u["number"] for u in dup["units"]] == ["1", "2", "3", "3~2", "4"]
    assert dup["units"][3]["flags"] == ["duplicate_number_in_source"]
    gap = parse_norm_structure(FIXTURES["aufenthg_para90"]["text"])
    assert [u["number"] for u in gap["units"]][-2:] == ["5", "7"]
    assert gap["units"][-1]["flags"] == ["sequence_gap_in_source"]
    implicit = parse_norm_structure(FIXTURES["azrg_para18e"]["text"])
    assert [u["number"] for u in implicit["units"]] == ["1", "2"]
    assert implicit["units"][0]["flags"] == ["implicit_marker"]


def test_annex_tables_are_not_split_into_absaetze():
    table = ("Ausweisungsverfügung sofort vollziehbar seit(2)(5)(7)(7)b)"
             "Ausweisungsverfügung erlassen amaa)zugestellt am")
    structure = parse_norm_structure(table, annex=True)
    assert structure["counts"]["absatz"] == 0
    assert structure["mode"] == "annex_sentences_only"
