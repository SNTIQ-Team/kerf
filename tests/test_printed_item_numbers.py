"""Amendment commands keep the item number printed in the BGBl.

Amending laws insert items during the parliamentary procedure (``16a.``,
``36a.``) and number leading items ``0``, ``0a``.  The command's ``item`` is
that printed number, not its position in the article: otherwise every
citation after the first inserted item says "Nr. N+1", and an Inkrafttreten
clause naming "Artikel 1 Nummer 27 Buchstabe b" dates the wrong norm.

The real case is BGBl. 2026 I Nr. 107 (13th SGB II amendment): Art. 1
inserts 16a (§ 16f), 36a (§ 56) and 40a (§ 64a); Art. 12 Abs. 2 brings
Art. 1 Nr. 26 Buchst. d (§ 31a Abs. 7) and Nr. 27 Buchst. b (§ 31b Abs. 3)
into force the day after promulgation (2026-04-23), the rest of Art. 1 on
2026-07-01.  The fixture pins the official text by SHA-256.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT))

from amendment_items import (  # noqa: E402
    annotate_commands, itemise_amendment_commands,
)
from backfill_bgbl_history import parse_amendment_commands  # noqa: E402
from change_dates import resolve_change_dates  # noqa: E402
from patch_parser import _split_labelled_items, item_number  # noqa: E402

from tools.build_law_history import ChangeDates, norm_changes  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC = {"article": "1", "heading": "Änderung des Beispielgesetzes",
             "text": ("Das Beispielgesetz vom 1. Januar 2000 (BGBl. I S. 1) "
                      "wird wie folgt geändert:\n"
                      "1. § 1 wird aufgehoben.\n"
                      "2. § 2 wird wie folgt geändert:\n"
                      "a) Absatz 1 wird aufgehoben.\n"
                      "b) Absatz 2 wird aufgehoben.\n"
                      "2a. § 2a wird aufgehoben.\n"
                      "2b. § 2b wird wie folgt geändert:\n"
                      "a) Absatz 1 wird aufgehoben.\n"
                      "b) Absatz 3 wird aufgehoben.\n"
                      "c) Absatz 4 wird aufgehoben.\n"
                      "3. § 3 wird aufgehoben.\n")}


def _real() -> dict:
    payload = json.loads(gzip.decompress(
        (FIXTURES / "bgbl_2026_107.json.gz").read_bytes()))
    assert hashlib.sha256(payload["text"].encode("utf-8")).hexdigest() == \
        payload["text_sha256"]
    return payload


def _article(payload: dict, number: str) -> dict:
    return next(a for a in payload["articles"] if a["article"] == number)


def _items_by_para(commands: list[dict]) -> dict[str, list]:
    out: dict[str, list] = {}
    for command in commands:
        para = (command.get("ref") or {}).get("para")
        if para:
            out.setdefault(para, []).append(command["item"])
    return out


def test_item_number_is_the_printed_label():
    assert item_number("16a", 17) == "16a"
    assert item_number("27", 28) == 27
    assert item_number("0", 1) == 0
    assert item_number(None, 1) == 1          # unnumbered: one command
    assert _split_labelled_items("§ 3 wird aufgehoben.") == [
        (None, "§ 3 wird aufgehoben.")]


def test_inserted_items_keep_their_number_and_later_items_do_not_shift():
    commands, norms = parse_amendment_commands(SYNTHETIC)
    assert [c["item"] for c in commands] == [1, 2, "2a", "2b", 3]
    assert _items_by_para(commands) == {
        "1": [1], "2": [2], "2a": ["2a"], "2b": ["2b"], "3": [3]}
    # sub-letters stay inside their item; nothing is split or renumbered
    assert "c) Absatz 4" in commands[3]["raw"]
    assert norms == ["§ 1", "§ 2", "§ 2a", "§ 2b", "§ 3"]
    # the annotation still finds each command's top-level position
    annotated = annotate_commands(commands, SYNTHETIC)
    assert [r["position_status"] for r in annotated] == ["aligned"] * 5
    assert [r["position"]["nummer"] for r in annotated] == [
        "1", "2", "2a", "2b", "3"]
    assert [r["leaf_count"] for r in annotated] == [1, 2, 1, 3, 1]


def test_items_before_one_keep_zero_and_zero_a():
    row = json.loads((FIXTURES / "bgbl_command_items.json").read_text())[
        "bgbl_2023_408_art10"]
    commands, _norms = parse_amendment_commands({
        "article": row["article"], "heading": row["heading"],
        "text": row["text"]})
    assert [c["item"] for c in commands[:4]] == [0, "0a", "0b", 1]
    assert commands[2]["ref"]["para"] == "8"


def test_bgbl_2026_107_items_match_the_printed_numbers():
    commands, _norms = parse_amendment_commands(_article(_real(), "1"))
    by_para = _items_by_para(commands)
    assert by_para["16f"] == ["16a"]
    assert by_para["56"] == ["36a"]
    # "Nach § 64 wird der folgende § 64a eingefügt" is addressed to § 64
    assert by_para["64"] == [40, "40a"]
    assert "§ 64a" in next(c["raw"] for c in commands
                           if c["item"] == "40a")[:40]
    assert by_para["16h"] == [17]
    assert by_para["19"] == [18]
    assert by_para["31a"] == [26]
    assert by_para["31b"] == [27]
    assert by_para["72"] == [44]
    # every command item is a printed label of the itemised leaves
    leaves = itemise_amendment_commands(_article(_real(), "1"))
    printed = {leaf["item"]["nummer"] for leaf in leaves}
    assert {str(c["item"]) for c in commands} <= printed


def test_bgbl_2026_107_commencement_lands_on_the_right_norms():
    """Art. 12 Abs. 2 → § 31a Abs. 7 and § 31b Abs. 3 on 2026-04-23."""
    payload = _real()
    section = _article(payload, "1")
    text = payload["text"]
    commands, norms = parse_amendment_commands(section)
    # change-date rows exactly as tools/resolve_change_dates.py + the
    # history bundle produce them, for the § 31a / § 31b leaves
    variants: dict[str, str] = {}
    rows = []
    for leaf in itemise_amendment_commands(section):
        if leaf["item"]["nummer"] not in {"26", "27"}:
            continue
        in_force = resolve_change_dates(text, "1", leaf["item"])["in_force"]
        blob = json.dumps({"date": in_force.get("date"),
                           "status": in_force.get("status")}, sort_keys=True)
        variants.setdefault(blob, str(len(variants)))
        rows.append({"kind": "item", "document_id": payload["document_id"],
                     "article": "1", "absatz": None,
                     "nummer": leaf["item"]["nummer"],
                     "buchstabe": leaf["item"]["buchstabe"],
                     "ref_para": (leaf.get("ref") or {}).get("para"),
                     "change_type": leaf.get("change_type"),
                     "in_force": variants[blob], "applies_from": None})
    document = {"kind": "document", "document_id": payload["document_id"],
                "variants": {v: json.loads(k) for k, v in variants.items()}}
    path = Path(__file__).parent / ".bgbl_2026_107_dates.jsonl"
    try:
        path.write_text("".join(json.dumps(r) + "\n"
                                for r in [document, *rows]))
        dates = ChangeDates([path])
    finally:
        path.unlink(missing_ok=True)
    event = {"document_id": payload["document_id"], "amending_article": "1",
             "affected_norms": ["§ 31a", "§ 31b"], "commands": commands,
             "published_at": payload["publication_date"]}
    act = {"id": "fed_sgb_2", "jurabk": "SGB 2", "slug": "sgb_2",
           "juris": "DE"}
    changes = norm_changes(act, [event], {}, dates, "2026-11-13")
    got = {(c["norm_label"], c["dates"]["in_force"].get("date")):
           (c["amending_act"]["items"], c["amending_act"]["citation"])
           for c in changes}
    assert got[("§ 31a", "2026-04-23")][0] == ["26.d"]
    assert got[("§ 31a", "2026-07-01")][0] == ["26.a,26.b,26.c"]
    assert got[("§ 31b", "2026-04-23")][0] == ["27.b"]
    assert got[("§ 31b", "2026-07-01")][0] == ["27.a"]
    assert got[("§ 31a", "2026-04-23")][1].startswith("Art. 1 Nr. 26 ")
    assert got[("§ 31b", "2026-04-23")][1].startswith("Art. 1 Nr. 27 ")
    assert not [key for key in got if key[1] is None]
