"""Source registry: ``python -m courts sync --source <id>``.

Every adapter module exposes ``adapters() -> dict[str, Adapter]``.  Modules
are discovered from ``MODULES``; a missing module is simply not registered.
"""
from __future__ import annotations

import importlib

from .base import Adapter

MODULES = ("rii", "nrw", "by", "juris", "bverwg", "bsg", "bag", "bverfg", "bb", "ni", "hb", "sn", "openjur")


def registry() -> dict[str, Adapter]:
    adapters: dict[str, Adapter] = {}
    for name in MODULES:
        try:
            module = importlib.import_module(f"{__name__}.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"{__name__}.{name}":
                continue
            raise
        adapters.update(module.adapters())
    return adapters


def get_adapter(source_id: str) -> Adapter:
    adapters = registry()
    if source_id not in adapters:
        raise KeyError(f"unknown source {source_id!r}; known: {', '.join(sorted(adapters))}")
    return adapters[source_id]
