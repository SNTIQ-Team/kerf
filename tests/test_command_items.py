"""Amendment-command boundaries on real final BGBl text (audit finding 10).

bgbl-1-2023-408 Art. 10 numbers its first commands 0, 0a and 0b before 1;
item 0b amends § 8 SGB XIV.  bgbl-1-2023-146 Art. 3 appends a new § 26
Absatz 6 to the SGB II whose quoted wording mentions "Satz 1 Nummer 2".
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from backfill_bgbl_history import parse_amendment_commands  # noqa: E402
from patch_parser import _split_items, parse_command  # noqa: E402

FIXTURE = json.loads((Path(__file__).parent / "fixtures"
                      / "bgbl_command_items.json").read_text())


def test_items_zero_and_lettered_zero_are_commands():
    row = FIXTURE["bgbl_2023_408_art10"]
    commands, norms = parse_amendment_commands({
        "article": row["article"], "heading": row["heading"],
        "text": row["text"]})
    first = [command["raw"][:40] for command in commands[:4]]
    assert first[0].startswith("Die Überschrift wird wie folgt gefasst")
    assert first[1].startswith("Die Inhaltsübersicht wird")
    assert first[2].startswith("In § 8 Absatz 2 Satz 1")
    assert first[3].startswith("In § 12 Absatz 1 Satz 2")
    assert "§ 8" in norms
    # No command inherits the three leading items as its parent scope.
    assert not any("0b." in (command.get("parent_scope") or "")
                   for command in commands)
    assert commands[2]["ref"] == {"para": "8", "absatz": "2", "satz": "1"}


def test_item_sequence_still_rejects_dates_and_restarts():
    body = ("1. In § 3 wird die Angabe „31. Juli 2025“ ersetzt.\n"
            "31. Juli bleibt.\n2. § 4 wird aufgehoben.\n0a. kein Befehl\n")
    items = _split_items(body)
    assert len(items) == 2 and items[1].startswith("§ 4 wird aufgehoben")


def test_quoted_wording_never_becomes_the_address():
    row = FIXTURE["bgbl_2023_146"]
    ref = parse_command(row["raw"])["ref"]
    assert ref == {"para": "26", "absatz": "6"}


def test_lettered_and_three_digit_items_are_separate_commands():
    body = ("1. § 3 wird aufgehoben.\n1a. § 4 wird aufgehoben.\n"
            "1c. § 5 wird aufgehoben.\n2. § 6 wird aufgehoben.\n")
    assert [item.split()[1] for item in _split_items(body)] == [
        "3", "4", "5", "6"]
    long = "\n".join(f"{n}. § {n} wird aufgehoben." for n in range(1, 102))
    assert len(_split_items(long)) == 101
