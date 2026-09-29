"""Paragraph (Randnummer) assembly and quote location.

Rules (spec, "Identity: Paragraph"):
* A Randnummer runs until the next explicitly numbered block.
* Unnumbered text blocks continue the previous Rn of the same section.
* Explicit unnumbered headings are separate positional paragraphs, not Rn text.
* Page breaks never create a Rn (and never split one).
* A section's unnumbered blocks before its first Rn, and sections without any
  explicit numbers, get stable positional ids.
* A decision without any explicit Rn is ``numbering: none``; its body
  paragraphs are ``#p-<n>`` in document order.
"""
from __future__ import annotations

import json
import re
import unicodedata

from .model import HEADNOTE_SECTIONS, Block, Paragraph, Section

BLOCK_SEPARATOR = "\n\n"  # paragraphs.text joins its blocks with a blank line

RN_LABEL = re.compile(r"^\d{1,4}[a-z]?$")


def clean_label(label: str | None) -> str | None:
    if label is None:
        return None
    value = " ".join(label.split()).rstrip(".")
    return value if RN_LABEL.match(value) else None


def assemble(decision_id: str, sections: list[Section]) -> tuple[list[Paragraph], str]:
    """Return (paragraphs, numbering) where numbering is 'rn' or 'none'."""
    has_rn = any(
        clean_label(b.label) for s in sections if s.name not in HEADNOTE_SECTIONS
        for b in s.blocks if b.kind != "pagebreak")
    numbering = "rn" if has_rn else "none"
    paragraphs: list[Paragraph] = []
    used: set[str] = set()
    body_counter = 0
    # Positional counters run per section name across the whole decision, so a
    # repeated heading (two "Beschluss:" blocks) cannot produce duplicate ids.
    positional: dict[str, int] = {}
    leading: dict[str, int] = {}

    def add(pid: str, section: Section, rn: str | None, block: Block) -> Paragraph:
        if pid in used:
            raise ValueError(f"duplicate paragraph id {pid}")
        used.add(pid)
        para = Paragraph(id=pid, decision_id=decision_id, section=section.name,
                         ordinal=len(paragraphs) + 1, rn=rn,
                         numbering="rn" if rn else "none", blocks=[block])
        paragraphs.append(para)
        return para

    for section in sections:
        blocks = [b for b in section.blocks if b.kind != "pagebreak" and b.text.strip()]
        if not blocks:
            continue
        numbered = section.name not in HEADNOTE_SECTIONS and any(clean_label(b.label) for b in blocks)
        positional_ids = section.name in HEADNOTE_SECTIONS or (
            not numbered and (numbering == "rn" or section.name in ("tenor", "rechtsmittelbelehrung")))
        if positional_ids:
            # Positional ids; labels here are list markers, not Randnummern.
            for block in blocks:
                positional[section.name] = positional.get(section.name, 0) + 1
                add(f"{decision_id}#{section.name}-{positional[section.name]}", section, None, block)
            continue
        if numbering == "none":
            for block in blocks:
                body_counter += 1
                add(f"{decision_id}#p-{body_counter}", section, None, block)
            continue
        current: Paragraph | None = None
        for block in blocks:
            label = clean_label(block.label)
            if label:
                pid = f"{decision_id}#rn-{label}"
                if pid in used:
                    # e.g. a dissent that restarts numbering at 1
                    pid = f"{decision_id}#{section.name}-rn-{label}"
                if pid in used:
                    raise ValueError(f"Randnummer {label} appears twice in section {section.name}")
                current = add(pid, section, label, block)
            elif block.kind == "heading":
                # A source-labelled heading is not part of the preceding Rn.
                # A numbered heading was already handled above and keeps its Rn.
                current = None
                leading[section.name] = leading.get(section.name, 0) + 1
                add(f"{decision_id}#{section.name}-u{leading[section.name]}", section, None, block)
            elif current is not None:
                current.blocks.append(block)
            else:
                leading[section.name] = leading.get(section.name, 0) + 1
                add(f"{decision_id}#{section.name}-u{leading[section.name]}", section, None, block)
    return paragraphs, numbering


# --- quote location --------------------------------------------------------

_QUOTES = str.maketrans({"„": '"', "“": '"', "”": '"', "‚": "'", "‘": "'", "’": "'",
                         "«": '"', "»": '"', "‑": "-", "–": "-", "—": "-",
                         " ": " ", "­": ""})
_ELLIPSIS = re.compile(r"\s*(?:\[\s*(?:…|\.\.\.)\s*\]|\(\s*(?:…|\.\.\.)\s*\)|…|\.\.\.)\s*")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text).translate(_QUOTES)
    text = re.sub(r"(?<=\w)-\s*\n\s*(?=[a-zäöüß])", "", text)  # soft line hyphenation
    return " ".join(text.split()).casefold()


def quote_fragments(quote: str) -> list[str]:
    return [f for f in (normalise(p) for p in _ELLIPSIS.split(quote.translate(_QUOTES))) if f]


def matches_quote(text: str, quote: str) -> bool:
    """True when all quote fragments occur in order within ``text``."""
    haystack = normalise(text)
    position = 0
    for fragment in quote_fragments(quote):
        found = haystack.find(fragment, position)
        if found < 0:
            return False
        position = found + len(fragment)
    return True


def locate_quote(paragraphs: list[Paragraph], quote: str) -> list[str]:
    """Paragraph ids whose text contains the (possibly elided) quote."""
    if not quote_fragments(quote):
        return []
    return [p.id for p in paragraphs if matches_quote(p.text, quote)]


def anchor_of(paragraph_id: str) -> str:
    """The part of a public paragraph id after '#': ``<decision_id>#rn-10`` -> ``rn-10``."""
    decision_id, sep, anchor = paragraph_id.partition("#")
    if not sep or not anchor or "#" in decision_id:
        raise ValueError(f"not a paragraph id: {paragraph_id!r}")
    return anchor


def split_paragraph_id(paragraph_id: str) -> tuple[str, str] | None:
    """(decision_id, anchor) of a public paragraph id; decision ids never contain '#'."""
    decision_id, sep, anchor = paragraph_id.partition("#")
    return (decision_id, anchor) if sep and decision_id and anchor else None


def encode_blocks(paragraph) -> str:
    """v4 ``paragraphs.blocks``: ``[[length, locator], …]`` with a third element ``kind`` unless it is
    ``text``.  Block i starts at the sum of the earlier lengths plus 2 per earlier block (text = blocks
    joined by a blank line); offsets are code points."""
    out = []
    for block in paragraph.blocks:
        item = [len(block.text), block.locator]
        if block.kind != "text":
            item.append(block.kind)
        out.append(item)
    return json.dumps(out, ensure_ascii=False, separators=(",", ":"))


def decode_blocks(blocks: str) -> list[dict]:
    """``[{"start", "end", "locator", "kind"}]`` (the v3 form) from v4 ``paragraphs.blocks``."""
    out, position = [], 0
    for item in json.loads(blocks):
        length, locator = item[0], item[1]
        out.append({"start": position, "end": position + length, "locator": locator,
                    "kind": item[2] if len(item) > 2 else "text"})
        position += length + len(BLOCK_SEPARATOR)
    return out
