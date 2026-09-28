"""Offset-preserving normalisation of extracted Bundesgesetzblatt text.

``pdftotext`` output of the printed BGBl (until 2022) and of the electronic
BGBl (2023+) carries typesetting artefacts that break naive pattern matching:
hyphenated line breaks ("Num -\\nmer"), kerning spaces ("am T ag nach der
Verkündung", "Jan uar"), letter-spaced glyph runs ("A r t i k e l1N r .1 b")
and running page headers inside sentences.

Every consumer that derives a legal date must still quote the *verbatim*
official wording.  :class:`MappedText` therefore keeps, for every character of
the normalised string, the offset of the character in the original extraction
it came from.  A match on the normalised text maps back to an exact original
span (``orig_span``), which is what evidence records cite.

The module is pure text -> data; it performs no I/O.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from api.page_furniture import find_page_furniture  # noqa: E402

MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember")
MONTH_INDEX = {name.casefold(): number
               for number, name in enumerate(MONTHS, start=1)}
MONTH_INDEX["jänner"] = 1
LETTER = "A-Za-zÄÖÜäöüß"
LOWER = "a-zäöüß"


def fuzzy_word(word: str) -> str:
    """Regex for ``word`` tolerating one kerning space between letters."""
    return r"\s?".join(re.escape(char) for char in word)


MONTH_RE = "(?:" + "|".join(fuzzy_word(name) for name in MONTHS) + ")"
# ``1. Januar 2024`` including kerned forms ``1 . Jan uar 2024``.
DATE_RE = (rf"(?P<day>\d{{1,2}})\s?\.\s?(?P<month>{MONTH_RE})\s?"
           rf"(?P<year>\d\s?\d\s?\d\s?\d)(?!\d)")


def month_number(name: str) -> int | None:
    return MONTH_INDEX.get(re.sub(r"\s+", "", str(name or "")).casefold())


def german_date(day: object, month: object, year: object) -> str | None:
    """ISO date from printed German parts; ``None`` when not a real date."""
    number = month_number(str(month))
    try:
        if number is None:
            return None
        return date(int(re.sub(r"\s+", "", str(year))), number,
                    int(str(day))).isoformat()
    except (TypeError, ValueError):
        return None


def german_date_text(value: str) -> str | None:
    match = re.search(DATE_RE, str(value or ""))
    if not match:
        return None
    return german_date(match.group("day"), match.group("month"),
                       match.group("year"))


def format_german_date(iso: str) -> str:
    day = date.fromisoformat(iso)
    return f"{day.day}. {MONTHS[day.month - 1]} {day.year}"


# ---------------------------------------------------------------- MappedText


@dataclass(frozen=True)
class MappedText:
    """A derived string plus, per character, its offset in the original."""

    text: str
    index: tuple[int, ...]
    original: str

    @classmethod
    def identity(cls, text: str, offset: int = 0,
                 original: str | None = None) -> "MappedText":
        return cls(text, tuple(range(offset, offset + len(text))),
                   original if original is not None else text)

    def orig_span(self, start: int, end: int) -> tuple[int, int]:
        """Original ``[start, end)`` covering normalised ``[start, end)``."""
        if not self.index:
            return (0, 0)
        start = max(0, min(start, len(self.index) - 1))
        end = max(start + 1, min(end, len(self.index)))
        return self.index[start], self.index[end - 1] + 1

    def verbatim(self, start: int, end: int) -> str:
        a, b = self.orig_span(start, end)
        return self.original[a:b]

    def sub(self, pattern: str | re.Pattern[str], repl,
            flags: int = 0) -> "MappedText":
        """``re.sub`` that keeps the character map.

        An equal-length replacement maps position by position.  A
        replacement with the same non-space characters as the match (space
        repair: kerning, glyph runs) maps each character to its original;
        otherwise the replacement is spread over the replaced span.
        """
        regex = re.compile(pattern, flags) if isinstance(pattern, str) \
            else pattern
        pieces: list[str] = []
        index: list[int] = []
        cursor = 0
        for match in regex.finditer(self.text):
            start, end = match.span()
            pieces.append(self.text[cursor:start])
            index.extend(self.index[cursor:start])
            value = repl(match) if callable(repl) else match.expand(repl)
            source = self.text[start:end]
            if len(value) == end - start:
                index.extend(self.index[start:end])
            elif value and re.sub(r"\s+", "", value) == re.sub(
                    r"\s+", "", source):
                solid = [start + offset for offset, char in enumerate(source)
                         if not char.isspace()]
                pointer = 0
                for char in value:
                    if char.isspace():
                        target = solid[min(pointer, len(solid) - 1)] \
                            if solid else start
                        index.append(self.index[target])
                    else:
                        index.append(self.index[solid[pointer]])
                        pointer += 1
            elif end > start and value:
                # Spread the replacement over the replaced span so a slice
                # of it still maps to the whole original region.
                # First and last replacement characters map to the first
                # and last replaced originals.
                last = end - start - 1
                steps = max(1, len(value) - 1)
                index.extend(self.index[start + (position * last + steps // 2)
                                        // steps]
                             for position in range(len(value)))
            else:
                anchor = self.index[start] if start < len(self.index) else (
                    self.index[-1] + 1 if self.index else 0)
                index.extend([anchor] * len(value))
            pieces.append(value)
            cursor = end
        pieces.append(self.text[cursor:])
        index.extend(self.index[cursor:])
        return MappedText("".join(pieces), tuple(index), self.original)

    def replace_spans(self, spans: list[tuple[int, int]],
                      value: str) -> "MappedText":
        """Replace non-overlapping ``[start, end)`` spans of ``self.text``."""
        pieces: list[str] = []
        index: list[int] = []
        cursor = 0
        for start, end in sorted(spans):
            if start < cursor:
                continue
            pieces.append(self.text[cursor:start])
            index.extend(self.index[cursor:start])
            pieces.append(value)
            anchor = self.index[start] if start < len(self.index) else 0
            index.extend([anchor] * len(value))
            cursor = end
        pieces.append(self.text[cursor:])
        index.extend(self.index[cursor:])
        return MappedText("".join(pieces), tuple(index), self.original)

    def slice(self, start: int, end: int) -> "MappedText":
        return MappedText(self.text[start:end], self.index[start:end],
                          self.original)


# ------------------------------------------------------------ normalisation

_RESPACE_VOCAB = sorted({
    "Doppelbuchstaben", "Doppelbuchstabe", "Dreifachbuchstaben",
    "Dreifachbuchstabe", "Buchstaben", "Buchstabe", "Buchst.", "Nummern",
    "Nummer", "Nr.", "Artikeln", "Artikels", "Artikel", "Absätze", "Absätzen",
    "Absatzes", "Absatz", "Abs.", "Sätze", "Satzes", "Satz", "sowie", "und",
    "bis", "tritt", "treten", "in", "Kraft", "außer", "mit", "Wirkung", "vom",
    "zum", "am", "Tag", "Tage", "nach", "der", "die", "das", "des", "dem",
    "den", "Verkündung", "Dieses", "dieses", "Diese", "diese", "Gesetz",
    "Gesetzes", "Verordnung", "Im", "im", "Übrigen", "übrigen", "Ausnahme",
    "von", "vorbehaltlich", "ersten", "auf", "folgenden", "Kalendermonats",
    "Monats", "jeweils", "ist", "wird", "werden", "Angabe", "Wort", "Wörter",
    "durch", "ersetzt", "eingefügt", "gestrichen", "aufgehoben", "gefasst",
    "geändert", "wie", "folgt", "In", "Nach", "vor", "frühestens", "jedoch",
    "zu", "zur", "zum", "angefügt", "folgender", "folgende", "Dem", "Der",
    "Am", "Mit", "soweit", "er", "sie", "betrifft", "hinsichtlich", "an",
    "welchem", "Juli", "Rechtsverordnungen", "Gleichzeitig",
    "Die", "Der", "Das", "Artikel", *MONTHS,
}, key=lambda word: (-len(word), word))
_RESPACE_RE = re.compile("|".join(re.escape(word) for word in _RESPACE_VOCAB))

# Kerning repairs.  ``Buchstaben`` is intentionally absent: "Buchstabe n"
# is a legitimate reference to the letter n.
_KERN_WORDS = (
    "Tage", "Tag", "Teil", "Verkündung", "vorbehaltlich", "Inkrafttreten",
    "Außerkrafttreten", "Artikels", "Artikel", "Nummern", "Nummer",
    "Buchstabe", "Doppelbuchstaben", "Doppelbuchstabe", "Dreifachbuchstabe",
    "Absätze", "Absatzes", "Absatz", "Übrigen", "übrigen", "Kalendermonats",
    "Kalendervierteljahres", "Quartals", "Monats", "folgenden", "Wirkung",
    "Kraft", "tritt", "treten", "Gesetzes", "Gesetz", "Verordnung",
    "Ausnahme", "frühestens", "jedoch", "Sätze", "Satzes", "Bundesgesetzblatt",
    "ausgegeben", "Ausgegeben", "bekanntgegeben", "bekannt", "Absätzen",
    "Artikeln", "Abweichendes", "bestimmt", "nichts", "soweit", "Ersten",
    "ersten", "zweiten", "dritten", "vierten", "fünften", "sechsten",
    "siebten", "achten", "neunten", "zehnten", "elften", "zwölften",
    "Inkrafttretens", "Kalenderjahres", "Monate", "nach", "des", "der",
    "die", "dem", "den", "auf", "mit", "vom", "Zeitpunkt", "Dieses", "Diese",
    "dieses", "Übrigen", "tritt", "hinsichtlich", "dieser", "insoweit",
    "Verordnung", "Rechtsverordnungen", *MONTHS,
)
_KERN_RE = re.compile(
    rf"(?<![{LETTER}])(?:"
    + "|".join(r"[ ]?".join(re.escape(c) for c in word)
               for word in sorted(_KERN_WORDS, key=lambda w: (-len(w), w)))
    + rf")(?![{LOWER}])")


def _respace_run(run: str) -> str:
    """Re-insert word boundaries into a collapsed letter-spaced glyph run."""
    out: list[str] = []
    word = ""
    position = 0

    def flush() -> None:
        nonlocal word
        if word:
            out.append(word)
            word = ""

    while position < len(run):
        char = run[position]
        if char in "0123456789":
            flush()
            match = re.match(r"\d+", run[position:])
            number = match.group(0)
            position += len(number)
            rest = run[position:]
            # A single lowercase suffix ("1b", "12a") belongs to the number
            # unless it starts a vocabulary word ("3und", "2bis").
            if rest[:1].isalpha() and rest[:1].islower() and \
                    not _RESPACE_RE.match(rest):
                after = rest[1:2]
                if not (after.isalpha() and after.islower()) or \
                        _RESPACE_RE.match(rest[1:]):
                    number += rest[0]
                    position += 1
            out.append(number)
            continue
        if char in ",;:()§“„\"'":
            flush()
            if char == "§" and run[position:position + 2] == "§§":
                out.append("§§")
                position += 2
            else:
                out.append(char)
                position += 1
            continue
        if char == ".":
            if word:
                word += "."
                flush()
            elif out:
                out[-1] += "."
            else:
                out.append(".")
            position += 1
            continue
        match = _RESPACE_RE.match(run, position)
        # Vocabulary words only start at a word boundary of the unknown
        # buffer, or when the buffer is itself a finished token -- such as
        # a single item letter ("Buchstabe eundf" -> "e und f").
        if match and (not word or not word[-1].isalpha() or
                      match.group(0)[0].isupper() or
                      (len(word) == 1 and word.islower() and match.group(0)
                       in {"und", "bis", "sowie", "tritt", "treten", "in",
                           "mit", "am", "oder"})):
            flush()
            out.append(match.group(0))
            position = match.end()
            continue
        word += char
        position += 1
    flush()
    text = " ".join(out)
    text = re.sub(r" ([,;:).])", r"\1", text)
    text = re.sub(r"([(„]) ", r"\1", text)
    return text


# A letter-spaced run starts with two short glyph tokens ("N r .1 ,2",
# "( 1 ) A r t …"), so a preceding normal word never dilutes the check.
_RUN_RE = re.compile(
    r"(?:(?<= )|^)\S{1,2} \S{1,2} (?:\S{1,8} ){3,}\S{1,8}(?= |$)")


def _glyph_prefix(tokens: list[str]) -> int:
    """Length of the longest prefix that is a glyph run (>= 6 tokens, >= 50 %
    single characters), or 0."""
    best = 0
    singles = 0
    for count, token in enumerate(tokens, start=1):
        singles += len(token) == 1
        if count >= 6 and singles * 2 >= count:
            best = count
    return best


def _collapse_tokens(tokens: list[str], sep: str = " ") -> str | None:
    """Collapse every glyph run inside a sequence of short tokens.

    A run starts at two short tokens; the scan continues after each run, so
    one long match of short words may contain several runs.  ``None`` when
    nothing was collapsed.
    """
    out: list[str] = []
    index = 0
    changed = False
    while index < len(tokens):
        if len(tokens[index]) <= 2 and index + 1 < len(tokens) and \
                len(tokens[index + 1]) <= 2:
            length = _glyph_prefix(tokens[index:])
            if length:
                out.append(_respace_run("".join(tokens[index:index + length])))
                index += length
                changed = True
                continue
        out.append(tokens[index])
        index += 1
    return sep.join(out) if changed else None


def _collapse_letterspacing(mapped: MappedText) -> MappedText:
    def repl(match: re.Match[str]) -> str:
        collapsed = _collapse_tokens(match.group(0).split(" "))
        return match.group(0) if collapsed is None else collapsed
    for _ in range(3):
        before = mapped.text
        mapped = mapped.sub(_RUN_RE, repl)
        if mapped.text == before:
            break
    return mapped


def normalise(text: str, *, offset: int = 0, original: str | None = None,
              strip_furniture: bool = True) -> MappedText:
    """Normalise one extracted BGBl passage, keeping the offset map.

    ``offset``/``original`` let a caller normalise a slice of a document
    while every mapped offset still refers to the complete document text.
    """
    mapped = MappedText.identity(text, offset, original)
    if strip_furniture:
        spans = find_page_furniture(text)
        if spans:
            mapped = mapped.replace_spans(
                [(span["start"], span["end"]) for span in spans], " ")
    mapped = mapped.sub(r"[\f ]", " ")
    mapped = mapped.sub("­", "")
    # "Num -\nmer" / "Verkün-\ndung"; keep "Bundes- und Landesrecht".
    mapped = mapped.sub(
        rf"(?<=[{LETTER}])[ \t]?-[ \t]*\n[ \t]*"
        rf"(?=[{LOWER}])(?!(?:und|oder|bis|sowie)\b)", "")
    mapped = mapped.sub(r"\s+", " ")
    # "Januar2 0 1 9i nK r a f t": a month glued to a spaced-out year.
    mapped = mapped.sub(rf"({MONTH_RE})(?=\d)", r"\1 ")
    # Mid-line hyphenation left by two-column extraction ("ers- ten").
    mapped = mapped.sub(
        rf"(?<=[{LOWER}])- (?=[{LOWER}])(?!(?:und|oder|bis|sowie)\b)", "")
    mapped = _collapse_letterspacing(mapped)

    def unkern(match: re.Match[str]) -> str:
        return match.group(0).replace(" ", "")
    mapped = mapped.sub(_KERN_RE, unkern)
    mapped = mapped.sub(rf"(?<![{LETTER}])a m(?= (?:\d|T|e))", "am")
    mapped = mapped.sub(rf"(\d) \.(?= ?{MONTH_RE})", r"\1.")
    # "Buchs tabea," -> "Buchstabe a,": kerned keyword glued to its value.
    mapped = mapped.sub(r"(?<![A-Za-zÄÖÜäöüß])((?:Doppel|Dreifach)?)B ?u ?c ?h ?s"
                        r" ?t ?a ?b ?e ?(?=[a-z]\d?(?![a-zäöüß]))",
                        lambda m: m.group(1) + "Buchstabe ")
    # Kerned abbreviations: "Nr . 3", "Abs . 2", "Buchst . a".
    mapped = mapped.sub(r"(?<![A-Za-zÄÖÜäöüß])(Nr|Nrn|Abs|Art|Buchst|"
                        r"Doppelbuchst|Dreifachbuchst) \.", r"\1.")
    return mapped


_LINE_RUN_RE = re.compile(r"(?:(?<= )|(?<=\n)|^)[^\s]{1,2} [^\s]{1,2} "
                          r"(?:[^\s]{1,8} ){3,}[^\s]{1,8}(?= |\n|$)", re.M)


def normalise_lines(text: str, *, strip_furniture: bool = True,
                    offset: int = 0, original: str | None = None) -> MappedText:
    """Like :func:`normalise` but keeps line breaks (for item splitting).

    Structure markers of amendment commands ("2. …", "b) …") are only
    recognised at line starts, so newlines must survive; letter-spaced
    runs are collapsed within a line.
    """
    mapped = MappedText.identity(text, offset, original)
    if strip_furniture:
        spans = find_page_furniture(text)
        if spans:
            mapped = mapped.replace_spans(
                [(span["start"], span["end"]) for span in spans], "\n")
    mapped = mapped.sub(r"[\f]", "\n")
    mapped = mapped.sub(r"[\u00a0\u2007\u2009\u202f]", " ")
    mapped = mapped.sub("\u00ad", "")
    mapped = mapped.sub(r"[ \t]+", " ")
    # "ge-\nändert" -> "geändert"; never glue a following "a) " item.
    mapped = mapped.sub(
        rf"(?<=[{LETTER}]) ?-[ \t]*\n[ \t]*(?=[{LOWER}])"
        rf"(?!(?:und|oder|bis|sowie)\b)(?![a-z]{{1,3}} ?\))", "")

    def repl(match: re.Match[str]) -> str:
        collapsed = _collapse_tokens(match.group(0).split(" "))
        return match.group(0) if collapsed is None else collapsed
    for _ in range(3):
        before = mapped.text
        mapped = mapped.sub(_LINE_RUN_RE, repl)
        if mapped.text == before:
            break
    # Kerned structure markers: "2 .§1 0 Abs. 1" -> "2. § 10 Abs. 1",
    # "b ) Die …" -> "b) Die …".
    mapped = mapped.sub(r"(?m)^([ \t]{0,3}\d{1,3}[a-z]?) \.(?=\S)", r"\1. ")
    mapped = mapped.sub(r"(?m)^([ \t]{0,3}[a-z]{1,3}) \)", r"\1)")
    mapped = mapped.sub(r"§(\d{1,3}) (\d{1,2}[a-z]?)(?![\d])", r"§ \1\2")
    return mapped


def compact(value: object) -> str:
    return " ".join(str(value or "").split())


# ------------------------------------------------------------- structure

ARTICLE_HEADING_RE = re.compile(
    r"(?m)^[ \t]*Artikel[ \t]+(?P<number>\d+(?:[ \t]*[a-z]\d*)?|[IVXLCDM]+)"
    r"[ \t]*$")
TITLE_DATE_RE = re.compile(
    r"(?m)^[ \t]*V[ \t]?om[ \t]+(?P<day>\d{1,2})[ \t]?\.[ \t]*"
    rf"(?P<month>{MONTH_RE})[ \t]+(?P<year>\d{{4}})[ \t]*(?:\*+\)?)?[ \t]*$")
SIGNATURE_RE = re.compile(
    r"(?m)^[ \t]*(?P<place>Berlin|Bonn),?[ \t]+den[ \t]+(?P<day>\d{1,2})"
    rf"[ \t]?\.[ \t]*(?P<month>{MONTH_RE})[ \t]+(?P<year>\d{{4}})")
PROMULGATION_FORMULA_RE = re.compile(
    r"Das\s+vorstehende\s+Gesetz\s+wird\s+hiermit|"
    r"Die\s+verfassungsmäßigen\s+Rechte\s+des\s+Bundesrates|"
    r"Die\s+Zustimmung\s+des\s+Bundesrates\s+ist\s+erteilt|"
    r"(?m:^[ \t]*(?:Berlin|Bonn),?[ \t]+den[ \t]+\d)")


_QUOTE_SPAN_RE = re.compile(r"„[^“”„]{0,20000}[“”]")


def quote_spans(text: str) -> list[tuple[int, int]]:
    """Spans of „…“ quotations (replacement wording, never structure).

    Bounded like ``patch_parser.QUOTE_RE``: a quotation ends at the first
    closing mark, and an unclosed „ spans nothing (pdftotext occasionally
    drops a closing mark; that must not swallow the rest of the act).
    """
    return [match.span() for match in _QUOTE_SPAN_RE.finditer(text)]


def _inside(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(a < position < b for a, b in spans)


_HEADING_BODY_START = re.compile(
    r"^(?:\(\s*\d+[a-z]?\s*\)|(?:Das|Die|Der|Dem|Den|Dieses|Diese|Im|In|Nach|"
    r"Vor|Bei|Am|Mit|Artikel|Es)\s|§|\d+\.)")


def article_headings(text: str, start: int = 0,
                     end: int | None = None) -> list[dict]:
    """Article headings ``Artikel N`` outside quotations, with offsets.

    Each row: ``article``, ``start`` (heading line), ``body_start`` (after
    the heading lines) and ``heading`` (joined, de-hyphenated title lines).
    """
    end = len(text) if end is None else end
    quotes = quote_spans(text)
    rows = []
    for match in ARTICLE_HEADING_RE.finditer(text, start, end):
        if _inside(match.start(), quotes):
            continue
        number = re.sub(r"\s+", "", match.group("number"))
        cursor = match.end()
        heading_lines: list[str] = []
        body_start = cursor
        lines = text[cursor:min(end, cursor + 600)].split("\n")
        consumed = 0
        for line in lines[1:8]:
            clean = line.strip()
            consumed_line = len(line) + 1
            if not clean:
                if heading_lines:
                    break
                consumed += consumed_line
                continue
            if _HEADING_BODY_START.match(clean) or len(heading_lines) >= 4:
                break
            heading_lines.append(clean)
            consumed += consumed_line
        body_start = cursor + 1 + consumed if lines[1:] else cursor
        heading = "\n".join(heading_lines)
        heading = re.sub(rf"(?<=[{LETTER}])\s?-\n(?=[{LOWER}])", "", heading)
        rows.append({"article": number, "start": match.start(),
                     "heading_end": match.end(),
                     "body_start": min(body_start, end),
                     "heading": compact(heading)})
    return rows


def page_index(text: str, position: int) -> int:
    """Zero-based PDF page of ``position`` (pages separated by ``\\f``)."""
    return text.count("\f", 0, max(0, position))
