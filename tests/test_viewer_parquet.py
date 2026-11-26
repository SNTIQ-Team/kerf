import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import viewer_parquet as viewer


CARD = "---\nlicense: other\nconfigs:\n  - config_name: patches\n    data_files: patches.jsonl\n  - config_name: court\n    data_files: court/*.parquet\n---\nDataset.\n"


def test_viewer_recovers_card_without_reloading_heavy_dependencies(tmp_path, monkeypatch):
    raw = tmp_path / "patches.jsonl"
    raw.write_text('{"count":7}\n')
    calls = []
    def convert(source, target, cache, old):
        calls.append(old)
        target.write_bytes(b"parquet " + source.read_bytes())
        return [{"name": "count", "dtype": "int64"}]
    monkeypatch.setattr(viewer, "convert", convert)
    first = viewer.prepare_viewer(CARD, tmp_path, [raw.name])
    assert "data_files: viewer/patches.parquet" in first
    assert "data_files: court/*.parquet" in first and first.count("dataset_info:") == 1
    assert first.index("dataset_info:") < first.index("configs:")
    assert viewer.prepare_viewer(CARD, tmp_path, [raw.name]) == first
    assert calls == [None] and raw.read_bytes() == b'{"count":7}\n'
    file = tmp_path / "viewer/patches.parquet"
    file.write_bytes(b"broken")
    viewer.prepare_viewer(CARD, tmp_path, [raw.name])
    assert len(calls) == 2 and file.read_bytes().startswith(b"parquet ")
    raw.write_text('{"count":8}\n')
    viewer.prepare_viewer(CARD, tmp_path, [raw.name])
    receipt = json.loads((file.parent / "manifest.json").read_bytes())["configs"]["patches"]
    assert len(calls) == 3
    assert receipt["source_sha256"] == hashlib.sha256(raw.read_bytes()).hexdigest()
    assert receipt["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()


def test_native_json_types_survive_parquet_and_empty_release(tmp_path):
    datasets = pytest.importorskip("datasets")
    pytest.importorskip("pyarrow.parquet")
    raw = tmp_path / "patches.jsonl"
    raw.write_text('{"day":"2020-01-02","items":[{"n":2}]}\n{"day":"2021-04-12","items":[]}\n')
    card = CARD.replace("  - config_name: court\n    data_files: court/*.parquet\n", "")
    legacy = datasets.load_dataset("json", data_files=str(raw), split="train", cache_dir=str(tmp_path/"cache"))
    (tmp_path/"README.md").write_text(viewer.prepare_viewer(card, tmp_path, [raw.name]))
    restored = datasets.load_dataset(str(tmp_path), "patches", split="train", streaming=True)
    assert restored.features == legacy.features
    assert list(restored) == list(legacy)
    raw.write_bytes(b"")
    (tmp_path/"README.md").write_text(viewer.prepare_viewer(card, tmp_path, [raw.name]))
    empty = datasets.load_dataset(str(tmp_path), "patches", split="train", streaming=True)
    assert list(empty) == [] and empty.features == legacy.features


def test_late_columns_use_the_whole_release_schema(tmp_path, monkeypatch):
    datasets = pytest.importorskip("datasets")
    pq = pytest.importorskip("pyarrow.parquet")
    original = datasets.load_dataset
    chunks = []
    def small_first_chunk(*args, **kwargs):
        chunks.append(kwargs.setdefault("chunksize", 30))
        return original(*args, **kwargs)
    monkeypatch.setattr(datasets, "load_dataset", small_first_chunk)
    raw = tmp_path / "records.jsonl"
    raw.write_text('{"id":1}\n' * 20 + '{"id":3,"late":"verified"}\n')
    dst = tmp_path / "records.parquet"
    features = viewer.convert(raw, dst, tmp_path/"cache", None)
    assert len(chunks) == 2 and chunks[1] > raw.stat().st_size
    assert [field["name"] for field in features] == ["id", "late"]
    assert pq.read_table(dst).to_pylist()[-1] == {"id": 3, "late": "verified"}
