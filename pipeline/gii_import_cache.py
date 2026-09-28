"""Bounded, verified per-act checkpoints for resumable GII corpus refreshes."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path


class ImportBudgetExceeded(RuntimeError):
    pass


def _encode(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


class GiiImportCache:
    def __init__(self, root: Path, *, max_bytes: int = 100 * 1024**2,
                 min_free_bytes: int = 1024**3):
        if max_bytes < 1 or min_free_bytes < 0:
            raise ValueError("invalid import storage budget")
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes

    def _path(self, slug: str) -> Path:
        if not re.fullmatch(r"[a-z0-9_][a-z0-9_-]*", slug):
            raise ValueError("invalid GII slug")
        if self.root.is_symlink():
            raise ValueError("import cache cannot be a symlink")
        target = self.root / f"{slug}.json.gz"
        if target.is_symlink():
            raise ValueError("import checkpoint cannot be a symlink")
        return target

    def used_bytes(self) -> int:
        total = 0
        for path in self.root.glob("*.json.gz"):
            if path.is_symlink() or not path.is_file():
                raise ValueError("invalid import checkpoint")
            total += path.stat().st_size
        return total

    @staticmethod
    def _validate(slug: str, day: str, act: dict, norms: list) -> None:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("invalid checkpoint day")
        if not isinstance(act, dict) or not isinstance(norms, list) or \
                act.get("slug") != slug or act.get("norm_count") != len(norms) or \
                any(not isinstance(n, dict) or n.get("slug") != slug for n in norms):
            raise ValueError("checkpoint law identity or norm count mismatch")

    def get(self, slug: str, day: str) -> tuple[dict, list] | None:
        path = self._path(slug)
        if not path.exists():
            return None
        try:
            raw = json.loads(gzip.decompress(path.read_bytes()))
            digest = raw.pop("sha256")
            if hashlib.sha256(_encode(raw)).hexdigest() != digest:
                raise ValueError("checkpoint digest mismatch")
            self._validate(slug, raw["day"], raw["act"], raw["norms"])
        except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid import checkpoint: {slug}") from exc
        if raw["day"] != day:
            return None
        return raw["act"], raw["norms"]

    def put(self, slug: str, day: str, act: dict, norms: list) -> None:
        path = self._path(slug)
        self._validate(slug, day, act, norms)
        value = {"day": day, "act": act, "norms": norms}
        value["sha256"] = hashlib.sha256(_encode(value)).hexdigest()
        packed = gzip.compress(_encode(value), compresslevel=9, mtime=0)
        previous_size = path.stat().st_size if path.exists() else 0
        if self.used_bytes() - previous_size + len(packed) > self.max_bytes:
            raise ImportBudgetExceeded("GII archive budget reached; checkpoints retained")
        self.root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.root).free - len(packed) < self.min_free_bytes:
            raise ImportBudgetExceeded("disk reserve reached; checkpoints retained")
        descriptor, name = tempfile.mkstemp(prefix=".gii-", dir=self.root)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(packed)
                stream.flush()
                os.fsync(stream.fileno())
            if gzip.decompress(temporary.read_bytes()) != _encode(value):
                raise ValueError("checkpoint write verification failed")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
