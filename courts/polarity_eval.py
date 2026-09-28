"""Score the polarity rules against a gold file (conformance/gold/polarity-gold-*.json).

For each gold item the current rules are re-run on the stored paragraph text and citation span. Per assessment
type:
  proposed   the item still receives that assessment (a demoted rule yields mentions_with_cue instead,
             which the corpus does not store on schema v3: the rule is disabled there)
  precision  correct / proposed
  kept       correct items still proposed / correct items in the sample (the cost of the guards)
Rules whose type stays below MIN_PRECISION must be demoted (``Rule.demoted``); the test suite enforces it.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

from .citations import procedural_basis
from .polarity import ASSESSMENTS, assess

MIN_PRECISION = 0.9
GOLD_DIR = Path(__file__).resolve().parents[1] / "conformance" / "gold"


def rerun(item: dict) -> set[str]:
    target = item["target_id"]
    detail = {}
    if item["target_kind"] == "decision" and target and target.startswith("court:"):
        detail["court_id"] = target.split("/", 1)[0].removeprefix("court:")
    if item["target_kind"] == "norm" and procedural_basis(target):
        detail["procedural_basis"] = procedural_basis(target)
    return {p.assessment for p in assess(item["paragraph_text"], tuple(item["span"]), target_kind=item["target_kind"],
                                         target_id=target, own_court_id=item["own_court_id"],
                                         jurisdiction=item["jurisdiction"], detail=detail)}


def score(gold: dict) -> dict:
    proposed, correct, total_correct, demoted = Counter(), Counter(), Counter(), Counter()
    for item in gold["items"]:
        kind = item["assessment"]
        total_correct[kind] += item["correct"]
        result = rerun(item)
        if kind in result:
            proposed[kind] += 1
            correct[kind] += item["correct"]
        elif "mentions_with_cue" in result:
            demoted[kind] += 1
    out = {}
    for kind in sorted({i["assessment"] for i in gold["items"]}):
        n = sum(1 for i in gold["items"] if i["assessment"] == kind)
        out[kind] = {"sample": n, "correct_in_sample": total_correct[kind], "proposed": proposed[kind],
                     "correct_proposed": correct[kind],
                     "precision": round(correct[kind] / proposed[kind], 3) if proposed[kind] else None,
                     "kept": round(correct[kind] / total_correct[kind], 3) if total_correct[kind] else None,
                     "demoted_or_disabled": demoted[kind]}
    return out


def load(path: Path | None = None) -> list[dict]:
    paths = [path] if path else sorted(GOLD_DIR.glob("polarity-gold-*.json"))
    return [json.loads(p.read_text(encoding="utf-8")) for p in paths]


if __name__ == "__main__":
    for gold in load(Path(sys.argv[1]) if len(sys.argv) > 1 else None):
        for kind, row in score(gold).items():
            print(f"{kind:24} {row}")
    assert set(ASSESSMENTS)
