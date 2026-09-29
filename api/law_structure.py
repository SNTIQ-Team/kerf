"""Legal structure of one norm body: Absatz, Satz, Nummer, Buchstabe.

Lexgraph's canonical federal states store each norm body as the flattened
GII text, e.g. ``§ 37 SGB XIV``::

    (1) Die ... erfüllen. Am 1. Januar 2021 ... unberührt.(2) Die Vereinbarung
    ... Regelungen über 1.den psychotherapeutisch zu betreuenden
    Personenkreis,2.Art und Ziel der Leistung,...sowie6.die Vergütung ...

Visual blocks (``<p>``, ``<dl>``, line breaks) are *not* legal locators: the
text above has two Absätze, and Absatz 2 ends with one sentence whose list has
six Nummern.  A counter over paragraphs or list elements would report three or
eight units instead.  This module parses legal units with explicit node types
and exact character offsets into the canonical body:

``absatz``      ``(1)``, ``(2a)`` – numbered paragraphs (Absätze)
``satz``        sentences within an Absatz (or within an unnumbered body)
``nummer``      ``1.``, ``1a.`` list items
``buchstabe``   ``a)``, ``b)`` sub-items
``doppelbuchstabe``  ``aa)``, ``bb)`` sub-sub-items
``text_block``  lead-in or closing words of a sentence that carries a list

Numbering must continue a monotone sequence (1, 1a, 2 …; a, b, c …), which
rejects ordinals, dates and years that merely look like markers.  Bavarian
texts carry explicit sentence numbers (``1Zuständige ...  2Die ...``); those
are used as sentence boundaries instead of the punctuation heuristic.

EU texts (``eu=True``; EUR-Lex/Formex articles, the EMRK) keep one line per
Unterabsatz: a line break inside an Absatz starts an ``unterabsatz`` node
(``abs/1/uabs/2``), list points (``a)``, ``1.``) stay on the line of their
lead-in, and points may hang directly off a sentence (``abs/1/satz/1/buchst/a``,
short ``abs/1/buchst/a``).  Sentences are counted across the whole Absatz, as
German courts cite them ("Art. 12 Abs. 5 Satz 2 DSGVO").  An article without
numbered paragraphs whose body has several Unterabsätze gets one implicit
Absatz per Unterabsatz, flagged ``counted_subparagraph`` ("Art. 267 Abs. 3
AEUV").
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

STRUCTURE_PARSER_VERSION = "law-structure/1.1"
EU_STRUCTURE_PARSER_VERSION = "law-structure/1.1+eu-1"

_ABSATZ = re.compile(r"\((\d{1,3})([a-z]?)\)\s*")
_NUMMER = re.compile(r"(?<![§\d])(\d{1,3})([a-z]{0,2})\.(?=[A-Za-zÄÖÜäöüß„»\"(])")
_NUMMER_SPACED = re.compile(r"(?<![§\d\w])(\d{1,3})([a-z]{0,2})\.[  ]+(?=[A-Za-zÄÖÜäöüß„»\"(])")
_BUCHSTABE = re.compile(r"([a-z])\1?\)(?=[\s]*[A-Za-zÄÖÜäöüß„»\"(§\d])")
_BAY_SATZ = re.compile(r"(?<![\d§.,])(\d{1,2})[  ]?(?=[A-ZÄÖÜ„»])")

# Nouns that make "2. Buch", "1. Januar" an ordinal rather than a sentence end.
_ORDINAL_NOUNS = frozenset("""
januar februar märz april mai juni juli august september oktober november
dezember buch buches buchs kapitel kapitels abschnitt abschnitts
abschnittes teil teils teiles titel titels unterabschnitt unterabschnitts
alternative alt halbsatz halbsatzes mal stufe stufen änderung änderungen
gesetz gesetzes verordnung verordnung klasse lebensjahr lebensjahres
lebensjahrs quartal quartals kalendermonat kalendermonats kalenderjahr
kalenderjahres monat monats woche tag tages jahres jahr durchführungsverordnung
durchgang ordnung grades rang ranges stelle staatsvertrag bundes landes
""".split())

_ABBREVIATIONS = frozenset("""
abs art nr s satz z b u a d h i v m vgl bzw ggf gem buchst lit ziff bgbl
gvbl abl ca dr prof ff f bzgl inkl insb sog usw evtl etc einschl anl
bl nrn rn rdnr lfd zb ua dh ivm vo eg eu ewg ag gmbh mbh kg ohg
""".split())


@dataclass
class Node:
    type: str
    number: str | None
    start: int
    end: int
    children: list["Node"] = field(default_factory=list)
    marker_end: int | None = None  # end of the "(2)" / "1." marker
    flags: list[str] = field(default_factory=list)

    def as_dict(self, text: str, prefix: str = "") -> dict[str, Any]:
        locator = _join_locator(prefix, self.type, self.number)
        body_start = self.marker_end if self.marker_end is not None \
            else self.start
        payload: dict[str, Any] = {
            "type": self.type,
            "number": self.number,
            "locator": locator,
            "start": self.start,
            "end": self.end,
            "text": text[body_start:self.end].strip(),
        }
        if self.marker_end is not None:
            payload["marker"] = text[self.start:self.marker_end].strip()
        if self.flags:
            payload["flags"] = list(self.flags)
        if self.children:
            # Sentences keep their Absatz-wide numbering under an Unterabsatz.
            inner = prefix if self.type == "unterabsatz" else locator
            payload["children"] = [child.as_dict(text, inner)
                                   for child in self.children]
        return payload


_LOCATOR_KEY = {
    "absatz": "abs", "satz": "satz", "nummer": "nr", "buchstabe": "buchst",
    "doppelbuchstabe": "buchst", "text_block": "text", "unterabsatz": "uabs",
}


def _join_locator(prefix: str, node_type: str, number: str | None) -> str:
    key = _LOCATOR_KEY.get(node_type, node_type)
    if node_type == "absatz" and number is None:
        return prefix
    part = f"{key}/{number}" if number is not None else key
    return f"{prefix}/{part}" if prefix else part


def _num_key(number: str) -> tuple[int, str]:
    match = re.fullmatch(r"(\d+)([a-z]*)", number)
    if not match:
        return (10**6, number)
    return (int(match.group(1)), match.group(2))


def _follows(previous: str | None, candidate: str) -> bool:
    """Whether ``candidate`` may follow ``previous`` in a numbered sequence."""
    if previous is None:
        return candidate in {"1", "1a"} or candidate == "0"
    prev_n, prev_s = _num_key(previous.split("~")[0])
    cand_n, cand_s = _num_key(candidate)
    if cand_n == prev_n:
        return bool(cand_s) and cand_s > prev_s
    return cand_n == prev_n + 1 and cand_s in {"", "a"}


def _letter_follows(previous: str | None, candidate: str) -> bool:
    if previous is None:
        return candidate in {"a", "aa"}
    if len(candidate) != len(previous):
        return False
    return ord(candidate[0]) == ord(previous[0]) + 1


# ------------------------------------------------------------------ Absatz

def _absatz_nodes(text: str) -> list[Node]:
    """Numbered Absätze at a boundary, in a monotone sequence.

    Real consolidations are not always tidy, and each irregularity is kept
    visible instead of being merged into the previous Absatz:

    * a repealed Absatz can vanish (``(5) … (7)``): accepted as a gap;
    * a number can occur twice (``(3) … (3)``): the second becomes ``3~2``;
    * the first Absatz can be unmarked (``Text … (2) …``): implicit ``1``.
    """
    nodes: list[Node] = []
    previous: str | None = None
    for match in _ABSATZ.finditer(text):
        start = match.start()
        before = text[:start].rstrip()
        at_boundary = (not before or before[-1] in ".:;!?“”\")]»"
                       or before.endswith("weggefallen"))
        if not at_boundary:
            continue
        number = match.group(1) + match.group(2)
        flags: list[str] = []
        if previous is None:
            if number not in {"1", "0"}:
                if not before or int(match.group(1)) > 3:
                    continue
                # Leading unmarked text followed by "(2)": an implicit Abs. 1.
                implicit = Node("absatz", "1", 0, start)
                implicit.flags = ["implicit_marker"]
                nodes.append(implicit)
                previous = "1"
                if not _follows(previous, number):
                    continue
        elif not _follows(previous, number):
            prev_num = int(re.match(r"\d+", previous.split("~")[0]).group(0))
            cand_num = int(match.group(1))
            follows_text = re.match(r"[\s\u00a0]*[A-ZÄÖÜ„»\"]",
                                    text[match.end():match.end() + 4])
            if number == previous.split("~")[0] and follows_text:
                count = sum(1 for node in nodes
                            if (node.number or "").split("~")[0] == number)
                number = f"{number}~{count + 1}"
                flags.append("duplicate_number_in_source")
            elif prev_num < cand_num <= prev_num + 3 and \
                    not match.group(2) and follows_text:
                flags.append("sequence_gap_in_source")
            else:
                continue
        node = Node("absatz", number, start, len(text), marker_end=match.end())
        node.flags = flags
        nodes.append(node)
        previous = number
    for node, following in zip(nodes, nodes[1:]):
        node.end = following.start
    return nodes


# -------------------------------------------------------------- list items

def _items(text: str, start: int, end: int, *, spaced: bool
           ) -> list[Node]:
    """Top-level Nummern in ``text[start:end]`` with their Buchstaben."""
    pattern = _NUMMER_SPACED if spaced else _NUMMER
    candidates = []
    for match in pattern.finditer(text, start, end):
        number = match.group(1) + match.group(2)
        if match.group(2) and len(match.group(2)) > 1:
            continue
        before = text[start:match.start()].rstrip()
        # GII glues "sowie6.die"; accept a letter directly before the number
        # only in unspaced (flattened) text, otherwise demand a separator.
        if spaced and match.start() > start and text[match.start() - 1].isalnum():
            continue
        # Whitespace is a separator, not a reason to reject "und 1. ...".
        # Dates/ordinals and explicit citation numbers are still not list items.
        next_word = re.match(r"([\wÄÖÜäöüß]+)", text[match.end():end])
        if next_word and next_word.group(1).casefold() in _ORDINAL_NOUNS:
            continue
        if re.search(r"(?:§{1,2}|\b(?:Abs|Art|Nr|S|Satz|Nummer|Artikel|Absatz)\.?)[ \u00a0]*$", before, re.I):
            continue
        candidates.append((match, number))
    items: list[Node] = []
    previous: str | None = None
    for match, number in candidates:
        if not _follows(previous, number):
            continue
        if previous is None:
            # A list opens after a lead-in ending in a word or colon, never
            # in the middle of a date such as "am 1.Januar" (no such GII
            # flattening exists) — the sequence rule already rejects years.
            pass
        items.append(Node("nummer", number, match.start(), end,
                          marker_end=match.end()))
        previous = number
    for node, following in zip(items, items[1:]):
        node.end = following.start
    for node in items:
        node.children = _letters(text, node.marker_end or node.start,
                                 node.end, double=False)
        for child in node.children:
            child.children = _letters(text, child.marker_end or child.start,
                                      child.end, double=True)
    return items


def _letters(text: str, start: int, end: int, *, double: bool
             ) -> list[Node]:
    nodes: list[Node] = []
    previous: str | None = None
    for match in _BUCHSTABE.finditer(text, start, end):
        token = text[match.start():match.end() - 1]
        is_double = len(token) == 2
        if is_double != double:
            if double or not is_double:
                continue
            # "oderc)": a doubled-looking token is really one letter glued
            # to the previous word; only accept the trailing letter.
            token = token[-1]
            letter_start = match.end() - 2
        else:
            letter_start = match.start()
        if double and not is_double:
            continue
        if not _letter_follows(previous, token):
            continue
        # A single letter glued to a preceding word is accepted only while
        # continuing an open sequence (b → c), never to open one.
        if previous is None and letter_start > start and \
                text[letter_start - 1].isalpha():
            continue
        nodes.append(Node("doppelbuchstabe" if double else "buchstabe",
                          token, letter_start, end,
                          marker_end=match.end()))
        previous = token
    for node, following in zip(nodes, nodes[1:]):
        node.end = following.start
    return nodes


# ------------------------------------------------------------ sentences

def _is_sentence_end(text: str, dot: int, end: int) -> bool:
    """Whether the ``.``/``?``/``!`` at ``dot`` closes a sentence."""
    after = text[dot + 1:end]
    stripped = after.lstrip("  \t\n")
    if not stripped:
        return True
    # A sentence may start with a norm reference, a number, or a quoted /
    # parenthesized statement. Abbreviation/ordinal guards below still apply.
    opener = stripped.lstrip('(„»" ')
    if not opener or not (opener[0].isupper() or opener[0] == "§" or opener[0].isdigit()):
        return False
    if text[dot] in "?!":
        return True
    token = re.search(r"([\wÄÖÜäöüß]+)$", text[:dot])
    if token is None:
        return True
    word = token.group(1)
    if word.casefold() in _ABBREVIATIONS or (len(word) == 1 and
                                             word.isalpha()):
        return False
    if word.isdigit():
        if len(word) >= 4:
            return True  # a year closing the sentence
        next_word = re.match(r"([\wÄÖÜäöüß]+)", stripped)
        if next_word and next_word.group(1).casefold() in _ORDINAL_NOUNS:
            return False
        return True
    return True


def _sentences(text: str, start: int, end: int, items: list[Node],
               *, bavarian: bool, closing: bool = True) -> list[Node]:
    """Sentence nodes of one Absatz body, lists kept inside their Satz."""
    if bavarian:
        numbered = _bavarian_sentences(text, start, end)
        if numbered:
            for sentence in numbered:
                # Each explicitly numbered sentence starts its own Nr. 1 list.
                # Parsing the whole Absatz once loses subsequent lists.
                own_items = _items(text, sentence.marker_end or sentence.start,
                                   sentence.end, spaced=True)
                _attach_items(text, sentence, own_items)
            return numbered
    protected = [(item.start, item.end) for item in items]
    boundaries: list[int] = []
    for match in re.finditer(r"[.?!](?=[\s ]*(?:\S|$))", text[:end]):
        dot = match.start()
        if dot < start:
            continue
        inside = next((span for span in protected
                       if span[0] <= dot < span[1]), None)
        if inside is not None:
            # Inside a list only the final punctuation of the last item can
            # end the sentence; items themselves are not sentences.
            last_item = protected and inside == protected[-1]
            if not last_item:
                continue
            if text[dot + 1:inside[1]].strip():
                continue
        if _is_sentence_end(text, dot, end):
            boundaries.append(dot + 1)
    sentences: list[Node] = []
    cursor = start
    for boundary in boundaries:
        if text[cursor:boundary].strip():
            sentences.append(Node("satz", None, cursor, boundary))
        cursor = boundary
    if text[cursor:end].strip():
        sentences.append(Node("satz", None, cursor, end))
    for index, sentence in enumerate(sentences, start=1):
        sentence.number = str(index)
        _attach_items(text, sentence, items, closing=closing)
    return sentences


def _bavarian_sentences(text: str, start: int, end: int) -> list[Node]:
    marks = []
    previous = 0
    for match in _BAY_SATZ.finditer(text, start, end):
        number = int(match.group(1))
        before = text[start:match.start()].rstrip()
        if number != previous + 1:
            continue
        if before and before[-1] not in ".:;!?“”\")»":
            continue
        marks.append(match)
        previous = number
    if len(marks) < 2 or marks[0].group(1) != "1":
        return []
    nodes = []
    for index, match in enumerate(marks):
        node_end = marks[index + 1].start() if index + 1 < len(marks) else end
        nodes.append(Node("satz", match.group(1), match.start(), node_end,
                          marker_end=match.end()))
    nodes[0].start = min(nodes[0].start, start)
    return nodes


def _attach_items(text: str, sentence: Node, items: list[Node],
                  *, closing: bool = True) -> None:
    inner = [item for item in items
             if sentence.start <= item.start < sentence.end]
    if not inner:
        return
    blocks: list[Node] = []
    lead_end = inner[0].start
    if text[sentence.marker_end or sentence.start:lead_end].strip():
        blocks.append(Node("text_block", "einleitung",
                           sentence.marker_end or sentence.start, lead_end))
    for item in inner:
        item.end = min(item.end, sentence.end)
    blocks.extend(inner)
    # EU texts put words that close a whole list on their own Unterabsatz
    # line, so ", sofern …" inside the last point belongs to that point.
    tail = _closing_words(text, inner[-1], sentence.end) if closing else None
    if tail is not None:
        inner[-1].end = tail.start
        _trim_children(inner[-1])
        blocks.append(tail)
    sentence.children = blocks


def _closing_words(text: str, last: Node, end: int) -> Node | None:
    """Detect a Schlussteil (", wenn ...") after the last list item."""
    segment = text[last.marker_end or last.start:end]
    match = re.search(r"[,;]\s*(?=(?:wenn|soweit|sofern|es\s+sei\s+denn|"
                      r"und\s+wenn|falls)\b)", segment)
    if not match:
        return None
    absolute = (last.marker_end or last.start) + match.end()
    return Node("text_block", "schlussteil", absolute, end)


def _trim_children(node: Node) -> None:
    for child in node.children:
        child.end = min(child.end, node.end)
        _trim_children(child)


# ----------------------------------------------------------------- public

def _lines(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Non-empty line spans of ``text[start:end]`` (EU Unterabsätze)."""
    spans, cursor = [], start
    for match in re.finditer(r"\n", text[start:end]):
        stop = start + match.start()
        if text[cursor:stop].strip():
            spans.append((cursor, stop))
        cursor = start + match.end()
    if text[cursor:end].strip():
        spans.append((cursor, end))
    return spans


def _eu_items(text: str, start: int, end: int, *, spaced: bool
              ) -> list[Node]:
    """Nummern, or points (``a)``) that hang directly off the sentence."""
    items = _items(text, start, end, spaced=spaced)
    return items or _letters(text, start, end, double=False)


def _counted_subparagraphs(text: str) -> list[Node]:
    nodes = []
    for index, (start, end) in enumerate(_lines(text, 0, len(text)), start=1):
        node = Node("absatz", str(index), start, end)
        node.flags = ["counted_subparagraph"]
        nodes.append(node)
    return nodes if len(nodes) > 1 else []


def _eu_children(body: str, start: int, end: int, *, spaced: bool
                 ) -> list[Node]:
    blocks = _lines(body, start, end)
    if len(blocks) <= 1:
        items = _eu_items(body, start, end, spaced=spaced)
        return _sentences(body, start, end, items, bavarian=False,
                          closing=False)
    children, counter = [], 0
    for index, (block_start, block_end) in enumerate(blocks, start=1):
        items = _eu_items(body, block_start, block_end, spaced=spaced)
        sentences = _sentences(body, block_start, block_end, items,
                               bavarian=False, closing=False)
        for sentence in sentences:
            counter += 1
            sentence.number = str(counter)
        node = Node("unterabsatz", str(index), block_start, block_end)
        node.children = sentences
        children.append(node)
    return children


def parse_norm_structure(text: str, *, bavarian: bool = False,
                         annex: bool = False, eu: bool = False
                         ) -> dict[str, Any]:
    """Parse one canonical norm body into typed legal units.

    The result counts legal units only; offsets always refer to ``text`` so a
    caller can render or diff the canonical bytes without re-tokenizing.
    Annexes (Anlage/Anhang) are usually tables or forms whose "(2)(5)(7)"
    are column references, not Absätze: they get sentence units only.
    ``eu`` enables Unterabsätze and direct points (module docstring).
    """
    body = text or ""
    absaetze = [] if annex else _absatz_nodes(body)
    if eu and not annex and not absaetze:
        absaetze = _counted_subparagraphs(body)
    spaced = bavarian or bool(re.search(r"\b1\.[  ]+\S", body)) and \
        not re.search(r"\b1\.(?=[A-Za-zÄÖÜäöüß„])", body)
    containers = absaetze or [Node("absatz", None, 0, len(body))]
    for absatz in containers:
        content_start = absatz.marker_end if absatz.marker_end is not None \
            else absatz.start
        if eu and not annex:
            absatz.children = _eu_children(body, content_start, absatz.end,
                                           spaced=spaced)
            continue
        items = [] if annex else _items(body, content_start, absatz.end,
                                        spaced=spaced)
        absatz.children = _sentences(body, content_start, absatz.end, items,
                                     bavarian=bavarian)
    units = [node.as_dict(body) for node in containers]
    counts = {"absatz": len(absaetze), "satz": 0, "nummer": 0,
              "buchstabe": 0, "doppelbuchstabe": 0, "text_block": 0}
    if eu:
        counts["unterabsatz"] = 0

    def count(node: dict[str, Any]) -> None:
        if node["type"] in counts and node["type"] != "absatz":
            counts[node["type"]] += 1
        for child in node.get("children") or []:
            count(child)

    for unit in units:
        count(unit)
    return {
        "parser_version": (EU_STRUCTURE_PARSER_VERSION if eu
                           else STRUCTURE_PARSER_VERSION),
        "mode": "annex_sentences_only" if annex else "legal_units",
        "numbered_absaetze": bool(absaetze),
        "counts": counts,
        "units": units,
    }


def iter_units(structure: dict[str, Any]):
    """Depth-first iteration over parsed units."""
    stack = list(reversed(structure.get("units") or []))
    while stack:
        node = stack.pop()
        yield node
        stack.extend(reversed(node.get("children") or []))


def normalize_sentence(value: str) -> str:
    """Comparison form: Unicode NFC, collapsed whitespace, no marker glue."""
    text = unicodedata.normalize("NFC", value or "")
    return " ".join(text.split())


def _satz_nodes(absatz: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for child in absatz.get("children") or []:
        if child["type"] == "unterabsatz":
            out.extend(child.get("children") or [])
        else:
            out.append(child)
    return out


def sentence_units(text: str, *, bavarian: bool = False,
                   annex: bool = False, eu: bool = False
                   ) -> list[dict[str, Any]]:
    """Leaf sentences with structural locators, in document order.

    A sentence that carries a list is one unit (its list items are reported
    as ``items`` with their own locators) because legal amendments and
    citations address the whole Satz as well as its Nummern.
    """
    structure = parse_norm_structure(text, bavarian=bavarian, annex=annex,
                                     eu=eu)
    result = []
    for absatz in structure["units"]:
        for satz in _satz_nodes(absatz):
            items = [child for child in satz.get("children") or []
                     if child["type"] in {"nummer", "buchstabe"}]
            full = text[satz["start"]:satz["end"]]
            result.append({
                "locator": satz["locator"],
                "absatz": absatz.get("number"),
                "satz": satz.get("number"),
                "start": satz["start"],
                "end": satz["end"],
                "text": normalize_sentence(full),
                "sha256": hashlib.sha256(
                    normalize_sentence(full).encode("utf-8")).hexdigest(),
                "items": [{"locator": item["locator"],
                           "short_locator": _short_locator(absatz, item),
                           "number": item["number"]} for item in items],
            })
    return result


def _short_locator(absatz: dict[str, Any], item: dict[str, Any]) -> str:
    prefix = absatz.get("locator") or ""
    key = "buchst" if item.get("type") == "buchstabe" else "nr"
    return (f"{prefix}/{key}/{item['number']}" if prefix
            else f"{key}/{item['number']}")


def find_unit(structure: dict[str, Any], sub: str) -> dict[str, Any] | None:
    """The unit a sub-unit path names, as courts and citations write it.

    Accepts the exact locator, the short form without the Satz of a list
    item (``abs/2/nr/3`` for ``abs/2/satz/1/nr/3``), an Unterabsatz-qualified
    point (``abs/1/uabs/1/buchst/f``) and, for an article whose Absätze are
    counted Unterabsätze, ``uabs/<n>`` for ``abs/<n>``.
    """
    def short(locator: str) -> str:
        return re.sub(r"/?satz/\d+(?=/(?:nr|buchst)/)", "", locator)

    def search(units, wanted: str):
        stack = list(reversed(units))
        while stack:
            node = stack.pop()
            locator = node["locator"]
            if locator == wanted or (node["type"] in {
                    "nummer", "buchstabe", "doppelbuchstabe"}
                    and short(locator).lstrip("/") == wanted):
                return node
            stack.extend(reversed(node.get("children") or []))
        return None

    units = structure.get("units") or []
    found = search(units, sub)
    if found is not None:
        return found
    match = re.fullmatch(r"((?:abs/[^/]+/)?)uabs/(\d+)(/.+)?", sub)
    if not match:
        return None
    parent_path, number = match.group(1).rstrip("/"), match.group(2)
    rest = match.group(3) or ""
    if not parent_path and any("counted_subparagraph" in (u.get("flags") or [])
                               for u in units):
        return search(units, f"abs/{number}{rest}")
    holder = search(units, (f"{parent_path}/" if parent_path else "")
                    + f"uabs/{number}")
    if holder is None or not rest:
        return holder
    return search(holder.get("children") or [],
                  f"{parent_path}{rest}".lstrip("/"))


def human_locator(locator: str) -> str:
    """Render ``abs/2/satz/3/nr/1`` as ``Abs. 2 Satz 3 Nr. 1``."""
    labels = {"abs": "Abs.", "uabs": "UAbs.", "satz": "Satz", "nr": "Nr.",
              "buchst": "Buchst.", "text": ""}
    parts = [part for part in (locator or "").split("/") if part]
    out = []
    for key, value in zip(parts[::2], parts[1::2]):
        if key == "text":
            out.append("Einleitung" if value == "einleitung" else
                       "Schlussteil" if value == "schlussteil" else value)
        else:
            out.append(f"{labels.get(key, key)} {value}")
    return " ".join(out)
