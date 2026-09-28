"""Parse an Inkrafttreten article and resolve the date of one amendment item.

An amending act's commencement article ("Inkrafttreten", "Inkrafttreten,
Außerkrafttreten", …) is itself official evidence.  This module turns it into
explicit rules and answers, for an address inside the act — ``Artikel 1
Nummer 18 Buchstabe b`` — when that part entered into force::

    parsed = parse_commencement_text(article_text)
    resolve_in_force(parsed, item_path("1", nummer="18", buchstabe="b"),
                     promulgated="2023-08-18")
    -> {"date": "2023-08-19", "status": "computed_from_commencement_clause",
        "basis": "…", "clause": "…verbatim…", "retroactive": False, …}

Grammar handled (German federal drafting conventions):

* whole-act defaults: ``Dieses Gesetz tritt … in Kraft``, ``Diese Verordnung
  …``, ``vorbehaltlich der Absätze 2 bis 4``, ``soweit in den folgenden
  Absätzen nichts Abweichendes bestimmt ist``; remainders ``Im Übrigen …``;
* scopes: article lists and ranges (``Artikel 2 bis 4, 7``), sub-article
  scopes at every level (``Artikel 1 Nummer 1, 5, 12 Buchstabe b, Nummer 13,
  16 Buchstabe b und c``: a deeper keyword narrows only the *last* value),
  ``In Artikel 1 treten die Nummern 2 und 4 …``, collective articles
  (``Artikel 12 Absatz 23 Nummer 2``), exclusions (``mit Ausnahme von …``),
  enumerated lists (``Am … treten in Kraft: 1. … 2. …``) and inverted order;
* timings: explicit dates, ``mit Wirkung vom`` (retroactive), ``am Tag(e)
  nach der Verkündung``, ``am ersten Tag des [n-ten] auf die Verkündung
  folgenden Kalendermonats``/Quartals/Jahres, ``n Monate nach der
  Verkündung``, ``am n-ten Tag nach der Verkündung``, ``frühestens jedoch
  am …``; conditional clauses (``an dem Tag, an dem …``, Bekanntmachung)
  stay unknown with a reason.

Resolution: the most specific rule containing the item wins; equally
specific rules with different dates, parts of the item governed by other
rules, partial ("insoweit", "hinsichtlich") or conditional clauses make the
result ``unknown`` with an explicit reason.  Nothing is guessed.
"""
from __future__ import annotations

import json
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from bgbl_text import (
    DATE_RE, MappedText, article_headings, compact, format_german_date,
    german_date, normalise, quote_spans,
)

SCHEMA = "lexgraph-commencement/1"
LEVELS = ("article", "para", "absatz", "satz", "nummer", "buchstabe",
          "doppelbuchstabe", "dreifachbuchstabe",
          # Units of the *amended* law named inside an amending item
          # ("in Artikel 1 Nummer 3 § 707d des BGB"): always narrower than
          # the item and never part of a query.
          "target_para", "target_absatz", "target_satz", "target_nummer",
          "target_buchstabe")
ITEM_LEVELS = {"nummer", "buchstabe", "doppelbuchstabe", "dreifachbuchstabe"}
RANK = {level: rank for rank, level in enumerate(LEVELS)}
LEVEL_LABEL = {"article": "Artikel", "para": "§", "absatz": "Absatz",
               "satz": "Satz", "nummer": "Nummer", "buchstabe": "Buchstabe",
               "doppelbuchstabe": "Doppelbuchstabe",
               "dreifachbuchstabe": "Dreifachbuchstabe",
               "target_para": "§", "target_absatz": "Absatz",
               "target_satz": "Satz", "target_nummer": "Nummer",
               "target_buchstabe": "Buchstabe"}
LETTER_LEVELS = {"buchstabe": 1, "doppelbuchstabe": 2, "dreifachbuchstabe": 3}

COMMENCEMENT_HEADING_RE = re.compile(
    r"(?i)^(?:In-\s*und\s*Außer|Inkraft|Inkraftt|In\s?kraft|Inkrafttre|"
    r"Inkraftkreten|[\d ]*Inkrafttreten|Außerkrafttreten.*Inkraft)")
ORDINALS = {
    "ersten": 1, "erste": 1, "zweiten": 2, "zweite": 2, "dritten": 3,
    "dritte": 3, "vierten": 4, "vierte": 4, "fünften": 5, "fünfte": 5,
    "sechsten": 6, "sechste": 6, "siebten": 7, "siebenten": 7, "siebte": 7,
    "achten": 8, "achte": 8, "neunten": 9, "neunte": 9, "zehnten": 10,
    "zehnte": 10, "elften": 11, "elfte": 11, "zwölften": 12, "zwölfte": 12,
    "dreizehnten": 13, "vierzehnten": 14, "fünfzehnten": 15,
    "sechzehnten": 16, "siebzehnten": 17, "achtzehnten": 18,
    "neunzehnten": 19, "zwanzigsten": 20, "einundzwanzigsten": 21,
    "vierundzwanzigsten": 24, "dreißigsten": 30,
}
CARDINALS = {
    "einem": 1, "einen": 1, "ein": 1, "eins": 1, "zwei": 2, "drei": 3,
    "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9,
    "zehn": 10, "elf": 11, "zwölf": 12, "achtzehn": 18, "vierundzwanzig": 24,
}
ORD_RE = r"(?:\d{1,2}\.|" + "|".join(sorted(ORDINALS, key=len, reverse=True)) \
    + ")"
CARD_RE = r"(?:\d{1,2}|" + "|".join(sorted(CARDINALS, key=len, reverse=True)) \
    + ")"
VERK = r"Verkündung"
OWN = r"(?: (?:dieses Gesetzes|dieser Verordnung|des Gesetzes(?= (?:in|außer) Kraft)))?"


def _ordinal(value: str | None, default: int = 1) -> int:
    if not value:
        return default
    value = value.strip().rstrip(".")
    if value.isdigit():
        return int(value)
    return ORDINALS.get(value.casefold(), default)


def _cardinal(value: str) -> int | None:
    value = value.strip()
    if value.isdigit():
        return int(value)
    return CARDINALS.get(value.casefold())


# ------------------------------------------------------------------ values


def value_key(value: str) -> tuple:
    """Sort key for an item label: ``7`` < ``7a`` < ``8`` < ``8z1``."""
    value = str(value).strip().casefold()
    match = re.fullmatch(r"(\d+)([a-z]*)(\d*)", value)
    if match:
        return (0, int(match.group(1)), match.group(2),
                int(match.group(3) or 0))
    return (1, value)


@dataclass
class Value:
    low: str
    high: str | None = None          # inclusive range end, or None
    high_exclusive: bool = False
    refined: bool = False

    def contains(self, label: str) -> bool:
        key = value_key(label)
        if self.high is None:
            return value_key(self.low) == key
        lo, hi = value_key(self.low), value_key(self.high)
        if key < lo:
            return False
        return key < hi if self.high_exclusive else key <= hi

    def as_json(self) -> Any:
        if self.high is None:
            return self.low
        return {"from": self.low, "to": self.high,
                "to_exclusive": self.high_exclusive}

    @staticmethod
    def from_json(value: Any) -> "Value":
        if isinstance(value, dict):
            return Value(str(value["from"]), str(value["to"]),
                         bool(value.get("to_exclusive")))
        return Value(str(value))


def path_label(path: list) -> str:
    parts = []
    for level, value in path:
        value = value if isinstance(value, (str, dict)) else value.as_json()
        if isinstance(value, dict):
            text = f"{value['from']} bis {value['to']}"
            if value.get("to_exclusive"):
                text += " (ohne " + value["to"] + ")"
        else:
            text = value
        label = LEVEL_LABEL[level]
        parts.append(f"{label} {text}" if label != "§" else f"§ {text}")
    return " ".join(parts) or "gesamtes Gesetz"


def item_path(article: str | None = None, *, absatz: str | None = None,
              nummer: str | None = None, buchstabe: str | None = None,
              doppelbuchstabe: str | None = None,
              dreifachbuchstabe: str | None = None,
              para: str | None = None) -> list[tuple[str, str]]:
    """Query address inside the amending act, most general level first."""
    path = []
    for level, value in (("article", article), ("para", para),
                         ("absatz", absatz), ("nummer", nummer),
                         ("buchstabe", buchstabe),
                         ("doppelbuchstabe", doppelbuchstabe),
                         ("dreifachbuchstabe", dreifachbuchstabe)):
        if value is not None and str(value).strip():
            path.append((level, re.sub(r"\s+", "", str(value)).casefold()))
    return path


# ------------------------------------------------------------------ scopes

KEYWORDS: tuple[tuple[str, str], ...] = (
    ("dreifachbuchstabe", r"Dreifachbuchstaben?\b|Dreifachbuchst\."),
    ("doppelbuchstabe", r"Doppelbuchstaben?\b|Doppelbuchst\.|Dbuchst\."),
    # Glued forms ("Buchstabea", "Nummer5") come from collapsed PDF runs.
    ("buchstabe", r"Buchstaben?\b|Buchstabe(?=[a-z](?![a-zäöüß]))|Buchst\.|"
                  r"Bst\."),
    ("nummer", r"Nummern?\b|Nummer(?=\d)|Nr\.|Nrn\."),
    ("absatz", r"Absatz(?:es)?\b|Absatz(?=\d)|Absätzen?\b|Abs\."),
    ("satz", r"Satz(?:es)?\b|Sätzen?\b"),
    ("article", r"Artikeln?s?\b|Artikel(?=\d)|Art\.(?=\s?\d)"),
    ("para", r"§§?"),
)
KEYWORD_RE = re.compile("|".join(f"(?P<{level}>{pattern})"
                                 for level, pattern in KEYWORDS))
NUMERIC_VALUE_RE = re.compile(r"\d+(?:[a-z](?![a-zäöüß])\d*)?")
ROMAN_VALUE_RE = re.compile(r"[IVXLC]+\b")
LETTER_VALUE_RE = {
    # "Buchstabe a, a1, a2": inserted letters carry a digit suffix.
    1: re.compile(r"[a-z]\d*(?![a-zäöüß])"),
    2: re.compile(r"([a-z])\1(?![a-zäöüß])"),
    3: re.compile(r"([a-z])\1\1(?![a-zäöüß])"),
}
SKIP_WORDS = {"die", "der", "das", "des", "den", "dem", "jeweils", "in",
              "In", "Die", "Der", "Das", "auch", "ferner", "neuen", "neue",
              "folgende", "folgenden", "genannten"}
PARTIAL_RE = re.compile(
    r"(?P<kind>hinsichtlich|insoweit|soweit|sofern|für\s+Personen|"
    r"nur\s+für|mit\s+der\s+Maßgabe|wenn|sobald|nach\s+Maßgabe)\b")
EXCEPT_RE = re.compile(
    r"mit\s+Ausnahme\s+(?:von|der|des|dem|den)?|ausgenommen|außer\s+(?=Artikel|"
    r"Nummer|Nr\.|Buchst)")
CITATION_RE = re.compile(
    r"(?:(?:das|die|der)\s+)?(?:zuletzt\s+)?durch\s+"
    r"(?=Artikel|Art\.|§|das\s+Gesetz|die\s+Verordnung|den\s+Artikel)"
    r"[^,;]*?(?:geändert\s+worden\s+(?:ist|sind)|geändert|$)")
# "Die §§ 659 und 660 der ZPO in der Fassung des Artikels 3 Nr. 9": the
# article reference is the scope, restricted to the named norms.
FASSUNG_RE = re.compile(
    r"in\s+der\s+(?:durch\s+|Fassung\s+(?:des|der)\s+)(?=Artikels?\b|Art\.)")
HALBSATZ_RE = re.compile(
    r"(?:erste[rn]?|zweite[rn]?|dritte[rn]?|vierte[rn]?|letzte[rn]?)\s+"
    r"Halbsatz\b|Halbsatz\s+\d")
# The act itself as subject: "Dieses Gesetz", "Diese Verordnung", "Das
# Gesetz" -- but not "Die Verordnung über …"/"Das Gesetz zur …" (another act).
DEFAULT_RE = re.compile(
    r"^(?:[Ii]m\s+[Üü]brigen\s+)?(?:[Dd]ieses|[Dd]as|[Dd]iese|[Dd]ie)\s+"
    r"(?:Gesetz|Verordnung|Änderungsgesetz|Änderungsverordnung)\b"
    r"(?!\s+(?:über|zur|zum|vom|zu|des|der|für|in\s+der|in\s+der|gegen|"
    r"betreffend)\b)")
FRAGMENT_RE = re.compile(r"^(?:Nr|Nrn|Abs|Art|Buchst|Doppelbuchst|"
                         r"Dreifachbuchst|Artik\w*|Numm\w*|Buchs\w*|"
                         r"Absat\w*|Doppelb\w*|Dreifachb\w*)\.?$")
FULL_KEYWORDS = {"Artikel", "Artikels", "Artikeln", "Nummer", "Nummern",
                 "Buchstabe", "Buchstaben", "Absatz", "Absätze",
                 "Doppelbuchstabe", "Doppelbuchstaben", "Dreifachbuchstabe"}
REMAINDER_RE = re.compile(
    r"\b[Ii]m\s+[Üü]brigen\b|\b[Üü]brigen?\s+(?:Vorschriften|Artikel|"
    r"Bestimmungen|Änderungen)\b|\bDie\s+übrigen\b|\bübrige\s+Artikel\b")


@dataclass
class _Group:
    level: str
    values: list[Value]
    parent: list[tuple[str, Value]]
    # Narrows the last element of a multi-valued list ("9" in "Absatz 1,
    # 3, 9 Nummer 1 und 14"): further values are level-ambiguous.
    narrows_list_end: bool = False
    parent_level: str | None = None
    refinements: int = 0


class ScopeParse:
    """Result of parsing one scope expression."""

    def __init__(self) -> None:
        self.paths: list[list[tuple[str, Value]]] = []
        self.exclusions: list[list[tuple[str, Value]]] = []
        self.partial: list[dict] = []
        self.unrecognized: list[str] = []
        self.default = False
        self.remainder = False
        self.garbled = False
        self.garble_contexts: list[list] = []
        self.level_ambiguities: list[dict] = []

    def as_json(self) -> dict:
        def encode(paths):
            return [[[level, value.as_json()] for level, value in path]
                    for path in paths]
        return {"paths": encode(self.paths),
                "exclusions": encode(self.exclusions),
                "partial": self.partial, "unrecognized": self.unrecognized,
                "default": self.default, "remainder": self.remainder,
                "garbled": self.garbled,
                "garble_contexts": self.garble_contexts,
                "level_ambiguities": self.level_ambiguities}


def _emit(groups: list[_Group]) -> list[list[tuple[str, Value]]]:
    paths = []
    for group in groups:
        for value in group.values:
            if not value.refined:
                paths.append(group.parent + [(group.level, value)])
    return paths


def _refine(group: _Group) -> Value:
    """Mark the group's last value as refined; split a refined range."""
    last = group.values[-1]
    if last.high is not None and not last.refined:
        whole = Value(last.low, last.high, high_exclusive=True)
        single = Value(last.high, refined=True)
        group.values[-1:] = [whole, single]
        return single
    last.refined = True
    return last


def parse_scope(text: str, base: list[tuple[str, Value]] | None = None
                ) -> ScopeParse:
    """Parse ``Artikel 1 Nummer 5 und 7, Artikel 3 …`` into address paths.

    ``base`` is a parent path for relative scopes (exclusions inside an
    article: ``Artikel 1 mit Ausnahme von Nummer 18``).
    """
    result = ScopeParse()
    text = compact(text)
    # Parentheticals name the affected norms ("(§§ 5, 36)"); never scope.
    text = re.sub(r"\((?:[^()]|\([^()]*\))*\)", " ", text)
    if DEFAULT_RE.search(text):
        result.default = True
    if REMAINDER_RE.search(text):
        result.remainder = True
    groups: list[_Group] = []
    stack: list[_Group] = []
    target = result.paths
    exclusion_mode = False
    base = list(base or [])
    exclusion_base: list[tuple[str, Value]] = []
    position = 0
    expect_range_end = False
    enum_expected = 1
    partial_mode: dict | None = None
    last_article: _Group | None = None
    fassung_from: int | None = None
    fassung_text = ""
    # Exclusion context: "Nummer 10 mit Ausnahme von Buchstabe c, Nummer 11"
    # excludes inside Nummer 10 and resumes the main list at "Nummer 11".
    saved: tuple[list[_Group], list[_Group]] | None = None
    comma_since_value = False
    exclusion_level: str | None = None
    exclusion_first: str | None = None

    def finish() -> None:
        target.extend(_emit(groups))
        if saved is not None and exclusion_mode:
            result.paths.extend(_emit(saved[0]))
            saved[0].clear()
        groups.clear()
        stack.clear()

    def last_path_json() -> list | None:
        current = _emit(groups)
        if not current:
            return None
        return [[level, value.as_json()] for level, value in current[-1]]

    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        rest = text[position:]
        if partial_mode is not None:
            # A qualifier ("hinsichtlich § 4 …", "soweit er sich auf Artikel
            # 44 des … bezieht") runs to the next ", [n.] Artikel" or the end.
            stop = re.search(r",\s*(?:(?:und|sowie)\s+)?(?:\d{1,2}\.\s+)?"
                             r"(?=(?:in\s+)?Artikel\b)", rest)
            end = stop.end() if stop else len(rest)
            partial_mode["text"] = compact(partial_mode["text"] + " " +
                                           rest[:stop.start() if stop else
                                                end])
            partial_mode = None
            position += end
            continue
        match = FASSUNG_RE.match(rest)
        if match and not exclusion_mode:
            finish()
            norms = [path for path in result.paths
                     if path and path[0][0] == "para"]
            fassung_text = "; ".join(path_label(path) for path in norms)
            # In place: ``target`` aliases ``result.paths``.
            result.paths[:] = [path for path in result.paths
                               if not (path and path[0][0] == "para")]
            fassung_from = len(result.paths)
            position += match.end()
            last_article = None
            continue
        match = HALBSATZ_RE.match(rest)
        if match:
            result.partial.append({"kind": "halbsatz",
                                   "scope": last_path_json(),
                                   "text": match.group(0)})
            position += match.end()
            continue
        match = CITATION_RE.match(rest)
        if match:
            # "…, die zuletzt durch Artikel 187 der Verordnung vom … geändert
            # worden ist": a citation of another act, never a scope.
            position += match.end()
            stack.clear()
            continue
        match = EXCEPT_RE.match(rest)
        if match and not exclusion_mode:
            current = _emit(groups)
            exclusion_base = list(current[-1]) if current else []
            exclusion_level = exclusion_base[-1][0] if exclusion_base else None
            exclusion_first = None
            saved = (list(groups), list(stack))
            groups = []
            stack = []
            exclusion_mode = True
            target = result.exclusions
            position += match.end()
            continue
        match = PARTIAL_RE.match(rest)
        if match:
            scope = None
            if groups:
                current = _emit(groups)
                scope = [[level, value.as_json()] for level, value in
                         current[-1]] if current else None
            partial_mode = {"kind": compact(match.group("kind")),
                            "scope": scope, "text": ""}
            result.partial.append(partial_mode)
            position += match.end()
            continue
        match = KEYWORD_RE.match(rest)
        if match:
            level = next(name for name in RANK if match.group(name))
            if exclusion_mode and exclusion_level is not None:
                if exclusion_first is None:
                    exclusion_first = level
                elif RANK[level] <= RANK[exclusion_level] < RANK[
                        exclusion_first] and any(g.values for g in groups):
                    # Back to the main list at the level of the excepted item.
                    target.extend(_emit(groups))
                    groups, stack = saved
                    target = result.paths
                    exclusion_mode = False
                    exclusion_base = []
                    exclusion_level = None
            position += match.end()
            if level == "satz" and not re.match(r"\s*\d", text[position:]):
                result.unrecognized.append(match.group(0))
                continue
            while stack and not stack[-1].values:
                stack.pop()
            levels_open = {group.level for group in stack}
            if stack and stack[-1].level.startswith("target_") and \
                    comma_since_value and level in ITEM_LEVELS:
                # "§ 802k Abs. 3 und 4, Nr. 14a": the comma returns to the
                # amending act's own item list.
                while stack and stack[-1].level.startswith("target_"):
                    stack.pop()
            elif stack and stack[-1].level.startswith("target_") and \
                    "target_" + level in RANK and level != "article":
                # "§ 3a Absatz 2 Satz 4 Nummer 2 und 3 des VwVfG": once
                # inside an amended-law norm, deeper keywords stay there.
                level = "target_" + level
            elif level in {"para", "absatz", "satz"} and stack and \
                    level not in levels_open and \
                    stack[-1].level in ITEM_LEVELS | {
                        "target_para", "target_absatz", "target_satz"}:
                # A § / Absatz of the amended law inside an amending item.
                level = "target_" + level
            while stack and (RANK[stack[-1].level] >= RANK[level] or
                             not stack[-1].values):
                stack.pop()
            if not stack and level != "article" and last_article is not None \
                    and last_article.values and not (exclusion_mode and
                                                     exclusion_base):
                # "…, Artikel 1 Nummer 6 … zweiter Halbsatz und Nummer 11":
                # a deeper keyword after an interruption keeps its article.
                stack.append(last_article)
            narrows_list_end = False
            parent_level = None
            if stack:
                # Only the first narrowing right after a list ("Absatz 1, 3,
                # 9 Nummer 1 und 14") is level-ambiguous; a repeated keyword
                # ("…, Nummer 2, 10") is not.
                narrows_list_end = stack[-1].refinements == 0 and (
                    len(stack[-1].values) >= 2 or (
                        stack[-1].values and
                        stack[-1].values[-1].high is not None))
                stack[-1].refinements += 1
                parent_level = stack[-1].level
                if result.level_ambiguities and stack[-1].values:
                    # A continuation narrowed further ("2 und 3 Buchstabe
                    # b") reads naturally only at its literal level.
                    last = [[lv, v.as_json()] for lv, v in
                            stack[-1].parent + [(stack[-1].level,
                                                 stack[-1].values[-1])]]
                    result.level_ambiguities = [
                        entry for entry in result.level_ambiguities
                        if entry["literal"] != last]
                refined = _refine(stack[-1])
                parent = stack[-1].parent + [(stack[-1].level, refined)]
            else:
                source = exclusion_base if exclusion_mode else base
                # "Artikel 1 bis 5 mit Ausnahme des Artikels 3": a sibling
                # exclusion replaces, not extends, the excepted level.
                parent = [element for element in source
                          if RANK[element[0]] < RANK[level]]
            group = _Group(level, [], parent, narrows_list_end=narrows_list_end,
                           parent_level=parent_level)
            groups.append(group)
            stack.append(group)
            if level == "article":
                last_article = group
            expect_range_end = False
            continue
        match = re.match(r"(?:,|;|und\b|sowie\b|oder\b|u\.)", rest)
        if match:
            if match.group(0) in {",", ";"}:
                comma_since_value = True
            position += match.end()
            continue
        match = re.match(r"(?:bis\b|[-–])", rest)
        if match and stack and stack[-1].values:
            expect_range_end = True
            position += match.end()
            continue
        # Enumerator of a list ("1. Artikel …", "und 2. Artikel …").
        match = re.match(r"(\d{1,2})\.\s+(?=(?:in\s+)?(?:Artikel|§|die|der)\b)",
                         rest)
        if match and int(match.group(1)) == enum_expected:
            enum_expected += 1
            position += match.end()
            if exclusion_mode and saved is not None:
                target.extend(_emit(groups))
                groups, stack = saved
                target = result.paths
                exclusion_mode = False
                exclusion_base = []
                exclusion_level = None
            while stack:
                stack.pop()
            continue
        if stack:
            level = stack[-1].level
            if level in LETTER_LEVELS:
                value_re = LETTER_VALUE_RE[LETTER_LEVELS[level]]
            else:
                value_re = NUMERIC_VALUE_RE
            match = value_re.match(rest)
            if not match and level == "article":
                match = ROMAN_VALUE_RE.match(rest)
            if match:
                value = match.group(0)
                position += match.end()
                current = stack[-1]
                ahead = KEYWORD_RE.match(text[position:].lstrip())
                if ahead and level not in LETTER_LEVELS and not expect_range_end:
                    ahead_level = next(name for name in RANK
                                       if ahead.group(name))
                    if ahead_level == level or (
                            ahead_level in ITEM_LEVELS and level in ITEM_LEVELS
                            and RANK[ahead_level] <= RANK[level]):
                        # "die Artikel 12, 13 Nummer 4, 13a Nummer 2": a
                        # value followed by a keyword of its own level
                        # belongs to the enclosing (article) list.
                        owners = [group for group in stack
                                  if RANK[group.level] < RANK[ahead_level]
                                  and group.level not in LETTER_LEVELS]
                        if owners:
                            while stack and stack[-1] is not owners[-1]:
                                stack.pop()
                            current = stack[-1]
                # Kerning split "4 1a" → "41a": two values never touch.
                glued = re.match(r"\s(\d+[a-z]?)(?![a-zäöüß\d])",
                                 text[position:])
                if glued and level not in LETTER_LEVELS and \
                        value.isdigit() and len(value) == 1:
                    value += glued.group(1)
                    position += glued.end()
                comma_since_value = False
                if current.values and current.narrows_list_end and \
                        current.parent_level and current.level not in \
                        LETTER_LEVELS and current.parent_level not in \
                        LETTER_LEVELS and not expect_range_end and \
                        current.parent:
                    # "Absatz 1, 3, 6 bis 8, 9 Nummer 1 und 14": Nummer 14
                    # of Absatz 9, or Absatz 14?  Record both readings.
                    literal = current.parent + [(current.level, Value(value))]
                    alternative = current.parent[:-1] + [
                        (current.parent_level, Value(value))]
                    result.level_ambiguities.append({
                        "literal": [[lv, v.as_json()] for lv, v in literal],
                        "alternative": [[lv, v.as_json()]
                                        for lv, v in alternative]})
                if expect_range_end and current.values:
                    previous = current.values[-1]
                    if previous.high is None and not previous.refined:
                        previous.high = value
                    else:
                        current.values.append(Value(value))
                    expect_range_end = False
                else:
                    current.values.append(Value(value))
                continue
        match = re.match(r"\S+", rest)
        word = match.group(0)
        position += match.end()
        clean = word.strip(",.;:")
        if clean in SKIP_WORDS or not clean:
            continue
        if FRAGMENT_RE.match(clean) and clean not in FULL_KEYWORDS:
            # Where the wording broke down: only items under this prefix
            # (article and first item level) may have been lost.
            context = []
            for group in stack[:2]:
                if group.values:
                    context.append([group.level, group.values[-1].as_json()])
            if not context and last_article is not None and \
                    last_article.values:
                context = [["article", last_article.values[-1].as_json()]]
            result.garble_contexts.append(context)
        if stack and stack[-1].values:
            # Words after an address end the current list ("… des
            # Aufenthaltsgesetzes"); a later deeper keyword may resume the
            # last article (see ``last_article``).
            stack.clear()
        result.unrecognized.append(clean)
    finish()
    if fassung_from is not None:
        for path in result.paths[fassung_from:]:
            result.partial.append({
                "kind": "in_der_fassung",
                "scope": [[level, value.as_json()] for level, value in path],
                "text": fassung_text})
    # Drop keyword groups that never received a value ("Artikel" alone) and
    # item-level references without an article ("die in Absatz 3 genannten"
    # points into the commencement article itself, not into the act).
    # Keyword fragments the grammar could not use ("Nr" of a kerned
    # "Nr ."): the scope is incomplete there and must not be trusted.
    result.garbled = bool(result.garble_contexts)
    stray = [path for path in result.paths
             if path and path[0][0] not in {"article", "para"}]
    result.unrecognized.extend(path_label(path) for path in stray)
    result.paths = [path for path in result.paths
                    if path and path[0][0] in {"article", "para"}]
    result.exclusions = [path for path in result.exclusions if path]
    return result


# ----------------------------------------------------------------- timings

TIMING_PATTERNS: tuple[tuple[str, str], ...] = (
    ("placeholder", r"am (?:…|\.{3}|_{3,})"),
    ("fixed_retro", rf"(?:mit Wirkung (?:vom|zum|ab)|rückwirkend (?:zum|vom|"
                    rf"ab|mit Wirkung vom)(?: dem)?) {DATE_RE}"),
    ("first_of_period",
     rf"am (?:ersten|1\.) Tage? des (?:(?P<ord1>{ORD_RE}) )?auf "
     rf"(?:die {VERK}|den Monat der {VERK})(?: (?:dieses|des) Gesetzes| "
     rf"dieser Verordnung)? folgenden (?:(?P<ord2>{ORD_RE}) )?"
     rf"(?P<unit>Kalendermonats|Monats|Kalendervierteljahres|"
     rf"Vierteljahres|Quartals|Kalenderjahres|Jahres)"),
    ("first_of_period",
     rf"am (?:ersten|1\.) Tage? des (?:(?P<ord1>{ORD_RE}) )?"
     rf"(?P<unit>Kalendermonats|Monats|Quartals|Kalendervierteljahres) nach "
     rf"(?:der )?{VERK}(?: (?:dieses|des) Gesetzes)?"),
    ("first_of_period",
     rf"am Ersten des (?:(?P<ord1>{ORD_RE}) )?(?:auf die {VERK} folgenden )?"
     rf"(?P<unit>Kalendermonats|Monats)(?: nach (?:der )?{VERK})?"),
    ("elliptic_ordinal",
     rf"am (?:{ORD_RE}) des (?:(?:{ORD_RE}) )?auf (?:die {VERK}|den Monat der "
     rf"{VERK}) folgenden"),
    ("day_of_period",
     rf"am (?P<dayord>{ORD_RE}) Tage? des (?:(?P<ord1>{ORD_RE}) )?auf "
     rf"(?:die {VERK}|den Monat der {VERK}) folgenden (?:(?P<ord2>{ORD_RE}) )?"
     rf"(?:Kalender)?[Mm]onats"),
    ("jan1_next_year",
     rf"am 1\. Januar des (?:auf die {VERK} )?folgenden (?:Kalender)?[Jj]ahres"),
    ("day_after", rf"am Tage? nach (?:der|seiner) {VERK}"),
    ("promulgation_day", rf"(?:am|mit dem) Tage? (?:der|seiner) {VERK}"),
    ("nth_day_after", rf"am (?P<ord>{ORD_RE}) Tage? nach (?:der )?{VERK}"),
    ("months_after",
     rf"(?P<n>{CARD_RE}) (?:Kalender)?Monate(?:n)? nach (?:der )?{VERK}"),
    ("conditional",
     r"(?:an dem Tage?|zu dem Zeitpunkt|mit dem Tage?|am Tage?)(?:,)? "
     r"(?:an dem|zu dem|in dem|des Inkrafttretens|der Inkraftsetzung|"
     r"nach dem Inkrafttreten|an welchem|, an dem)|an dem Tage?\b|"
     r"gleichzeitig mit|zeitgleich mit|sobald|mit dem Inkrafttreten|"
     r"mit Inkrafttreten|zum Datum\b|zu dem Datum\b|zum Zeitpunkt\b|"
     r"am Tage? (?:des|der) (?!Verkündung)[A-ZÄÖÜ][a-zäöüß]+|"
     r"nach (?:der )?Bekanntmachung\b|"
     r"mit (?:der )?Inbetriebnahme\b|mit (?:dem )?Beginn\b|mit der "
     r"Anwendbarkeit\b|an dem auf\b"),
    ("fixed", rf"(?:am|zum|ab dem) {DATE_RE}"),
)
TIMING_RES = [(kind, re.compile(pattern, re.IGNORECASE))
              for kind, pattern in TIMING_PATTERNS]
QUALIFIER_RE = re.compile(
    rf",? (?:(?:frühestens|jedoch frühestens|frühestens jedoch|jedoch nicht "
    rf"vor dem|nicht jedoch vor dem|nicht vor dem)|(?P<latest>spätestens"
    rf"(?: jedoch)?|jedoch spätestens))(?: (?:am|mit Wirkung vom))? "
    rf"(?:{DATE_RE}|(?P<rel>am Tage? nach der {VERK}))")
VERB_RE = re.compile(r"(?<![A-Za-zäöüß])(?:tritt|treten)(?![a-zäöüß])")
PREDICATE_RE = re.compile(
    r"(?<![A-Za-zäöüß])(?P<kind>in|außer) Kraft(?![a-zäöüß])")
APPLICATION_RE = re.compile(
    r"(?<![A-Za-zäöüß])(?:anzuwenden|Anwendung|gilt|gelten|zu "
    r"berücksichtigen|anwendbar)(?![a-zäöüß])")
PROVISO_RE = re.compile(
    r"[Vv]orbehaltlich (?:der|des) (?:(?:folgenden|nachfolgenden) )?"
    r"(?:Absatzes|Absätze|Satzes|Sätze|Artikels|Artikel|Regelungen)"
    r"(?: \d+[a-z]?(?: ?(?:bis|und|,|sowie) ?\d+[a-z]?)*)?"
    r"(?: Satz \d+(?: (?:und|bis) \d+)?)?|"
    r",? soweit (?:nicht )?(?:in |im )?(?:den |dem )?(?:(?:folgenden|"
    r"nachfolgenden) )?(?:Absatz|Absätzen|Satz|Sätzen|Artikeln?|"
    r"Vorschriften|Bestimmungen)?(?: \d+[a-z]?(?:(?:,| und| bis) "
    r"\d+[a-z]?)*)?(?: dieses Artikels)? ?(?:nichts|nicht etwas|etwas) "
    r"(?:Abweichendes|anderes|Anderes) (?:bestimmt|geregelt|regelt)"
    r"(?: ist| wird)?|"
    r",? soweit (?:in den (?:folgenden|nachfolgenden) Absätzen|in den "
    r"Absätzen \d+[a-z]?(?: (?:bis|und) \d+[a-z]?)?|nicht (?:in )?"
    r"(?:Absatz|den Absätzen|Satz) [\d a-z,]+?|nachstehend|nachfolgend|"
    r"im Folgenden) ?(?:nichts|nicht etwas|etwas) (?:Abweichendes|"
    r"anderes|Anderes) (?:bestimmt|geregelt|regelt)(?: ist| wird)?|"
    r",? soweit nicht (?:Absatz|Satz) [\d a-z,]+? etwas Abweichendes "
    r"(?:bestimmt|regelt)|"
    r"[Aa]bweichend von (?:den )?(?:Absatz|Absätzen|Satz) \d+[a-z]?"
    r"(?: (?:und|bis) \d+[a-z]?)?(?: Satz \d+)?,?")


def _timing(kind: str, match: re.Match[str], sentence: str) -> dict:
    timing: dict[str, Any] = {"type": kind, "phrase": match.group(0)}
    if kind in {"fixed", "fixed_retro"}:
        timing["type"] = "fixed"
        timing["date"] = german_date(match.group("day"), match.group("month"),
                                     match.group("year"))
        timing["retroactive_phrase"] = kind == "fixed_retro"
        if timing["date"] is None:
            timing["type"] = "unparsed"
    elif kind == "first_of_period":
        unit = match.group("unit")
        count = _ordinal(match.groupdict().get("ord1") or
                         match.groupdict().get("ord2"))
        timing["type"] = {"Kalendermonats": "first_day_of_month",
                          "Monats": "first_day_of_month",
                          "Kalendervierteljahres": "first_day_of_quarter",
                          "Vierteljahres": "first_day_of_quarter",
                          "Quartals": "first_day_of_quarter",
                          "Kalenderjahres": "first_day_of_year",
                          "Jahres": "first_day_of_year"}[unit]
        timing["n"] = count
    elif kind == "elliptic_ordinal":
        # "am sechsten des auf die Verkündung folgenden Kalendermonats":
        # 6th day of the next month, or first day of the sixth month?
        timing["type"] = "ambiguous"
    elif kind == "day_of_period":
        timing["type"] = "day_of_month"
        timing["day"] = _ordinal(match.group("dayord"))
        timing["n"] = _ordinal(match.groupdict().get("ord1") or
                               match.groupdict().get("ord2"))
    elif kind == "jan1_next_year":
        timing["type"] = "first_day_of_year"
        timing["n"] = 1
    elif kind == "nth_day_after":
        timing["n"] = _ordinal(match.group("ord"))
    elif kind == "months_after":
        timing["n"] = _cardinal(match.group("n"))
        if timing["n"] is None:
            timing["type"] = "unparsed"
    elif kind in {"day_after", "promulgation_day"}:
        tail = sentence[match.end():match.end() + 80]
        own = re.match(OWN, tail)
        rest = tail[own.end():] if own else tail
        # "am Tag nach der Verkündung des Gesetzes zur …" is another act.
        if re.match(r" (?:des|der) (?!Gesetzes (?:in|außer) Kraft)", rest) \
                and not re.match(r" (?:dieses|dieser) ", tail):
            timing["type"] = "conditional"
            timing["condition"] = "promulgation_of_another_instrument"
    elif kind == "conditional":
        timing["condition"] = "event_dependent"
    return timing


def _find_timings(sentence: str) -> list[dict]:
    """Non-overlapping timing phrases, earliest first, qualifiers attached."""
    found: list[dict] = []
    taken: list[tuple[int, int]] = []
    for kind, regex in TIMING_RES:
        for match in regex.finditer(sentence):
            start, end = match.span()
            if any(start < b and a < end for a, b in taken):
                continue
            if kind == "fixed":
                before = sentence[max(0, start - 12):start]
                # "für die am 28. April 1999 …": a condition, not a timing.
                if re.search(r"(?:die|der|das|dem|den|für|welche[rn]?|"
                             r"bis|vor|nach)\s$", before):
                    continue
            timing = _timing(kind, match, sentence)
            timing["start"], timing["end"] = start, end
            found.append(timing)
            taken.append((start, end))
    found.sort(key=lambda row: row["start"])
    for qualifier in QUALIFIER_RE.finditer(sentence):
        owner = [row for row in found if row["end"] <= qualifier.start() + 2]
        overlapped = [row for row in found
                      if qualifier.start() <= row["start"] < qualifier.end()]
        for row in overlapped:
            found.remove(row)
        if not owner:
            continue
        owner = owner[-1]
        key = "latest" if qualifier.group("latest") else "earliest"
        if qualifier.group("rel"):
            owner[key] = {"type": "day_after"}
        else:
            owner[key] = german_date(qualifier.group("day"),
                                     qualifier.group("month"),
                                     qualifier.group("year"))
        owner["qualifier_phrase"] = qualifier.group(0).lstrip(", ")
        owner["end"] = max(owner["end"], qualifier.end())
    return found


def compute_timing(timing: dict, promulgated: str | None
                   ) -> tuple[str | None, str | None, str | None]:
    """``(date, status, reason)`` for one timing and the promulgation day."""
    kind = timing.get("type")
    if kind == "fixed":
        return timing["date"], "official_commencement_clause", None
    if kind in {"conditional", "placeholder", "unparsed", "none",
                "ambiguous"}:
        reason = {
            "conditional": timing.get("condition") or "event_dependent",
            "placeholder": "placeholder_date_in_printed_clause",
            "unparsed": "timing_not_parsed",
            "none": "clause_states_no_date",
            "ambiguous": "ambiguous_timing_wording",
        }[kind]
        return None, None, reason
    if promulgated is None:
        return None, None, "promulgation_date_unknown"
    day = date.fromisoformat(promulgated)
    if kind == "day_after":
        result = day + timedelta(days=1)
    elif kind == "promulgation_day":
        result = day
    elif kind == "nth_day_after":
        result = day + timedelta(days=int(timing["n"]))
    elif kind in {"first_day_of_month", "day_of_month"}:
        month = day.month - 1 + int(timing.get("n") or 1)
        year, month = day.year + month // 12, month % 12 + 1
        wanted = int(timing.get("day") or 1)
        if wanted > monthrange(year, month)[1]:
            return None, None, "timing_not_parsed"
        result = date(year, month, wanted)
    elif kind == "first_day_of_quarter":
        quarter = (day.month - 1) // 3 + int(timing.get("n") or 1)
        result = date(day.year + quarter // 4, (quarter % 4) * 3 + 1, 1)
    elif kind == "first_day_of_year":
        result = date(day.year + int(timing.get("n") or 1), 1, 1)
    elif kind == "months_after":
        # Period rule (§§ 187 Abs. 1, 188 Abs. 2, 3 BGB): the period ends
        # with the day numbered like the promulgation day (or the month's
        # last day); the provision is in force from the following day.
        month = day.month - 1 + int(timing["n"])
        year, month = day.year + month // 12, month % 12 + 1
        period_end = date(year, month, min(day.day,
                                           monthrange(year, month)[1]))
        result = period_end + timedelta(days=1)
    else:
        return None, None, "timing_not_parsed"
    earliest = timing.get("earliest")
    if isinstance(earliest, dict):
        earliest = (day + timedelta(days=1)).isoformat()
    if earliest and result.isoformat() < earliest:
        result = date.fromisoformat(earliest)
    return result.isoformat(), "computed_from_commencement_clause", None


def _computation_basis(timing: dict, promulgated: str | None) -> str:
    kind = timing.get("type")
    if kind == "fixed":
        return "explicit calendar date in the clause"
    base = (f"Verkündung {format_german_date(promulgated)}"
            if promulgated else "Verkündung (date unknown)")
    text = {
        "day_after": f"{base} + 1 Tag",
        "promulgation_day": f"{base} (Tag der Verkündung)",
        "nth_day_after": f"{base} + {timing.get('n')} Tage",
        "first_day_of_month": f"erster Tag des {timing.get('n')}. auf die "
                              f"{base} folgenden Kalendermonats",
        "day_of_month": f"{timing.get('day')}. Tag des {timing.get('n')}. "
                        f"auf die {base} folgenden Kalendermonats",
        "first_day_of_quarter": f"erster Tag des {timing.get('n')}. auf die "
                                f"{base} folgenden Quartals",
        "first_day_of_year": f"1. Januar des {timing.get('n')}. auf die "
                             f"{base} folgenden Jahres",
        "months_after": f"{base} + {timing.get('n')} Monate: Fristende mit "
                        f"Ablauf des Tages gleicher Zahl, Inkrafttreten am "
                        f"Folgetag (§§ 187 Abs. 1, 188 Abs. 2, 3 BGB)",
    }.get(kind, kind or "unknown")
    if timing.get("earliest"):
        earliest = timing["earliest"]
        if isinstance(earliest, str):
            text += f", frühestens {format_german_date(earliest)}"
    return text


# --------------------------------------------------------------- sentences

_ABBREVIATIONS = {"Nr", "Abs", "Art", "S", "Buchst", "Doppelbuchst",
                  "Dreifachbuchst", "Bst", "bzw", "vgl", "Ziff", "lit",
                  "BGBl", "Nrn", "u", "ff", "f", "Bd", "Anl", "z", "B", "i",
                  "V", "m", "gem", "d", "h", "Dbuchst", "Satz"}


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Split normalised commencement text into sentences/clauses."""
    spans = []
    start = 0
    depth = 0
    quotes = quote_spans(text)
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        if depth or any(a < index < b for a, b in quotes):
            continue
        if char not in ".;":
            continue
        if char == ";" and re.match(r"\s*(?:(?:und|sowie|oder)\s+)?\d{1,2}\.\s",
                                    text[index + 1:index + 12]):
            # "…: 1. Artikel 2 Nummer 1; 2. Artikel 3 …; 3. …" is one list.
            continue
        following = text[index + 1:index + 3]
        if following and not re.match(r"\s*(?:[A-ZÄÖÜ(§]|$|\d)", following):
            continue
        if char == "." and following.strip().startswith("(") and not re.match(
                r"\s*\(\s?\d+[a-z]?\s?\)", text[index + 1:index + 10]):
            # "vom 13. Dezember 2007 (BGBl. …)": a citation, not an end.
            continue
        if re.match(r"\s*(?:\d{1,20}\s+){0,4}\(\s?\d+[a-z]?\s?\)\s",
                    text[index + 1:index + 60]):
            # A new numbered paragraph always ends the sentence
            # ("… Nummer 11. (2) Artikel 2 …").
            spans.append((start, index + 1))
            start = index + 1
            continue
        before = re.search(r"(\S+)$", text[start:index])
        token = before.group(1) if before else ""
        if char == "." and token:
            if token in _ABBREVIATIONS or token.rstrip(".") in _ABBREVIATIONS:
                continue
            if token.isdigit() and len(token) != 4:
                continue
        if following and re.match(r"\s*\d", following):
            continue
        spans.append((start, index + 1))
        start = index + 1
    if text[start:].strip():
        spans.append((start, len(text)))
    return [(a + len(text[a:b]) - len(text[a:b].lstrip()), b)
            for a, b in spans if text[a:b].strip()]


PARAGRAPH_RE = re.compile(r"^\(\s?(?P<number>\d+[a-z]?)\s?\)\s*")


def scope_subject(text: str) -> str:
    """Leading words of a clause, without paragraph marker."""
    return compact(PARAGRAPH_RE.sub("", text))[:120]


def _strip_scope_text(text: str) -> str:
    text = PROVISO_RE.sub(" ", text)
    text = VERB_RE.sub(" ", text)
    text = PREDICATE_RE.sub(" ", text)
    text = re.sub(r"(?:^|\s)(?:jeweils|erst|nur|gemeinsam)\s", " ", text)
    return compact(text).strip(" ,:;")


@dataclass
class _Sentence:
    start: int
    end: int
    text: str
    paragraph: str | None


def _rule(sentence: _Sentence, mapped: MappedText, index: int, seg: int,
          *, event: str, scope: ScopeParse, timing: dict | None,
          seg_start: int, seg_end: int) -> dict:
    if scope.default and DEFAULT_RE.match(scope_subject(sentence.text)):
        # "Dieses Gesetz tritt vorbehaltlich … Satz 1 am …": the act itself
        # is the subject; stray numbers are proviso residue.
        kind = "default"
        scope.unrecognized.extend(path_label(path) for path in scope.paths)
        scope.paths = []
    elif scope.paths:
        kind = "scoped"
    elif scope.remainder:
        kind = "remainder"
    elif scope.default:
        kind = "default"
    else:
        # Subject not understood: it may or may not cover a queried item.
        kind = "unparsed"
    clause_start, clause_end = mapped.orig_span(sentence.start, sentence.end)
    segment_start, segment_end = mapped.orig_span(
        sentence.start + seg_start, sentence.start + seg_end)
    timing = dict(timing or {"type": "none", "phrase": ""})
    timing.pop("start", None)
    timing.pop("end", None)
    extra = {"ambiguity": "scope_not_fully_parsed"} if scope.garbled else {}
    return {**extra,
        "rule_id": f"s{index}r{seg}",
        "paragraph": sentence.paragraph,
        "sentence_index": index,
        "event": event,
        "kind": kind,
        "scope": scope.as_json(),
        "timing": timing,
        "retroactive_phrase": bool(timing.get("retroactive_phrase")),
        "clause": mapped.original[clause_start:clause_end],
        "clause_start": clause_start,
        "clause_end": clause_end,
        "clause_normalized": sentence.text,
        "segment": mapped.original[segment_start:segment_end],
        "segment_normalized": compact(sentence.text[seg_start:seg_end]),
    }


def _parse_sentence(sentence: _Sentence, mapped: MappedText,
                    index: int) -> tuple[list[dict], dict | None]:
    text = sentence.text
    predicates = list(PREDICATE_RE.finditer(text))
    verbs = list(VERB_RE.finditer(text))
    timings = _find_timings(text)
    rules: list[dict] = []
    if not predicates:
        squashed = re.sub(r"\s+", "", text)
        if re.search(r"(?:in|außer)Kraft", squashed):
            # A commencement sentence the normaliser could not repair
            # ("i nK r a f t"): it may cover any item, so it blocks.
            rule = _rule(sentence, mapped, index, 1,
                         event="in_kraft" if "inKraft" in squashed else
                         "ausser_kraft", scope=ScopeParse(),
                         timing={"type": "unparsed", "phrase": ""},
                         seg_start=0, seg_end=len(text))
            rule["kind"] = "unparsed"
            rule["ambiguity"] = "commencement_sentence_not_parsed"
            return [rule], None
        if APPLICATION_RE.search(text):
            return _application_rules(sentence, mapped, index, timings), None
        if re.search(r"bekannt", text):
            return [], {"kind": "bekanntmachung_note", "text": text}
        return [], {"kind": "no_predicate", "text": text}
    if re.search(r"^Das vorstehende|^Die verfassungsmäßigen", text):
        return [], None
    if re.search(r"\b(?:bleibt|bleiben)\b", text) and PREDICATE_RE.search(
            text) and not VERB_RE.search(text):
        # "§ 7 … bleibt in der … geltenden Fassung bis zum … in Kraft":
        # continued validity of old law, not commencement of an item.
        return [], {"kind": "continued_validity_note", "text": text}
    if re.search(r"\bdürfen\b", text) and re.search(
            r"Rechtsverordnungen|Verordnungen|Verwaltungsvorschriften", text):
        # "Auf Grund der genannten Vorschriften erlassene Rechtsverordnungen
        # dürfen frühestens … in Kraft treten": secondary legislation.
        return [], {"kind": "secondary_legislation_note", "text": text}
    # Inverted order: "Am 1. Januar 2024 treten in Kraft: …".
    first_verb = verbs[0].start() if verbs else None
    if timings and first_verb is not None and \
            not _strip_scope_text(text[:timings[0]["start"]]):
        # "Am 1. Januar 2024 treten in Kraft: …", also "Abweichend von
        # Absatz 1 treten am Tag nach der Verkündung in Kraft: 1. …".
        timing = timings[0]
        anchor = max(verbs[0].end(), timing["end"]) if verbs[0].start() < \
            timing["start"] else verbs[0].end()
        tail = text[anchor:]
        scope_text = PREDICATE_RE.sub(" ", tail, count=1)
        scope = parse_scope(_strip_scope_text(scope_text))
        predicate = next((p for p in predicates if p.start() >
                          timing["end"]), predicates[0])
        event = "in_kraft" if predicate.group("kind") == "in" else \
            "ausser_kraft"
        rules.append(_rule(sentence, mapped, index, 1, event=event,
                           scope=scope, timing=timing, seg_start=0,
                           seg_end=len(text)))
        if len(timings) > 1:
            rules[-1]["ambiguity"] = "several_timings_in_inverted_clause"
        return rules, None
    if not timings:
        scope_text = _strip_scope_text(text[:predicates[0].start()])
        scope = parse_scope(scope_text)
        event = "in_kraft" if predicates[0].group("kind") == "in" else \
            "ausser_kraft"
        maßgabe = re.search(r"mit der Maßgabe", text)
        condition = re.search(r"\b(?:wenn|sobald|sofern|frühestens|an dem|"
                              r"nachdem)\b", text)
        timing = {"type": "conditional", "phrase": condition.group(0),
                  "condition": "event_dependent",
                  "condition_text": compact(text[condition.start():])[:400]} \
            if condition else {"type": "none", "phrase": ""}
        rule = _rule(sentence, mapped, index, 1,
                     event="anwendung" if maßgabe else event, scope=scope,
                     timing=timing, seg_start=0, seg_end=len(text))
        if maßgabe:
            rule["application_condition"] = text[maßgabe.start():]
        return [rule], None
    previous_scope: ScopeParse | None = None
    cursor = 0
    listed = re.match(r"^(?:Die|die)\s+(?:folgenden|nachstehenden|"
                      r"nachfolgenden)\s+\w+", text)
    if listed and len(timings) == 1:
        # "Die folgenden Änderungen treten am … in Kraft: 1. Artikel 3 …"
        colon = text.find(":", timings[0]["end"])
        if colon != -1:
            scope = parse_scope(_strip_scope_text(text[colon + 1:]))
            predicate = next((p for p in predicates if p.start() >=
                              timings[0]["end"]), predicates[-1])
            event = "in_kraft" if predicate.group("kind") == "in" else \
                "ausser_kraft"
            return [_rule(sentence, mapped, index, 1, event=event,
                          scope=scope, timing=timings[0], seg_start=0,
                          seg_end=len(text))], None
    for number, timing in enumerate(timings, start=1):
        raw_scope = text[cursor:timing["start"]]
        # A predicate inside the scope means an earlier timing went
        # unrecognised ("… t r e t e na m1 .J u l i …in Kraft. (10) …");
        # the scope then mixes two clauses and must not be trusted.
        merged = bool(PREDICATE_RE.search(
            PROVISO_RE.sub(" ", raw_scope)[:-1] if number > 1 else
            raw_scope)) and bool(re.search(r"Kraft\s*[.;]", raw_scope))
        scope_text = _strip_scope_text(raw_scope)
        scope_text = re.sub(r"^(?:und|sowie|,)\s+", "", scope_text)
        scope = parse_scope(scope_text) if scope_text else None
        if scope is None or (not scope.paths and not scope.default and
                             not scope.remainder and previous_scope):
            scope = previous_scope or parse_scope("")
        predicate = next((p for p in predicates if p.start() >=
                          timing["end"]), None)
        if predicate is None:
            predicate = predicates[-1]
        event = "in_kraft" if predicate.group("kind") == "in" else \
            "ausser_kraft"
        segment_end = timing["end"]
        next_start = timings[number]["start"] if number < len(timings) \
            else len(text)
        tail = text[timing["end"]:next_start]
        if number == len(timings):
            segment_end = len(text)
        rule = _rule(sentence, mapped, index, number, event=event,
                     scope=scope, timing=timing, seg_start=cursor,
                     seg_end=segment_end)
        if merged:
            rule["ambiguity"] = "clause_boundary_not_recognised"
        partial = re.match(r"\s*(?:in Kraft|außer Kraft)?,? ?(?:insoweit|"
                           r"soweit|als|sofern|für Personen|hinsichtlich|"
                           r"mit der Maßgabe)\b", PROVISO_RE.sub(" ", tail))
        if partial or re.search(r"\binsoweit\b", raw_scope):
            rule["scope"]["partial"].append({
                "kind": "clause_qualifier", "scope": None,
                "text": compact(tail)[:300]})
        if timing.get("type") == "conditional":
            rule["timing"]["condition_text"] = compact(
                text[timing["start"]:])[:400]
        rules.append(rule)
        previous_scope = scope
        # The next segment starts after this timing and any trailing
        # predicate/connector ("…, Artikel 2 tritt am …").
        cursor = timing["end"]
    return rules, None


def _application_rules(sentence: _Sentence, mapped: MappedText, index: int,
                       timings: list[dict]) -> list[dict]:
    text = sentence.text
    dated = [t for t in timings if t.get("type") == "fixed"]
    application = re.search(
        rf"(?:ab (?:dem )?|vom |mit Wirkung vom |erstmals (?:ab |für .{{0,80}}?"
        rf"ab )?){DATE_RE}(?: an)?", text)
    first_marker = APPLICATION_RE.search(text)
    scope_text = text[:first_marker.start()] if first_marker else text
    if application and application.start() < (first_marker.start()
                                               if first_marker else 0):
        scope_text = text[application.end():first_marker.start()]
    scope = parse_scope(_strip_scope_text(scope_text))
    timing = None
    if application:
        timing = {"type": "fixed", "phrase": application.group(0),
                  "date": german_date(application.group("day"),
                                      application.group("month"),
                                      application.group("year"))}
    elif dated:
        timing = {"type": "conditional", "phrase": dated[0]["phrase"],
                  "condition": "application_condition"}
    else:
        timing = {"type": "conditional", "phrase": "",
                  "condition": "application_condition"}
    rule = _rule(sentence, mapped, index, 1, event="anwendung", scope=scope,
                 timing=timing, seg_start=0, seg_end=len(text))
    rule["application_condition"] = text
    return [rule]


def _inherit_article(rule: dict, previous: list[dict]) -> None:
    """"(3) In Artikel 1 tritt § 71 … in Kraft. § 71 Absatz 2 Satz 2 tritt
    am …": a §-scope continuing the paragraph keeps the article of the
    sentence before it."""
    paths = rule["scope"]["paths"]
    if not paths or not all(path and path[0][0] == "para" for path in paths):
        return
    same = [row for row in previous if row.get("paragraph") ==
            rule.get("paragraph") and row["paragraph"] is not None and
            row["sentence_index"] in {rule["sentence_index"] - 1,
                                      rule["sentence_index"]}]
    for row in reversed(same):
        roots = {json.dumps(path[0]) for path in row["scope"]["paths"]
                 if len(path) > 1 and path[0][0] == "article" and
                 path[1][0] == "para"}
        if len(roots) == 1:
            article = json.loads(next(iter(roots)))
            rule["scope"]["paths"] = [[article] + path for path in paths]
            rule["scope"]["article_inherited_from"] = row["rule_id"]
            return


def parse_commencement_text(text: str, *, offset: int = 0,
                            original: str | None = None,
                            article: str | None = None,
                            heading: str | None = None) -> dict:
    """Parse the body of one commencement article.

    ``offset``/``original``: when ``text`` is a slice of a document, pass the
    slice start and the whole document so every clause offset refers to the
    document (for evidence records).
    """
    mapped = normalise(text, offset=offset, original=original)
    body = mapped.text
    sentences: list[_Sentence] = []
    paragraph: str | None = None
    for start, end in _sentence_spans(body):
        chunk = body[start:end]
        junk = re.match(r"(?:\d{2,20}\s+)+(?=\(\s?\d)", chunk)
        if junk:
            start += junk.end()
            chunk = body[start:end]
        marker = PARAGRAPH_RE.match(chunk)
        if marker:
            paragraph = marker.group("number")
            start += marker.end()
            chunk = body[start:end]
        if not chunk.strip():
            continue
        sentences.append(_Sentence(start, end, chunk.strip(), paragraph))
    rules: list[dict] = []
    notes: list[dict] = []
    for index, sentence in enumerate(sentences, start=1):
        found, note = _parse_sentence(sentence, mapped, index)
        if found and re.match(r"(?:Gleichzeitig|Zugleich)\b", sentence.text):
            # "Gleichzeitig treten … in Kraft": the preceding sentence's
            # moment, stated by the clause itself.
            previous = [rule for rule in rules
                        if rule["sentence_index"] == index - 1 and
                        rule["event"] in {"in_kraft", "ausser_kraft"} and
                        rule["timing"].get("type") not in {"none", None}]
            for rule in found:
                if previous and rule["timing"].get("type") in {
                        "none", "conditional"} and rule["timing"].get(
                        "phrase") in {"", "gleichzeitig mit", None} or (
                        previous and rule["timing"].get("type") == "none"):
                    rule["timing"] = {**previous[-1]["timing"],
                                      "inherited_from": previous[-1][
                                          "rule_id"]}
                    rule["retroactive_phrase"] = previous[-1][
                        "retroactive_phrase"]
        for position, rule in enumerate(found):
            _inherit_article(rule, rules + found[:position])
        rules.extend(found)
        if note:
            clause_start, clause_end = mapped.orig_span(sentence.start,
                                                        sentence.end)
            notes.append({**note, "paragraph": sentence.paragraph,
                          "clause": mapped.original[clause_start:clause_end],
                          "clause_start": clause_start,
                          "clause_end": clause_end})
    bekanntmachung = any("bekannt" in row["clause_normalized"]
                         for row in rules) or any(
        "bekannt" in note.get("text", "") for note in notes)
    if bekanntmachung:
        for rule in rules:
            if rule["timing"].get("type") == "conditional" and \
                    rule["event"] != "anwendung":
                rule["timing"]["condition"] = "bekanntmachung_dependent"
    return {"schema": SCHEMA, "article": article, "heading": heading,
            "rules": rules, "notes": notes,
            "bekanntmachung": bekanntmachung,
            "normalized": body}


# ------------------------------------------------------------- locating

SIGNATURE_START_RE = re.compile(
    r"Das\s+vorstehende\s+(?:Gesetz|Verordnung)|Die\s+verfassungsmäßigen\s+"
    r"Rechte|Die\s+Zustimmung\s+des\s+Bundesrates|(?m:^[ \t]*(?:Berlin|Bonn),?"
    r"[ \t]+den[ \t]+\d)|Der\s+Bundespräsident|(?m:^[ \t]*Hinweis\s+der\s+"
    r"Schriftleitung)")
# The commencement article also ends where an annex begins.
ARTICLE_END_RE = re.compile(SIGNATURE_START_RE.pattern +
                            r"|(?m:^[ \t]*Anlage\b)")
MAX_COMMENCEMENT_CHARS = 20000
COMMENCEMENT_BODY_RE = re.compile(
    r"^\s*(?:\(\s?1\s?\)\s*)?(?:(?:Dieses|Diese)\s+(?:Gesetz|Verordnung)|"
    r"Artikel\s+\d|Die\s+Artikel|Am\s+|Mit\s+Wirkung|Im\s+Übrigen)"
    r"[^.]{0,400}?(?:tritt|treten)\b")


def act_region(text: str, *, legacy: bool) -> tuple[int, int]:
    """Span of the retrieved act inside its document text.

    A printed act's PDF may begin with the tail of the previous act and end
    with the start of the next one.  The act begins at its first real
    ``Artikel 1`` heading (not a table-of-contents entry); it ends before
    the next act's ``Vom`` title line or before an ``Artikel 1`` that
    follows a signature block (text order may put the next act's articles
    before its title).
    """
    if not legacy:
        return 0, len(text)
    from bgbl_text import SIGNATURE_RE, TITLE_DATE_RE, page_index
    headings = article_headings(text)
    real = []
    for position, row in enumerate(headings):
        following = headings[position + 1]["start"] if position + 1 < len(
            headings) else len(text)
        # Table-of-contents entries carry a heading but no body.
        if len(text[row["body_start"]:following].strip()) > 120:
            real.append(row)
    ones = [row for row in real if row["article"] == "1"]
    first = next((row for row in ones if page_index(text, row["start"]) <= 1),
                 ones[0] if ones else None)
    start = first["start"] if first else 0
    titles = [match for match in TITLE_DATE_RE.finditer(text)]
    own = next((m for m in titles if page_index(text, m.start()) == 0), None)
    later = [m.start() for m in titles if m is not own and m.start() > start]
    restarts = [row["start"] for row in ones
                if row["start"] > start and _plausible_heading(text, row)
                and SIGNATURE_RE.search(text, start, row["start"])]
    end = min(later[:1] + restarts[:1] + [len(text)])
    return start, end


def _plausible_heading(text: str, row: dict) -> bool:
    """A real ``Artikel N`` heading follows a finished line.

    Broken legacy text layers put "Artikel 2" on its own line in the middle
    of a sentence ("… § 99,\nArtikel 2 \n§ 11 Satz 1 …").
    """
    before = text[max(0, row["start"] - 400):row["start"]].rstrip()
    previous = before.rsplit("\n", 1)[-1].strip() if before else ""
    if not previous:
        return True
    return not re.search(r"[,;]$|(?:und|sowie|oder|bis|des|der|die|den|dem|"
                         r"von|in|§)$", previous)


def find_commencement_article(text: str, *, legacy: bool = False) -> dict | None:
    """The act's Inkrafttreten article: span, number and heading.

    Returns ``{"article", "heading", "start", "body_start", "end"}``
    (offsets into ``text``) or ``None``.
    """
    region_start, region_end = act_region(text, legacy=legacy)
    headings = [row for row in article_headings(text, region_start,
                                                region_end)
                if _plausible_heading(text, row)]
    chosen = None
    for position, row in enumerate(headings):
        end = headings[position + 1]["start"] if position + 1 < len(
            headings) else region_end
        body = text[row["body_start"]:end]
        heading = row["heading"]
        compact_heading = re.sub(r"\s+", "", heading)
        by_heading = bool(COMMENCEMENT_HEADING_RE.match(heading) or
                          COMMENCEMENT_HEADING_RE.match(compact_heading))
        if heading.startswith("Außerkrafttreten") and not re.search(
                r"in\s+Kraft", body[:1500]):
            by_heading = False
        by_body = not heading and bool(COMMENCEMENT_BODY_RE.match(
            compact(body[:600])))
        if not (by_heading or by_body):
            # Heading text kerned/garbled ("Inkraft-\ntreten"): accept a
            # body that opens with a commencement clause.
            if re.match(r"^(?:Inkraft|treten\b)", compact_heading) and \
                    COMMENCEMENT_BODY_RE.match(compact(body[:600])):
                by_body = True
            else:
                continue
        stop = ARTICLE_END_RE.search(text, row["body_start"], end)
        chosen = {"article": row["article"], "heading": heading,
                  "start": row["start"], "body_start": row["body_start"],
                  "end": min(stop.start() if stop else end,
                             row["body_start"] + MAX_COMMENCEMENT_CHARS),
                  "located_by": "heading" if by_heading else "body"}
    if chosen is None:
        # Acts without article structure ("§ 5 Inkrafttreten") or with the
        # clause as the last sentence before the signature.
        stop = SIGNATURE_START_RE.search(text, region_start, region_end)
        limit = stop.start() if stop else region_end
        quotes = quote_spans(text)
        candidates = [m for m in re.compile(
            r"(?:Dieses|Diese)\s+(?:Gesetz|Verordnung)\s+tritt").finditer(
            text, region_start, limit)
            if not any(a < m.start() < b for a, b in quotes)]
        if candidates:
            paragraph = re.search(r"(?m)^[ \t]*§[ \t]*(\d+[a-z]?)[ \t]*\n",
                                  text[max(region_start,
                                           candidates[-1].start() - 200):
                                       candidates[-1].start()])
            chosen = {"article": None,
                      "para": paragraph.group(1) if paragraph else None,
                      "heading": None, "start": candidates[-1].start(),
                      "body_start": candidates[-1].start(), "end": limit,
                      "located_by": "final_clause"}
    return chosen


_SUBSECTION_RE = re.compile(r"(?m)^[ \t]*\((\d+[a-z]?)\)[ \t]+")
_ITEM_RE = re.compile(r"(?m)^[ \t]{0,3}(\d{1,3}[a-z]?)[ \t]?\.[ \t]+")


def _item_labels(text: str) -> tuple[set[str], bool]:
    """Top-level item numbers ("1.", "1a.", "2.") and whether the integer
    sequence is gap-free (only then is an absent number evidence)."""
    from bgbl_text import normalise_lines
    text = normalise_lines(text).text
    quotes = quote_spans(text)
    labels = set()
    previous = None
    for match in _ITEM_RE.finditer(text):
        if any(a < match.start() < b for a, b in quotes):
            continue
        label = match.group(1)
        if previous is None and label not in {"0", "0a", "1"}:
            continue
        labels.add(label)
        previous = label
    numbers = sorted({int(re.match(r"\d+", label).group(0))
                      for label in labels})
    complete = bool(numbers) and numbers == list(range(numbers[0],
                                                       numbers[-1] + 1)) \
        and numbers[0] in {0, 1}
    return labels, complete


def document_structure(text: str, *, legacy: bool = False) -> dict:
    """Which articles, subsections (Absätze) and items the act contains.

    ``{article: {"nummern": {...}, "absaetze": {absatz: {...}}}}`` --
    used only to choose between two grammatical readings of a scope.
    """
    region_start, region_end = act_region(text, legacy=legacy)
    headings = [row for row in article_headings(text, region_start,
                                                region_end)
                if _plausible_heading(text, row)]
    structure: dict[str, dict] = {}
    for position, row in enumerate(headings):
        end = headings[position + 1]["start"] if position + 1 < len(
            headings) else region_end
        body = text[row["body_start"]:end]
        quotes = quote_spans(body)
        markers = []
        expected = 1
        for match in _SUBSECTION_RE.finditer(body):
            if any(a < match.start() < b for a, b in quotes):
                continue
            number = match.group(1)
            if number.isdigit() and int(number) == expected:
                markers.append(match)
                expected += 1
            elif not number.isdigit() and markers:
                markers.append(match)
        absaetze: dict[str, tuple[set[str], bool]] = {}
        for index, match in enumerate(markers):
            stop = markers[index + 1].start() if index + 1 < len(markers) \
                else len(body)
            absaetze[match.group(1)] = _item_labels(body[match.end():stop])
        nummern, complete = _item_labels(body)
        structure.setdefault(row["article"], {
            "nummern": nummern, "complete": complete and not absaetze,
            "absaetze": absaetze})
    return structure


def _path_exists(path: list, structure: dict) -> bool | None:
    """``True``/``False`` only where the structure is reliable, else None.

    Absence counts only for a gap-free numbering (a missing "3." among
    "1., 2., 4." more likely is an extraction gap than a real absence).
    """
    if not path or path[0][0] != "article" or not isinstance(path[0][1], str):
        return None
    article = structure.get(str(path[0][1]))
    if article is None:
        return None
    if len(path) == 1:
        return True
    level, value = path[1]
    if not isinstance(value, str):
        return None
    if level == "absatz":
        if value in article["absaetze"]:
            if len(path) == 2:
                return True
            if path[2][0] == "nummer" and isinstance(path[2][1], str):
                labels, complete = article["absaetze"][value]
                if path[2][1] in labels:
                    return True
                return False if complete else None
            return None
        absaetze = sorted(int(key) for key in article["absaetze"]
                          if key.isdigit())
        gap_free = absaetze and absaetze == list(range(1, absaetze[-1] + 1))
        return False if gap_free else None
    if level == "nummer" and not article["absaetze"]:
        if value in article["nummern"]:
            return True
        return False if article["complete"] else None
    return None


def disambiguate_levels(parsed: dict, structure: dict) -> dict:
    """Resolve level-ambiguous scope readings against the act's structure.

    A reading naming an item the act does not contain is dropped; if
    exactly one reading exists it is used, otherwise both stay and block.
    """
    for rule in parsed.get("rules") or []:
        scope = rule["scope"]
        kept = []
        for entry in scope.get("level_ambiguities") or []:
            literal = _path_exists(entry["literal"], structure)
            alternative = _path_exists(entry["alternative"], structure)
            narrowed = entry["literal"][:len(entry["alternative"])]
            redundant = narrowed == entry["alternative"] or any(
                path == entry["alternative"] for path in scope["paths"])
            if literal is True or redundant or (literal is False and
                                                alternative is False):
                # Decided: the grammatical (literal) reading stands.
                entry["resolved"] = "literal"
                scope.setdefault("level_resolutions", []).append(entry)
                continue
            if literal is not False or alternative is not True:
                # Undecided: the literal reading applies meanwhile; the entry
                # stays for better structural evidence.
                kept.append(entry)
                continue
            if alternative and literal is False:
                scope["paths"] = [entry["alternative"] if path == entry[
                    "literal"] else path for path in scope["paths"]]
                entry["resolved"] = "alternative"
                scope.setdefault("level_resolutions", []).append(entry)
                continue
            kept.append(entry)
        scope["level_ambiguities"] = kept
    return parsed


def parse_document_commencement(text: str, *, legacy: bool = False) -> dict:
    """Locate and parse the commencement article of a document text."""
    located = find_commencement_article(text, legacy=legacy)
    if located is None:
        return {"schema": SCHEMA, "article": None, "heading": None,
                "rules": [], "notes": [], "bekanntmachung": False,
                "located": None, "reason": "no_commencement_article_found"}
    body = text[located["body_start"]:located["end"]]
    parsed = parse_commencement_text(
        body, offset=located["body_start"], original=text,
        article=located.get("article"), heading=located.get("heading"))
    parsed["located"] = located
    if any(rule["scope"].get("level_ambiguities") for rule in parsed["rules"]):
        disambiguate_levels(parsed, document_structure(text, legacy=legacy))
    return parsed


# --------------------------------------------------------------- resolve


def _decode(path: list) -> list[tuple[str, Value]]:
    return [(level, value if isinstance(value, Value) else
             Value.from_json(value)) for level, value in path]


def relation(scope: list, query: list[tuple[str, str]],
             target: dict | None = None) -> str:
    """``contains`` | ``inside`` | ``overlap`` | ``disjoint``.

    ``target`` (``{"para": "7"}``) is the norm of the amended law the queried
    item changes; it decides clause scopes that name amended-law norms
    instead of amending-act items ("Die §§ 659 und 660 … treten …").
    """
    scope = _decode(scope)
    if scope and query and scope[0][0] == "para" and query[0][0] == "article":
        wanted = (target or {}).get("para")
        if wanted and not scope[0][1].contains(str(wanted).casefold()):
            return "disjoint"
        return "overlap"
    for position, ((s_level, s_value), (q_level, q_value)) in enumerate(
            zip(scope, query)):
        if s_level != q_level:
            if s_level == "para" and q_level in ITEM_LEVELS | {"absatz"} \
                    and position > 0:
                # "in Artikel 3 § 18a Absatz 11": an amended-law norm
                # directly under an amending article.
                paras = (target or {}).get("paras") or (
                    [(target or {})["para"]] if (target or {}).get("para")
                    else [])
                if paras and not any(s_value.contains(str(p).casefold())
                                     for p in paras):
                    return "disjoint"
            return "overlap"
        if not s_value.contains(q_value):
            return "disjoint"
    if len(scope) <= len(query):
        return "contains"
    extra = scope[len(query):]
    if target and all(level.startswith("target_") for level, _ in extra):
        # "in Artikel 1 Nummer 3 § 707d des BGB": decided by the amended
        # norm the queried (leaf) item changes.
        wanted = {"target_para": target.get("paras") or (
                      [target["para"]] if target.get("para") else [])}
        for key in ("absatz", "satz", "nummer", "buchstabe"):
            wanted["target_" + key] = [target[key]] if target.get(key) \
                else []
        for level, value in extra:
            known = [str(item).casefold() for item in wanted.get(level) or []]
            if not known:
                return "inside"
            if not any(value.contains(item) for item in known):
                return "disjoint"
        return "contains"
    return "inside"


def _timing_key(timing: dict, promulgated: str | None) -> tuple:
    day, status, reason = compute_timing(timing, promulgated)
    if day:
        return ("date", day)
    return ("unknown", reason, timing.get("type"), timing.get("phrase"))


def _applicable(rules: Iterable[dict], query: list[tuple[str, str]],
                target: dict | None = None
                ) -> tuple[list[tuple[dict, int, list]], list[tuple[dict, str]]]:
    applicable = []
    overlapping = []
    for rule in rules:
        scope = rule["scope"]
        holes = [path for path in scope["exclusions"]
                 if relation(path, query, target) in {"inside", "overlap"}]
        if any(relation(path, query, target) == "contains"
               for path in scope["exclusions"]):
            continue
        if rule["kind"] in {"default", "remainder"}:
            applicable.append((rule, 0, holes))
            continue
        if rule["kind"] == "unparsed":
            overlapping.append((rule, "unparsed"))
            continue
        relations = [(relation(path, query, target), len(path))
                     for path in scope["paths"]]
        containing = [length for rel, length in relations if rel == "contains"]
        if containing:
            applicable.append((rule, max(containing), holes))
        if any(rel in {"inside", "overlap"} for rel, _ in relations):
            overlapping.append((rule, "inside"))
        elif rule.get("ambiguity") == "scope_not_fully_parsed":
            if any(relation(context, query, target) in {"contains", "inside",
                                                        "overlap"}
                   for context in scope.get("garble_contexts") or [[]]):
                overlapping.append((rule, "ambiguous"))
        elif rule.get("ambiguity") and not containing:
            # A rule whose wording was not fully understood may have lost
            # items: it blocks every item of the articles it names (all
            # items if it names none).
            articles = _article_values(scope)
            if not articles or (query and query[0][0] == "article" and any(
                    Value.from_json(value).contains(query[0][1])
                    for value in articles)):
                overlapping.append((rule, "ambiguous"))
    return applicable, overlapping


def _article_values(scope: dict) -> list:
    return [path[0][1] for path in scope.get("paths") or []
            if path and path[0][0] == "article"]


def _partial_for(rule: dict, query: list[tuple[str, str]],
                 target: dict | None = None) -> list[dict]:
    rows = []
    for partial in rule["scope"].get("partial") or []:
        scope = partial.get("scope")
        if scope is None or relation(scope, query, target) in {
                "contains", "inside", "overlap"}:
            rows.append(partial)
    return rows


def _as_query(path: list) -> list[tuple[str, str]] | None:
    """A scope path as a query, if it names single items only."""
    query = []
    for level, value in _decode(path):
        if value.high is not None or level.startswith("target_"):
            return None
        query.append((level, value.low))
    return query


def _unknown(reason: str, **extra: Any) -> dict:
    return {"date": None, "status": "unknown", "basis": None, "clause": None,
            "retroactive": False, "reason": reason, "rule_ids": [],
            "evidence": [], **extra}


def _clause_evidence(rule: dict, meta: dict | None) -> dict:
    meta = meta or {}
    return {"kind": "commencement_clause", "snippet": rule["clause"],
            "start": rule["clause_start"], "end": rule["clause_end"],
            "document_id": meta.get("document_id"),
            "text_sha256": meta.get("text_sha256"),
            "pdf_sha256": meta.get("pdf_sha256")}


def _article_ref(parsed: dict, rule: dict) -> str:
    parts = []
    if parsed.get("article"):
        parts.append(f"Artikel {parsed['article']}")
    elif (parsed.get("located") or {}).get("para"):
        parts.append(f"§ {parsed['located']['para']}")
    if rule.get("paragraph"):
        parts.append(f"Absatz {rule['paragraph']}")
    return " ".join(parts) or "Inkrafttretensklausel"


def resolve_rules(parsed: dict, query: list[tuple[str, str]], *,
                  promulgated: str | None = None, event: str = "in_kraft",
                  meta: dict | None = None, target: dict | None = None
                  ) -> dict:
    """Resolve ``query`` against the parsed rules of one event kind."""
    rules = [rule for rule in parsed.get("rules") or []
             if rule["event"] == event]
    if not rules:
        return _unknown("no_commencement_rules" if event == "in_kraft" else
                        "not_stated")
    applicable, overlapping = _applicable(rules, query, target)
    if not applicable:
        return _unknown("no_rule_covers_item",
                        candidates=[rule["rule_id"] for rule, _ in
                                    overlapping])
    best_rank = max(rank for _, rank, _ in applicable)
    best = [(rule, holes) for rule, rank, holes in applicable
            if rank == best_rank]
    keys = {_timing_key(rule["timing"], promulgated) for rule, _ in best}
    rule_ids = [rule["rule_id"] for rule, _ in best]
    if len(keys) > 1:
        return _unknown("conflicting_rules", rule_ids=rule_ids,
                        clauses=[rule["clause"] for rule, _ in best])
    rule = best[0][0]
    holes = [hole for _, rule_holes in best for hole in rule_holes]
    key = next(iter(keys))
    # Parts of the queried item governed by other rules.
    others = []
    for other, _ in overlapping:
        if other in [row for row, _ in best]:
            continue
        other_key = _timing_key(other["timing"], promulgated)
        if other_key != key:
            others.append(other)
    if others or holes:
        parts = [{"rule_id": other["rule_id"],
                  "scope": other["scope"]["paths"],
                  "timing": other["timing"].get("phrase")}
                 for other in others]
        if holes and not others:
            # The excluded part falls to the next rule in line (usually the
            # whole-act default); the item is split only if that differs.
            best_ids = {row["rule_id"] for row, _ in best}
            for hole in holes:
                hole_query = _as_query(hole)
                if hole_query is None:
                    return _unknown("item_partly_excluded_by_clause",
                                    rule_ids=rule_ids,
                                    excluded=[path_label(_decode(h))
                                              for h in holes])
                fallback = resolve_rules(
                    {**parsed, "rules": [row for row in rules
                                         if row["rule_id"] not in best_ids]},
                    hole_query, promulgated=promulgated, event=event,
                    target=target)
                if fallback.get("date") is None or _timing_key(
                        {"type": "fixed", "date": fallback["date"]},
                        promulgated) != key:
                    return _unknown("item_split_by_exclusion",
                                    rule_ids=rule_ids,
                                    excluded=[path_label(_decode(h))
                                              for h in holes],
                                    excluded_part_date=fallback.get("date"))
        if others:
            return _unknown("item_split_across_rules", rule_ids=rule_ids,
                            parts=parts)
    partial = _partial_for(rule, query, target)
    if partial:
        return _unknown("partial_scope_clause", rule_ids=rule_ids,
                        qualifier=partial,
                        clause=rule["clause"])
    ambiguity = rule.get("ambiguity")
    if ambiguity == "scope_not_fully_parsed" and not any(
            relation(context, query, target) in {"contains", "inside",
                                                 "overlap"}
            for context in rule["scope"].get("garble_contexts") or [[]]):
        ambiguity = None
    if ambiguity:
        return _unknown(ambiguity, rule_ids=rule_ids, clause=rule["clause"])
    day, status, reason = compute_timing(rule["timing"], promulgated)
    evidence = [_clause_evidence(rule, meta)]
    if day is None:
        return {**_unknown(reason, rule_ids=rule_ids), "clause": rule["clause"],
                "evidence": evidence,
                "condition": rule["timing"].get("condition_text")}
    retroactive = (day < promulgated) if promulgated else bool(
        rule.get("retroactive_phrase"))
    scope_text = "gesamtes Gesetz" if rule["kind"] == "default" else (
        "im Übrigen" if rule["kind"] == "remainder" else
        "; ".join(path_label(_decode(path))
                  for path in rule["scope"]["paths"]
                  if relation(path, query, target) == "contains"))
    basis = (f"{_article_ref(parsed, rule)} ({scope_text}): "
             f"'{compact(rule['segment'])[:300]}'; "
             f"{_computation_basis(rule['timing'], promulgated)}")
    extra: dict[str, Any] = {}
    if rule["timing"].get("type") == "months_after" and promulgated:
        extra["computation_rule"] = "bgb_187_188_period"
        extra["alternative_reading"] = {
            "rule": "same_day_number_without_following_day",
            "date": (date.fromisoformat(day) - timedelta(days=1)).isoformat(),
            "note": "'n Monate nach der Verkündung' is read by some as the "
                    "day numbered like the promulgation day itself"}
    return {**extra, "date": day, "status": status, "basis": basis,
            "clause": rule["clause"], "retroactive": retroactive,
            "retroactive_phrase": bool(rule.get("retroactive_phrase")),
            "reason": None, "rule_ids": rule_ids,
            "specificity": best_rank, "rule_kind": rule["kind"],
            "timing": {k: v for k, v in rule["timing"].items()
                       if k not in {"start", "end"}},
            "evidence": evidence}


def resolve_in_force(parsed: dict, query: list[tuple[str, str]], *,
                     promulgated: str | None = None,
                     meta: dict | None = None,
                     target: dict | None = None) -> dict:
    if not parsed.get("rules"):
        return _unknown(parsed.get("reason") or "no_commencement_rules")
    return resolve_rules(parsed, query, promulgated=promulgated,
                         event="in_kraft", meta=meta, target=target)


def resolve_applies_from(parsed: dict, query: list[tuple[str, str]], *,
                         promulgated: str | None = None,
                         meta: dict | None = None,
                         target: dict | None = None) -> dict:
    """Application date only when the clause itself states one."""
    rules = [rule for rule in parsed.get("rules") or []
             if rule["event"] == "anwendung"]
    if not rules:
        return {"date": None, "status": "not_stated", "basis": None}
    applicable, overlapping = _applicable(rules, query, target)
    if not applicable and not overlapping:
        return {"date": None, "status": "not_stated", "basis": None}
    result = resolve_rules(parsed, query, promulgated=promulgated,
                           event="anwendung", meta=meta, target=target)
    if result["status"] == "unknown":
        result["status"] = "unknown"
        result.setdefault("reason", "application_condition")
        if result.get("reason") == "no_rule_covers_item":
            return {"date": None, "status": "not_stated", "basis": None}
        rule_ids = set(result.get("rule_ids") or [])
        conditions = [rule.get("application_condition") for rule in rules
                      if rule["rule_id"] in rule_ids]
        if conditions:
            result["condition"] = conditions[0]
    return result


# --------------------------------------------------------------- DIP


def dip_rules(rows: Iterable[dict]) -> dict:
    """DIP ``inkrafttreten`` rows as pseudo-rules with fixed dates."""
    rules = []
    for index, row in enumerate(rows or [], start=1):
        day = str(row.get("datum") or "")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            continue
        explanation = compact(row.get("erlaeuterung"))
        # DIP abbreviates long lists ("…", "u.a.", "verschiedene Artikel"):
        # such a row cannot show that an unlisted item falls elsewhere.
        # DIP also cuts the field at 250 characters mid-list.
        truncated = bool(re.search(
            r"\.\.\.|…|\bu\.\s?a\.|\bverschiedene\b|\busw\.|\betc\.",
            explanation, re.IGNORECASE)) or len(explanation) >= 240
        text = explanation
        if re.search(r"\b(?:tritt|treten)\b", text):
            match = VERB_RE.search(text)
            head = text[:match.start()]
            tail = text[match.end():]
            exception = EXCEPT_RE.search(tail)
            if exception:
                cut = re.search(r"\b(?:am|mit Wirkung|an dem)\b",
                                tail[exception.end():])
                head += " " + tail[:exception.end() + (
                    cut.start() if cut else len(tail))]
            text = head
        scope = parse_scope(_strip_scope_text(text))
        if not explanation:
            kind = "default"
        elif not scope.paths:
            kind = "remainder" if scope.remainder or re.search(
                r"übrig", explanation, re.I) else "unparsed"
        else:
            kind = "scoped"
        if kind == "unparsed":
            if truncated:
                rules.append({"rule_id": f"dip{index}", "event": "note",
                              "kind": "unparsed", "truncated": True,
                              "scope": scope.as_json(),
                              "timing": {"type": "fixed", "date": day,
                                         "phrase": explanation},
                              "clause": explanation, "clause_start": None,
                              "clause_end": None, "segment": explanation,
                              "paragraph": None, "sentence_index": index})
            continue
        rules.append({"rule_id": f"dip{index}", "event": "in_kraft",
                      "truncated": truncated,
                      "kind": kind, "scope": scope.as_json(),
                      "timing": {"type": "fixed", "date": day,
                                 "phrase": explanation or "(ohne Erläuterung)"},
                      "clause": explanation, "clause_start": None,
                      "clause_end": None, "segment": explanation,
                      "paragraph": None, "sentence_index": index})
    return {"schema": SCHEMA, "article": None, "rules": rules, "notes": []}


_COMPRESSED_RE = re.compile(r"(\b(?:Nr\.|Nummer)\s*)(\d+)([a-z])\b")


def _expand_compressed(rows: list[dict]) -> list[dict]:
    """DIP writes "Nr. 2b" for "Nr. 2 Buchst. b"; the printed act cannot.

    Only used as a second reading when the literal reading disagrees.
    """
    expanded = []
    for row in rows:
        text = str(row.get("erlaeuterung") or "")
        new = _COMPRESSED_RE.sub(r"\1\2 Buchst. \3", text)
        expanded.append({**row, "erlaeuterung": new} if new != text else row)
    return expanded


def dip_cross_check(dip_rows: Iterable[dict] | None,
                    query: list[tuple[str, str]], resolved: dict,
                    target: dict | None = None) -> dict:
    """Compare a printed-clause resolution with DIP's Inkrafttreten rows."""
    rows = list(dip_rows or [])
    if not rows:
        return {"source": "DIP inkrafttreten", "status": "not_available"}
    dip = resolve_rules(dip_rules(rows), query, target=target)
    notation = None
    if resolved.get("date") and dip.get("date") != resolved.get("date"):
        alternative = _expand_compressed(rows)
        if alternative != rows:
            second = resolve_rules(dip_rules(alternative), query,
                                   target=target)
            if second.get("date") == resolved.get("date"):
                dip = second
                notation = "dip_compressed_item_notation"
    finding = {"source": "DIP inkrafttreten", "dip_date": dip.get("date"),
               "dip_reason": dip.get("reason"),
               "dip_rows": [rows[int(rule_id[3:]) - 1]
                            for rule_id in dip.get("rule_ids") or []
                            if rule_id.startswith("dip")],
               "printed_date": resolved.get("date")}
    if notation:
        finding["dip_reading"] = notation
    truncated = [rule["rule_id"] for rule in dip_rules(rows)["rules"]
                 if rule.get("truncated")]
    if truncated:
        finding["dip_truncated_rules"] = truncated
    if dip.get("date") is None:
        finding["status"] = "dip_unresolved"
    elif truncated and resolved.get("date") != dip["date"]:
        # DIP elided part of its own list ("…"): only agreement is
        # informative; a difference may be an item DIP left out.
        finding["status"] = "dip_inconclusive_truncated"
    elif resolved.get("date") is None:
        finding["status"] = "printed_unknown"
    else:
        finding["status"] = ("agree" if dip["date"] == resolved["date"]
                             else "disagree")
    return finding
