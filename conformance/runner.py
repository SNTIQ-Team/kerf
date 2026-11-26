"""Python implementation of the shared conformance kinds (see conformance/README.md).

``run_case(case) -> actual`` dispatches on ``case["kind"]``; ``matches(expected,
actual, mode)`` implements the comparison.  Module-specific kinds live in
``courts_kinds.py`` and ``kerf_kinds.py`` (each exports ``KINDS``).
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
CASES = Path(__file__).with_name("cases")
FORMAT = "sntiq-conformance/1"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def read_input(path: str, *, decompress: bool = True) -> bytes:
    data = (ROOT / path).read_bytes()
    return gzip.decompress(data) if decompress and path.endswith(".gz") else data


def load_cases(module: str | None = None) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(CASES.rglob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        case["_path"] = str(path.relative_to(ROOT))
        if case.get("format") != FORMAT:
            raise ValueError(f"{path}: format must be {FORMAT}")
        if module is None or case.get("module") == module:
            cases.append(case)
    return cases


def matches(expected: Any, actual: Any, mode: str = "subset") -> bool:
    if mode == "exact":
        return expected == actual
    if isinstance(expected, dict) and expected and all(k.startswith("$") for k in expected):
        # Operators on lists: {"$contains": [...]} / {"$excludes": [...]} (negative expectations);
        # on strings: {"$contains": "substring"} / {"$excludes": "substring"}.
        if isinstance(actual, str):
            contains, excludes = expected.get("$contains", ""), expected.get("$excludes")
            return isinstance(contains, str) and contains in actual and not (excludes and excludes in actual)
        if not isinstance(actual, list):
            return False
        return all(item in actual for item in expected.get("$contains", [])) and \
            not any(item in actual for item in expected.get("$excludes", []))
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and matches(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(
            matches(e, a) for e, a in zip(expected, actual))
    return expected == actual


def kinds() -> dict[str, Callable[[dict], Any]]:
    registry: dict[str, Callable[[dict], Any]] = {}
    for name in ("courts_kinds", "kerf_kinds"):
        try:
            module = __import__(f"conformance.{name}", fromlist=["KINDS"])
        except ModuleNotFoundError as exc:
            if exc.name != f"conformance.{name}":
                raise
            continue
        registry.update(module.KINDS)
    return registry


def run_case(case: dict[str, Any]) -> Any:
    registry = kinds()
    if case["kind"] not in registry:
        raise KeyError(f"no Python implementation for kind {case['kind']!r}")
    return registry[case["kind"]](case["input"])


def main() -> int:
    failed = 0
    for case in load_cases():
        actual = run_case(case)
        ok = matches(case["expected"], actual, case.get("match", "subset"))
        status = "ok" if ok else ("xfail" if case.get("status") == "open" else "FAIL")
        failed += status == "FAIL"
        print(f"{status:5} {case['id']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
