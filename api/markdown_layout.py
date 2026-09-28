"""Presentation-only Absatz spacing bound to exact official reader bodies.

Canonical text and machine Markdown remain unchanged. A bare ``(2)`` inside
text is not evidence of a paragraph boundary.
"""
from __future__ import annotations

import re
from typing import Any

from tools.reader_blocks import (
    ReaderBlocksError, _exact_whitespace, flatten_nodes, text_hash,
)

ParagraphLayouts = dict[tuple[str, str], tuple[int, ...]]
_MARKER = re.compile(r"\s*(\(\d+[a-z]?\))")
_EXCLUDED = {"table", "pre", "dl", "ul", "ol"}


def _separate(body: str, offsets: tuple[int, ...]) -> str:
    # Work backwards so all offsets continue to refer to the original body.
    for offset in reversed(offsets):
        if body[:offset].strip():
            body = body[:offset].rstrip() + "\n\n" + body[offset:]
    return body


def source_paragraph_layouts(payloads: list[dict[str, Any]],
                             norm_bodies: list[dict[str, Any]] | None = None,
                             ) -> ParagraphLayouts:
    """Extract boundaries only from exact-hash-matching safe source blocks."""
    layouts: ParagraphLayouts = {}
    ambiguous = set()
    bodies = {}
    canonical = {(str(row.get("enbez") or "").strip(), text_hash(row["text"])):
                 row["text"] for row in (norm_bodies or [])
                 if isinstance(row.get("text"), str)}
    for payload in payloads:
        rows = list((payload.get("norms") or {}).items())
        rows.extend((row.get("enbez"), row) for row in
                    (payload.get("norms_by_index") or {}).values())
        for label, row in rows:
            if not isinstance(label, str) or not isinstance(row, dict):
                continue
            try:
                blocks = row.get("blocks")
                body = flatten_nodes(blocks)
                if text_hash(body) != row.get("text_sha256"):
                    target = canonical.get((label.strip(), row.get("text_sha256")))
                    if target is None:
                        continue
                    # GII list whitespace can differ from the canonical body.
                    # Reconcile text leaves only when every letter agrees;
                    # source structure and canonical source hashes stay intact.
                    blocks = _exact_whitespace(blocks, target)
                    body = flatten_nodes(blocks)
            except ReaderBlocksError:
                continue
            digest = text_hash(body)
            if digest != row.get("text_sha256"):
                continue
            candidates, excluded = set(), []

            def walk(nodes, position=0, blocked=False):
                for node in nodes:
                    if node["type"] == "text":
                        position += len(node["text"])
                        continue
                    start = position
                    skip = (blocked or node["tag"] in _EXCLUDED or
                            node.get("attrs", {}).get("role") == "footnote")
                    position = walk(node["children"], start, skip)
                    if skip:
                        excluded.append((start, position))
                    elif node["tag"] in {"p", "div"}:
                        match = _MARKER.match(body, start, position)
                        if match:
                            candidates.add(match.start(1))
                return position

            walk(blocks)
            offsets = tuple(sorted(offset for offset in candidates
                                   if not any(start <= offset < end
                                              for start, end in excluded)))
            key = (label.strip(), digest)
            if key in layouts and layouts[key] != offsets:
                ambiguous.add(key)
            layouts[key] = offsets
            bodies[key] = body
    for key in ambiguous:
        layouts.pop(key, None)
    # Recognize our own formatted output too: repeated formatting must not
    # reclassify a line inside a known table/pre/list using the fallback.
    for key, offsets in list(layouts.items()):
        formatted_key = (key[0], text_hash(_separate(bodies[key], offsets)))
        if formatted_key not in ambiguous:
            layouts.setdefault(formatted_key, ())
    return layouts


def apply_paragraph_layout(enbez: str, body: str,
                           layouts: ParagraphLayouts) -> str:
    offsets = layouts.get((enbez.strip(), text_hash(body)))
    if offsets is not None:
        return _separate(body, offsets)
    # Without structure, strengthen existing line boundaries only. Never
    # infer a boundary from a parenthesized number embedded in prose.
    return re.sub(r"[^\S\r\n]*(?:\r?\n[ \t]*)+(?=\(\d+[a-z]?\))",
                  "\n\n", body)
