"""Legacy BGBl articles are matched to every corpus act they name exactly.

Real excerpts (tests/fixtures/legacy_heading_matches.json): BGBl. I 2011
S. 2258 Art. 3 whose PDF text runs the first, letter-spaced command into the
heading ("… Asylbewerberleistungsgesetzes I n§1A b s a t z …"), and the
collective Art. 19 "Weitere Änderungen zum Jahr 2018" of BGBl. I 2016
S. 3234 (Bundesteilhabegesetz) whose subsections amend other acts.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

import backfill_legacy_bgbl_history as legacy  # noqa: E402
from backfill_bgbl_history import act_aliases  # noqa: E402

CASES = json.loads((Path(__file__).parent / "fixtures" /
                    "legacy_heading_matches.json").read_text())


def _act(slug, jurabk, title):
    act = {"slug": slug, "jurabk": jurabk, "long_title": title}
    act["aliases"] = sorted(act_aliases(act))
    return act


ACTS = [
    _act("asylblg", "AsylbLG", "Asylbewerberleistungsgesetz"),
    _act("bbig_2005", "BBiG 2005", "Berufsbildungsgesetz"),
    _act("betrvg", "BetrVG", "Betriebsverfassungsgesetz"),
    _act("arbgg", "ArbGG", "Arbeitsgerichtsgesetz"),
    _act("gvg", "GVG", "Gerichtsverfassungsgesetz"),
]


def test_run_on_heading_is_cut_and_matched():
    sections = legacy.legacy_article_sections(CASES["runon"]["text"])
    [section] = [s for s in sections if s["article"] == "3"]
    assert section["heading"] == "Änderung des Asylbewerberleistungsgesetzes"
    index = legacy.heading_act_index(ACTS)
    assert [a["slug"] for a in legacy.heading_named_acts(section, index)] == \
        ["asylblg"]


def test_heading_of_another_act_is_not_matched():
    index = legacy.heading_act_index(ACTS)
    other = {"heading": "Änderung des Einführungsgesetzes zum "
                        "Gerichtsverfassungsgesetz"}
    assert legacy.heading_named_acts(other, index) == []


def test_collective_article_is_split_per_amended_act():
    case = CASES["collective"]
    section = {"article": case["article"],
               "heading": "Weitere Änderungen zum Jahr 2018",
               "text": case["text"]}
    index = legacy.heading_act_index(ACTS)
    found = legacy.collective_subsections(section, index)
    slugs = [act["slug"] for act, _pseudo in found]
    assert {"bbig_2005", "betrvg", "arbgg"} <= set(slugs)
    act, pseudo = next(pair for pair in found if pair[0]["slug"] == "arbgg")
    assert pseudo["heading"].startswith("Änderung des ")
    assert pseudo["legacy_collective_subsection"] == "6"
    # only that subsection's commands travel with the pseudo-section
    assert "Betriebsverfassungsgesetz" not in pseudo["text"].split("\n", 2)[2]
