"""Packed raw-capture storage: append-only, zstd-compressed, content-hash addressed.

One file per capture exhausted the btrfs metadata of /home (O(100k) small
files).  Captures are now appended to a few large pack files per source:

    packs/<source_id>/<source_id>-<NNNN>.pack      (rotated at PACK_MAX_BYTES)

Each record is self-describing, so the index can be rebuilt by scanning::

    header  = MAGIC(6) | sha256(32) | codec(1) | raw_size(u64) | stored_size(u64)   (big endian)
    payload = stored_size bytes (zstd frame, or gzip for codec 2)

The index lives in the main database table ``captures`` (sha256 -> pack,
payload offset, lengths, codec).  Appends are fsynced before the index row is
written; a crash can leave unindexed bytes at a pack's tail, never an index row
pointing at missing bytes.  Readers verify sha256 over the decompressed bytes.
"""
from __future__ import annotations

import gzip
import hashlib
import os
import re
import sqlite3
import struct
from dataclasses import dataclass
from pathlib import Path

try:  # Python 3.14+
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - older Python: gzip packs
    _zstd = None

MAGIC = b"SCAP1\n"
HEADER = struct.Struct(">6s32sBQQ")
CODECS = {1: "zstd", 2: "gzip"}
CODEC_IDS = {v: k for k, v in CODECS.items()}
PACK_MAX_BYTES = 512 * 1024 * 1024
ZSTD_LEVEL = 12
PACK_NAME = re.compile(r"^(?P<source>[a-z0-9_-]+)-(?P<n>\d{4})\.pack$")


class CaptureIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class CaptureRef:
    sha256: str
    pack: str          # path relative to the corpus home
    offset: int        # payload offset (after the record header)
    stored_size: int
    raw_size: int
    codec: str

    @property
    def path_ref(self) -> str:
        return f"{self.pack}@{self.offset}"


def compress(data: bytes) -> tuple[bytes, str]:
    if _zstd is not None:
        return _zstd.compress(data, level=ZSTD_LEVEL), "zstd"
    return gzip.compress(data, compresslevel=9, mtime=0), "gzip"


def decompress(data: bytes, codec: str) -> bytes:
    if codec == "zstd":
        if _zstd is None:
            raise CaptureIntegrityError("zstd codec unavailable in this Python")
        return _zstd.decompress(data)
    if codec == "gzip":
        return gzip.decompress(data)
    raise CaptureIntegrityError(f"unknown codec {codec!r}")


def _nocow(directory: Path) -> None:
    """Best effort: disable btrfs copy-on-write for new pack files (fewer extents, less metadata)."""
    try:
        import fcntl
        FS_IOC_GETFLAGS, FS_IOC_SETFLAGS, FS_NOCOW_FL = 0x80086601, 0x40086602, 0x00800000
        fd = os.open(directory, os.O_RDONLY)
        try:
            buf = bytearray(struct.pack("l", 0))
            fcntl.ioctl(fd, FS_IOC_GETFLAGS, buf)
            flags = struct.unpack("l", buf)[0]
            if not flags & FS_NOCOW_FL:
                fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack("l", flags | FS_NOCOW_FL))
        finally:
            os.close(fd)
    except (OSError, ImportError):
        pass


def compressed_empty_file(path: Path) -> None:
    """Best effort: create an empty file with btrfs transparent compression (FS_COMPR_FL) and without NOCOW.
    The corpus database compresses to ~40 % with zstd; NOCOW (inherited from the corpus directory) would
    disable compression.  The flags can only be changed while the file is empty."""
    Path(path).unlink(missing_ok=True)
    Path(path).touch()
    try:
        import fcntl
        FS_IOC_GETFLAGS, FS_IOC_SETFLAGS, FS_NOCOW_FL, FS_COMPR_FL = 0x80086601, 0x40086602, 0x00800000, 0x00000004
        fd = os.open(path, os.O_RDONLY)
        try:
            buf = bytearray(struct.pack("l", 0))
            fcntl.ioctl(fd, FS_IOC_GETFLAGS, buf)
            flags = struct.unpack("l", buf)[0]
            fcntl.ioctl(fd, FS_IOC_SETFLAGS, struct.pack("l", (flags & ~FS_NOCOW_FL) | FS_COMPR_FL))
        finally:
            os.close(fd)
    except (OSError, ImportError):
        pass


class PackStore:
    """Writer/reader for one corpus home.  One writer per source (see sync.source_lock)."""

    def __init__(self, home: Path, conn: sqlite3.Connection, *, max_bytes: int = PACK_MAX_BYTES):
        self.home = Path(home)
        self.conn = conn
        self.max_bytes = max_bytes

    # -- writing ---------------------------------------------------------------
    def _current_pack(self, source_id: str, incoming: int) -> Path:
        directory = self.home / "packs" / source_id
        if not directory.exists():
            directory.mkdir(parents=True)
            _nocow(directory)
        numbers = sorted(int(m.group("n")) for p in directory.iterdir()
                         if (m := PACK_NAME.match(p.name)) and m.group("source") == source_id)
        n = numbers[-1] if numbers else 1
        path = directory / f"{source_id}-{n:04d}.pack"
        if path.exists() and path.stat().st_size + incoming > self.max_bytes and path.stat().st_size > 0:
            path = directory / f"{source_id}-{n + 1:04d}.pack"
        return path

    def lookup(self, sha256: str) -> CaptureRef | None:
        row = self.conn.execute("SELECT sha256, pack, offset, stored_size, raw_size, codec FROM captures "
                                "WHERE sha256 = ?", (sha256,)).fetchone()
        return CaptureRef(*row) if row else None

    def put(self, body: bytes, source_id: str, extension: str) -> CaptureRef:
        digest = hashlib.sha256(body).hexdigest()
        existing = self.lookup(digest)
        if existing is not None:
            return existing
        payload, codec = compress(body)
        record = HEADER.pack(MAGIC, bytes.fromhex(digest), CODEC_IDS[codec], len(body), len(payload)) + payload
        path = self._current_pack(source_id, len(record))
        with open(path, "ab") as handle:
            start = handle.seek(0, os.SEEK_END)
            handle.write(record)
            handle.flush()
            os.fsync(handle.fileno())
        ref = CaptureRef(digest, path.relative_to(self.home).as_posix(), start + HEADER.size, len(payload),
                         len(body), codec)
        self.conn.execute("""INSERT OR IGNORE INTO captures(sha256, source_id, extension, pack, offset, stored_size,
                               raw_size, codec, stored_at)
                             VALUES(?,?,?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))""",
                          (digest, source_id, extension, ref.pack, ref.offset, ref.stored_size, ref.raw_size, codec))
        return ref

    # -- reading ---------------------------------------------------------------
    def read(self, ref: CaptureRef) -> bytes:
        with open(self.home / ref.pack, "rb") as handle:
            handle.seek(ref.offset - HEADER.size)
            header = handle.read(HEADER.size)
            payload = handle.read(ref.stored_size)
        magic, digest, codec_id, raw_size, stored_size = HEADER.unpack(header)
        if magic != MAGIC or digest.hex() != ref.sha256 or stored_size != ref.stored_size:
            raise CaptureIntegrityError(f"pack record header mismatch for {ref.sha256} in {ref.pack}")
        data = decompress(payload, CODECS.get(codec_id, "?"))
        if len(data) != raw_size or hashlib.sha256(data).hexdigest() != ref.sha256:
            raise CaptureIntegrityError(f"capture {ref.sha256} fails its content hash")
        return data

    def get(self, sha256: str) -> bytes:
        ref = self.lookup(sha256)
        if ref is None:
            raise FileNotFoundError(f"capture {sha256} not in pack index")
        return self.read(ref)


def scan_pack(path: Path):
    """Yield (payload_offset, sha256, codec, raw_size, stored_size) for every record; stops at a torn tail."""
    size = path.stat().st_size
    with open(path, "rb") as handle:
        position = 0
        while position + HEADER.size <= size:
            handle.seek(position)
            magic, digest, codec_id, raw_size, stored_size = HEADER.unpack(handle.read(HEADER.size))
            if magic != MAGIC or position + HEADER.size + stored_size > size:
                return
            yield position + HEADER.size, digest.hex(), CODECS.get(codec_id, "?"), raw_size, stored_size
            position += HEADER.size + stored_size
