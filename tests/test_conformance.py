"""Shared conformance suite (conformance/README.md): every case, courts and Lexgraph."""
import pytest

from conformance.runner import load_cases, matches, run_case

CASES = load_cases()


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_conformance_case(case):
    if case.get("status") == "open":
        pytest.xfail(f"open correction: {case.get('description')}")
    actual = run_case(case)
    assert matches(case["expected"], actual, case.get("match", "subset")), (case["id"], actual)


def test_federal_alias_targets_exist_in_the_gii_catalogue():
    import json
    from pathlib import Path
    from api.norm_identity import FEDERAL_ALIASES
    catalog = {a["abbrev"] for a in json.loads((Path(__file__).parents[1] / "conformance" / "fixtures" /
                                                "lexgraph_data" / "gii_catalog.json").read_text())["acts"]}
    assert {k: v for k, v in FEDERAL_ALIASES.items() if v not in catalog} == {}
