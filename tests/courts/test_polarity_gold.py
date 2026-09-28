"""Polarity precision against the hand-labelled gold files (conformance/gold): every assessment type the
corpus stores must reach 0.9 precision; a rule below it is demoted (mentions_with_cue, not stored)."""
import pytest

from courts.polarity_eval import MIN_PRECISION, load, score

GOLD = load()


@pytest.mark.parametrize("gold", GOLD, ids=[g.get("created", str(i)) + ":" + g.get("sample", "")[:30]
                                            for i, g in enumerate(GOLD)])
def test_stored_assessment_types_reach_min_precision(gold):
    for kind, row in score(gold).items():
        if row["proposed"]:
            assert row["precision"] >= MIN_PRECISION, (kind, row)


def test_gold_items_are_self_contained():
    for gold in GOLD:
        for item in gold["items"]:
            assert item["paragraph_text"][item["span"][0]:item["span"][1]], item["sample_id"]
            assert isinstance(item["correct"], bool)
