"""Reversible byte archives for inactive, dated JSONL snapshots.

Historical filenames remain regular reference files. The cumulative manifest
pins every original byte stream and its observation path; gzip objects are
verified before references are published and before readers see any rows.
"""
from __future__ import annotations

import copy
import errno
import fcntl
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import uuid
from contextlib import contextmanager, nullcontext
from datetime import date
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

REFERENCE_MAGIC = b"KERF-SNAPSHOT-REF/1\n"
# Original archive references can only be verified against their retained bytes.
_ARCHIVE_HEADERS = (REFERENCE_MAGIC, b"LEXGRAPH-SNAPSHOT-REF/1\n")
_HEADER_LIMIT = max(map(len, _ARCHIVE_HEADERS))
STORE_NAME = ".compact"
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
DEFAULT_MIN_FREE_BYTES = 128 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_MISSING = object()


class SnapshotArchiveError(ValueError):
    """The archive or a source failed a boundary or integrity check."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=True,
                       separators=(",", ":"), allow_nan=False) + "\n").encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


@contextmanager
def _directory_fd(path: Path, *, create: bool = False):
    """Anchor every component with O_NOFOLLOW, including ancestor directories."""
    absolute = _absolute(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(part, mode=0o755, dir_fd=fd)
                    os.fsync(fd)
                except FileExistsError:
                    pass
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY |
                                os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise SnapshotArchiveError(
                        f"symlink or non-directory boundary: {absolute}") from exc
                raise
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _identity(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid)


def _regular_stat(path: Path) -> os.stat_result:
    with _directory_fd(path.parent) as fd:
        info = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise SnapshotArchiveError(f"symlink or non-file boundary: {path}")
    return info


def _read_regular(path: Path) -> tuple[bytes, os.stat_result]:
    with _directory_fd(path.parent) as parent:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            try:
                fd = os.open(path.name, flags | os.O_NOATIME, dir_fd=parent)
            except PermissionError:
                fd = os.open(path.name, flags, dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SnapshotArchiveError(f"symlink boundary: {path}") from exc
            raise
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise SnapshotArchiveError(f"non-file boundary: {path}")
            data = stream.read()
            after = os.fstat(stream.fileno())
            current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if _identity(before) != _identity(after) or \
                    _identity(before) != _identity(current):
                raise SnapshotArchiveError(f"source changed while reading: {path}")
    return data, before


def _atomic_write(path: Path, payload: bytes, *, expected=None,
                  metadata: dict | None = None, create_parent: bool = False):
    with _directory_fd(path.parent, create=create_parent) as parent:
        temporary = f".{path.name}.{uuid.uuid4().hex}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                if metadata:
                    current = os.fstat(stream.fileno())
                    if (current.st_uid, current.st_gid) != \
                            (metadata["uid"], metadata["gid"]):
                        os.fchown(stream.fileno(), metadata["uid"], metadata["gid"])
                    os.fchmod(stream.fileno(), metadata["mode"])
                    os.utime(stream.fileno(), ns=(metadata["atime_ns"],
                                                 metadata["mtime_ns"]))
                else:
                    os.fchmod(stream.fileno(), 0o644)
                os.fsync(stream.fileno())
            try:
                current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                current = None
            if current and not stat.S_ISREG(current.st_mode):
                raise SnapshotArchiveError(f"symlink or non-file boundary: {path}")
            if expected is _MISSING and current is not None or \
                    expected is not None and expected is not _MISSING and \
                    (current is None or _identity(current) != _identity(expected)):
                raise SnapshotArchiveError(f"source changed before replacement: {path}")
            os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass


@contextmanager
def _archive_lock(root: Path):
    with _directory_fd(root) as parent:
        try:
            fd = os.open(".compact.lock", os.O_RDWR | os.O_CREAT |
                         os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
        except OSError as exc:
            raise SnapshotArchiveError(f"archive lock boundary: {exc}") from exc
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise SnapshotArchiveError("archive lock is not a regular file")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SnapshotArchiveError("another snapshot archive writer is active") from exc
            yield
        finally:
            os.close(fd)


def _snapshot_path(value: Any) -> PurePosixPath:
    if not isinstance(value, str):
        raise SnapshotArchiveError("snapshot path must be a relative string")
    path = PurePosixPath(value)
    if path.is_absolute() or len(path.parts) < 3 or \
            any(part in (".", "..") or part.startswith(".") for part in path.parts) or \
            path.as_posix() != value or path.suffix != ".jsonl":
        raise SnapshotArchiveError(f"invalid snapshot path boundary: {value!r}")
    try:
        if date.fromisoformat(path.parts[1]).isoformat() != path.parts[1]:
            raise ValueError
    except ValueError as exc:
        raise SnapshotArchiveError(f"invalid snapshot observation date: {value}") from exc
    return path


def _object_path(digest: str) -> str:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise SnapshotArchiveError("invalid object SHA-256")
    return f"objects/sha256/{digest[:2]}/{digest}.gz"


def _empty_manifest() -> dict:
    return {
        "schema_version": 1, "kind": "kerf-snapshot-byte-archive",
        "date_basis": "snapshot_observation_not_legal_effective_date",
        "compression": "gzip-mtime-0-empty-filename",
        "objects": {}, "files": {},
    }


def load_manifest(root: Path) -> dict:
    path = Path(root) / STORE_NAME / "manifest.json"
    try:
        raw, _ = _read_regular(path)
    except FileNotFoundError:
        return _empty_manifest()
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotArchiveError(f"invalid archive manifest: {path}") from exc
    template = _empty_manifest()
    if isinstance(value, dict) and value.get("kind") == "lexgraph-snapshot-byte-archive":
        value["kind"] = template["kind"]
    if not isinstance(value, dict) or any(value.get(k) != v for k, v in
            template.items() if k not in ("objects", "files")) or \
            not isinstance(value.get("objects"), dict) or \
            not isinstance(value.get("files"), dict):
        raise SnapshotArchiveError("unsupported archive manifest schema")
    for digest, obj in value["objects"].items():
        if not isinstance(obj, dict) or obj.get("path") != _object_path(digest):
            raise SnapshotArchiveError("archive object path does not match digest")
        if not isinstance(obj.get("gzip_sha256"), str) or \
                not _DIGEST.fullmatch(obj["gzip_sha256"]):
            raise SnapshotArchiveError("invalid compressed object hash")
        for field in ("raw_bytes", "gzip_bytes"):
            if type(obj.get(field)) is not int or obj[field] < 0:
                raise SnapshotArchiveError(f"invalid object {field}")
        offloaded = obj.get("offloaded")
        if offloaded is not None and (
                not isinstance(offloaded, dict)
                or not isinstance(offloaded.get("receipt"), str)
                or not isinstance(offloaded.get("at"), str)):
            raise SnapshotArchiveError("invalid offload record")
    for relative, record in value["files"].items():
        parsed = _snapshot_path(relative)
        if not isinstance(record, dict) or record.get("sha256") not in value["objects"] or \
                record.get("source") != parsed.parts[0] or \
                record.get("observed_at") != parsed.parts[1]:
            raise SnapshotArchiveError("invalid snapshot provenance or object reference")
        for field in ("mode", "uid", "gid", "mtime_ns", "atime_ns", "ctime_ns"):
            if type(record.get(field)) is not int or \
                    (field in ("mode", "uid", "gid") and record[field] < 0):
                raise SnapshotArchiveError(f"invalid original file metadata: {field}")
        if record["mode"] > 0o7777:
            raise SnapshotArchiveError("invalid original file mode")
    return value


@lru_cache(maxsize=4)
def _cached_manifest(root: str, identity: tuple[int, ...]) -> dict:
    return load_manifest(Path(root))


def _reference(relative: str, record: dict, obj: dict) -> dict:
    return {"snapshot_path": relative, "sha256": record["sha256"],
            "raw_bytes": obj["raw_bytes"], "gzip_bytes": obj["gzip_bytes"],
            "gzip_sha256": obj["gzip_sha256"]}


def _parse_reference(raw: bytes) -> dict:
    if len(raw) > 4096 or not raw.startswith(_ARCHIVE_HEADERS):
        raise SnapshotArchiveError("invalid snapshot reference")
    try:
        value = json.loads(raw.split(b"\n", 1)[1])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotArchiveError("invalid snapshot reference JSON") from exc
    if not isinstance(value, dict):
        raise SnapshotArchiveError("invalid snapshot reference shape")
    _snapshot_path(value.get("snapshot_path"))
    return value


def is_snapshot_reference(path: Path) -> bool:
    path = Path(path)
    with _directory_fd(path.parent) as parent:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        try:
            try:
                fd = os.open(path.name, flags | os.O_NOATIME, dir_fd=parent)
            except PermissionError:
                fd = os.open(path.name, flags, dir_fd=parent)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SnapshotArchiveError(f"symlink boundary: {path}") from exc
            raise
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise SnapshotArchiveError(f"non-file boundary: {path}")
            return stream.readline(_HEADER_LIMIT) in _ARCHIVE_HEADERS


def _object_present(root: Path, obj: dict) -> bool:
    try:
        _regular_stat(root / STORE_NAME / obj["path"])
    except FileNotFoundError:
        return False
    return True


def _is_offloaded(root: Path, obj: dict) -> bool:
    """Bytes live only in the workstation archive (a receipt proved it)."""
    return bool(obj.get("offloaded")) and not _object_present(root, obj)


def _verified_object(root: Path, digest: str, obj: dict) -> bytes:
    if _is_offloaded(root, obj):
        raise SnapshotArchiveError(
            f"archived bytes {digest} were offloaded to the workstation "
            f"archive (receipt {obj['offloaded']['receipt']}); restore the "
            "object from ~/.local/share/sntiq-legal/kerf-archive to read it")
    compressed, _ = _read_regular(root / STORE_NAME / obj["path"])
    if len(compressed) != obj["gzip_bytes"] or _sha(compressed) != obj["gzip_sha256"]:
        raise SnapshotArchiveError(f"compressed object integrity mismatch: {digest}")
    if len(compressed) < 18 or compressed[:4] != b"\x1f\x8b\x08\x00" or \
            compressed[4:8] != b"\x00\x00\x00\x00":
        raise SnapshotArchiveError(f"non-deterministic gzip header: {digest}")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(compressed), mode="rb") as stream:
            raw = stream.read(obj["raw_bytes"] + 1)
            if len(raw) != obj["raw_bytes"] or stream.read(1):
                raise SnapshotArchiveError(f"object length integrity mismatch: {digest}")
    except (OSError, EOFError) as exc:
        raise SnapshotArchiveError(f"invalid gzip object: {digest}") from exc
    if _sha(raw) != digest:
        raise SnapshotArchiveError(f"uncompressed object hash mismatch: {digest}")
    return raw


@contextmanager
def open_snapshot_text(path: Path):
    """Open ordinary JSONL or an integrity-checked original archived stream."""
    path = Path(path)
    with path.open("rb") as stream:
        prefix = stream.readline(_HEADER_LIMIT)
        if prefix not in _ARCHIVE_HEADERS:
            stream.seek(0)
            with io.TextIOWrapper(stream, encoding="utf-8") as text:
                yield text
            return
    raw, _ = _read_regular(path)
    reference = _parse_reference(raw)
    relative = _snapshot_path(reference["snapshot_path"])
    absolute = _absolute(path)
    root = absolute.parents[len(relative.parts) - 1]
    if root.joinpath(*relative.parts) != absolute:
        raise SnapshotArchiveError(f"reference path boundary mismatch: {path}")
    manifest_path = root / STORE_NAME / "manifest.json"
    manifest = _cached_manifest(str(root), _identity(_regular_stat(manifest_path)))
    record = manifest["files"].get(relative.as_posix())
    if record is None:
        raise SnapshotArchiveError(f"snapshot reference is absent from manifest: {path}")
    obj = manifest["objects"][record["sha256"]]
    if reference != _reference(relative.as_posix(), record, obj):
        raise SnapshotArchiveError(f"snapshot reference integrity mismatch: {path}")
    original = _verified_object(root, record["sha256"], obj)
    with io.TextIOWrapper(io.BytesIO(original), encoding="utf-8") as text:
        yield text


def _file_state(root: Path, relative: str, record: dict, obj: dict,
                *, allow_missing: bool = False) -> tuple[str, Any]:
    try:
        raw, info = _read_regular(root / relative)
    except FileNotFoundError:
        if allow_missing:
            return "missing", _MISSING
        raise SnapshotArchiveError(f"archived snapshot path is missing: {relative}")
    if raw.startswith(_ARCHIVE_HEADERS):
        if _parse_reference(raw) != _reference(relative, record, obj):
            raise SnapshotArchiveError(f"snapshot reference integrity mismatch: {relative}")
        return "reference", info
    if len(raw) != obj["raw_bytes"] or _sha(raw) != record["sha256"]:
        raise SnapshotArchiveError(f"archived source changed: {relative}")
    return "raw", info


def _verify_reference_coverage(root: Path, manifest: dict):
    with _directory_fd(root):
        pass
    for child in sorted(root.iterdir()):
        if child.name == STORE_NAME:
            continue
        if child.is_symlink():
            raise SnapshotArchiveError(f"symlink boundary: {child}")
        entries = _walk_files(child) if child.is_dir() else [(child, _regular_stat(child))]
        for path, _ in entries:
            if path.suffix == ".jsonl" and is_snapshot_reference(path):
                relative = path.relative_to(root).as_posix()
                if relative not in manifest["files"]:
                    raise SnapshotArchiveError(
                        f"untracked snapshot reference absent from manifest: {relative}")


def verify_archive(root: Path) -> dict:
    root = _absolute(root)
    manifest = load_manifest(root)
    _verify_reference_coverage(root, manifest)
    offloaded = 0
    for digest, obj in manifest["objects"].items():
        if _is_offloaded(root, obj):
            offloaded += 1
            continue
        _verified_object(root, digest, obj)
    for relative, record in manifest["files"].items():
        _file_state(root, relative, record, manifest["objects"][record["sha256"]])
    return {"verified_files": len(manifest["files"]),
            "verified_objects": len(manifest["objects"]) - offloaded,
            "offloaded_objects": offloaded,
            "archive_object_bytes": sum(
                obj["gzip_bytes"] for obj in manifest["objects"].values()
                if not _is_offloaded(root, obj))}


def _walk_files(root: Path):
    with _directory_fd(root):
        pass
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            if (Path(directory) / name).is_symlink():
                raise SnapshotArchiveError(f"symlink boundary: {Path(directory) / name}")
        for name in sorted(names):
            path = Path(directory) / name
            yield path, _regular_stat(path)


def _inventory(root: Path, keep: int) -> tuple[list[Path], list[Path], list[str], list[dict]]:
    candidates, recent, retained, exclusions = [], [], [], []
    with _directory_fd(root):
        pass
    for family in sorted(root.iterdir()):
        if family.name in (STORE_NAME, ".compact.lock"):
            continue
        if family.is_symlink():
            raise SnapshotArchiveError(f"symlink boundary: {family}")
        if not family.is_dir():
            exclusions.append({"path": family.name, "reason": "outside dated family",
                               "bytes": _regular_stat(family).st_size})
            continue
        days = []
        other = []
        for path in sorted(family.iterdir()):
            if path.is_symlink():
                raise SnapshotArchiveError(f"symlink boundary: {path}")
            try:
                valid = path.is_dir() and date.fromisoformat(path.name).isoformat() == path.name
            except ValueError:
                valid = False
            (days if valid else other).append(path)
        newest = set(days[-keep:])
        retained.extend(path.relative_to(root).as_posix() for path in days[-keep:])
        for day in days + other:
            entries = _walk_files(day) if day.is_dir() else [(day, _regular_stat(day))]
            for path, info in entries:
                if day in days and path.suffix == ".jsonl" and \
                        not any(part.startswith(".") for part in path.relative_to(root).parts):
                    (recent if day in newest else candidates).append(path)
                else:
                    exclusions.append({"path": path.relative_to(root).as_posix(),
                                       "reason": "non-JSONL or undated auxiliary file",
                                       "bytes": info.st_size})
    return candidates, recent, retained, exclusions


def _gzip(raw: bytes) -> bytes:
    result = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=result,
                       compresslevel=9, mtime=0) as stream:
        stream.write(raw)
    return result.getvalue()


def _record(relative: str, digest: str, info: os.stat_result) -> dict:
    parts = _snapshot_path(relative).parts
    return {"sha256": digest, "source": parts[0], "observed_at": parts[1],
            "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid,
            "atime_ns": info.st_atime_ns, "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns}


def _disk_guard(root: Path, required: int, minimum: int):
    if type(minimum) is not int or minimum < 0:
        raise ValueError("min_free_bytes must be a non-negative integer")
    if shutil.disk_usage(root).free < required + minimum:
        raise SnapshotArchiveError(f"insufficient free space: need {required + minimum} bytes")


def _write_object(root: Path, digest: str, obj: dict, compressed: bytes):
    path = root / STORE_NAME / obj["path"]
    try:
        _regular_stat(path)
    except FileNotFoundError:
        _atomic_write(path, compressed, expected=_MISSING, create_parent=True)
    _verified_object(root, digest, obj)


def _replace_reference(root: Path, relative: str, record: dict, obj: dict,
                       expected: os.stat_result):
    _atomic_write(root / relative, REFERENCE_MAGIC + _json_bytes(
        _reference(relative, record, obj)), expected=expected, metadata=record)


def _restore_files(root: Path, manifest: dict, relatives: list[str], minimum: int,
                   *, dry_run: bool = False) -> int:
    targets = []
    for relative in relatives:
        record = manifest["files"][relative]
        obj = manifest["objects"][record["sha256"]]
        kind, current = _file_state(root, relative, record, obj, allow_missing=True)
        if kind != "raw":
            targets.append((relative, record, obj, current))
    _disk_guard(root, sum(obj["raw_bytes"] for _, _, obj, _ in targets), minimum)
    if dry_run:
        return len(targets)
    for relative, record, obj, expected in targets:
        original = _verified_object(root, record["sha256"], obj)
        _atomic_write(root / relative, original, expected=expected,
                      metadata=record, create_parent=True)
    return len(targets)


def restore_snapshots(root: Path, *, min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
                       dry_run: bool = False) -> dict:
    root = _absolute(root)
    with nullcontext() if dry_run else _archive_lock(root):
        manifest = load_manifest(root)
        _verify_reference_coverage(root, manifest)
        for digest, obj in manifest["objects"].items():
            if not _is_offloaded(root, obj):
                _verified_object(root, digest, obj)
        restored = _restore_files(root, manifest, sorted(manifest["files"]),
                                  min_free_bytes, dry_run=dry_run)
    return {"dry_run": dry_run, "restored_files": 0 if dry_run else restored,
            "files_to_restore": restored, "archive_retained": True}


def compact_snapshots(root: Path, *, keep: int = 2, dry_run: bool = True,
                      max_new_bytes: int = MAX_ARCHIVE_BYTES,
                      max_archive_bytes: int = MAX_ARCHIVE_BYTES,
                      min_free_bytes: int = DEFAULT_MIN_FREE_BYTES) -> dict:
    if type(keep) is not int or keep < 2:
        raise ValueError("keep must retain at least two newest dated snapshots")
    if type(max_new_bytes) is not int or not 0 <= max_new_bytes <= MAX_ARCHIVE_BYTES:
        raise ValueError("max_new_bytes must be between zero and 500 MiB")
    if type(max_archive_bytes) is not int or not 0 <= max_archive_bytes <= MAX_ARCHIVE_BYTES:
        raise ValueError("max_archive_bytes must be between zero and 500 MiB")
    root = _absolute(root)
    if dry_run:
        return _compact(root, keep, True, max_new_bytes, max_archive_bytes, min_free_bytes)
    with _archive_lock(root):
        return _compact(root, keep, False, max_new_bytes, max_archive_bytes, min_free_bytes)


def _compact(root: Path, keep: int, dry_run: bool, budget: int,
              archive_budget: int, minimum: int) -> dict:
    manifest = load_manifest(root)
    verify_archive(root)
    candidates, recent, retained, exclusions = _inventory(root, keep)
    updated = copy.deepcopy(manifest)
    prepared, new_objects, already = {}, {}, 0
    recent_refs = []
    for path in recent:
        relative = path.relative_to(root).as_posix()
        raw, _ = _read_regular(path)
        if raw.startswith(_ARCHIVE_HEADERS):
            if relative not in manifest["files"]:
                raise SnapshotArchiveError(f"untracked recent reference: {path}")
            recent_refs.append(relative)
    for path in candidates:
        relative = path.relative_to(root).as_posix()
        raw, info = _read_regular(path)
        if raw.startswith(_ARCHIVE_HEADERS):
            if relative not in manifest["files"]:
                raise SnapshotArchiveError(f"untracked snapshot reference: {path}")
            already += 1
            continue
        digest = _sha(raw)
        if relative in manifest["files"] and \
                manifest["files"][relative]["sha256"] != digest:
            raise SnapshotArchiveError(f"archived source changed during planning: {relative}")
        if digest in updated["objects"] and \
                _is_offloaded(root, updated["objects"][digest]):
            # Identical bytes were offloaded earlier; this raw copy is local,
            # so store the object again instead of referencing absent bytes.
            obj = dict(updated["objects"][digest])
            obj.pop("offloaded", None)
            updated["objects"][digest] = obj
            new_objects.setdefault(digest, relative)
        if digest not in updated["objects"]:
            compressed = _gzip(raw)
            obj = {"path": _object_path(digest), "raw_bytes": len(raw),
                   "gzip_bytes": len(compressed), "gzip_sha256": _sha(compressed)}
            updated["objects"][digest] = obj
            try:
                _regular_stat(root / STORE_NAME / obj["path"])
            except FileNotFoundError:
                new_objects[digest] = relative
            else:
                _verified_object(root, digest, obj)
        updated["files"].setdefault(relative, _record(relative, digest, info))
        prepared[relative] = info
    object_dir = root / STORE_NAME / "objects"
    existing_bytes = sum(info.st_size for _, info in _walk_files(object_dir)) \
        if object_dir.exists() else 0
    # Within the budgets, compact the oldest snapshots first and defer the
    # rest: an archive at its cap is a scheduling state (the workstation has
    # not yet receipted older objects), not an error.  Deferred files simply
    # stay raw until a later run.
    new_bytes, deferred_digests = 0, set()
    for digest, relative in sorted(
            new_objects.items(),
            key=lambda item: (_snapshot_path(item[1]).parts[1], item[1])):
        size = updated["objects"][digest]["gzip_bytes"]
        if new_bytes + size <= budget and \
                existing_bytes + new_bytes + size <= archive_budget:
            new_bytes += size
        else:
            deferred_digests.add(digest)
    deferred_files = []
    if deferred_digests:
        for relative in list(prepared):
            digest = updated["files"][relative]["sha256"]
            if digest in deferred_digests:
                deferred_files.append(relative)
                prepared.pop(relative)
                if relative not in manifest["files"]:
                    updated["files"].pop(relative)
        for digest in deferred_digests:
            new_objects.pop(digest)
            if digest in manifest["objects"]:
                updated["objects"][digest] = copy.deepcopy(
                    manifest["objects"][digest])
            else:
                updated["objects"].pop(digest)
    manifest_bytes = _json_bytes(updated)
    reference_bytes = sum(len(REFERENCE_MAGIC) + len(_json_bytes(_reference(
        relative, updated["files"][relative],
        updated["objects"][updated["files"][relative]["sha256"]])))
        for relative in prepared)
    recent_bytes = sum(updated["objects"][updated["files"][r]["sha256"]]["raw_bytes"]
                       for r in recent_refs)
    _disk_guard(root, new_bytes + len(manifest_bytes) + reference_bytes + recent_bytes, minimum)
    result = {
        "dry_run": dry_run, "candidate_files": len(prepared),
        "candidate_raw_bytes": sum(info.st_size for info in prepared.values()),
        "new_object_bytes": new_bytes, "total_archive_object_bytes": existing_bytes + new_bytes,
        "compacted_files": 0, "already_compacted_files": already,
        "deferred_files": len(deferred_files),
        "deferred_reason": ("archive budget reached; waiting for workstation "
                            "receipts to offload older objects"
                            if deferred_files else None),
        "restored_recent_files": 0, "recent_references_to_restore": len(recent_refs),
        "retained_snapshots": retained, "excluded_files": len(exclusions),
        "excluded_bytes": sum(row["bytes"] for row in exclusions), "exclusions": exclusions,
    }
    if dry_run:
        return result
    for digest, relative in new_objects.items():
        raw, info = _read_regular(root / relative)
        if _identity(info) != _identity(prepared[relative]) or _sha(raw) != digest:
            raise SnapshotArchiveError(f"source changed before object storage: {relative}")
        _write_object(root, digest, updated["objects"][digest], _gzip(raw))
    # Verify the entire batch and its raw inodes before publishing any reference.
    for digest, obj in updated["objects"].items():
        if not _is_offloaded(root, obj):
            _verified_object(root, digest, obj)
    for relative, original in prepared.items():
        if _identity(_regular_stat(root / relative)) != _identity(original):
            raise SnapshotArchiveError(f"source changed before archive publication: {relative}")
    if updated != manifest:
        _atomic_write(root / STORE_NAME / "manifest.json", manifest_bytes, create_parent=True)
    result["restored_recent_files"] = _restore_files(root, updated, recent_refs, minimum)
    for relative, original in prepared.items():
        record = updated["files"][relative]
        obj = updated["objects"][record["sha256"]]
        _replace_reference(root, relative, record, obj, original)
        result["compacted_files"] += 1
    verify_archive(root)
    return result


def offload_objects(root: Path, receipts: dict[str, str], *, at: str,
                    dry_run: bool = True) -> dict:
    """Mark receipted objects offloaded, then delete their local bytes.

    ``receipts`` maps object digest -> receipt id.  The caller has already
    proven that the workstation holds each compressed object with exactly
    the manifest's ``gzip_sha256``.  The manifest is written first, so an
    interrupted run leaves "offloaded but still present" objects, which
    remain fully readable.
    """
    root = _absolute(root)
    with nullcontext() if dry_run else _archive_lock(root):
        manifest = load_manifest(root)
        updated = copy.deepcopy(manifest)
        chosen, freed = [], 0
        for digest, receipt in sorted(receipts.items()):
            obj = updated["objects"].get(digest)
            if obj is None or not _object_present(root, obj):
                continue
            compressed, _ = _read_regular(root / STORE_NAME / obj["path"])
            if _sha(compressed) != obj["gzip_sha256"]:
                raise SnapshotArchiveError(
                    f"object differs from its manifest; not offloading: {digest}")
            obj["offloaded"] = {"receipt": receipt, "at": at}
            chosen.append(digest)
            freed += len(compressed)
        result = {"dry_run": dry_run, "offloaded_objects": len(chosen),
                  "freed_bytes": freed}
        if dry_run or not chosen:
            return result
        _atomic_write(root / STORE_NAME / "manifest.json", _json_bytes(updated),
                      create_parent=True)
        for digest in chosen:
            path = root / STORE_NAME / updated["objects"][digest]["path"]
            path.unlink()
        verify_archive(root)
        return result
