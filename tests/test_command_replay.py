"""Fail-closed command replay on real Lexgraph transitions.

tests/fixtures/command_replay_real.json holds four real transitions: the
official text before and after (Wayback/GII captures and GII daily states)
and the verbatim commands of the one BGBl article between them.  Replaying
the commands must reproduce the later official text exactly (identity).
"""
import json
from pathlib import Path

import pytest

from api.command_replay import (ReplayUnsupported, apply_change,
                                clean_command, parse_scope)
from api.history_identity import norm_identity

CASES = json.loads((Path(__file__).parent / "fixtures" /
                    "command_replay_real.json").read_text())


@pytest.mark.parametrize("kind", sorted(CASES))
def test_real_transitions_replay_exactly(kind):
    case = CASES[kind]
    before, after = case["before"], case["after"]
    titel, text = apply_change(before["titel"], before["text"],
                               case["change"]["commands"])
    assert norm_identity(after["enbez"], titel, text) == norm_identity(
        after["enbez"], after["titel"], after["text"]), kind


def test_wrong_target_fails_closed():
    case = CASES["replace"]
    with pytest.raises(ReplayUnsupported):
        apply_change("", "(1) Ein ganz anderer Text.",
                     case["change"]["commands"])


def test_unsupported_grammar_raises():
    with pytest.raises(ReplayUnsupported):
        apply_change("", "(1) Text.", [{"raw": "Die Inhaltsübersicht wird "
                                               "wie folgt geändert: a) …"}])


def test_pdf_hyphenation_and_scope():
    assert clean_command("des Luftverkehrs -\ngesetzes") == \
        "des Luftverkehrsgesetzes"
    assert clean_command("Kinder-\nund Jugendhilfe") == \
        "Kinder- und Jugendhilfe"
    scope = parse_scope("§ 7 Absatz 1 Satz 2 Nummer 2 Buchstabe b")
    assert (scope.absatz, scope.satz, scope.narrower) == ("1", "2", True)


def test_repealed_absatz_keeps_gii_placeholder():
    _titel, text = apply_change(
        "T", "(1) Erster Satz.(2) Zweiter Satz.(3) Dritter Satz.",
        [{"raw": "Absatz 2 wird aufgehoben."}])
    assert text == "(1) Erster Satz.(2) (weggefallen)(3) Dritter Satz."
