"""Laptop-side archive of historical law captures (phase 4).

Layout (root ``R`` = ``$LEXGRAPH_HISTORY_ARCHIVE`` or
``~/.local/share/sntiq-legal/lexgraph-archive/history``)::

    R/raw/<source>/sha256/<xx>/<sha256>.<ext>.gz   gzip of the raw bytes;
                                                   sha256 of the UNCOMPRESSED bytes
    R/ledger/<source>.jsonl                        append-only capture ledger
    R/parsed/<source>/<act_id>/<sha256>.json.gz    one parse per raw object
    R/cdx/<source>/<act_id>/<kind>.json            cached discovery listings

The raw store is content addressed and verified on every read.  Nothing here
ever writes into the git checkout or into the main checkout's data dirs.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

CAPTURE_SCHEMA = "lexgraph-history-capture/1"
PARSED_SCHEMA = "lexgraph-history-parsed/1"
USER_AGENT = "SNTIQ-lexgraph/0.2 (research; deless500@gmail.com)"

REPO_ROOT = Path(__file__).resolve().parent.parent


def archive_root(explicit: str | os.PathLike | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("LEXGRAPH_HISTORY_ARCHIVE")
    if env:
        return Path(env).expanduser()
    return Path("~/.local/share/sntiq-legal/lexgraph-archive/history").expanduser()


def web_data_root(explicit: str | os.PathLike | None = None) -> Path:
    """Directory holding ``acts/<id>.json`` and ``federal_states/``.

    Worktrees do not contain the gitignored web data; point
    ``LEXGRAPH_WEB_DATA`` (or ``--web-data``) at the main checkout's
    ``web/data`` instead of copying it.
    """
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("LEXGRAPH_WEB_DATA")
    if env:
        return Path(env).expanduser()
    return REPO_ROOT / "web" / "data"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def wayback_digest(data: bytes) -> str:
    """Base32 SHA-1 as used by the Wayback CDX ``digest`` field."""
    return base64.b32encode(hashlib.sha1(data).digest()).decode("ascii")


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                               dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def gzip_bytes(data: bytes) -> bytes:
    # mtime=0 and no filename: identical input -> identical archive bytes.
    return gzip.compress(data, compresslevel=9, mtime=0)


class HistoryArchive:
    def __init__(self, root: str | os.PathLike | None = None):
        self.root = archive_root(root)

    # ----------------------------------------------------------- raw objects
    def raw_path(self, source: str, sha256: str, ext: str) -> Path:
        return (self.root / "raw" / source / "sha256" / sha256[:2]
                / f"{sha256}.{ext}.gz")

    def find_raw(self, source: str, sha256: str) -> Path | None:
        folder = self.root / "raw" / source / "sha256" / sha256[:2]
        if not folder.is_dir():
            return None
        for path in folder.glob(f"{sha256}.*.gz"):
            return path
        return None

    def has_raw(self, source: str, sha256: str) -> bool:
        return self.find_raw(source, sha256) is not None

    def put_raw(self, source: str, data: bytes, ext: str) -> tuple[str, Path]:
        sha = sha256_hex(data)
        existing = self.find_raw(source, sha)
        if existing is not None:
            return sha, existing
        path = self.raw_path(source, sha, ext)
        _atomic_write(path, gzip_bytes(data))
        return sha, path

    def read_raw(self, source: str, sha256: str) -> bytes:
        path = self.find_raw(source, sha256)
        if path is None:
            raise FileNotFoundError(f"raw object {source}/{sha256} not stored")
        data = gzip.decompress(path.read_bytes())
        if sha256_hex(data) != sha256:
            raise ValueError(f"raw object {path} fails sha256 verification")
        return data

    def iter_raw(self, source: str) -> Iterator[tuple[str, str, Path]]:
        """Yield ``(sha256, ext, path)`` for every stored object of a source."""
        base = self.root / "raw" / source / "sha256"
        if not base.is_dir():
            return
        for path in sorted(base.glob("*/*.gz")):
            name = path.name[:-3]
            sha, _, ext = name.partition(".")
            yield sha, ext, path

    # ---------------------------------------------------------------- ledger
    def ledger_path(self, source: str) -> Path:
        return self.root / "ledger" / f"{source}.jsonl"

    def append_ledger(self, source: str, row: dict) -> None:
        path = self.ledger_path(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def read_ledger(self, source: str) -> list[dict]:
        path = self.ledger_path(source)
        if not path.is_file():
            return []
        rows = []
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue   # a torn last line from an interrupted run
        return rows

    # ---------------------------------------------------------------- parsed
    def parsed_path(self, source: str, act_id: str, sha256: str) -> Path:
        return self.root / "parsed" / source / act_id / f"{sha256}.json.gz"

    def write_parsed(self, source: str, act_id: str, sha256: str,
                     record: dict) -> Path:
        path = self.parsed_path(source, act_id, sha256)
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
        _atomic_write(path, gzip_bytes(payload))
        return path

    def read_parsed(self, source: str, act_id: str, sha256: str) -> dict | None:
        path = self.parsed_path(source, act_id, sha256)
        if not path.is_file():
            return None
        return json.loads(gzip.decompress(path.read_bytes()))

    def iter_parsed(self, source: str, act_id: str | None = None
                    ) -> Iterator[dict]:
        base = self.root / "parsed" / source
        if act_id:
            base = base / act_id
        if not base.is_dir():
            return
        for path in sorted(base.rglob("*.json.gz")):
            yield json.loads(gzip.decompress(path.read_bytes()))

    # ------------------------------------------------------------------- cdx
    def cdx_path(self, source: str, act_id: str, kind: str) -> Path:
        return self.root / "cdx" / source / act_id / f"{kind}.json"

    def write_json(self, path: Path, payload: object) -> None:
        _atomic_write(path, json.dumps(payload, ensure_ascii=False,
                                       indent=0).encode("utf-8"))
