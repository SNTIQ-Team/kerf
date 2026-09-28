"""Act resolution for the courts corpus: Lexgraph's canonical resolver, nothing else.

Operating principle 1 (one implementation per concern): norm identity lives in
``api.norm_identity.ActResolver`` and is shared with Lexgraph's
``/norms/resolve``.  This module only locates Lexgraph's data plane (the act
index ``wiki.json`` and the full GII catalogue ``gii_catalog.json``) and
caches one resolver per process.
"""
from __future__ import annotations

import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.norm_identity import ActIdentity, ActResolver, long_name_abbreviation  # noqa: E402

__all__ = ["ActIdentity", "resolve_act", "long_name_abbreviation", "lexgraph_data_dir", "resolver"]


class LexgraphDataUnavailable(RuntimeError):
    """Without Lexgraph's act index every norm would silently stay unresolved: fail loudly instead."""


def lexgraph_data_dir() -> Path:
    candidates = []
    if os.environ.get("LEXGRAPH_DATA_DIR"):
        candidates.append(Path(os.environ["LEXGRAPH_DATA_DIR"]))
    candidates.append(ROOT / "web" / "data")
    try:  # a git worktree shares the main checkout's generated data plane
        common = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--git-common-dir"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
        if common:
            candidates.append((ROOT / common).resolve().parent / "web" / "data")
    except (OSError, subprocess.SubprocessError):
        pass
    for candidate in candidates:
        if (candidate / "wiki.json").is_file():
            return candidate
    raise LexgraphDataUnavailable("Lexgraph act index (wiki.json) not found; set LEXGRAPH_DATA_DIR "
                                  f"(looked in: {', '.join(str(c) for c in candidates)})")


@lru_cache(maxsize=1)
def resolver() -> ActResolver:
    return ActResolver.from_data_dir(lexgraph_data_dir())


def resolve_act(abbreviation: str, land: str | None = None) -> ActIdentity:
    return resolver().resolve_act(abbreviation, land=land)
