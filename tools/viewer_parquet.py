"""Use one Parquet builder across Kerf Hub configurations, preserving the JSON reader's types."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def convert(source: Path, target: Path, cache: Path, previous_features: list | None) -> list:
    import pyarrow as pa
    import pyarrow.parquet as pq
    from datasets import Features, load_dataset
    from datasets.exceptions import DatasetGenerationError
    from datasets.table import CastError, table_cast

    if source.stat().st_size:
        kwargs = {"data_files": str(source.resolve()), "split": "train", "cache_dir": str(cache)}
        try:
            ds = load_dataset("json", **kwargs)
        except DatasetGenerationError as exc:
            if not isinstance(exc.__cause__, (CastError, TypeError)):
                raise
            # A late top-level column raises CastError. A late nested field
            # raises TypeError when Arrow casts its struct to the first chunk's
            # schema. Infer both from the complete release table.
            ds = load_dataset("json", chunksize=source.stat().st_size + 1, **kwargs)
        table, features = ds.data.table, ds.features
    else:
        features = Features._from_yaml_list(previous_features or [])
        table = pa.Table.from_batches([], schema=features.arrow_schema)
    if table.num_rows:
        pq.write_table(table, target, compression="zstd")
    else:
        # An empty row group makes the HF streaming reader choose a zero
        # batch size. A valid Parquet footer with no row groups is iterable.
        with pq.ParquetWriter(target, table.schema, compression="zstd"):
            pass
    # Parquet stores second-resolution timestamps as milliseconds. The Hub's
    # per-config feature declaration restores the JSON builder's original type.
    restored = table_cast(pq.read_table(target), features.arrow_schema)
    if not table.equals(restored):
        raise ValueError(f"Parquet round-trip changed rows from {source.name}")
    return features._to_yaml_list()


def prepare_viewer(card: str, out: Path, names: list[str]) -> str:
    head, sep, body = card.partition("\n---\n")
    if not sep or "\nconfigs:\n" not in head or "\ndataset_info:" in head:
        raise ValueError("Expected a generated Kerf card with one configs section")
    directory = out / "viewer"
    directory.mkdir(exist_ok=True)
    inventory = directory / "manifest.json"
    previous = {}
    if inventory.exists():
        saved = json.loads(inventory.read_bytes())
        if saved["format"] != "kerf-viewer-parquet/1":
            raise ValueError("Unknown Kerf viewer manifest")
        previous = saved["configs"]
    entries = {}
    schema = []
    with TemporaryDirectory(prefix=".viewer-", dir=out) as temp:
        stage = Path(temp)
        for name in names:
            if not re.fullmatch(r"[a-z][a-z0-9_]*\.jsonl", name):
                raise ValueError(f"Invalid viewer source name {name!r}")
            key = Path(name).stem
            declaration = f"    data_files: {name}\n"
            if (head+"\n").count(declaration) != 1:
                raise ValueError(f"Missing JSON source in dataset card: {name}")
            head = (head+"\n").replace(declaration, f"    data_files: viewer/{key}.parquet\n").removesuffix("\n")
            raw, asset = out / name, directory / f"{key}.parquet"
            sha = digest(raw)
            entry = previous.get(key)
            if not (entry and entry["source_sha256"] == sha and asset.is_file() and digest(asset) == entry["sha256"]):
                staged = stage / asset.name
                features = convert(raw, staged, stage / "cache", entry["features"] if entry else None)
                if digest(raw) != sha:
                    raise ValueError(f"Viewer source changed during export: {name}")
                entry = {"source_sha256": sha, "sha256": digest(staged), "features": features}
            entries[key] = entry
            schema.append(f"  - config_name: {key}\n    features: " + json.dumps(entry["features"], ensure_ascii=False))
        for key in previous.keys() - entries.keys():
            if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
                raise ValueError("Invalid obsolete viewer filename")
            stale = directory / f"{key}.parquet"
            if stale.exists() and digest(stale) != previous[key]["sha256"]:
                raise ValueError(f"Obsolete viewer asset has unrecorded changes: {stale.name}")
        contents = {"format": "kerf-viewer-parquet/1", "configs": entries}
        staged_inventory = stage / "manifest.json"
        staged_inventory.write_text(json.dumps(contents, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        for asset in stage.glob("*.parquet"):
            asset.replace(directory / asset.name)
        for key in previous.keys() - entries.keys():
            (directory / f"{key}.parquet").unlink(missing_ok=True)
        staged_inventory.replace(inventory)
    head = head.replace("\nconfigs:\n", "\ndataset_info:\n" + "\n".join(schema) + "\nconfigs:\n", 1)
    return head + sep + body
