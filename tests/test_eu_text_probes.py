"""A requested subset cannot replace the EU article reader's full daily snapshot."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
import common
import fetch_eu_texts as eu
from tools.eu_history import load_latest


def test_partial_and_failed_probes_preserve_the_reader_snapshot(tmp_path, monkeypatch):
    snapshots = tmp_path / "snapshots"
    monkeypatch.setattr(common, "SNAPSHOTS", snapshots)
    monkeypatch.setattr(eu, "CACHE", tmp_path / "cache" / "eu_texts")
    monkeypatch.setattr(eu, "CORPUS", [
        {"celex": "32016R0679", "jurabk": "DSGVO"}, {"celex": "32024R1352", "jurabk": "VO (EU) 2024/1352"}])
    monkeypatch.setattr(eu, "extension_decisions", lambda _cellar: ["32025D1460"])
    fail = set()
    caches = {}

    def fetch(cellar, spec, *, lisbon):
        celex = spec["celex"]
        if celex in fail:
            raise RuntimeError("Source unavailable")
        caches.setdefault(celex, []).append(getattr(cellar, "cache", eu.CACHE))
        return {"celex": celex, "editions": [{"articles": [{"enbez": "Art. 1", "text": celex}]}], "warnings": []}

    monkeypatch.setattr(eu, "fetch_act", fetch)
    assert eu.main([]) == 0
    full = next((snapshots / "eu_texts").rglob("acts.jsonl"))
    before = full.read_bytes()
    assert eu.main(["--acts", "32024R1352"]) == 0
    assert full.read_bytes() == before
    selection = next((snapshots / "eu_texts_probes").rglob("acts.jsonl"))
    assert [r["celex"] for r in common.read_jsonl(selection)] == ["32024R1352"]
    assert caches["32024R1352"][0] != caches["32024R1352"][1]
    assert eu.main(["--acts", "32016R0679,UNKNOWN"]) == 1
    fail.add("32016R0679")
    assert eu.main(["--acts", "32016R0679,32024R1352"]) == 1
    assert list((snapshots / "eu_texts_probes").rglob("acts.jsonl")) == [selection]
    assert full.read_bytes() == before
    assert [r["celex"] for r in load_latest(snapshots)] == ["32016R0679", "32024R1352", "32025D1460"]

    def discovery_down(_cellar):
        raise RuntimeError("Extension discovery unavailable")

    fail.clear()
    monkeypatch.setattr(eu, "extension_decisions", discovery_down)
    assert eu.main([]) == 1
    assert full.read_bytes() == before
    assert eu.main(["--acts", "32024R1352"]) == 0
    with pytest.raises(SystemExit):
        eu.main(["--acts", ""])
