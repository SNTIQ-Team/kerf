from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from common import read_jsonl, write_jsonl  # noqa: E402


@pytest.fixture
def archive():
    assert (ROOT / "pipeline" / "snapshot_storage.py").is_file(), \
        "reversible snapshot storage is not implemented"
    return importlib.import_module("snapshot_storage")


def _file(root: Path, day: str, data: bytes, *, family: str = "dip",
          name: str = "events.jsonl") -> Path:
    path = root / family / day / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _snapshots(tmp_path: Path) -> tuple[Path, list[Path]]:
    root = tmp_path / "snapshots"
    paths = [_file(root, f"2026-01-0{i}",
                   (f'{{"day":{i},"text":"original"}}\n' * 20).encode())
             for i in range(1, 5)]
    return root, paths


def test_roundtrip_preserves_bytes_observation_dates_and_file_metadata(
        tmp_path: Path, archive) -> None:
    root, paths = _snapshots(tmp_path)
    payload = b' {"url":"https://example.test/source","text":"\\u00e4"}\r\n\r\n'
    paths[0].write_bytes(payload)
    paths[0].chmod(0o640)
    os.utime(paths[0], ns=(1700000000123456789, 1700000001987654321))
    before = paths[0].stat()
    result = archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)

    assert result["compacted_files"] == 2
    assert paths[0].is_file()
    assert paths[0].read_bytes() != payload
    assert list(read_jsonl(paths[0])) == [{
        "url": "https://example.test/source", "text": "\u00e4"}]
    manifest = archive.load_manifest(root)
    record = manifest["files"]["dip/2026-01-01/events.jsonl"]
    assert record["source"] == "dip"
    assert record["observed_at"] == "2026-01-01"
    assert record["mtime_ns"] == before.st_mtime_ns
    assert record["atime_ns"] == before.st_atime_ns
    assert archive.verify_archive(root)["verified_files"] == 2

    archive.restore_snapshots(root, min_free_bytes=0)
    assert paths[0].stat().st_atime_ns == before.st_atime_ns
    assert paths[0].read_bytes() == payload
    assert paths[0].stat().st_mtime_ns == before.st_mtime_ns
    assert paths[0].stat().st_mode & 0o777 == 0o640
    assert archive.verify_archive(root)["verified_files"] == 2


def test_duplicate_bytes_share_one_object_and_rerun_is_idempotent(
        tmp_path: Path, archive) -> None:
    root, paths = _snapshots(tmp_path)
    paths[1].write_bytes(paths[0].read_bytes())
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    manifest = archive.load_manifest(root)
    assert len(manifest["files"]) == 2
    assert len(manifest["objects"]) == 1
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    assert result["compacted_files"] == 0
    assert result["new_object_bytes"] == 0
    assert {p: p.read_bytes() for p in before} == before


def test_dry_run_is_read_only_and_keeps_two_newest_per_family(
        tmp_path: Path, archive) -> None:
    root, paths = _snapshots(tmp_path)
    paths += [_file(root, f"2026-02-0{i}", b'{"family":"gii"}\n',
                    family="gii") for i in range(1, 4)]
    summary = _file(root, "2026-01-01", b'{"source":"original"}',
                    name="summary.json")
    loose = _file(root, "cache", b'{"cache":true}\n')
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = archive.compact_snapshots(root, min_free_bytes=0)
    assert result["candidate_files"] == 3
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert not (root / ".compact").exists()
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    for path in [paths[2], paths[3], paths[-2], paths[-1], summary, loose]:
        assert path.read_bytes() == before[path]
    assert result["excluded_files"] >= 2


def test_corrupt_archive_aborts_read_restore_and_recompaction(
        tmp_path: Path, archive) -> None:
    root, paths = _snapshots(tmp_path)
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    manifest = archive.load_manifest(root)
    metadata = next(iter(manifest["objects"].values()))
    obj = root / ".compact" / metadata["path"]
    obj.write_bytes(obj.read_bytes()[:-7] + b"corrupt")
    before = {p: p.read_bytes() for p in paths}
    with pytest.raises(archive.SnapshotArchiveError, match="integrity|hash|gzip"):
        list(read_jsonl(paths[0]))
    with pytest.raises(archive.SnapshotArchiveError):
        archive.restore_snapshots(root, min_free_bytes=0)
    with pytest.raises(archive.SnapshotArchiveError):
        archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    assert {p: p.read_bytes() for p in paths} == before


@pytest.mark.parametrize("kind", ["file", "directory", "store"])
def test_symlinks_cannot_escape_source_or_archive_boundaries(
        tmp_path: Path, archive, kind: str) -> None:
    root, paths = _snapshots(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "events.jsonl"
    target.write_bytes(b'{"outside":true}\n')
    if kind == "file":
        paths[0].unlink()
        paths[0].symlink_to(target)
    elif kind == "directory":
        shutil.rmtree(paths[0].parent)
        paths[0].parent.symlink_to(outside, target_is_directory=True)
    else:
        (root / ".compact").symlink_to(outside, target_is_directory=True)
    with pytest.raises(archive.SnapshotArchiveError, match="symlink|boundary"):
        archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    assert target.read_bytes() == b'{"outside":true}\n'
    assert list(outside.iterdir()) == [target]


def test_budget_and_free_space_fail_before_replacing_sources(
        tmp_path: Path, archive) -> None:
    root, paths = _snapshots(tmp_path)
    before = {p: p.read_bytes() for p in paths}
    with pytest.raises(archive.SnapshotArchiveError, match="budget"):
        archive.compact_snapshots(root, dry_run=False, max_new_bytes=0,
                                  min_free_bytes=0)
    with pytest.raises(archive.SnapshotArchiveError, match="free space"):
        archive.compact_snapshots(root, dry_run=False,
                                  min_free_bytes=10**30)
    assert {p: p.read_bytes() for p in paths} == before


def test_changed_source_aborts_before_first_reference_replacement(
        tmp_path: Path, archive, monkeypatch) -> None:
    root, paths = _snapshots(tmp_path)
    original_write = archive._write_object

    def write_and_change(*args, **kwargs):
        result = original_write(*args, **kwargs)
        paths[0].write_bytes(b'{"changed":true}\n')
        return result

    monkeypatch.setattr(archive, "_write_object", write_and_change)
    with pytest.raises(archive.SnapshotArchiveError, match="changed"):
        archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    assert list(read_jsonl(paths[0])) == [{"changed": True}]
    assert list(read_jsonl(paths[1])) == [{"day": 2, "text": "original"}] * 20
    assert not paths[1].read_bytes().startswith(archive.REFERENCE_MAGIC)


def test_interrupted_replacements_resume_without_losing_originals(
        tmp_path: Path, archive, monkeypatch) -> None:
    root, paths = _snapshots(tmp_path)
    before = {p: p.read_bytes() for p in paths}
    original_replace = archive._replace_reference
    calls = 0

    def interrupted(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption")
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(archive, "_replace_reference", interrupted)
    with pytest.raises(OSError, match="interruption"):
        archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    monkeypatch.setattr(archive, "_replace_reference", original_replace)
    assert archive.verify_archive(root)["verified_files"] == 2
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    archive.restore_snapshots(root, min_free_bytes=0)
    assert {p: p.read_bytes() for p in paths} == before


def test_reference_cannot_redirect_to_a_different_root(
        tmp_path: Path, archive) -> None:
    root, paths = _snapshots(tmp_path)
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    payload = json.loads(paths[0].read_bytes().split(b"\n", 1)[1])
    payload["snapshot_path"] = "../outside/events.jsonl"
    paths[0].write_bytes(archive.REFERENCE_MAGIC + json.dumps(payload).encode())
    with pytest.raises(archive.SnapshotArchiveError, match="path|boundary"):
        list(read_jsonl(paths[0]))
    with pytest.raises(archive.SnapshotArchiveError):
        archive.restore_snapshots(root, min_free_bytes=0)


def test_recent_references_are_materialized_when_newer_snapshots_disappear(
        tmp_path: Path, archive) -> None:
    root, paths = _snapshots(tmp_path)
    before = {p: p.read_bytes() for p in paths}
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    shutil.rmtree(paths[2].parent)
    shutil.rmtree(paths[3].parent)
    result = archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    assert result["restored_recent_files"] == 2
    assert paths[0].read_bytes() == before[paths[0]]
    assert paths[1].read_bytes() == before[paths[1]]


def test_jsonl_writer_atomically_replaces_files_without_mutating_hardlinks(
        tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    target.write_bytes(b'{"original":true}\n')
    sibling = tmp_path / "original.jsonl"
    os.link(target, sibling)
    write_jsonl(target, [{"replacement": True}])
    assert sibling.read_bytes() == b'{"original":true}\n'
    assert list(read_jsonl(target)) == [{"replacement": True}]


@pytest.mark.parametrize("module,function", [
    ("official_states", "_read_jsonl_strict"),
    ("verified_reconstruction", "_read_jsonl"),
])
def test_strict_history_readers_accept_archived_originals(
        tmp_path: Path, archive, module: str, function: str) -> None:
    root, paths = _snapshots(tmp_path)
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    sys.path.insert(0, str(ROOT / "tools"))
    reader = getattr(importlib.import_module(module), function)
    assert reader(paths[0]) == [{"day": 1, "text": "original"}] * 20


def test_pruning_retains_compact_references_and_manifest_integrity(
        tmp_path: Path, archive) -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    from official_states import archive_gii_states
    from prune_gii_snapshots import prune_gii_snapshots

    root = tmp_path / "snapshots"
    snapshots = []
    for day in ("2026-01-01", "2026-01-02", "2026-01-03"):
        act = {"slug": "demo", "jurabk": "Demo", "norm_count": 1,
               "builddate": "20260101000000", "doknr": "A"}
        norm = {"slug": "demo", "jurabk": "Demo", "enbez": "1",
                "text": "original", "doknr": "N"}
        a = _file(root, day, (json.dumps(act) + "\n").encode(),
                  family="gii", name="acts.jsonl")
        _file(root, day, (json.dumps(norm) + "\n").encode(),
              family="gii", name="norms.jsonl")
        snapshots.append(a.parent)
    store = tmp_path / "federal_states"
    archive_gii_states(snapshots, store)
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    archive_gii_states(snapshots, store)
    prune_gii_snapshots(root / "gii", store)
    assert archive.verify_archive(root)["verified_files"] == 2
    assert (snapshots[0] / "acts.jsonl").is_file()
    archive.restore_snapshots(root, min_free_bytes=0)
    prune_gii_snapshots(root / "gii", store)
    assert archive.verify_archive(root)["verified_files"] == 2
    assert (snapshots[0] / "acts.jsonl").is_file()


def test_cli_defaults_to_dry_run_and_respects_shared_refresh_lock(
        tmp_path: Path, archive) -> None:
    import fcntl

    root, paths = _snapshots(tmp_path)
    script = ROOT / "tools" / "compact_snapshots.py"
    assert script.is_file(), "compaction CLI is not implemented"
    lock = tmp_path / "refresh.lock"
    command = [sys.executable, str(script), "compact", "--snapshots-root", str(root),
               "--lock-file", str(lock), "--min-free-mib", "0"]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["dry_run"] is True
    assert not (root / ".compact").exists()
    with lock.open("wb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        result = subprocess.run(command + ["--apply"], capture_output=True, text=True)
        assert result.returncode == 1
        assert "refresh lock" in result.stderr
        assert not (root / ".compact").exists()
    result = subprocess.run(command + ["--apply"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["compacted_files"] == 2
    assert paths[-1].read_bytes().startswith(b'{"day":4')


def test_missing_reference_can_be_restored_from_manifest(
        tmp_path: Path, archive) -> None:
    root, paths = _snapshots(tmp_path)
    original = paths[0].read_bytes()
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    paths[0].unlink()
    archive.restore_snapshots(root, min_free_bytes=0)
    assert paths[0].read_bytes() == original


def test_restore_refuses_to_overwrite_changed_original_before_any_write(
        tmp_path: Path, archive) -> None:
    root, paths = _snapshots(tmp_path)
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    first_reference = paths[0].read_bytes()
    paths[1].write_bytes(b'{"newer_observation":true}\n')
    with pytest.raises(archive.SnapshotArchiveError, match="changed"):
        archive.restore_snapshots(root, min_free_bytes=0)
    assert paths[0].read_bytes() == first_reference
    assert paths[1].read_bytes() == b'{"newer_observation":true}\n'


def test_cli_can_reuse_inherited_refresh_lock_and_restore_is_explicit(
        tmp_path: Path, archive) -> None:
    import fcntl

    root, paths = _snapshots(tmp_path)
    original = paths[0].read_bytes()
    lock = tmp_path / "refresh.lock"
    command = [sys.executable, str(ROOT / "tools" / "compact_snapshots.py"),
               "--snapshots-root", str(root), "--lock-file", str(lock),
               "--min-free-mib", "0"]
    with lock.open("wb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        result = subprocess.run(command + ["compact", "--apply", "--lock-fd",
                                str(stream.fileno())], pass_fds=(stream.fileno(),),
                                capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    reference = paths[0].read_bytes()
    result = subprocess.run(command + ["restore"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["files_to_restore"] == 2
    assert paths[0].read_bytes() == reference
    result = subprocess.run(command + ["restore", "--apply"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert paths[0].read_bytes() == original


def test_empty_symlinked_object_directory_is_rejected_even_on_dry_run(
        tmp_path: Path, archive) -> None:
    root, _ = _snapshots(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".compact").mkdir()
    (root / ".compact" / "objects").symlink_to(outside, target_is_directory=True)
    with pytest.raises(archive.SnapshotArchiveError, match="symlink|boundary"):
        archive.compact_snapshots(root, min_free_bytes=0)
    assert not list(outside.iterdir())


def test_original_checksum_is_verified_even_if_gzip_metadata_matches(
        tmp_path: Path, archive) -> None:
    import gzip
    import hashlib

    root, paths = _snapshots(tmp_path)
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    manifest = archive.load_manifest(root)
    digest = manifest["files"]["dip/2026-01-01/events.jsonl"]["sha256"]
    metadata = manifest["objects"][digest]
    corrupt = gzip.compress(b"X" * metadata["raw_bytes"], mtime=0)
    (root / ".compact" / metadata["path"]).write_bytes(corrupt)
    metadata["gzip_bytes"] = len(corrupt)
    metadata["gzip_sha256"] = hashlib.sha256(corrupt).hexdigest()
    (root / ".compact" / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(archive.SnapshotArchiveError, match="uncompressed object hash"):
        archive.verify_archive(root)


def test_common_writer_failure_keeps_complete_previous_file(tmp_path: Path) -> None:
    target = tmp_path / "rows.jsonl"
    target.write_bytes(b'{"previous":true}\n')

    def partial_rows():
        yield {"first": True}
        raise RuntimeError("source stopped")

    with pytest.raises(RuntimeError, match="source stopped"):
        write_jsonl(target, partial_rows())
    assert target.read_bytes() == b'{"previous":true}\n'
    assert list(tmp_path.iterdir()) == [target]


def test_bavarian_forward_history_is_identical_after_compaction(
        tmp_path: Path, archive, monkeypatch) -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    import build_web_data

    root = tmp_path / "snapshots"
    for day, text in (("2026-01-01", "old exact text"),
                      ("2026-01-02", "old exact text"),
                      ("2026-01-03", "new exact text")):
        row = {"jurabk": "Demo", "enbez": "Art. 1", "text": text}
        _file(root, day, (json.dumps(row) + "\n").encode(),
              family="bayern_recht", name="norms.jsonl")
    ledger = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(build_web_data, "SNAPSHOTS", root)
    monkeypatch.setattr(build_web_data, "BY_DIFF_LEDGER", ledger)
    build_web_data._update_by_diff_ledger()
    original = ledger.read_bytes()
    assert list(read_jsonl(ledger))[0]["old"] == "old exact text"
    ledger.unlink()
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    build_web_data._update_by_diff_ledger()
    assert ledger.read_bytes() == original


def test_federal_observation_pairs_keep_archived_states_and_provenance(
        tmp_path: Path, archive) -> None:
    sys.path.insert(0, str(ROOT / "tools"))
    from federal_history import exact_gii_state_events

    root = tmp_path / "snapshots"
    snapshots = []
    for number in (1, 2, 3):
        day = f"2026-01-0{number}"
        act = {"slug": "demo", "jurabk": "Demo", "norm_count": 1,
               "builddate": f"2026010{number}000000", "doknr": "A"}
        norm = {"slug": "demo", "jurabk": "Demo", "enbez": "1",
                "text": f"original body {number}", "doknr": "N"}
        path = _file(root, day, (json.dumps(act) + "\n").encode(),
                     family="gii", name="acts.jsonl")
        _file(root, day, (json.dumps(norm) + "\n").encode(),
              family="gii", name="norms.jsonl")
        snapshots.append(path.parent)
    before = exact_gii_state_events(snapshots)
    assert len(before) == 2
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    assert exact_gii_state_events(snapshots) == before


@pytest.mark.parametrize("missing", ["manifest", "entry"])
def test_verify_rejects_references_omitted_from_manifest(
        tmp_path: Path, archive, missing: str) -> None:
    root, _ = _snapshots(tmp_path)
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    path = root / ".compact" / "manifest.json"
    if missing == "manifest":
        path.unlink()
    else:
        manifest = json.loads(path.read_bytes())
        manifest["files"].pop("dip/2026-01-01/events.jsonl")
        path.write_text(json.dumps(manifest))
    with pytest.raises(archive.SnapshotArchiveError, match="untracked|manifest"):
        archive.verify_archive(root)
    with pytest.raises(archive.SnapshotArchiveError, match="untracked|manifest"):
        archive.restore_snapshots(root, min_free_bytes=0)


def test_restored_source_changed_after_initial_verification_is_not_replaced(
        tmp_path: Path, archive, monkeypatch) -> None:
    root, paths = _snapshots(tmp_path)
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    archive.restore_snapshots(root, min_free_bytes=0)
    verify = archive.verify_archive
    changed = b'{"concurrent_observation":true}\n'

    def verify_then_change(*args, **kwargs):
        result = verify(*args, **kwargs)
        paths[0].write_bytes(changed)
        return result

    monkeypatch.setattr(archive, "verify_archive", verify_then_change)
    with pytest.raises(archive.SnapshotArchiveError, match="changed"):
        archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    assert paths[0].read_bytes() == changed


def test_free_space_guard_includes_all_reference_files_for_tiny_snapshots(
        tmp_path: Path, archive, monkeypatch) -> None:
    from datetime import date, timedelta
    from types import SimpleNamespace

    root = tmp_path / "snapshots"
    paths = [_file(root, (date(2026, 1, 1) + timedelta(days=i)).isoformat(), b"{}\n")
             for i in range(50)]
    monkeypatch.setattr(archive.shutil, "disk_usage", lambda _: SimpleNamespace(free=20000))
    with pytest.raises(archive.SnapshotArchiveError, match="free space"):
        archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    assert all(path.read_bytes() == b"{}\n" for path in paths)


@pytest.mark.parametrize("add_new_snapshot", [False, True])
def test_configurable_cumulative_cap_counts_existing_and_new_objects(
        tmp_path: Path, archive, add_new_snapshot: bool) -> None:
    root, paths = _snapshots(tmp_path)
    archive.compact_snapshots(root, dry_run=False, min_free_bytes=0)
    existing_bytes = sum(path.stat().st_size for path in
                         (root / ".compact" / "objects").rglob("*.gz"))
    cap = existing_bytes - 1
    if add_new_snapshot:
        paths.append(_file(root, "2026-01-05", b'{"day":5}\n'))
        cap = existing_bytes + 1
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    with pytest.raises(archive.SnapshotArchiveError, match="archive budget"):
        archive.compact_snapshots(root, dry_run=False,
                                  max_new_bytes=500 * 1024 * 1024,
                                  max_archive_bytes=cap, min_free_bytes=0)
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("cap", [-1, 500 * 1024 * 1024 + 1, True])
def test_cumulative_cap_rejects_invalid_or_above_hard_limit(
        tmp_path: Path, archive, cap) -> None:
    root, _ = _snapshots(tmp_path)
    with pytest.raises(ValueError, match="max_archive_bytes"):
        archive.compact_snapshots(root, max_archive_bytes=cap, min_free_bytes=0)
    assert not (root / ".compact").exists()


def test_cli_exposes_configurable_cumulative_archive_cap(
        tmp_path: Path, archive) -> None:
    root, _ = _snapshots(tmp_path)
    command = [sys.executable, str(ROOT / "tools" / "compact_snapshots.py"),
               "compact", "--snapshots-root", str(root), "--max-new-mib", "500",
               "--min-free-mib", "0", "--max-archive-mib"]
    result = subprocess.run(command + ["0"], capture_output=True, text=True)
    assert result.returncode == 1
    assert "archive budget" in result.stderr
    result = subprocess.run(command + ["300"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["candidate_files"] == 2
    assert not (root / ".compact").exists()
