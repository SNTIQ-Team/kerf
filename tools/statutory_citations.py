"""Deterministic current-text statutory citation/backlink index.

The input is the already parsed official GII/BAYERN.RECHT act detail corpus.
Only explicit ``§``/``§§`` and ``Art.``/``Artikel`` designators are extracted.
An unqualified designator is a same-act reference.  A cross-act edge is made
only when the immediately attached act name/abbreviation has an exact entry in
the alias registry.  Unknown or ambiguous aliases and missing target norms are
retained as unresolved evidence; this module never guesses with fuzzy text.

The result describes the current consolidated state observed at build time.
It is a citation graph, not a claim about legal effect or interpretation.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping


SCHEMA_VERSION = 1
MAX_ALIAS_WINDOW = 180
MAX_EXCERPT = 360
MAX_RANGE_EXPANSION = 200
BLOCK_BOUNDARY = "\x00"
READER_BLOCK_TAGS = frozenset({
    "p", "div", "ul", "ol", "li", "dl", "dt", "dd", "table", "thead",
    "tbody", "tr", "td", "th", "blockquote", "h1", "h2", "h3", "h4",
    "h5", "h6", "img", "source-file", "pre", "caption",
})

DESIGNATOR_RE = re.compile(
    r"^(?P<marker>§|Art(?:ikel)?\.?)\s*(?P<number>\d+[a-zA-Z]*)$",
    re.IGNORECASE,
)
CITATION_START_RE = re.compile(
    r"(?<![\w§])(?P<marker>§§|§|Art(?:ikel[sn]?)?\.?)\s*(?=\d)",
    re.IGNORECASE,
)
BASE_RE = re.compile(r"\d+[a-zA-Z]*")
PINPOINT_LABEL_RE = re.compile(
    r"\s+(?:Abs(?:atz|ätze)?\.?|Unterabs(?:atz|ätze)?\.?|Satz|Sätze|S\.|"
    r"Nrn?\.?|Nummer(?:n)?|(?:Doppel|Dreifach)?Buchst(?:aben?)?\.?|"
    r"Halbsatz|Alternative)\s*",
    re.IGNORECASE,
)
PINPOINT_VALUE_RE = re.compile(
    r"\d+[a-zA-Z]*(?!\w)"
    r"(?:\s*(?:,|bis|und|oder)\s*\d+[a-zA-Z]*(?!\w))*",
    re.IGNORECASE,
)
PINPOINT_SINGLE_VALUE_RE = re.compile(r"\d+[a-zA-Z]*(?!\w)")
PINPOINT_LETTER_STOPWORDS = (
    r"(?:der|die|das|den|dem|des|ein|und|oder|wie|mit|von|vom|zur|zum|für|"
    r"auf|aus|bei|bis|ist|als|im|in|am|an|zu|je|ob|es|er|sie)"
)
PINPOINT_LETTER_VALUES_RE = re.compile(
    rf"(?!{PINPOINT_LETTER_STOPWORDS}\b)[a-zA-Z]{{1,3}}(?!\w)"
    rf"(?:\s*(?:,|bis|und|oder)\s*(?!{PINPOINT_LETTER_STOPWORDS}\b)"
    r"[a-zA-Z]{1,3}(?!\w))*",
    re.IGNORECASE,
)
PINPOINT_LABEL_SEPARATOR_RE = re.compile(r"\s*(?:,|und|oder)(?=\s)", re.I)
CONNECTED_CITATION_RE = re.compile(
    r"\s*(?:in\s+Verbindung\s+mit|i\.\s*V\.\s*m\.|"
    r"(?:,|und|oder|sowie)(?:\s+die)?)\s*", re.I,
)
NORM_SEPARATOR_RE = re.compile(
    r"\s*(?P<separator>,|bis(?:\s+einschließlich)?|[-–—]|und|oder|sowie)\s*",
    re.IGNORECASE,
)
FULL_DATE_RE = re.compile(
    r"\d{1,2}\.\s*(?:Januar|Februar|März|April|Mai|Juni|Juli|August|"
    r"September|Oktober|November|Dezember|(?:0?[1-9]|1[0-2])\.)"
    r"\s*\d{4}(?!\d)", re.IGNORECASE,
)
ALIAS_PREFIX_RE = re.compile(
    r"^(?:(?:des|der|dem|den|die|das|nach|gemäß|in|im|von|vom|"
    r"dieses|dieser|diesem|dieser|nach\s+Maßgabe|im\s+Sinn(?:e)?)\s+|"
    r"[\s,()\-–—])*$",
    re.IGNORECASE,
)
UNKNOWN_ALIAS_PREFIX_RE = re.compile(
    r"^(?:(?:des|der|dem|den|die|das|nach|gemäß|in|im|von|vom|"
    r"nach\s+Maßgabe|im\s+Sinn(?:e)?)\s+|[\s,()\-–—])*",
    re.IGNORECASE,
)
UNKNOWN_LONG_ALIAS_RE = re.compile(
    r"(?P<alias>(?:[A-ZÄÖÜ][\wÄÖÜäöüß/.-]*\s+){0,7}"
    r"(?:[A-ZÄÖÜ][\wÄÖÜäöüß/.-]*?"
    r"(?:gesetz(?:es)?|gesetzbuch(?:es)?|verordnung(?:en)?|ordnung(?:en)?)|"
    r"Gesetz(?:es)?|Gesetzbuch(?:es)?|Verordnung(?:en)?|Ordnung(?:en)?))"
)
UNKNOWN_SHORT_ALIAS_RE = re.compile(
    r"(?P<alias>(?=[A-Za-zÄÖÜäöüß0-9/.-]{2,40}(?:\s|$))"
    r"(?:[A-ZÄÖÜ]{2,12}|"
    r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß0-9/.-]*[A-ZÄÖÜ]"
    r"[A-Za-zÄÖÜäöüß0-9/.-]*)(?:\s+[IVXLCDM]+)?)"
)
EU_INSTRUMENT_ALIAS_RE = re.compile(
    r"(?:Verordnung|Richtlinie)\s*"
    r"(?:(?:\((?:EU|EG|EWG)\)\s*(?:Nr\.?\s*)?"
    r"\d{1,4}/\d{1,4})|(?:\d{4}/\d{1,4}/(?:EU|EG|EWG)))"
    r"(?![\w/])",
    re.IGNORECASE,
)
EU_INSTRUMENT_PREFIX_RE = re.compile(
    r"(?:Verordnung|Richtlinie)\s*\((?:EU|EG|EWG)\)", re.IGNORECASE,
)
ORDINAL_BOOK_RE = re.compile(
    r"(?:Ersten|Zweiten|Dritten|Vierten|Fünften|Sechsten|Siebten|Achten|"
    r"Neunten|Zehnten|Elften|Zwölften|Dreizehnten|Vierzehnten)\s+Buches\b",
    re.IGNORECASE,
)


# This is a closed alias registry, not a fuzzy title table.  Targets are the
# exact JurAbk values used by the current corpus.  Entries remain useful when a
# cited act is outside the curated corpus: the mention is then explicitly
# exported as unresolved instead of silently becoming a same-act link.
EXPLICIT_ALIAS_TARGETS: dict[str, str] = {
    "AufenthG": "AufenthG 2004",
    "Aufenthaltsgesetz": "AufenthG 2004",
    "Aufenthaltsgesetzes": "AufenthG 2004",
    "AsylG": "AsylVfG 1992",
    "Asylgesetz": "AsylVfG 1992",
    "Asylgesetzes": "AsylVfG 1992",
    "BeschV": "BeschV 2013",
    "Beschäftigungsverordnung": "BeschV 2013",
    "FreizügG/EU": "FreizügG/EU 2004",
    "Freizügigkeitsgesetz/EU": "FreizügG/EU 2004",
    "Staatsangehörigkeitsgesetz": "StAG",
    "Staatsangehörigkeitsgesetzes": "StAG",
    "Asylbewerberleistungsgesetz": "AsylbLG",
    "Asylbewerberleistungsgesetzes": "AsylbLG",
    "Grundgesetz": "GG",
    "Grundgesetzes": "GG",
    "Bürgerliches Gesetzbuch": "BGB",
    "Bürgerlichen Gesetzbuchs": "BGB",
    "Bayerisches Verwaltungsverfahrensgesetz": "BayVwVfG",
    "Bayerischen Verwaltungsverfahrensgesetzes": "BayVwVfG",
    "Bayerisches Polizeiaufgabengesetz": "PAG",
    "Bayerischen Polizeiaufgabengesetzes": "PAG",
    "Bayerisches Integrationsgesetz": "BayIntG",
    "Bayerischen Integrationsgesetzes": "BayIntG",
    "Bayerisches Erziehungs- und Unterrichtsgesetz": "BayEUG",
    "Bayerischen Erziehungs- und Unterrichtsgesetzes": "BayEUG",
    "Gemeindeordnung": "GO",
    "Gemeindeordnung für den Freistaat Bayern": "GO",
    "GO": "GO",
    "Landkreisordnung": "LKrO",
    "LKrO": "LKrO",
    "Bezirksordnung": "BezO",
    "BezO": "BezO",
    "Finanzausgleichsgesetz": "FAG",
    "Finanzausgleichsgesetzes": "FAG",
    "FAG": "FAG",
}

_SGB_ALIASES = (
    ("I", "Erstes", "Ersten", "SGB 1"),
    ("II", "Zweites", "Zweiten", "SGB 2"),
    ("III", "Drittes", "Dritten", "SGB 3"),
    ("IV", "Viertes", "Vierten", "SGB 4"),
    ("V", "Fünftes", "Fünften", "SGB 5"),
    ("VI", "Sechstes", "Sechsten", "SGB 6"),
    ("VII", "Siebtes", "Siebten", "SGB 7"),
    ("VIII", "Achtes", "Achten", "SGB 8"),
    ("IX", "Neuntes", "Neunten", "SGB 9 2018"),
    ("X", "Zehntes", "Zehnten", "SGB 10"),
    ("XI", "Elftes", "Elften", "SGB 11"),
    ("XII", "Zwölftes", "Zwölften", "SGB 12"),
    ("XIII", "Dreizehntes", "Dreizehnten", "SGB 13"),
    ("XIV", "Vierzehntes", "Vierzehnten", "SGB 14"),
)
for _roman, _nominative, _genitive, _target in _SGB_ALIASES:
    EXPLICIT_ALIAS_TARGETS.update({
        f"SGB {_roman}": _target,
        f"{_nominative} Buch Sozialgesetzbuch": _target,
        f"{_genitive} Buches Sozialgesetzbuch": _target,
        f"Sozialgesetzbuch {_nominative} Buch": _target,
    })


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def _key(value: object) -> str:
    return _clean(value).casefold()


def canonical_norm(value: object) -> str | None:
    """Canonicalize punctuation/spacing, without approximate matching."""
    cleaned = _clean(value)
    match = DESIGNATOR_RE.fullmatch(cleaned)
    if not match:
        return None
    marker = "§" if match.group("marker").startswith("§") else "Art."
    return f"{marker} {match.group('number').casefold()}"


def citation_filter_key(value: object) -> str:
    """Exact API/SQLite key with typography-only norm normalization."""
    cleaned = _clean(value).casefold()
    cleaned = re.sub(r"^(?:artikel|art)\.?\s*", "art. ", cleaned)
    cleaned = re.sub(r"^§\s*", "§ ", cleaned)
    return cleaned


@dataclass(frozen=True)
class TargetSpec:
    start: str
    end: str | None = None
    pinpoint: str | None = None


@dataclass(frozen=True)
class CitationGroup:
    start: int
    end: int
    marker: str
    raw: str
    specs: tuple[TargetSpec, ...]


@dataclass(frozen=True)
class AliasMatch:
    text: str
    target_jurabks: tuple[str, ...]
    end: int
    known: bool


def _pinpoint_level(label: str) -> int:
    value = label.strip().casefold().rstrip(".")
    if value.startswith(("abs", "unterabs")):
        return 1
    if value in {"satz", "sätze", "s"}:
        return 2
    if value == "halbsatz":
        return 3
    if value.startswith(("nr", "nummer")):
        return 4
    if value.startswith("buchst"):
        return 5
    return 6


def _parse_pinpoints(text: str, position: int, *,
                     plural_heads: bool = False,
                     require_another_head: bool = False,
                     ) -> tuple[int, str | None]:
    start = position
    while True:
        label = PINPOINT_LABEL_RE.match(text, position)
        if not label and position > start:
            separator = PINPOINT_LABEL_SEPARATOR_RE.match(text, position)
            if separator:
                label = PINPOINT_LABEL_RE.match(text, separator.end())
        if not label:
            break
        # A plural section designator introduces a list of section heads.
        # Bare numeric continuations therefore belong to that list; repeated
        # pinpoint labels remain attached to the current head.  Singular
        # sections retain ordinary Absatz/Satz lists.  Letter lists cannot
        # be numeric section heads and remain together in either case.
        if re.match(r"\s*(?:Doppel|Dreifach)?Buchst", label.group(), re.I):
            value_pattern = PINPOINT_LETTER_VALUES_RE
        elif require_another_head and not re.match(
                r"\s*(?:Absätze|Unterabsätze|Sätze|Nummern|Nrn)\b",
                label.group(), re.I):
            value_pattern = PINPOINT_SINGLE_VALUE_RE
        else:
            value_pattern = PINPOINT_VALUE_RE
        value = value_pattern.match(text, label.end())
        if not value:
            break
        value_end = value.end()
        following_label = PINPOINT_LABEL_RE.match(text, value_end)
        if plural_heads and value_pattern is PINPOINT_VALUE_RE \
                and following_label and _pinpoint_level(
                    following_label.group()) <= _pinpoint_level(label.group()):
            # A following list item carrying its own pinpoint starts a new
            # section head.  A plain trailing Absatz list stays attached to
            # the established head ("§§ 23 und 24 Abs. 1 und 2").
            items = list(re.finditer(
                r"\s*(?:,|und|oder|sowie)\s*\d+[a-zA-Z]*$",
                value.group(), re.I))
            if items:
                value_end = value.start() + items[-1].start()
        position = value_end
    if position == start:
        return position, None
    return position, _clean(text[start:position])


def parse_citation_groups(text: str) -> list[CitationGroup]:
    """Parse explicit norm groups; standalone Absatz/Satz is out of scope."""
    groups: list[CitationGroup] = []
    search_from = 0
    while True:
        match = CITATION_START_RE.search(text, search_from)
        if not match:
            break
        marker = "§" if match.group("marker").startswith("§") else "Art."
        first = BASE_RE.match(text, match.end())
        if not first:
            search_from = match.end()
            continue
        first_norm = f"{marker} {first.group().casefold()}"
        plural_heads = match.group("marker") == "§§"
        position, pinpoint_tail = _parse_pinpoints(
            text, first.end(), plural_heads=plural_heads,
            require_another_head=plural_heads)
        specs = [TargetSpec(first_norm, pinpoint=(
            f"{first_norm} {pinpoint_tail}" if pinpoint_tail else None))]
        while True:
            separator = NORM_SEPARATOR_RE.match(text, position)
            if not separator:
                break
            following_at = separator.end()
            repeated_marker = CITATION_START_RE.match(text, following_at)
            if repeated_marker:
                repeated_kind = "§" if repeated_marker.group(
                    "marker").startswith("§") else "Art."
                if repeated_kind != marker:
                    break
                following_at = repeated_marker.end()
            elif FULL_DATE_RE.match(text, following_at):
                # A temporal qualifier such as "§ 176 bis einschließlich
                # 31. Dezember 2012" does not introduce another norm head.
                break
            following = BASE_RE.match(text, following_at)
            if not following:
                break
            following_norm = f"{marker} {following.group().casefold()}"
            following_end, following_pinpoint = _parse_pinpoints(
                text, following.end(), plural_heads=plural_heads)
            word = separator.group("separator").casefold()
            if word.startswith("bis") or word in {"-", "–", "—"}:
                previous = specs[-1]
                if previous.end is not None or previous.pinpoint:
                    break
                specs[-1] = TargetSpec(previous.start, following_norm, (
                    f"{following_norm} {following_pinpoint}"
                    if following_pinpoint else None))
            else:
                specs.append(TargetSpec(following_norm, pinpoint=(
                    f"{following_norm} {following_pinpoint}"
                    if following_pinpoint else None)))
            position = following_end
        groups.append(CitationGroup(
            start=match.start(), end=position, marker=marker,
            raw=_clean(text[match.start():position]), specs=tuple(specs),
        ))
        # When a repeated marker was consumed (``§ 1 und § 2 SGB II``), do
        # not emit a duplicate group for its second head.  Otherwise the next
        # search still finds an independent later citation and its own alias.
        search_from = max(position, match.end())
    return groups


def _alias_registry(
        acts: list[dict],
        explicit_aliases: Mapping[str, str] | None = None,
        ) -> tuple[dict[str, set[str]], dict[str, set[str]], list[tuple[str, re.Pattern]]]:
    targets: dict[str, set[str]] = {}
    forms: dict[str, set[str]] = {}

    def add(alias: object, target: object) -> None:
        alias_text, target_text = _clean(alias), _clean(target)
        if not alias_text or not target_text:
            return
        alias_key = _key(alias_text)
        targets.setdefault(alias_key, set()).add(target_text)
        forms.setdefault(alias_key, set()).add(alias_text)

    for act in acts:
        jurabk = act["jurabk"]
        add(jurabk, jurabk)
        add(act.get("title"), jurabk)
    for alias, target in EXPLICIT_ALIAS_TARGETS.items():
        add(alias, target)
    for alias, target in (explicit_aliases or {}).items():
        add(alias, target)

    patterns: list[tuple[str, re.Pattern]] = []
    for alias_key, aliases in forms.items():
        for alias in aliases:
            pattern = re.compile(
                rf"(?<![\w/]){re.escape(alias)}(?![\w/])",
                re.IGNORECASE,
            )
            patterns.append((alias_key, pattern))
    patterns.sort(key=lambda item: len(item[1].pattern), reverse=True)
    return targets, forms, patterns


def _alias_segment(text: str, group: CitationGroup) -> tuple[str, int]:
    end = min(len(text), group.end + MAX_ALIAS_WINDOW)
    next_marker = CITATION_START_RE.search(text, group.end)
    if next_marker:
        end = min(end, next_marker.start())
    for separator in (";", ".", ":", "\n", BLOCK_BOUNDARY):
        found = text.find(separator, group.end, end)
        if found >= 0:
            end = min(end, found)
    return text[group.end:end], group.end


def _match_alias(text: str, group: CitationGroup,
                 targets: dict[str, set[str]],
                 patterns: list[tuple[str, re.Pattern]],
                 source_jurabk: str = "") -> AliasMatch | None:
    # Inspect explicit foreign instruments before sentence segmentation: the
    # period in "Nr." is part of an EU identifier, not a sentence boundary.
    raw_segment = text[group.end:group.end + MAX_ALIAS_WINDOW]
    prefix = UNKNOWN_ALIAS_PREFIX_RE.match(raw_segment)
    prefix_end = prefix.end() if prefix else 0
    eu = (EU_INSTRUMENT_ALIAS_RE.match(raw_segment, prefix_end)
          or EU_INSTRUMENT_PREFIX_RE.match(raw_segment, prefix_end))
    if eu:
        alias = _clean(eu.group())
        return AliasMatch(alias, (alias,), group.end + eu.end(), False)

    segment, absolute_start = _alias_segment(text, group)
    candidates: list[tuple[int, int, str, re.Match]] = []
    for alias_key, pattern in patterns:
        found = pattern.search(segment)
        if not found:
            continue
        prefix = segment[:found.start()]
        parenthesized = (found.start() > 0 and segment[found.start() - 1] == "("
                         and found.end() < len(segment)
                         and segment[found.end()] == ")")
        if parenthesized or ALIAS_PREFIX_RE.fullmatch(prefix):
            candidates.append((found.start(), -len(found.group()),
                               alias_key, found))
    if candidates:
        _, _, alias_key, found = min(candidates)
        alias_end = found.end()
        # Include the closing delimiter of a matched parenthesized alias,
        # but leave an enclosing prose parenthesis outside the citation.
        if found.start() > 0 and segment[found.start() - 1] == "(" \
                and segment[alias_end:alias_end + 1] == ")":
            alias_end += 1
        return AliasMatch(
            text=_clean(found.group()),
            target_jurabks=tuple(sorted(targets[alias_key], key=str.casefold)),
            end=absolute_start + alias_end,
            known=True,
        )

    # A leading law-shaped token is evidence of a cross-act citation even when
    # it is not in the closed alias table.  Export it unresolved; do not fall
    # back to a plausible-looking same-act norm.
    prefix = UNKNOWN_ALIAS_PREFIX_RE.match(segment)
    position = prefix.end() if prefix else 0
    remainder = segment[position:]
    book = ORDINAL_BOOK_RE.match(remainder)
    if book:
        alias = _clean(book.group())
        target = alias
        if re.match(r"^SGB\s", source_jurabk, re.IGNORECASE):
            target = next((target for _, _, genitive, target in _SGB_ALIASES
                           if alias.casefold() ==
                           f"{genitive} Buches".casefold()), alias)
        return AliasMatch(
            alias, (target,), absolute_start + position + book.end(),
            target != alias,
        )
    if re.match(r"(?:dies(?:es|er|em)|vorliegend(?:es|er|em))\s+"
                r"(?:Gesetz(?:es)?|Verordnung)", remainder, re.IGNORECASE):
        return None
    unknown = UNKNOWN_LONG_ALIAS_RE.match(remainder) \
        or UNKNOWN_SHORT_ALIAS_RE.match(remainder)
    if not unknown:
        return None
    alias = _clean(unknown.group("alias"))
    if alias.casefold() in {"gesetz", "gesetzes", "verordnung"}:
        return None
    return AliasMatch(
        text=alias,
        target_jurabks=(alias,),
        end=absolute_start + position + unknown.end(),
        known=False,
    )


def _excerpt(text: str, start: int, end: int) -> str:
    left = max(0, start - 100)
    right = min(len(text), max(end, start + 1) + 180)
    value = _clean(text[left:right])
    if len(value) > MAX_EXCERPT:
        value = value[:MAX_EXCERPT - 1].rstrip() + "…"
    if left:
        value = "…" + value
    if right < len(text) and not value.endswith("…"):
        value += "…"
    return value


def _citation_id(key: tuple) -> str:
    payload = json.dumps(key, ensure_ascii=False, separators=(",", ":"))
    return "cite:" + hashlib.sha256(payload.encode()).hexdigest()[:24]


def _structured_parse_body(text: str, nodes: list[dict]
                           ) -> tuple[str, list[int], list[dict]]:
    """Insert parser-only boundaries at verified source block edges.

    Inline runs stay contiguous. The positions let every exported quote and
    excerpt refer to the original canonical text, never the temporary copy.
    """
    chunks: list[str] = []
    boundaries: set[int] = set()
    raw_scopes: list[tuple[int, int, str]] = []
    position = 0

    def leading_inline(children: list[dict]) -> str:
        result: list[str] = []
        for child in children:
            if child["type"] == "text":
                result.append(child["text"])
            elif child["tag"] in READER_BLOCK_TAGS:
                break
            else:
                result.append(leading_inline(child["children"]))
        return "".join(result)

    def visit(children: list[dict]) -> None:
        nonlocal position
        for node in children:
            if node["type"] == "text":
                chunks.append(node["text"])
                position += len(node["text"])
            else:
                block = (node["tag"] in READER_BLOCK_TAGS
                         or node.get("attrs", {}).get("role") in {"sentence", "footnote"})
                start = position
                if block:
                    boundaries.add(position)
                visit(node["children"])
                if block:
                    boundaries.add(position)
                if node["tag"] in {"dd", "li"}:
                    heading = re.match(
                        r"\s*(?:aus|nach)\s+(?:dem|der)\s+"
                        r"(?P<law>[^:\n]{1,180}):",
                        leading_inline(node["children"]), re.I)
                    if heading:
                        raw_scopes.append((start, position, heading.group("law")))

    visit(nodes)
    raw = "".join(chunks)
    # Sidecar loading already checked the canonical hash and complete text.
    # XML indentation can differ from canonical whitespace normalization.
    if re.sub(r"\s+", "", raw) != re.sub(r"\s+", "", text):
        return text, [], []
    mapped: set[int] = set()
    offsets: dict[int, int] = {}
    raw_at = text_at = 0
    for boundary in sorted(boundaries):
        while raw_at < boundary:
            if raw[raw_at].isspace():
                while raw_at < len(raw) and raw[raw_at].isspace():
                    raw_at += 1
                while text_at < len(text) and text[text_at].isspace():
                    text_at += 1
            else:
                while text_at < len(text) and text[text_at].isspace():
                    text_at += 1
                if text_at >= len(text) or raw[raw_at] != text[text_at]:
                    return text, [], []
                raw_at += 1
                text_at += 1
        if 0 < text_at < len(text):
            mapped.add(text_at)
        offsets[boundary] = text_at
    output: list[str] = []
    inserted: list[int] = []
    previous = 0
    for offset in sorted(mapped):
        output.extend((text[previous:offset], BLOCK_BOUNDARY))
        inserted.append(offset + len(inserted))
        previous = offset
    output.append(text[previous:])
    scopes = [{"start": offsets[start], "end": offsets[end], "law": law}
              for start, end, law in raw_scopes]
    return "".join(output), inserted, scopes


def _act_inventory(acts: Iterable[dict], reader_directory: Path | None = None,
                   ) -> tuple[list[dict], dict[str, list[dict]]]:
    inventory: list[dict] = []
    by_jurabk: dict[str, list[dict]] = {}
    for source in acts:
        act_id = _clean(source.get("id"))
        jurabk = _clean(source.get("jurabk"))
        if not act_id or not jurabk:
            continue
        structured: dict = {}
        indexed_structure: dict = {}
        if reader_directory is not None:
            from tools.reader_blocks import ReaderBlocksError, load_sidecar
            try:
                sidecar = load_sidecar(reader_directory, source)
                structured = sidecar["norms"]
                indexed_structure = sidecar.get("norms_by_index", {})
            except ReaderBlocksError:
                pass
        norm_order: list[str] = []
        norm_set: set[str] = set()
        norm_variants: dict[str, set[tuple[str, str]]] = {}
        norms: list[dict] = []
        for source_index, raw_norm in enumerate(source.get("norms") or []):
            source_designator = _clean(raw_norm.get("enbez"))
            source_norm = canonical_norm(source_designator) or source_designator
            if not source_norm:
                continue
            text = str(raw_norm.get("text") or "")
            row = {"norm": source_norm, "text": text}
            structure = (indexed_structure.get(str(source_index))
                         or structured.get(str(raw_norm.get("enbez") or "")))
            if structure is not None:
                parse_text, inserted, scopes = _structured_parse_body(
                    text, structure["blocks"])
                row.update(parse_text=parse_text, inserted=inserted,
                           scopes=scopes, structured=True)
            norms.append(row)
            target_designator = canonical_norm(source_designator)
            if target_designator:
                norm_variants.setdefault(target_designator, set()).add(
                    (text, _clean(raw_norm.get("glied") or raw_norm.get("gliederung"))))
            if target_designator and target_designator not in norm_set:
                norm_set.add(target_designator)
                norm_order.append(target_designator)
        act = {
            "id": act_id,
            "jurabk": jurabk,
            "juris": _clean(source.get("juris")),
            "title": _clean(source.get("title")),
            "norms": norms,
            "norm_order": norm_order,
            "norm_set": norm_set,
            "ambiguous_norms": {name for name, variants in norm_variants.items()
                                if len(variants) > 1},
            "norm_position": {norm: index
                              for index, norm in enumerate(norm_order)},
            "norm_markers": {norm.split(" ", 1)[0] for norm in norm_order},
        }
        inventory.append(act)
        by_jurabk.setdefault(_key(jurabk), []).append(act)
    inventory.sort(key=lambda act: act["id"])
    return inventory, by_jurabk


def _range_targets(spec: TargetSpec, target_act: dict
                   ) -> tuple[list[tuple[str, str | None]], str | None]:
    if spec.end is None:
        if spec.start not in target_act["norm_set"]:
            return [], "target_norm_not_in_current_corpus"
        if spec.start in target_act["ambiguous_norms"]:
            return [], "ambiguous_target_norm"
        return [(spec.start, spec.pinpoint)], None
    positions = target_act["norm_position"]
    if spec.start not in positions or spec.end not in positions:
        return [], "range_endpoint_not_in_current_corpus"
    first, last = positions[spec.start], positions[spec.end]
    if first > last:
        return [], "range_order_invalid"
    selected = target_act["norm_order"][first:last + 1]
    if any(norm in target_act["ambiguous_norms"] for norm in selected):
        return [], "range_contains_ambiguous_target_norm"
    if len(selected) > MAX_RANGE_EXPANSION:
        return [], "range_too_broad"
    return [(norm, spec.pinpoint if norm == spec.end else None)
            for norm in selected], None


def _unresolved_designator(spec: TargetSpec) -> str:
    if spec.end:
        return f"{spec.start} bis {spec.end}"
    return spec.pinpoint or spec.start


def build_citation_index(acts: Iterable[dict], *, built_at: str,
                         explicit_aliases: Mapping[str, str] | None = None,
                         source_snapshots: Mapping[str, str] | None = None,
                         reader_directory: Path | None = None,
                         ) -> dict:
    """Build a stable outbound/inbound index from current official text."""
    inventory, by_jurabk = _act_inventory(acts, reader_directory)
    alias_targets, _, alias_patterns = _alias_registry(
        inventory, explicit_aliases)
    deduped: dict[tuple, dict] = {}
    source_norms_scanned = 0
    structured_source_norms = 0
    structure_boundaries = 0
    contextual_citation_groups = 0

    for source_act in inventory:
        for source_norm_row in source_act["norms"]:
            source_norms_scanned += 1
            original = source_norm_row["text"]
            body = source_norm_row.get("parse_text", original)
            inserted = source_norm_row.get("inserted", [])
            structured_source_norms += bool(source_norm_row.get("structured"))
            structure_boundaries += len(inserted)
            groups = parse_citation_groups(body)
            aliases = [_match_alias(
                    body, group, alias_targets, alias_patterns,
                    source_act["jurabk"]) for group in groups]
            for index, group in enumerate(groups):
                if aliases[index] is not None:
                    continue
                source_start = group.start - bisect_left(inserted, group.start)
                scopes = [scope for scope in source_norm_row.get("scopes", [])
                          if scope["start"] <= source_start < scope["end"]]
                if not scopes:
                    continue
                scope = min(scopes, key=lambda item: item["end"] - item["start"])
                heading_text = " des " + scope["law"]
                named = _match_alias(
                    heading_text, CitationGroup(0, 0, "§", "", ()),
                    alias_targets, alias_patterns, source_act["jurabk"])
                if named is None and not (UNKNOWN_LONG_ALIAS_RE.match(scope["law"])
                                           or UNKNOWN_SHORT_ALIAS_RE.fullmatch(scope["law"])):
                    continue
                # An explicitly named list scope is foreign evidence even if
                # it is outside the alias registry or names multiple laws.
                if named is None or named.end != len(heading_text):
                    named = AliasMatch(scope["law"], (scope["law"],), 0, False)
                aliases[index] = AliasMatch(named.text, named.target_jurabks,
                                            group.end, named.known)
                contextual_citation_groups += 1
            # In a syntactically connected citation chain, a trailing law
            # names the preceding unqualified heads as well.  Never cross
            # arbitrary prose, punctuation, or an independently named law.
            for index in range(len(groups) - 2, -1, -1):
                following_alias = aliases[index + 1]
                bridge = body[groups[index].end:groups[index + 1].start]
                if aliases[index] is None and following_alias is not None \
                        and CONNECTED_CITATION_RE.fullmatch(bridge):
                    aliases[index] = AliasMatch(
                        following_alias.text, following_alias.target_jurabks,
                        groups[index].end, following_alias.known)
            for group, alias in zip(groups, aliases):
                alias_end = alias.end if alias else group.end
                source_start = group.start - bisect_left(inserted, group.start)
                source_end = alias_end - bisect_left(inserted, alias_end)
                citation_text = _clean(original[source_start:source_end])
                excerpt = _excerpt(original, source_start, source_end)

                if alias is None:
                    target_candidates = [source_act]
                    target_jurabk = source_act["jurabk"]
                    alias_reason = None if group.marker in source_act[
                        "norm_markers"] else "unqualified_foreign_marker"
                elif len(alias.target_jurabks) != 1:
                    target_candidates = []
                    target_jurabk = " | ".join(alias.target_jurabks)
                    alias_reason = "ambiguous_explicit_alias"
                else:
                    target_jurabk = alias.target_jurabks[0]
                    target_candidates = by_jurabk.get(_key(target_jurabk), [])
                    if len(target_candidates) > 1:
                        alias_reason = "ambiguous_target_act"
                        target_candidates = []
                    elif not target_candidates:
                        alias_reason = "target_act_not_in_current_corpus"
                    else:
                        alias_reason = None

                resolved_target = target_candidates[0] \
                    if len(target_candidates) == 1 else None
                kind = "self" if (resolved_target is not None
                                  and resolved_target["id"] == source_act["id"]
                                  ) else "cross_act"
                if alias is None:
                    kind = "self"

                for spec in group.specs:
                    targets: list[tuple[str, str | None]] = []
                    reason = alias_reason
                    if resolved_target is not None:
                        targets, norm_reason = _range_targets(
                            spec, resolved_target)
                        reason = reason or norm_reason
                    if not targets:
                        targets = [(_unresolved_designator(spec),
                                    spec.pinpoint)]

                    for target_norm, target_pinpoint in targets:
                        status = "resolved" if reason is None else "unresolved"
                        target_act_id = resolved_target["id"] \
                            if resolved_target is not None else None
                        target_act_jurabk = resolved_target["jurabk"] \
                            if resolved_target is not None else target_jurabk
                        key = (
                            source_act["id"], source_norm_row["norm"],
                            target_act_id, target_act_jurabk, target_norm,
                            target_pinpoint, kind, status, reason,
                            # Each exact source group needs all its target rows.
                            # An earlier single-head mention must not consume
                            # the evidence for that head in a later compound.
                            citation_text,
                        )
                        existing = deduped.get(key)
                        if existing:
                            existing["occurrence_count"] += 1
                            if alias and alias.text not in existing[
                                    "matched_aliases"]:
                                existing["matched_aliases"].append(alias.text)
                                existing["matched_aliases"].sort(
                                    key=str.casefold)
                            continue
                        deduped[key] = {
                            "id": _citation_id(key),
                            "status": status,
                            "unresolved_reason": reason,
                            "kind": kind,
                            "source_act": source_act["id"],
                            "source_jurabk": source_act["jurabk"],
                            "source_norm": source_norm_row["norm"],
                            "source_excerpt": excerpt,
                            "source_snapshot": (source_snapshots or {}).get(
                                source_act["juris"]),
                            "date_basis": (
                                "current_consolidated_snapshot_observation_"
                                "not_legal_effect"),
                            "citation_text": citation_text,
                            "matched_aliases": [alias.text] if alias else [],
                            "target_act": target_act_id,
                            "target_jurabk": target_act_jurabk,
                            "target_norm": target_norm,
                            "target_pinpoint": target_pinpoint,
                            "occurrence_count": 1,
                            "machine_extracted": True,
                            "current_state_only": True,
                            "legal_interpretation": "not_asserted",
                        }

    rows = sorted(deduped.values(), key=lambda row: (
        row["source_act"], row["source_norm"], row["target_jurabk"] or "",
        row["target_norm"] or "", row["target_pinpoint"] or "", row["id"],
    ))
    counts = {
        "total": len(rows),
        "resolved": sum(row["status"] == "resolved" for row in rows),
        "unresolved": sum(row["status"] == "unresolved" for row in rows),
        "self": sum(row["kind"] == "self" for row in rows),
        "cross_act": sum(row["kind"] == "cross_act" for row in rows),
        "acts_scanned": len(inventory),
        "source_norms_scanned": source_norms_scanned,
        "structured_source_norms": structured_source_norms,
        "structure_boundaries": structure_boundaries,
        "contextual_citation_groups": contextual_citation_groups,
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "built_at": built_at,
        "machine_extracted": True,
        "current_state_only": True,
        "legal_interpretation": "not_asserted",
        "source_policy": {
            "official_current_text_only": True,
            "inputs": ["GII", "BAYERN.RECHT"],
            "cross_act_resolution": "exact_explicit_alias_only",
            "fuzzy_matching": False,
            "range_expansion": "existing_target_norm_order_only",
            "standalone_absatz_or_satz_references": "not_extracted",
            "unresolved_mentions_retained": True,
            "source_block_boundaries": "hash_verified_reader_sidecar_when_available",
        },
        "source_snapshots": dict(sorted((source_snapshots or {}).items())),
        "counts": counts,
        "citations": rows,
    }


def citation_manifest(index: Mapping[str, object]) -> dict:
    """Return the small JSON envelope; citation rows live only in SQLite."""
    manifest = {
        key: value for key, value in index.items() if key != "citations"
    }
    counts = manifest.get("counts") or {}
    manifest["storage"] = {
        "format": "sqlite3",
        "file": "citations.sqlite",
        "table": "citation",
        "rows": int(counts.get("total") or 0),
        "ordering": "ordinal",
        "read_only": True,
    }
    return manifest


def write_citation_database(output: Path, index: Mapping[str, object]) -> None:
    """Write a deterministic, atomically replaced exact citation index."""
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(output.name + ".tmp")
    tmp.unlink(missing_ok=True)
    rows = index.get("citations") or []
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript("""
            PRAGMA page_size = 4096;
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            PRAGMA temp_store = MEMORY;
            PRAGMA application_id = 0x4c584354;
            PRAGMA user_version = 1;

            CREATE TABLE citation_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;

            CREATE TABLE citation (
                ordinal INTEGER PRIMARY KEY,
                id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL
                    CHECK (status IN ('resolved', 'unresolved')),
                unresolved_reason TEXT,
                kind TEXT NOT NULL CHECK (kind IN ('self', 'cross_act')),
                source_act TEXT NOT NULL,
                source_act_key TEXT NOT NULL,
                source_jurabk TEXT NOT NULL,
                source_jurabk_key TEXT NOT NULL,
                source_norm TEXT NOT NULL,
                source_norm_key TEXT NOT NULL,
                source_excerpt TEXT NOT NULL,
                source_snapshot TEXT,
                date_basis TEXT NOT NULL,
                citation_text TEXT NOT NULL,
                matched_aliases_json TEXT NOT NULL,
                target_act TEXT,
                target_act_key TEXT NOT NULL,
                target_jurabk TEXT NOT NULL,
                target_jurabk_key TEXT NOT NULL,
                target_norm TEXT NOT NULL,
                target_norm_key TEXT NOT NULL,
                target_pinpoint TEXT,
                target_pinpoint_key TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL CHECK (occurrence_count > 0),
                machine_extracted INTEGER NOT NULL
                    CHECK (machine_extracted IN (0, 1)),
                current_state_only INTEGER NOT NULL
                    CHECK (current_state_only IN (0, 1)),
                legal_interpretation TEXT NOT NULL
            );

            CREATE INDEX citation_source_act
                ON citation(source_act_key, source_norm_key, kind, ordinal);
            CREATE INDEX citation_source_jurabk
                ON citation(source_jurabk_key, source_norm_key, kind, ordinal);
            CREATE INDEX citation_target_act
                ON citation(target_act_key, target_norm_key, kind, ordinal);
            CREATE INDEX citation_target_jurabk
                ON citation(target_jurabk_key, target_norm_key, kind, ordinal);
            CREATE INDEX citation_target_pinpoint
                ON citation(target_pinpoint_key, kind, ordinal);
            CREATE INDEX citation_status
                ON citation(status, unresolved_reason, ordinal);
        """)
        metadata = {
            "schema_version": str(index.get("schema_version") or ""),
            "built_at": str(index.get("built_at") or ""),
            "counts": json.dumps(
                index.get("counts") or {}, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")),
            "source_policy": json.dumps(
                index.get("source_policy") or {}, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")),
            "source_snapshots": json.dumps(
                index.get("source_snapshots") or {}, ensure_ascii=False,
                sort_keys=True, separators=(",", ":")),
        }
        conn.executemany(
            "INSERT INTO citation_meta(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )
        insert = """INSERT INTO citation(
            ordinal, id, status, unresolved_reason, kind,
            source_act, source_act_key, source_jurabk, source_jurabk_key,
            source_norm, source_norm_key, source_excerpt, source_snapshot,
            date_basis, citation_text, matched_aliases_json,
            target_act, target_act_key, target_jurabk, target_jurabk_key,
            target_norm, target_norm_key, target_pinpoint,
            target_pinpoint_key, occurrence_count, machine_extracted,
            current_state_only, legal_interpretation
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""
        def values():
            """Yield SQLite rows without duplicating the citation graph.

            A broad full-text corpus can contain tens of thousands of citation
            dictionaries.  Building a second equally large tuple list here
            needlessly raises the peak RSS of the refresh/watch workers.
            ``sqlite3.executemany`` consumes this iterator incrementally.
            """
            for ordinal, row in enumerate(rows):
                yield (
                ordinal, row["id"], row["status"],
                row.get("unresolved_reason"), row["kind"],
                row["source_act"], _key(row["source_act"]),
                row["source_jurabk"], _key(row["source_jurabk"]),
                row["source_norm"], citation_filter_key(row["source_norm"]),
                row["source_excerpt"], row.get("source_snapshot"),
                row["date_basis"], row["citation_text"],
                json.dumps(row.get("matched_aliases") or [],
                           ensure_ascii=False, separators=(",", ":")),
                row.get("target_act"), _key(row.get("target_act")),
                row["target_jurabk"], _key(row["target_jurabk"]),
                row["target_norm"], citation_filter_key(row["target_norm"]),
                row.get("target_pinpoint"),
                citation_filter_key(row.get("target_pinpoint")),
                int(row["occurrence_count"]),
                int(bool(row["machine_extracted"])),
                int(bool(row["current_state_only"])),
                row["legal_interpretation"],
                )
        conn.executemany(insert, values())
        conn.commit()
        check = conn.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise RuntimeError(f"citation sqlite quick_check failed: {check}")
    except Exception:
        conn.close()
        tmp.unlink(missing_ok=True)
        raise
    else:
        conn.close()
    os.replace(tmp, output)


__all__ = [
    "EXPLICIT_ALIAS_TARGETS",
    "build_citation_index",
    "canonical_norm",
    "citation_filter_key",
    "citation_manifest",
    "parse_citation_groups",
    "write_citation_database",
]
