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
