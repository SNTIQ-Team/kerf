"""Workstation archive receipts and legacy attestations.

The production VPS keeps only recent data.  Older source bytes live in the
workstation archive (``~/.local/share/sntiq-legal/lexgraph-archive``), and
two signed documents connect the two sides:

receipt
    Written by the workstation after it pulled a VPS manifest and verified
    every listed file's SHA-256 in its own copy.  The VPS may delete a file
    only if a verified receipt lists that exact path *and* hash.
attestation
    Written by the workstation after ``backfill_legacy_bgbl_history.py
    --rebuild`` verified every pre-2023 BGBl source (PDF hash, publication
    header, text) and derived the amendment candidates.  The VPS build then
    admits exactly the attested candidate rows instead of re-reading source
    PDFs it no longer stores.

Both are signed with ``ssh-keygen -Y sign`` by a dedicated workstation key
and verified against ``/etc/sntiq/lexgraph-archive.allowed_signers``
(override: ``LEXGRAPH_ARCHIVE_SIGNERS``).  Namespaces keep a receipt from
being replayed as an attestation and vice versa.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

PRINCIPAL = "lexgraph-archive@workstation"
RECEIPT_NAMESPACE = "lexgraph-archive-receipt"
ATTESTATION_NAMESPACE = "lexgraph-legacy-attestation"
DEFAULT_SIGNERS = Path("/etc/sntiq/lexgraph-archive.allowed_signers")
ATTESTATION_NAME = "attestation.json"
RECEIPT_SCHEMA = "lexgraph-archive-receipt/1"
MANIFEST_SCHEMA = "lexgraph-archive-manifest/1"
ATTESTATION_SCHEMA = "lexgraph-legacy-attestation/1"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_SAFE_NAME = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,120}")

# Source files whose behaviour determines the legacy candidates.  Their
# hashes are recorded in each attestation so the VPS can tell which parser
# produced the rows it serves.
PARSER_FILES = (
    "pipeline/backfill_legacy_bgbl_history.py",
    "pipeline/backfill_bgbl_history.py",
    "pipeline/patch_parser.py",
    "pipeline/fetch_bgbl_documents.py",
    "api/page_furniture.py",
)


class ArchiveReceiptError(ValueError):
    """A receipt, manifest or attestation failed verification."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def row_digest(row: dict) -> str:
    """Identity of one candidate row as the build receives it."""
    return sha256_bytes(canonical_json(row))


def signers_path() -> Path:
    return Path(os.environ.get("LEXGRAPH_ARCHIVE_SIGNERS") or DEFAULT_SIGNERS)


def sign_file(path: Path, key: Path, namespace: str) -> Path:
    """Create ``<path>.sig`` with the workstation signing key."""
    path, signature = Path(path), Path(str(path) + ".sig")
    signature.unlink(missing_ok=True)
    result = subprocess.run(
        ["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", namespace,
         str(path)], capture_output=True, text=True, check=False)
    if result.returncode != 0 or not signature.is_file():
        raise ArchiveReceiptError(
            f"signing {path.name} failed: {result.stderr.strip()}")
    return signature


def verify_signature(payload: bytes, signature: Path, namespace: str,
                     signers: Path | None = None) -> None:
    signers = Path(signers or signers_path())
    if not signers.is_file():
        raise ArchiveReceiptError(f"allowed signers file missing: {signers}")
    if not Path(signature).is_file():
        raise ArchiveReceiptError(f"signature missing: {signature}")
    result = subprocess.run(
        ["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", PRINCIPAL,
         "-n", namespace, "-s", str(signature)],
        input=payload, capture_output=True, check=False)
    if result.returncode != 0:
        raise ArchiveReceiptError(
            f"signature check failed for {Path(signature).name}: "
            f"{result.stderr.decode(errors='replace').strip()}")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ArchiveReceiptError(f"unreadable JSON: {path}: {exc}") from exc


def validate_relative(path: str) -> str:
    parts = Path(path).parts
    if not path or Path(path).is_absolute() or ".." in parts or \
            any(part.startswith(".writing-") for part in parts):
        raise ArchiveReceiptError(f"invalid archive path: {path!r}")
    return Path(path).as_posix()


def load_manifest(path: Path) -> dict:
    value = _load_json(path)
    if not isinstance(value, dict) or value.get("schema") != MANIFEST_SCHEMA or \
            not isinstance(value.get("files"), dict):
        raise ArchiveReceiptError(f"not an archive manifest: {path}")
    for relative, entry in value["files"].items():
        validate_relative(relative)
        if not isinstance(entry, dict) or not _DIGEST.fullmatch(
                str(entry.get("sha256") or "")) or \
                type(entry.get("size")) is not int:
            raise ArchiveReceiptError(f"invalid manifest entry: {relative}")
    return value


def receipted_hashes(offload_root: Path, signers: Path | None = None
                     ) -> dict[str, set[str]]:
    """``{relative path: {sha256, …}}`` over every verified receipt.

    A receipt that fails verification is ignored as a whole (it can only
    ever *withhold* permission to delete, never grant it).
    """
    return dict(_receipted(str(Path(offload_root).resolve()),
                           str(Path(signers or signers_path()).resolve()),
                           _receipt_stamp(Path(offload_root))))


def _receipt_stamp(offload_root: Path) -> tuple:
    folder = offload_root / "receipts"
    if not folder.is_dir():
        return ()
    return tuple(sorted((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                        for p in folder.iterdir()))


@lru_cache(maxsize=4)
def _receipted(offload_root: str, signers: str, _stamp: tuple
               ) -> tuple[tuple[str, frozenset[str]], ...]:
    root = Path(offload_root)
    found: dict[str, set[str]] = {}
    folder = root / "receipts"
    if not folder.is_dir():
        return ()
    for receipt_path in sorted(folder.glob("*.json")):
        try:
            payload = receipt_path.read_bytes()
            verify_signature(payload, Path(str(receipt_path) + ".sig"),
                             RECEIPT_NAMESPACE, Path(signers))
            receipt = json.loads(payload)
            if receipt.get("schema") != RECEIPT_SCHEMA:
                raise ArchiveReceiptError("wrong receipt schema")
            name = str(receipt.get("manifest") or "")
            if not _SAFE_NAME.fullmatch(name):
                raise ArchiveReceiptError("unsafe manifest name")
            manifest_path = root / "manifests" / name
            if sha256_bytes(manifest_path.read_bytes()) != receipt.get(
                    "manifest_sha256"):
                raise ArchiveReceiptError("manifest hash differs from receipt")
            manifest = load_manifest(manifest_path)
            excluded = set(receipt.get("excluded") or [])
            for relative, entry in manifest["files"].items():
                if relative not in excluded:
                    found.setdefault(relative, set()).add(entry["sha256"])
        except (ArchiveReceiptError, OSError, ValueError):
            continue
    return tuple((path, frozenset(hashes)) for path, hashes in found.items())


def parser_identity(repo_root: Path) -> dict:
    files = {}
    for relative in PARSER_FILES:
        path = Path(repo_root) / relative
        files[relative] = sha256_file(path) if path.is_file() else None
    return {"files": files,
            "sha256": sha256_bytes(canonical_json(files))}


def build_attestation(snapshot_dir: Path, *, repo_root: Path,
                      rebuilt_at: str, receipt: str | None,
                      git_commit: str | None,
                      verified_documents: int) -> dict:
    """Describe one rebuilt legacy snapshot directory exactly."""
    snapshot_dir = Path(snapshot_dir)
    files = {}
    for name in ("candidates.jsonl", "documents.jsonl", "summary.json"):
        files[name] = sha256_file(snapshot_dir / name)
    rows = []
    with open(snapshot_dir / "candidates.jsonl", encoding="utf-8") as stream:
        for line in stream:
            if line.strip():
                rows.append(row_digest(json.loads(line)))
    return {
        "schema": ATTESTATION_SCHEMA,
        "kind": "lexgraph-legacy-bgbl-attestation",
        "snapshot": snapshot_dir.name,
        "files": files,
        "candidate_rows": sorted(set(rows)),
        "candidate_count": len(rows),
        "verified_documents": verified_documents,
        "parser": parser_identity(repo_root),
        "git_commit": git_commit,
        "rebuilt_at": rebuilt_at,
        "archive_receipt": receipt,
        "verification": ("every captured official PDF hash, publication "
                         "header and text re-verified on the workstation; "
                         "candidates re-derived from the verified text"),
    }


def verify_attestation(snapshot_dir: Path, signers: Path | None = None
                       ) -> dict:
    """Verify signature and exact file hashes; return the attestation."""
    snapshot_dir = Path(snapshot_dir)
    path = snapshot_dir / ATTESTATION_NAME
    payload = path.read_bytes() if path.is_file() else None
    if payload is None:
        raise ArchiveReceiptError(f"no attestation in {snapshot_dir}")
    verify_signature(payload, Path(str(path) + ".sig"),
                     ATTESTATION_NAMESPACE, signers)
    value = json.loads(payload)
    if value.get("schema") != ATTESTATION_SCHEMA or \
            value.get("snapshot") != snapshot_dir.name:
        raise ArchiveReceiptError("attestation does not describe this snapshot")
    for name, digest in (value.get("files") or {}).items():
        if not _SAFE_NAME.fullmatch(name) or \
                sha256_file(snapshot_dir / name) != digest:
            raise ArchiveReceiptError(f"attested file changed: {name}")
    if set(value.get("files") or {}) != {
            "candidates.jsonl", "documents.jsonl", "summary.json"}:
        raise ArchiveReceiptError("attestation file set is incomplete")
    return value


@lru_cache(maxsize=4)
def _attested_rows(snapshot_dir: str, signers: str, _stamp: tuple
                   ) -> tuple[frozenset[str], dict]:
    value = verify_attestation(Path(snapshot_dir), Path(signers))
    rows = frozenset(value.get("candidate_rows") or [])
    return rows, {key: value.get(key) for key in (
        "snapshot", "parser", "git_commit", "rebuilt_at", "archive_receipt",
        "candidate_count", "verified_documents")}


def attested_rows(snapshot_dir: Path, signers: Path | None = None
                  ) -> tuple[frozenset[str], dict]:
    """Cached ``(row digests, summary)`` for a verified attestation."""
    snapshot_dir = Path(snapshot_dir).resolve()
    stamp = tuple(
        (name, (snapshot_dir / name).stat().st_mtime_ns,
         (snapshot_dir / name).stat().st_size)
        for name in (ATTESTATION_NAME, ATTESTATION_NAME + ".sig",
                     "candidates.jsonl", "documents.jsonl", "summary.json")
        if (snapshot_dir / name).exists())
    return _attested_rows(str(snapshot_dir),
                          str(Path(signers or signers_path()).resolve()),
                          stamp)


def write_json_atomic(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_json(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def iter_files(root: Path, prefixes: Iterable[str]):
    """Yield ``(relative, path)`` for regular files below the prefixes."""
    root = Path(root)
    for prefix in prefixes:
        base = root / prefix
        if not base.exists():
            continue
        for directory, dirs, names in os.walk(base, followlinks=False):
            dirs.sort()
            for name in sorted(names):
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    continue
                if name.startswith(".writing-") or name.endswith(".tmp") or \
                        name == ".compact.lock":
                    continue
                yield path.relative_to(root).as_posix(), path
