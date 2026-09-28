from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from patch_parser import _split_items
from backfill_bgbl_history import parse_amendment_commands


def test_lettered_amendment_items_remain_separate_norms() -> None:
    # BGBl. 2023 I Nr. 408, Art. 1 uses inserted numbers 1a, 4a and 7a.
    body = (
        "Das Zwölfte Buch Sozialgesetzbuch wird wie folgt geändert:\n"
        "1. Das Inhaltsverzeichnis wird wie folgt geändert:\n"
        "a) Die Angabe zu § 142 wird wie folgt gefasst: „Verpflegung“.\n"
        "1a. In § 27b Absatz 2 wird das Wort „alt“ durch „neu“ ersetzt.\n"
        "2. In § 34 Absatz 3a wird das Wort „dem“ durch „den“ ersetzt.\n"
        "2a. § 35 wird aufgehoben.\n"
        "2b. § 36 wird aufgehoben.\n"
        "3. § 42a wird aufgehoben.\n"
    )
    commands, affected = parse_amendment_commands({"text": body})
    assert len(commands) == 6
    assert "§ 27b" in affected
    assert "§ 35" in affected
    assert "§ 36" in affected
    assert commands[1]["ref"]["para"] == "27b"
    assert "2. In § 34" not in commands[1]["raw"]


def test_quoted_numbering_and_dates_do_not_create_amendment_items() -> None:
    body = (
        "1. § 11 wird wie folgt gefasst:\n"
        "„(1) Es gelten\n1. der erste Fall,\n1a. der weitere Fall und\n"
        "2. der zweite Fall.\n31. Juli 2025 ist der Stichtag.“\n"
        "1a. § 12 wird aufgehoben.\n"
        "2. § 13 wird aufgehoben.\n"
    )
    items = _split_items(body)
    assert len(items) == 3
    assert "1a. der weitere Fall" in items[0]
    assert items[1].startswith("§ 12")


def test_three_digit_top_level_items_are_not_merged() -> None:
    body = "\n".join(f"{number}. § {number} wird aufgehoben."
                     for number in range(1, 102))
    assert len(_split_items(body)) == 101
