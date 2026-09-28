"""Segment Bundestag Drucksachen into source-bound legislative-material fragments.

Pure parsing, no network.  The input is the exact per-page text that
``pdftotext -layout`` produced for one Drucksache (``pages[i]`` is PDF page
``i + 1``; ``None`` marks a page that was not extracted).  The output keeps
three things apart that a careless excerpt mixes up:

* **heading and body** are separate fields.  A body never contains the next
  heading (or any part of it) and a heading never contains body text; both
  carry character offsets into the exact page text, so
  ``pages[p - 1][start:end]`` reproduces the raw source slice;
* **what a passage is about and what it merely mentions**.  Only norms named
  in (or resolved from) the heading are ``target_norms`` with an
  ``explains`` relation (``drafts`` for the Entwurf itself); every other norm
  reference in the text is a ``mentions`` relation;
* **draft and enacted law**.  Numbers in a Regierungsentwurf use the draft's
  numbering.  A procedure that ended in promulgation does not make a draft
  passage the official justification of the enacted norm, so every fragment
  says ``correspondence_to_enacted_norm_verified: false`` and carries an
  ``adoption_status`` that never claims passage-level adoption.

Printed page furniture (running headers, the first-page masthead) is
removed before segmentation and never reaches a heading or body.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from api.page_furniture import find_page_furniture

PARSER_VERSION = "materials-parser/1"

ROLES = (
    "vorblatt", "entwurf", "begruendung", "stellungnahme_normenkontrollrat",
    "stellungnahme_bundesrat", "gegenaeusserung_bundesregierung",
    "beschlussempfehlung", "bericht",
)
ROLE_SLUGS = {
    "vorblatt": "vorblatt",
    "entwurf": "entwurf",
    "begruendung": "begruendung",
    "stellungnahme_normenkontrollrat": "stellungnahme-nkr",
    "stellungnahme_bundesrat": "stellungnahme-bundesrat",
    "gegenaeusserung_bundesregierung": "gegenaeusserung",
    "beschlussempfehlung": "beschlussempfehlung",
    "bericht": "bericht",
}
# ``drafts`` is used only for the Entwurf itself: the draft wording of a norm
# is neither an explanation of it nor a mere mention.
RELATION_TYPES = ("explains", "mentions", "drafts")
GOVERNMENT_POSITIONS = ("agrees", "rejects", "examines", "partially_agrees")

ADOPTION_PROMULGATED = "procedure_promulgated_passage_correspondence_unverified"

# --------------------------------------------------------------------------
# Acts
# --------------------------------------------------------------------------

_ORDINALS = {
    "erst": "I", "zweit": "II", "dritt": "III", "viert": "IV", "fünft": "V",
    "sechst": "VI", "siebt": "VII", "siebent": "VII", "acht": "VIII",
    "neunt": "IX", "zehnt": "X", "elft": "XI", "zwölft": "XII",
    "dreizehnt": "XIII", "vierzehnt": "XIV",
}
_ORDINAL_RE = (r"(?:Erst|Zweit|Dritt|Viert|Fünft|Sechst|Siebent|Siebt|Acht|"
               r"Neunt|Zehnt|Elft|Zwölft|Dreizehnt|Vierzehnt)(?:es|en|e|er)?")

# Genitive (and a few nominative) long forms used in Drucksachen.  Only acts
# whose abbreviation is unambiguous are listed; an unknown long form is kept
# verbatim with ``canonical_id: null`` instead of being guessed.
LONG_NAMES: dict[str, str] = {
    "Bundesversorgungsgesetzes": "BVG",
    "Bundesversorgungsgesetz": "BVG",
    "Opferentschädigungsgesetzes": "OEG",
    "Opferentschädigungsgesetz": "OEG",
    "Soldatenversorgungsgesetzes": "SVG",
    "Soldatenversorgungsgesetz": "SVG",
    "Asylbewerberleistungsgesetzes": "AsylbLG",
    "Zivildienstgesetzes": "ZDG",
    "Häftlingshilfegesetzes": "HHG",
    "Bundesentschädigungsgesetzes": "BEG",
    "Strafrechtlichen Rehabilitierungsgesetzes": "StrRehaG",
    "Verwaltungsrechtlichen Rehabilitierungsgesetzes": "VwRehaG",
    "Gesetzes über den Auswärtigen Dienst": "GAD",
    "Strafprozeßordnung": "StPO",
    "Strafprozessordnung": "StPO",
    "Sozialgerichtsgesetzes": "SGG",
    "Gesetzes über Kosten der Gerichtsvollzieher": "GvKostG",
    "Einkommensteuergesetzes": "EStG",
    "Umsatzsteuergesetzes": "UStG",
    "Lastenausgleichsgesetzes": "LAG",
    "Altersteilzeitgesetzes": "AltTZG",
    "Zweiten Gesetzes über die Krankenversicherung der Landwirte": "KVLG 1989",
    "Gesetzes zur Förderung der Einstellung der landwirtschaftlichen "
    "Erwerbstätigkeit": "FELEG",
    "Dienstbeschädigungsausgleichsgesetzes": "DBAG",
    "Versorgungsmedizin-Verordnung": "VersMedV",
    "Risikostruktur-Ausgleichsverordnung": "RSAV",
    "Kraftfahrzeughilfe-Verordnung": "KfzHV",
    "Schwerbehindertenausweisverordnung": "SchwbAwV",
    "Beamtenversorgungsgesetzes": "BeamtVG",
    "Sonderurlaubsverordnung": "SUrlV",
    "Bundesbeihilfeverordnung": "BBhV",
    "Infektionsschutzgesetzes": "IfSG",
    "Jugendfreiwilligendienstegesetzes": "JFDG",
    "Anti-D-Hilfegesetzes": "AntiDHG",
    "Conterganstiftungsgesetzes": "ContStifG",
    "Bundesfreiwilligendienstgesetzes": "BFDG",
    "Bundesausbildungsförderungsgesetzes": "BAföG",
    "Gesetzes über die Alterssicherung der Landwirte": "ALG",
    "Wohngeldgesetzes": "WoGG",
    "Verwaltungsgerichtsordnung": "VwGO",
    "Grundgesetzes": "GG",
    "Behindertengleichstellungsgesetzes": "BGG",
    "Bürgerlichen Gesetzbuchs": "BGB",
    "Bürgerlichen Gesetzbuches": "BGB",
    "Gesetzes zur Einsetzung eines Nationalen Normenkontrollrates": "NKRG",
    "Gerichtsvollzieherkostengesetzes": "GvKostG",
    "Strafgesetzbuchs": "StGB",
    "Strafgesetzbuches": "StGB",
    "Bundeskindergeldgesetzes": "BKGG",
    "Krankenhausfinanzierungsgesetzes": "KHG",
    "Häftlingshilfegesetz": "HHG",
    "Strafrechtliches Rehabilitierungsgesetz": "StrRehaG",
    "Verwaltungsrechtliches Rehabilitierungsgesetz": "VwRehaG",
    "Zivildienstgesetz": "ZDG",
    "Grundgesetz": "GG",
    "Infektionsschutzgesetz": "IfSG",
    "Sozialgerichtsgesetz": "SGG",
    "Beamtenversorgungsgesetz": "BeamtVG",
    "Behindertengleichstellungsgesetz": "BGG",
}
_LONG_NAMES_BY_LENGTH = sorted(LONG_NAMES, key=len, reverse=True)

# Abbreviations that are recognised as acts even without an ``act_slugs``
# entry (their canonical_id is then ``null``).
KNOWN_ABBREVIATIONS = frozenset({
    "BVG", "OEG", "SVG", "ZDG", "BeamtVG", "HHG", "StrRehaG", "VwRehaG",
    "GG", "BGB", "SGG", "VwGO", "NKRG", "VersMedV", "AsylbLG", "EStG",
    "UStG", "BEG", "GAD", "StPO", "LAG", "AltTZG", "BGG", "IfSG", "BAföG",
    "WoGG", "ALG", "BFDG", "JFDG", "ContStifG", "AntiDHG", "KfzHV",
    "SchwbAwV", "BBhV", "RSAV", "SUrlV", "DBAG", "FELEG", "GvKostG",
    "StGB", "ZPO", "HGB", "VwVfG", "BDSG", "AufenthG", "AsylG", "BKGG",
    "WPflG", "SG", "UVG", "BSHG", "KVLG 1989",
})
# Capitalised tokens that look like act abbreviations but are courts or
# authorities.
_NOT_ACTS = frozenset({
    "BSG", "BVerfG", "BGH", "BVerwG", "BAG", "BFH", "EuGH", "LSG", "OVG",
    "VG", "OLG", "LG", "AG", "BMAS", "BMF", "BMG", "BMJV", "NKR", "EU", "EG",
    "DDR", "BR", "BT", "SER",
})
_SGB_RE = re.compile(r"SGB\s+(?P<roman>[IVX]{1,5})(?P<draft>-E)?(?![\w])")
_GENERIC_ABBR_RE = re.compile(
    r"(?P<abbr>[A-ZÄÖÜ][A-Za-zÄÖÜäöüß]{0,11}(?:G|V|VO|O|B))(?P<draft>-E)?"
    r"(?![\wäöüß-])")


def _is_abbreviation(token: str, known: Iterable[str]) -> bool:
    if token in _NOT_ACTS:
        return False
    if token in KNOWN_ABBREVIATIONS or token in known:
        return True
    upper = sum(1 for ch in token if ch.isupper())
    return upper >= 2 and len(token) <= 12 and bool(
        re.fullmatch(r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß]*(?:G|V|VO|O)", token))


def act_from_title(title: str) -> str | None:
    """Resolve the act an Artikel title creates or amends ("Änderung des …")."""
    text = _ws(title)
    paren = re.search(r"\((SGB\s+[IVX]{1,5}|[A-ZÄÖÜ][A-Za-zÄÖÜäöüß]{1,12})\)",
                      text)
    if paren and (paren.group(1).startswith("SGB")
                  or _is_abbreviation(paren.group(1), ())):
        return _ws(paren.group(1))
    m = re.search(r"Sozialgesetzbuch\s+(" + _ORDINAL_RE + r")\s+Buch", text) or \
        re.search(r"(" + _ORDINAL_RE + r")\s+Buch(?:es|s)?\s+Sozialgesetzbuch",
                  text)
    if m:
        return _sgb_from_ordinal(m.group(1))
    m = re.match(r"(?:Änderung|Aufhebung|Neufassung)\s+(?:des|der)\s+(.+?)\s*$",
                 text)
    if m:
        name = m.group(1)
        if name in LONG_NAMES:
            return LONG_NAMES[name]
    return None


def _sgb_from_ordinal(word: str) -> str | None:
    stem = re.sub(r"(?:es|en|e|er)$", "", word.lower())
    roman = _ORDINALS.get(stem)
    return f"SGB {roman}" if roman else None


# --------------------------------------------------------------------------
# Small text helpers
# --------------------------------------------------------------------------

def _ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def drucksache_slug(drucksache: str, chamber: str = "BT") -> str:
    wp, number = drucksache.split("/")
    return f"{chamber.lower()}-drs-{wp}-{number}"


# --------------------------------------------------------------------------
# Page model
# --------------------------------------------------------------------------

@dataclass
class _Item:
    kind: str                 # "text" | "blank" | "page_break" | "gap"
    page: int
    start: int = 0            # offset of first non-space character
    end: int = 0              # offset after last non-space character
    text: str = ""
    indent: int = 0           # column of ``start`` within its raw line
    width: int = 0            # column of ``end`` within its raw line


# First-page masthead of a Bundestag Drucksache.  ``api.page_furniture``
# covers the running header of the following pages but not this two-line
# block, so it is handled here.
_EXTRA_FURNITURE: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("drucksache_masthead", re.compile(
        r"^[ \t]*Deutscher[ \t]+Bundestag[ \t]+Drucksache[ \t]+\d{1,2}/\d{1,6}"
        r"[ \t]*\n[ \t]*\d{1,2}\.[ \t]+Wahlperiode[ \t]+\d{1,2}\.\d{1,2}\.\d{4}",
        re.M)),
    # Printer's colophon on the last page of a printed Drucksache.
    ("drucksache_colophon", re.compile(
        r"Gesamtherstellung:[^\n]*(?:\n[^\n]*){0,3}?ISSN[ \t]+\d{4}-\d{3}[\dX]")),
    ("drucksache_vorabfassung_footer", re.compile(
        r"Vorabfassung\s+-\s+wird\s+durch\s+die\s+lektorierte\s+Fassung\s+"
        r"ersetzt\.?")),
)


def page_furniture(text: str) -> list[dict[str, Any]]:
    """Furniture spans of one page: ``api.page_furniture`` plus the
    first-page masthead this module knows about."""
    spans = [dict(row, detected_by="api.page_furniture")
             for row in find_page_furniture(text)]
    for kind, pattern in _EXTRA_FURNITURE:
        for match in pattern.finditer(text):
            start, end = match.span()
            if start == end or any(start < row["end"] and row["start"] < end
                                   for row in spans):
                continue
            spans.append({"kind": kind, "start": start, "end": end,
                          "text": match.group(0),
                          "detected_by": "api.legislative_materials"})
    spans.sort(key=lambda row: row["start"])
    return spans


def _page_items(page: int, text: str,
                furniture: list[dict[str, Any]]) -> list[_Item]:
    items: list[_Item] = []
    pos = 0
    for raw in text.split("\n"):
        line_start, line_end = pos, pos + len(raw)
        pos = line_end + 1
        pieces = [(line_start, line_end)]
        for row in furniture:
            cut: list[tuple[int, int]] = []
            for s, e in pieces:
                if row["end"] <= s or row["start"] >= e:
                    cut.append((s, e))
                    continue
                if s < row["start"]:
                    cut.append((s, row["start"]))
                if row["end"] < e:
                    cut.append((row["end"], e))
            pieces = cut
        touched = pieces != [(line_start, line_end)]
        kept = False
        for s, e in pieces:
            segment = text[s:e]
            if not segment.strip():
                continue
            lead = len(segment) - len(segment.lstrip())
            start = s + lead
            end = s + len(segment.rstrip())
            items.append(_Item("text", page, start, end, text[start:end],
                               indent=start - line_start,
                               width=end - line_start))
            kept = True
        if not kept and not touched:
            items.append(_Item("blank", page, line_start, line_start))
    while items and items[0].kind == "blank":
        items.pop(0)
    while items and items[-1].kind == "blank":
        items.pop()
    return items


def _printed_page(furniture: list[dict[str, Any]]) -> int | None:
    for row in furniture:
        if row["kind"] == "drucksache_running_header":
            match = re.search(r"[–-]\s*(\d{1,4})\s*[–-]", row["text"])
            if match:
                return int(match.group(1))
    return None


# --------------------------------------------------------------------------
# Citations
# --------------------------------------------------------------------------

_UNIT_WORDS = {
    "Absatz": "absatz", "Absätze": "absatz", "Abs.": "absatz",
    "Satz": "satz", "Sätze": "satz",
    "Nummer": "nummer", "Nummern": "nummer", "Nr.": "nummer",
    "Buchstabe": "buchstabe", "Buchstaben": "buchstabe", "Buchst.": "buchstabe",
    "Doppelbuchstabe": "doppelbuchstabe", "Doppelbuchstaben": "doppelbuchstabe",
    "Halbsatz": "halbsatz",
}
_UNIT_LABEL = {"absatz": "Absatz", "satz": "Satz", "nummer": "Nummer",
               "buchstabe": "Buchstabe", "doppelbuchstabe": "Doppelbuchstabe",
               "halbsatz": "Halbsatz"}
_UNIT_SEGMENT = {"para": "para", "art": "art", "absatz": "abs",
                 "satz": "satz", "nummer": "nr", "buchstabe": "buchst",
                 "doppelbuchstabe": "dbuchst", "halbsatz": "hs"}
_HIERARCHY = {"para": 0, "art": 0, "absatz": 1, "satz": 2, "nummer": 3,
              "buchstabe": 4, "doppelbuchstabe": 5, "halbsatz": 6}
_LETTER_UNITS = {"buchstabe", "doppelbuchstabe"}

_T_UNIT = re.compile(r"(Absatz|Absätze|Abs\.|Satz|Sätze|Nummer|Nummern|Nr\.|"
                     r"Buchstaben?|Buchst\.|Doppelbuchstaben?|Halbsatz)"
                     r"(?![\wäöüß])")
_T_NUM = re.compile(r"(\d{1,4}[a-z]?)(?![\w.]\d|[\wäöüß])")
_T_LETTER = re.compile(r"([a-z]{1,3}\d?)(?![\wäöüß])")
_T_NEU = re.compile(r"[–-]\s*neu\s*(?:[–-](?!\s*\d))?")
_T_FF = re.compile(r"ff?\.")
_T_SEP = re.compile(r"(,|und|sowie|oder|bzw\.)(?![\wäöüß])")
_T_RANGE = re.compile(r"(bis|[–-])(?=\s*\d)")
_T_PARA = re.compile(r"§§?")
_T_ART = re.compile(r"(?:Artikel|Art\.)(?=\s*\d)")


@dataclass
class _Ref:
    unit: str                                  # "para" | "art"
    parts: list[tuple[str, str]]               # [(unit, value), ...]
    new_in_draft: bool = False
    following: bool = False
    range_citation: str | None = None


def _scan_refs(text: str, pos: int) -> tuple[int, list[_Ref]]:
    """Scan one citation run starting at ``§``/``§§``/``Artikel``.

    Returns ``(end, refs)``; ``end`` is the offset after the last token that
    belonged to the run (``pos`` when nothing was recognised).
    """
    top = "para"
    match = _T_PARA.match(text, pos)
    if not match:
        match = _T_ART.match(text, pos)
        top = "art"
        if not match:
            return pos, []
    refs: list[_Ref] = []
    cursor = match.end()
    end_ok = pos
    expect: str | None = top          # unit whose value must follow
    mode = "new_top"                  # how the next value is attached
    last_unit: str | None = None
    sep_pending = False
    range_pending = False

    def skip(p: int) -> int:
        while p < len(text) and text[p] in " \t\n ":
            p += 1
        return p

    while True:
        p = skip(cursor)
        if expect is not None:
            if expect in _LETTER_UNITS:
                vm = _T_LETTER.match(text, p) or _T_NUM.match(text, p)
            else:
                vm = _T_NUM.match(text, p)
            if not vm:
                break
            value = vm.group(1)
            if range_pending and refs:
                _expand_range(refs, expect, value)
            elif mode == "new_top":
                refs.append(_Ref(top, [(top, value)]))
            elif mode == "refine" and refs:
                refs[-1].parts.append((expect, value))
            elif mode == "new_prefix" and refs:
                base = refs[-1].parts
                cut = next((k for k, (u, _) in enumerate(base)
                            if _HIERARCHY[u] >= _HIERARCHY[expect] and k > 0),
                           len(base))
                refs.append(_Ref(refs[-1].unit, [*base[:cut], (expect, value)]))
            elif mode == "same_unit" and refs and \
                    any(u == expect for u, _ in refs[-1].parts):
                base = refs[-1].parts
                cut = max(k for k, (u, _) in enumerate(base) if u == expect)
                refs.append(_Ref(refs[-1].unit, [*base[:cut], (expect, value)]))
            else:
                break
            last_unit = expect
            expect = None
            sep_pending = range_pending = False
            cursor = end_ok = vm.end()
            continue
        if not refs:
            break
        m = _T_NEU.match(text, p)
        if m:
            refs[-1].new_in_draft = True
            cursor = end_ok = m.end()
            continue
        m = _T_FF.match(text, p)
        if m and not sep_pending:
            refs[-1].following = True
            cursor = end_ok = m.end()
            continue
        m = _T_PARA.match(text, p) if top == "para" else None
        if m:
            expect, mode, cursor = "para", "new_top", m.end()
            continue
        m = _T_UNIT.match(text, p)
        if m:
            unit = _UNIT_WORDS[m.group(1)]
            present = {u for u, _ in refs[-1].parts}
            expect = unit
            mode = "new_prefix" if sep_pending or unit in present else "refine"
            cursor = m.end()
            continue
        if sep_pending:
            # a bare value after a separator continues the last unit ...
            if last_unit is None:
                break
            vm = (_T_LETTER.match(text, p) if last_unit in _LETTER_UNITS
                  else _T_NUM.match(text, p))
            if not vm:
                break
            expect, mode = last_unit, "same_unit"
            # ... unless it is followed by a unit word of the same or a
            # higher level: "§§ 11 Absatz 2, 12 Absatz 4" lists paragraphs.
            after = _T_UNIT.match(text, skip(vm.end()))
            if after and last_unit != top and \
                    _HIERARCHY[_UNIT_WORDS[after.group(1)]] <= _HIERARCHY[last_unit]:
                expect, mode = top, "new_top"
            continue
        m = _T_RANGE.match(text, p)
        if m and last_unit is not None:
            expect, range_pending, cursor = last_unit, True, m.end()
            continue
        m = _T_SEP.match(text, p)
        if m:
            sep_pending, cursor = True, m.end()
            continue
        break
    return end_ok, refs


def _expand_range(refs: list[_Ref], unit: str, value: str) -> None:
    last = refs[-1]
    first_value = last.parts[-1][1]
    label = f"{first_value} bis {value}"
    if first_value.isdigit() and value.isdigit() and \
            0 < int(value) - int(first_value) <= 60 and last.parts[-1][0] == unit:
        last.range_citation = label
        prefix = last.parts[:-1]
        for number in range(int(first_value) + 1, int(value) + 1):
            refs.append(_Ref(last.unit, [*prefix, (unit, str(number))],
                             range_citation=label))
    else:
        last.range_citation = label
        refs.append(_Ref(last.unit, [*last.parts[:-1], (unit, value)],
                         range_citation=label))


@dataclass
class _ActHit:
    act: str | None
    basis: str                  # explicit | explicit_unmapped | self_reference
    end: int                    # | context | unresolved
    draft_suffix: bool = False


def _act_after(text: str, pos: int, known: Iterable[str]) -> _ActHit | None:
    rest = text[pos:pos + 200]
    m = re.match(r"\s+", rest)
    if not m:
        return None
    lead = m.end()
    body = rest[lead:]
    m = _SGB_RE.match(body)
    if m:
        return _ActHit(f"SGB {m.group('roman')}", "explicit",
                       pos + lead + m.end(), bool(m.group("draft")))
    m = re.match(r"KVLG\s+1989", body)
    if m:
        return _ActHit("KVLG 1989", "explicit", pos + lead + m.end())
    m = _GENERIC_ABBR_RE.match(body)
    if m and _is_abbreviation(m.group("abbr"), known):
        return _ActHit(m.group("abbr"), "explicit", pos + lead + m.end(),
                       bool(m.group("draft")))
    m = re.match(r"dieses\s+(?:Buches|Gesetzes)", body)
    if m:
        return _ActHit(None, "self_reference", pos + lead + m.end())
    m = re.match(r"(?:des|der)\s+(" + _ORDINAL_RE + r")\s+Buch(?:es|s)?"
                 r"(?:\s+Sozialgesetzbuch)?", body)
    if m:
        return _ActHit(_sgb_from_ordinal(m.group(1)), "explicit",
                       pos + lead + m.end())
    m = re.match(r"(?:(?:des|der)\s+)?", body)
    if m is not None:
        tail = body[m.end():]
        for name in _LONG_NAMES_BY_LENGTH:
            if tail.startswith(name) and not re.match(r"[\wäöüß]",
                                                      tail[len(name):len(name) + 1]):
                return _ActHit(LONG_NAMES[name], "explicit",
                               pos + lead + m.end() + len(name))
        u = re.match(r"[A-ZÄÖÜ][\wäöüß-]*(?:gesetzes|gesetzbuchs|gesetzbuches|"
                     r"verordnung|ordnung)(?![\wäöüß])", tail)
        if u and m.end() > 0 and u.group(0) not in {"Verordnung", "Ordnung"}:
            return _ActHit(u.group(0), "explicit_unmapped",
                           pos + lead + m.end() + u.end())
        u = re.match(r"(?:Verordnung|Gesetzes|Richtlinie|Abkommens|Vertrags|"
                     r"Übereinkommens|Satzung)\b[^,;.()]{0,80}", tail)
        if u and m.end() > 0:
            # another, unnamed-by-abbreviation instrument: never the context act
            return _ActHit(None, "explicit_unmapped",
                           pos + lead + m.end() + u.end())
    return None


@dataclass
class _Ctx:
    """Document-level resolution context."""
    act_slugs: dict[str, str]
    draft_acts: set[str]
    artikel_acts: dict[str, str | None]


def _canonical(act: str | None, ref_parts: Sequence[tuple[str, str]] | None,
               slugs: dict[str, str]) -> str | None:
    if not act or act not in slugs:
        return None
    base = f"de/bund/{slugs[act]}"
    if not ref_parts:
        return base
    return base + "".join(f"/{_UNIT_SEGMENT[u]}/{v}" for u, v in ref_parts)


def _citation_text(ref: _Ref | None, act: str | None) -> str:
    if ref is None:
        return act or ""
    head_unit, head_value = ref.parts[0]
    words = [("§ " if head_unit == "para" else "Artikel ") + head_value]
    for unit, value in ref.parts[1:]:
        words.append(f"{_UNIT_LABEL[unit]} {value}")
    if ref.following:
        words.append("ff.")
    if act:
        words.append(act)
    return " ".join(words)


def _norm(ref: _Ref | None, act: str | None, *, ctx: _Ctx, act_basis: str,
          source_text: str, draft_suffix: bool = False) -> dict[str, Any]:
    fields = {u: None for u in ("absatz", "satz", "nummer", "buchstabe",
                                "doppelbuchstabe", "halbsatz")}
    if ref is not None:
        for unit, value in ref.parts[1:]:
            fields[unit] = value
    draft_numbering = bool(act and (act in ctx.draft_acts or draft_suffix))
    return {
        "citation": _citation_text(ref, act),
        "source_text": source_text,
        "act": act,
        "act_basis": act_basis,
        "unit": ref.unit if ref is not None else None,
        "number": ref.parts[0][1] if ref is not None else None,
        **fields,
        "new_in_draft": bool(ref and ref.new_in_draft),
        "following": bool(ref and ref.following),
        "range": ref.range_citation if ref is not None else None,
        "numbering_basis": ("entwurf" if draft_numbering
                            else "as_cited_at_document_date"),
        "canonical_id": _canonical(act, ref.parts if ref else None,
                                   ctx.act_slugs),
        "correspondence_to_enacted_norm_verified": False,
    }


def _find_act_token(text: str, known: Iterable[str]) -> tuple[str, bool] | None:
    m = _SGB_RE.search(text)
    if m:
        return f"SGB {m.group('roman')}", bool(m.group("draft"))
    for m in _GENERIC_ABBR_RE.finditer(text):
        if _is_abbreviation(m.group("abbr"), known):
            return m.group("abbr"), bool(m.group("draft"))
    return None


_ARTIKEL_GROUP = re.compile(r"\b(?:Artikel\s+(?P<n>\d+[a-z]?)|(?P<typo>Artike1))"
                            r"(?![\w])")


def heading_targets(area: str, ctx: _Ctx) -> tuple[list[dict[str, Any]],
                                                   list[str]]:
    """Target norms of a Stellungnahme/Gegenäußerung heading.

    ``area`` is the citation part, e.g. ``"Artikel 1, § 34 Absatz 3 Satz 2
    SGB XIV"`` or ``"Artikel 6 Nummer 3 (§ 88 SVG), Artikel 16 …"``.
    """
    notes: list[str] = []
    area = _ws(area)
    area = re.sub(r"^Zu(?:m)?\s+", "", area)
    groups: list[tuple[str | None, str]] = []
    matches = list(_ARTIKEL_GROUP.finditer(area))
    if not matches:
        groups.append((None, area))
    else:
        for k, m in enumerate(matches):
            stop = matches[k + 1].start() if k + 1 < len(matches) else len(area)
            number = m.group("n")
            if m.group("typo"):
                number = "1"
                notes.append("source_typo: heading prints 'Artike1'; read as "
                             "'Artikel 1'")
            groups.append((number, area[m.end():stop]))
    known = ctx.act_slugs.keys()
    targets: list[dict[str, Any]] = []
    for artikel, text in groups:
        locator = _draft_locator(artikel, text)
        found_para = False
        pos = 0
        while True:
            hit = re.search(r"§", text[pos:])
            if not hit:
                break
            start = pos + hit.start()
            end, refs = _scan_refs(text, start)
            if not refs:
                pos = start + 1
                continue
            found_para = True
            prefix = text[max(0, start - 12):start]
            act_hit = _act_after(text, end, known)
            if act_hit and act_hit.act:
                act, basis, draft = act_hit.act, "explicit", act_hit.draft_suffix
            else:
                later = _find_act_token(text[end:], known)
                if later:
                    act, basis, draft = later[0], "explicit", later[1]
                else:
                    act = ctx.artikel_acts.get(artikel) if artikel else None
                    basis = "artikel_context" if act else "unresolved"
                    draft = False
            source = text[start:end].strip()
            if re.search(r"Anlage\s+zu\s*$", prefix):
                cited = re.search(r"Anlage\s+zu\s+§[^()]*", text)
                citation = _ws(cited.group(0)).rstrip(" ,") if cited else _ws(text)
                anchor = _norm(None, act, ctx=ctx, act_basis=basis,
                               source_text=citation, draft_suffix=draft)
                anchor.update(unit="anlage", citation=citation,
                              canonical_id=None, anlage_zu_para=refs[0].parts[0][1])
                anchor["draft_locator"] = locator
                targets.append(anchor)
                pos = end
                break
            for ref in refs:
                row = _norm(ref, act, ctx=ctx, act_basis=basis,
                            source_text=source, draft_suffix=draft)
                row["draft_locator"] = locator
                targets.append(row)
            pos = end
        if found_para:
            continue
        token = _find_act_token(text, known)
        act = token[0] if token else (ctx.artikel_acts.get(artikel)
                                      if artikel else None)
        if act:
            row = _norm(None, act, ctx=ctx,
                        act_basis="explicit" if token else "artikel_context",
                        source_text=_ws(text.strip("() ,")) or act,
                        draft_suffix=bool(token and token[1]))
            if re.search(r"\ballgemein\b", text):
                row["scope"] = "allgemein"
            row["draft_locator"] = locator
            targets.append(row)
        elif artikel:
            # A provision of the draft (Mantelgesetz) itself, e.g.
            # "Artikel 59 … (Inkrafttreten)": no norm of an act to link.
            targets.append({
                "citation": _ws(f"Artikel {artikel} {text}").strip(" ,"),
                "source_text": _ws(text), "act": None,
                "act_basis": "unresolved", "unit": "draft_artikel",
                "number": artikel, "absatz": None, "satz": None,
                "nummer": None, "buchstabe": None, "doppelbuchstabe": None,
                "halbsatz": None, "new_in_draft": bool(_T_NEU.search(text)),
                "following": False, "range": None,
                "numbering_basis": "entwurf", "canonical_id": None,
                "correspondence_to_enacted_norm_verified": False,
                "draft_locator": locator,
            })
    return _dedupe_targets(targets), notes


def _draft_locator(artikel: str | None, text: str) -> dict[str, Any] | None:
    if artikel is None:
        return None
    locator: dict[str, Any] = {"artikel": artikel}
    head = re.split(r"[(,§]", text.lstrip(" ("), maxsplit=1)[0]
    for word, unit in (("Nummer", "nummer"), ("Buchstabe", "buchstabe"),
                       ("Doppelbuchstabe", "doppelbuchstabe"),
                       ("Absatz", "absatz")):
        m = re.search(rf"\b{word}\s+(\d+[a-z]?|[a-z]{{1,3}}\d?)\b", head)
        if m:
            locator[unit] = m.group(1)
    if _T_NEU.search(head):
        locator["new_in_draft"] = True
    return locator


def _dedupe_targets(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    out = []
    for row in rows:
        key = (row["act"], row["unit"], row["number"], row["absatz"],
               row["satz"], row["nummer"], row["buchstabe"],
               row["doppelbuchstabe"], row.get("anlage_zu_para"))
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def parse_citation(text: str, act_slugs: dict[str, str], *,
                   draft_acts: Iterable[str] = ()) -> list[dict[str, Any]]:
    """Parse a free-standing citation such as ``"§ 37 SGB XIV"``.

    Used for API ``target`` arguments.  Unresolvable input yields ``[]``.
    """
    ctx = _Ctx(dict(act_slugs), set(draft_acts), {})
    text = _ws(text)
    start = text.find("§")
    if start < 0:
        m = _T_ART.search(text)
        if not m:
            token = _find_act_token(text, act_slugs.keys())
            if token and _ws(text) in {token[0], f"{token[0]}-E"}:
                return [_norm(None, token[0], ctx=ctx, act_basis="explicit",
                              source_text=text)]
            return []
        start = m.start()
    end, refs = _scan_refs(text, start)
    act_hit = _act_after(text, end, act_slugs.keys())
    if not refs or not act_hit or not act_hit.act:
        return []
    return [_norm(ref, act_hit.act, ctx=ctx, act_basis="explicit",
                  source_text=text[start:act_hit.end])
            for ref in refs]


# --------------------------------------------------------------------------
# Heading grammars
# --------------------------------------------------------------------------

_ANLAGE = re.compile(r"Anlage\s+(\d+)")
_ANSCHREIBEN = re.compile(r"^(?:BUNDESREPUBLIK DEUTSCHLAND|DIE BUNDESKANZLERIN|"
                          r"DER BUNDESKANZLER)\b")
_VORBLATT_HEADING = re.compile(
    r"^(?P<num>[A-H](?:\.\d{1,2})?)\.?\s+(?P<title>[A-ZÄÖÜ][^\n]{1,90})$")
_ROMAN_HEADING = re.compile(
    r"^(?P<num>[IVX]{1,6}(?:\.\d{1,2})*)\.?\s+(?P<title>[A-ZÄÖÜ‚„'\"][^\n]{0,110})$")
_BR_HEADING = re.compile(r"^(?P<num>\d{1,3})\.\s+(?P<rest>Zu(?:m)?\s+\S.*)$")
_BR_JUSTIFICATION = re.compile(r"Begründung\s*:")
_GA_START = re.compile(r"^Zu\s+(?:den\s+)?Ziffer(?:n)?\s+\d")
_GA_HEADING = re.compile(
    r"^Zu\s+(?:den\s+)?Ziffer(?:n)?\s+(?P<nums>\d+[a-z]?(?:\s*(?:,|und|bis)\s*"
    r"\d+[a-z]?)*)\s*(?:\((?P<paren>.*))?$")
_ZU_UNITS = {
    "Artikel": ("artikel", 0), "Artike1": ("artikel", 0),
    "Teil": ("teil", 1), "Kapitel": ("kapitel", 2),
    "Abschnitt": ("abschnitt", 3), "Unterabschnitt": ("unterabschnitt", 4),
    "Titel": ("titel", 5), "§": ("para", 6), "§§": ("para", 6),
    "Nummer": ("nummer", 6), "Nummern": ("nummer", 6),
    "Absatz": ("absatz", 7), "Absätze": ("absatz", 7),
    "Satz": ("satz", 8),
    "Buchstabe": ("buchstabe", 9), "Buchstaben": ("buchstabe", 9),
    "Doppelbuchstabe": ("doppelbuchstabe", 10),
    "Doppelbuchstaben": ("doppelbuchstabe", 10),
    "Dreifachbuchstabe": ("dreifachbuchstabe", 11),
}
_ZU_START = re.compile(
    r"^Zu\s+(?:den\s+)?(?:Artikel|Artike1|Teil|Kapitel|Abschnitt|Unterabschnitt|"
    r"Titel|§§?|Nummern?|Absatz|Absätze|Satz|Buchstaben?|Doppelbuchstaben?|"
    r"Dreifachbuchstabe)(?:\s|\d|$)")
_STRUCTURAL = {"teil", "kapitel", "abschnitt", "unterabschnitt", "titel"}
_STRUCT_LINE = re.compile(r"(?:Teil|Kapitel|Abschnitt|Unterabschnitt|Titel)\s+"
                          r"\d+[a-z]?")
_BODY_START = re.compile(r"^(?:Die|Der|Das)\s+(?:Bundesregierung|Bundesrat)\b")
_CITATION_CONT = re.compile(r"§|\bArtikel\b|\bAbsatz\b|\bNummer\b|\bSatz\b|"
                            r"\bSGB\b|[A-Za-z]G\)|\bneu\b")
_TOC_ENTRY = re.compile(r"^(?:§\s*\d+[a-z]?\s+\S|Anlage\b)")
_LIST_START = re.compile(
    r"^(?:[–•▪]\s|-\s|\d{1,3}[.)]\s|[a-z]{1,3}\)\s|\(\d{1,3}[a-z]?\)\s|"
    r"[„‚]\(?\d{1,3}[a-z]?[.)]\s|[„‚][a-z]{1,3}\d?\)\s)")
_TERMINAL = re.compile(r"(?:[.:;!?]|[.!?][“”\"‘’)])$")
_SUSPENDED = {"und", "oder", "bzw.", "bzw", "sowie", "bis"}


def parse_zu_heading(heading: str) -> dict[str, Any] | None:
    """Grammar of Begründung headings: ``Zu § 37 (Titel)``, ``Zu Nummer 1
    und zu Nummer 2``, ``Zu Buchstabe a Doppelbuchstabe aa`` …

    Returns ``None`` for body sentences that merely start with "Zu".
    """
    text = _ws(heading).rstrip(":").strip()
    title = None
    if "(" in text:
        open_at = text.index("(")
        if not text.endswith(")"):
            return None
        title = text[open_at + 1:-1].strip()
        text = text[:open_at].strip()
    m = re.match(r"^Zu\s+(?:den\s+)?", text)
    if not m:
        return None
    tokens = re.findall(r"§§?|\d+[a-z]?|[A-Za-zÄÖÜäöüß]+\d?|,", text[m.end():])
    units: list[tuple[str, list[str]]] = []
    current: str | None = None
    range_next = False
    for token in tokens:
        if token in _ZU_UNITS:
            unit = _ZU_UNITS[token][0]
            if units and units[-1][0] == unit:
                current = unit
            else:
                units.append((unit, []))
                current = unit
            continue
        if token in {"und", "zu", ",", "den"}:
            continue
        if token == "bis":
            range_next = True
            continue
        if current is None:
            return None
        is_value = bool(re.fullmatch(r"\d+[a-z]?", token)) or (
            current in {"buchstabe", "doppelbuchstabe", "dreifachbuchstabe"}
            and re.fullmatch(r"[a-z]{1,3}\d?", token))
        if not is_value:
            return None
        values = units[-1][1]
        if range_next and values and values[-1].isdigit() and token.isdigit() \
                and 0 < int(token) - int(values[-1]) <= 60:
            values.extend(str(n) for n in range(int(values[-1]) + 1,
                                                int(token) + 1))
        else:
            values.append(token)
        range_next = False
    if not units or any(not values for _, values in units):
        return None
    label = "Zu " + text[m.end():].strip()
    return {"units": units, "level": _ZU_UNITS_LEVEL[units[0][0]],
            "title": title, "label": _ws(label)}


_ZU_UNITS_LEVEL = {unit: level for unit, level in _ZU_UNITS.values()}
_UNIT_SLUG = {"artikel": "artikel", "teil": "teil", "kapitel": "kapitel",
              "abschnitt": "abschnitt", "unterabschnitt": "unterabschnitt",
              "titel": "titel", "para": "para", "nummer": "nummer",
              "absatz": "absatz", "satz": "satz", "buchstabe": "buchstabe",
              "doppelbuchstabe": "doppelbuchstabe",
              "dreifachbuchstabe": "dreifachbuchstabe"}


def _paren_delta(text: str) -> int:
    return text.count("(") - text.count(")")


# --------------------------------------------------------------------------
# Government position (Gegenäußerung)
# --------------------------------------------------------------------------

def _first_sentence(text: str) -> str:
    for m in re.finditer(r"[.!?](?=\s+[A-ZÄÖÜ„]|\s*$)", text):
        before = text[:m.start()]
        if re.search(r"(?:\bz\. ?B|\bNr|\bAbs|\bvgl|\bbzw|\bggf|\bd\. ?h|"
                     r"\bu\. ?a|\bS|\bff?|\bArt)$", before):
            continue
        return text[:m.end()]
    return text


def classify_government_position(sentence: str) -> str | None:
    s = _ws(sentence)
    if re.search(r"\bteilweise\b|\bin\s+modifizierter\s+Form\b|"
                 r"\bmit\s+(?:der\s+)?Maßgabe\b|\bim\s+Grundsatz\b", s) and \
            re.search(r"\bstimmt\b.*\bzu\b", s):
        return "partially_agrees"
    if re.search(r"\bprüft\b|\bwird\b[^.]*\bprüfen\b", s):
        return "examines"
    if re.search(r"\bstimmt\b[^.]*?\bnicht\s+zu\b", s) or \
            re.search(r"\blehnt\b[^.]*\bab\b", s):
        return "rejects"
    if re.search(r"\bstimmt\b[^.]*?\bzu\b", s):
        return "agrees"
    return None


def _government_position(body: str) -> tuple[str | None, dict[str, Any]]:
    paragraphs = [p.strip() for p in body.split("\n") if p.strip()]
    statements: list[dict[str, Any]] = []
    subpart = None
    for k, para in enumerate(paragraphs):
        if re.match(r"^Zu\s+Artikel\b.*:$", para):
            subpart = para.rstrip(":")
            continue
        if para.startswith("Die Bundesregierung") and (
                k == 0 or subpart is not None and
                paragraphs[k - 1].rstrip(":") == subpart):
            sentence = _first_sentence(para)
            statements.append({"subpart": subpart, "sentence": sentence,
                               "position": classify_government_position(
                                   sentence)})
    positions = {row["position"] for row in statements}
    value = positions.pop() if len(positions) == 1 else None
    basis: dict[str, Any] = {"derived_by": "parser",
                             "legal_effect_verified": False,
                             "statements": statements}
    if not statements:
        basis["note"] = "no position sentence found"
    elif value is None:
        basis["note"] = "sub-parts state differing positions"
    return value, basis


# --------------------------------------------------------------------------
# Segmentation
# --------------------------------------------------------------------------

@dataclass
class _Section:
    role: str
    marker: int                 # stream index where the section starts
    start: int                  # first stream index after the title
    end: int = 0
    title_idx: list[int] = field(default_factory=list)
    title: str = ""


@dataclass
class _Frag:
    role: str
    key: str
    heading_idx: list[int]
    section_path: list[str]
    body: list[int] = field(default_factory=list)
    just_heading_idx: list[int] | None = None
    just_body: list[int] | None = None
    targets: list[dict[str, Any]] = field(default_factory=list)
    relation: str = "explains"
    confidence: str = "high"
    default_act: str | None = None
    truncated: bool = False
    context_gap: bool = False
    keep_empty: bool = False
    skip_mentions: bool = False
    notes: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def add(self, index: int) -> None:
        (self.just_body if self.just_body is not None else self.body).append(index)


class _Parser:
    def __init__(self, pages: Sequence[str | None], *, drucksache: str,
                 meta: dict[str, Any], act_slugs: dict[str, str]):
        self.pages = pages
        self.drucksache = drucksache
        self.meta = meta
        chamber = str(meta.get("chamber") or "BT")
        self.doc_id = drucksache_slug(drucksache, chamber)
        draft_created = meta.get("draft_created_acts") or []
        self.ctx = _Ctx(
            dict(act_slugs),
            {row["act"] for row in draft_created if row.get("act")},
            {str(row["artikel"]): row["act"] for row in draft_created
             if row.get("artikel") and row.get("act")})
        self.stream: list[_Item] = []
        self.page_info: dict[int, dict[str, Any]] = {}
        self.warnings: list[str] = []
        self.fragments: list[dict[str, Any]] = []
        self.ids: set[str] = set()
        self.detected: dict[str, Any] = {}
        self.orphans: list[int] = []
        self.stage = procedure_stage(meta)
        self.adoption_status, self.adoption_basis = adoption(meta)

    # -- stream ------------------------------------------------------------
    def build_stream(self) -> None:
        for index, text in enumerate(self.pages):
            page = index + 1
            if text is None:
                if not self.stream or self.stream[-1].kind != "gap":
                    self.stream.append(_Item("gap", page))
                continue
            furniture = page_furniture(text)
            header = next((row["text"] for row in furniture
                           if row["kind"] == "drucksache_running_header"), None)
            if header:
                named = re.search(r"Drucksache\s+(\d{1,4}/\d{1,6})", header)
                if named and named.group(1) != self.drucksache:
                    self.warnings.append(
                        f"page {page}: running header names Drucksache "
                        f"{named.group(1)}, expected {self.drucksache}")
            self.page_info[page] = {
                "pdf_page": page,
                "printed_page": _printed_page(furniture),
                "text_sha256": _sha(text),
                "characters": len(text),
                "furniture": [{"kind": row["kind"], "start": row["start"],
                               "end": row["end"],
                               "detected_by": row["detected_by"]}
                              for row in furniture],
            }
            items = _page_items(page, text, furniture)
            if self.stream and self.stream[-1].kind != "gap":
                self.stream.append(_Item("page_break", page))
            self.stream.extend(items)
        self.widths: dict[int, int] = {}
        per_page: dict[int, list[int]] = {}
        for item in self.stream:
            if item.kind == "text":
                per_page.setdefault(item.page, []).append(item.width)
        for page, widths in per_page.items():
            widths.sort()
            self.widths[page] = (widths[int(0.8 * (len(widths) - 1))]
                                 if len(widths) >= 5 else widths[-1])

    def printed(self, page: int) -> int | None:
        return self.page_info.get(page, {}).get("printed_page")

    def text_at(self, k: int) -> _Item | None:
        if 0 <= k < len(self.stream) and self.stream[k].kind == "text":
            return self.stream[k]
        return None

    def next_text(self, k: int, *, blanks: int = 99) -> int | None:
        seen = 0
        while k < len(self.stream):
            kind = self.stream[k].kind
            if kind == "text":
                return k
            if kind == "gap":
                return None
            seen += 1
            if seen > blanks:
                return None
            k += 1
        return None

    # -- sections ------------------------------------------------------------
    def detect_sections(self) -> list[_Section]:
        doc_type = str(self.meta.get("type") or "")
        role = ("beschlussempfehlung" if "Beschlussempfehlung" in doc_type
                else "vorblatt")
        sections = [_Section(role, 0, 0)]
        i = 0
        while i < len(self.stream):
            item = self.stream[i]
            if item.kind != "text":
                i += 1
                continue
            text = item.text
            if role == "vorblatt" and _ANSCHREIBEN.match(text):
                role = "anschreiben"
                sections.append(_Section(role, i, i))
            elif _ANLAGE.fullmatch(text):
                j = self.next_text(i + 1)
                if j is not None and self.anlage_title_role(j) is not None:
                    title_idx = [j]
                    k = j + 1
                    while len(title_idx) < 3:
                        nxt = self.text_at(k)
                        if nxt is None or nxt.indent < 5 or \
                                self.stream[title_idx[-1]].text.endswith("."):
                            break
                        title_idx.append(k)
                        k += 1
                    title = _ws(" ".join(self.stream[t].text for t in title_idx))
                    role = self.anlage_title_role(j) or "unrecognized"
                    if role == "unrecognized":
                        self.warnings.append(
                            f"page {item.page}: unrecognised Anlage titled "
                            f"{title[:80]!r}; its text is not segmented")
                    sections.append(_Section(role, i, title_idx[-1] + 1,
                                             title_idx=title_idx, title=title))
                    i = title_idx[-1] + 1
                    continue
            elif text == "Begründung" and role in {"vorblatt", "anschreiben",
                                                     "entwurf"}:
                role = "begruendung"
                sections.append(_Section(role, i, i + 1, title_idx=[i],
                                         title="Begründung"))
            elif role == "beschlussempfehlung" and \
                    re.match(r"^Bericht\s+der\s+Abgeordneten\b", text):
                role = "bericht"
                sections.append(_Section(role, i, i, title="Bericht"))
            i += 1
        for k, section in enumerate(sections):
            section.end = (sections[k + 1].marker if k + 1 < len(sections)
                           else len(self.stream))
        return [s for s in sections if s.end > s.marker]

    def anlage_title_role(self, j: int) -> str | None:
        """Role announced by the line after an "Anlage N" marker; ``None``
        when that line does not open a Drucksache part (an annex inside the
        draft text, for instance)."""
        title = self.stream[j].text
        role = _anlage_role(title)
        if role != "unrecognized":
            return role
        if re.match(r"^(?:Stellungnahme|Gegenäußerung|Bericht|"
                    r"Beschlussempfehlung|Formulierungshilfe)\b", title):
            return "unrecognized"
        return None

    def section_rows(self, sections: list[_Section]) -> list[dict[str, Any]]:
        rows = []
        for section in sections:
            pages = [self.stream[k].page for k in range(section.marker,
                                                        section.end)
                     if self.stream[k].kind == "text"]
            if not pages:
                continue
            gaps = any(self.stream[k].kind == "gap"
                       for k in range(section.marker, section.end))
            rows.append({
                "role": section.role,
                "title": section.title or None,
                "pdf_page_start": pages[0],
                "pdf_page_end": pages[-1],
                "printed_page_start": self.printed(pages[0]),
                "printed_page_end": self.printed(pages[-1]),
                "contains_unextracted_pages": gaps,
                "segmented": section.role in ROLES,
            })
        return rows

    # -- generic loop ------------------------------------------------------
    def loop(self, section: _Section, detect, *, on_line=None,
             preamble=None, on_gap=None) -> None:
        current: _Frag | None = None
        after_gap = False
        i = section.start
        while i < section.end:
            item = self.stream[i]
            if item.kind == "gap":
                if current is not None:
                    current.truncated = True
                    self.emit(current)
                    current = None
                if on_gap:
                    on_gap()
                after_gap = True
                i += 1
                continue
            if item.kind != "text":
                if current is not None:
                    current.add(i)
                i += 1
                continue
            hit = detect(i, current)
            if hit is not None:
                nxt, frag = hit
                if current is not None:
                    self.emit(current)
                current = frag
                after_gap = False
                i = nxt
                continue
            if current is None and after_gap:
                # continuation of a passage whose heading lies on a page
                # that was not extracted: never attributed to a heading
                self.orphans.append(i)
                i += 1
                continue
            if current is None and preamble is not None:
                current = preamble()
            if on_line is not None and on_line(i, current):
                i += 1
                continue
            if current is not None:
                current.add(i)
            i += 1
        if current is not None:
            self.emit(current)

    def preamble(self, section: _Section, role: str):
        def make() -> _Frag:
            return _Frag(role, "preamble", list(section.title_idx),
                         [section.title] if section.title else [])
        return make

    def heading_lines(self, i: int, *, starts_heading,
                      zu_col: int | None = None) -> list[int]:
        """Stream indices of a (possibly wrapped) heading starting at ``i``."""
        idx = [i]
        depth = _paren_delta(self.stream[i].text)
        if depth > 0:
            d, k, candidates = depth, i + 1, []
            balanced = False
            while len(candidates) < 4:
                item = self.text_at(k)
                if item is None or starts_heading(item) or \
                        _BODY_START.match(item.text):
                    break
                candidates.append(k)
                d += _paren_delta(item.text)
                if d <= 0:
                    balanced = True
                    break
                if item.text.endswith((".", ":")):
                    break
                k += 1
            if balanced:
                idx.extend(candidates)
            else:
                # The source itself leaves the parenthetical open (seen in
                # Gegenäußerung headings).  Accept only lines that read like
                # citation continuations and end on a closing parenthesis.
                for k in candidates:
                    text = self.stream[k].text
                    if not _CITATION_CONT.search(text) or \
                            text.endswith((".", ":")):
                        break
                    idx.append(k)
                while len(idx) > 1 and not self.stream[idx[-1]].text.endswith(")"):
                    idx.pop()
        if zu_col is not None:
            k = idx[-1] + 1
            while len(idx) < 8:
                item = self.text_at(k)
                if item is None or starts_heading(item) or \
                        _BR_JUSTIFICATION.fullmatch(item.text):
                    break
                previous = self.stream[idx[-1]].text
                if previous.endswith((",", "–")) or (
                        item.indent > zu_col and
                        re.match(r"(?:Artikel|Artike1)\s+\d|§", item.text)):
                    idx.append(k)
                    k += 1
                    continue
                break
        return idx

    # -- composition -------------------------------------------------------
    def joiner(self, prev: _Item, cur: _Item, blank: bool, *,
               paragraphs: bool) -> str:
        pt, ct = prev.text, cur.text
        if blank and paragraphs:
            return "\n"
        if re.search(r"[A-Za-zÄÖÜäöüß]-$", pt):
            first = ct.split(" ", 1)[0]
            if first and first[0].islower():
                return " " if first.rstrip(",.;") in _SUSPENDED else "join"
            if first and (first[0].isupper() or first[0].isdigit()):
                return "keep"
        if not paragraphs:
            return " "
        if _LIST_START.match(ct):
            return "\n"
        width = self.widths.get(prev.page) or prev.width or 1
        if prev.width < 0.85 * width and _TERMINAL.search(pt):
            return "\n"
        if prev.width < 0.6 * width and not pt.endswith((",", "-", "–")):
            return "\n"
        return " "

    def compose(self, indices: Iterable[int], *, paragraphs: bool = True
                ) -> tuple[str, list[tuple[int, int]]]:
        chars: list[str] = []
        cmap: list[tuple[int, int]] = []
        prev: _Item | None = None
        blank = False
        for k in indices:
            item = self.stream[k]
            if item.kind == "blank":
                blank = prev is not None
                continue
            if item.kind != "text":
                continue
            if prev is not None:
                sep = self.joiner(prev, item, blank, paragraphs=paragraphs)
                if sep == "join":
                    chars.pop()
                    cmap.pop()
                elif sep != "keep":
                    chars.append(sep)
                    cmap.append((prev.page, prev.end))
            space = False
            for offset, ch in enumerate(item.text):
                if ch in " \t":
                    if space:
                        continue
                    space = True
                    chars.append(" ")
                else:
                    space = False
                    chars.append(ch)
                cmap.append((item.page, item.start + offset))
            prev = item
            blank = False
        return "".join(chars), cmap

    def spans(self, indices: Iterable[int]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for k in indices:
            item = self.stream[k]
            if item.kind != "text":
                continue
            if rows and rows[-1]["pdf_page"] == item.page:
                rows[-1]["end"] = item.end
            else:
                rows.append({"pdf_page": item.page,
                             "printed_page": self.printed(item.page),
                             "start": item.start, "end": item.end})
        return rows

    # -- mentions & relations ---------------------------------------------
    def mentions(self, text: str, cmap: list[tuple[int, int]], *,
                 default_act: str | None, targets: list[dict[str, Any]],
                 part: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        known = self.ctx.act_slugs.keys()
        pos = 0
        while True:
            m = re.search(r"§|\b(?:Artikel|Art\.)(?=\s*\d)", text[pos:])
            if not m:
                break
            start = pos + m.start()
            end, refs = _scan_refs(text, start)
            if not refs:
                pos = start + 1
                continue
            act_hit = _act_after(text, end, known)
            source_end = end
            draft = False
            if act_hit is not None:
                source_end = act_hit.end
                draft = act_hit.draft_suffix
                if act_hit.basis == "self_reference":
                    act, basis = default_act, ("self_reference" if default_act
                                               else "unresolved")
                else:
                    act, basis = act_hit.act, act_hit.basis
            elif refs[0].unit == "art":
                # "Artikel 1" without an act names an article of the draft
                # itself, not a norm.
                pos = end
                continue
            else:
                act = default_act
                basis = "context" if default_act else "unresolved"
            source = text[start:source_end]
            for ref in refs:
                row = _norm(ref, act, ctx=self.ctx, act_basis=basis,
                            source_text=source, draft_suffix=draft)
                if basis == "explicit_unmapped":
                    row["canonical_id"] = None
                if _covered_by_target(row, targets):
                    continue
                first = cmap[start]
                last = cmap[source_end - 1]
                same_page = [cmap[k] for k in range(start, source_end)
                             if cmap[k][0] == first[0]]
                span = {"pdf_page": first[0], "start": first[1],
                        "end": same_page[-1][1] + 1}
                if last[0] != first[0]:
                    span["continues_on_pdf_page"] = last[0]
                row.update(part=part, text_start=start, text_end=source_end,
                           span=span,
                           quoted_context=mention_context(text, start,
                                                          source_end))
                rows.append(row)
            pos = max(source_end, start + 1)
        return rows

    def relations(self, frag_id: str, frag: _Frag, heading: str,
                  heading_span: dict[str, Any],
                  mentions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        shell = {"id": frag_id, "heading": heading,
                 "heading_span": heading_span}
        rows = [relation_row(shell, relation=frag.relation, norm=target,
                             confidence=frag.confidence, from_mention=False)
                for target in frag.targets]
        rows += [relation_row(shell, relation="mentions", norm=mention,
                              confidence="medium", from_mention=True)
                 for mention in mentions]
        return rows

    # -- emit ------------------------------------------------------------
    def emit(self, frag: _Frag) -> None:
        body_text_idx = [k for k in frag.body if self.stream[k].kind == "text"]
        just_idx = [k for k in (frag.just_body or [])
                    if self.stream[k].kind == "text"]
        if not body_text_idx and not just_idx and not frag.keep_empty:
            return
        if not frag.heading_idx:
            return
        heading, _ = self.compose(frag.heading_idx, paragraphs=False)
        heading = _ws(heading)
        first = self.stream[frag.heading_idx[0]]
        last = self.stream[frag.heading_idx[-1]]
        heading_span = {"pdf_page": first.page,
                        "printed_page": self.printed(first.page),
                        "start": first.start, "end": last.end}
        body, cmap = self.compose(frag.body)
        base = f"{self.doc_id}:{ROLE_SLUGS[frag.role]}:{frag.key}"
        frag_id, n = base, 1
        while frag_id in self.ids:
            n += 1
            frag_id = f"{base}~{n}"
        if n > 1:
            frag.notes.append("duplicate_heading_key")
        self.ids.add(frag_id)
        mentions = [] if frag.skip_mentions else self.mentions(
            body, cmap, default_act=frag.default_act, targets=frag.targets,
            part="body")
        justification = None
        if frag.just_heading_idx is not None:
            j_heading, _ = self.compose(frag.just_heading_idx, paragraphs=False)
            j_body, j_map = self.compose(frag.just_body or [])
            j_first = self.stream[frag.just_heading_idx[0]]
            j_last = self.stream[frag.just_heading_idx[-1]]
            justification = {
                "heading": _ws(j_heading),
                "body": j_body,
                "heading_span": {"pdf_page": j_first.page,
                                 "printed_page": self.printed(j_first.page),
                                 "start": j_first.start, "end": j_last.end},
                "body_spans": self.spans(frag.just_body or []),
                "text_sha256": _sha(_ws(j_heading) + "\n" + j_body),
            }
            mentions += self.mentions(j_body, j_map,
                                      default_act=frag.default_act,
                                      targets=frag.targets,
                                      part="justification")
        pages = [self.stream[k].page for k in
                 [*frag.heading_idx, *body_text_idx, *just_idx,
                  *(frag.just_heading_idx or [])]]
        notes = list(frag.notes)
        if frag.truncated:
            notes.append("truncated_at_unextracted_page")
        if frag.context_gap:
            notes.append("context_resolved_across_unextracted_pages")
        row: dict[str, Any] = {
            "id": frag_id,
            "drucksache": self.drucksache,
            "role": frag.role,
            "ordinal": len(self.fragments),
            "section_path": frag.section_path,
            "procedure_stage": self.stage,
            "adoption_status": self.adoption_status,
            "adoption_basis": self.adoption_basis,
            "heading": heading,
            "body": body,
            "heading_span": heading_span,
            "body_spans": self.spans(frag.body),
            "page_start": min(pages),
            "page_end": max(pages),
            "complete": not frag.truncated,
            "target_norms": frag.targets,
            "mentions": mentions,
            "relations": self.relations(frag_id, frag, heading, heading_span,
                                        mentions),
        }
        if justification is not None:
            row["justification"] = justification
        if frag.role == "gegenaeusserung_bundesregierung" and \
                frag.key.startswith("ziffer-"):
            value, basis = _government_position(body)
            row["government_position"] = value
            row["government_position_basis"] = basis
        row.update(frag.extra)
        row["notes"] = notes
        row["text_sha256"] = _sha(heading + "\n" + body)
        row["parser_version"] = PARSER_VERSION
        self.fragments.append(row)

    # -- role handlers -----------------------------------------------------
    def vorblatt(self, section: _Section) -> None:
        title_block: list[int] = []
        parents: dict[str, str] = {}
        seen_heading = [False]

        def detect(i: int, current):
            item = self.stream[i]
            m = _VORBLATT_HEADING.match(item.text)
            if not m or item.indent > 3 or item.text.endswith((",", ".", ";")):
                return None
            seen_heading[0] = True
            num = m.group("num")
            label = _ws(item.text)
            path = [label]
            if "." in num:
                parent = parents.get(num.split(".")[0])
                if parent:
                    path = [parent, label]
            else:
                parents[num] = label
            key = num.lower().replace(".", "-")
            return i + 1, _Frag("vorblatt", key, [i], path, targets=[])

        def on_line(i: int, current) -> bool:
            if not seen_heading[0]:
                title_block.append(i)
                return True
            return False

        self.loop(section, detect, on_line=on_line)
        if title_block:
            block, _ = self.compose(title_block)
            lines = [self.stream[k].text for k in title_block]
            title = None
            for k, line in enumerate(lines):
                if line.startswith("Entwurf eines"):
                    # the title closes the title block of a Gesetzentwurf
                    title = " ".join(lines[k:])
                    break
            self.detected["title_block"] = block.strip()
            self.detected["title"] = _ws(title) if title else None
            self.detected["title_block_spans"] = self.spans(title_block)

    def entwurf(self, section: _Section) -> None:
        state: dict[str, Any] = {"artikel": None, "act": None, "stamm": False,
                                 "struct": [], "in_toc": False}
        absatz = re.compile(r"\(\d{1,3}[a-z]?\)")

        def is_heading_line(item: _Item) -> bool:
            return item.indent >= 15 and bool(
                re.fullmatch(r"(?:Artikel|§)\s*\d+[a-z]?", item.text)
                or _STRUCT_LINE.fullmatch(item.text))

        def block_title(i: int, *, max_lines: int = 4) -> list[int]:
            """Title lines under an Artikel/§/structural heading line."""
            j = self.next_text(i + 1, blanks=2)
            if j is None or any(self.stream[k].kind == "page_break"
                                for k in range(i + 1, j)):
                return []
            first = self.stream[j]
            if absatz.match(first.text) or is_heading_line(first):
                return []
            out = [j]
            width = self.widths.get(first.page, 1)
            while len(out) < max_lines:
                prev = self.stream[out[-1]]
                item = self.text_at(out[-1] + 1)
                if item is None or absatz.match(item.text) or \
                        is_heading_line(item):
                    break
                wraps = prev.text.endswith(("-", "–")) or \
                    prev.width >= 0.85 * width or \
                    (not _TERMINAL.search(prev.text) and item.indent >= 10)
                # a justified body line runs to the right margin; a wrapped
                # (centred) title line does not
                body_like = item.indent < 10 and item.width >= 0.93 * width
                if wraps and not body_like:
                    out.append(out[-1] + 1)
                    continue
                break
            return out

        def reclaim_structure(current: _Frag | None) -> None:
            """The Inhaltsübersicht ends where the first real § heading
            begins; structural headings printed between its last entry and
            that § belong to the text, not to the table of contents."""
            if current is None:
                return
            k = len(current.body) - 1
            while k >= 0:
                item = self.stream[current.body[k]]
                if item.kind == "text" and item.indent < 10:
                    break
                k -= 1
            trailing = current.body[k + 1:]
            del current.body[k + 1:]
            struct: list[tuple[int, str, str]] = []
            for idx in trailing:
                item = self.stream[idx]
                m = _STRUCT_LINE.fullmatch(item.text) if item.kind == "text" \
                    else None
                if not m:
                    continue
                unit, number = item.text.split()[0].lower(), item.text.split()[1]
                level = _ZU_UNITS_LEVEL[unit]
                label = _ws(" ".join(self.stream[t].text
                                     for t in [idx, *block_title(idx)]))
                struct = [row for row in struct if row[0] < level]
                struct.append((level, f"{unit}-{number}", label))
            state["struct"] = struct

        def detect(i: int, current):
            item = self.stream[i]
            text = item.text
            if item.indent < 15:
                return None
            m = re.fullmatch(r"Artikel\s+(\d+[a-z]?)", text)
            if m:
                number = m.group(1)
                title_idx = block_title(i)
                title = _ws(" ".join(self.stream[k].text for k in title_idx))
                act = act_from_title(title)
                stamm = bool(title) and not re.match(
                    r"(?:Änderung|Aufhebung|Inkrafttreten|Außerkrafttreten|"
                    r"Folgeänderung|Bekanntmachung|Einschränkung|Übergang)",
                    title)
                if act:
                    self.ctx.artikel_acts.setdefault(number, act)
                    if stamm:
                        self.ctx.draft_acts.add(act)
                state.update(artikel=number, act=act, stamm=stamm, struct=[],
                             in_toc=False)
                targets = [_norm(None, act, ctx=self.ctx,
                                 act_basis="artikel_heading",
                                 source_text=title)] if act else []
                for target in targets:
                    target["draft_locator"] = {"artikel": number}
                frag = _Frag("entwurf", f"artikel-{number}", [i, *title_idx],
                             [f"Artikel {number}"], targets=targets,
                             relation="drafts", default_act=act)
                return (title_idx[-1] + 1 if title_idx else i + 1), frag
            if not state["stamm"]:
                return None
            m = _STRUCT_LINE.fullmatch(text)
            if m:
                if state["in_toc"]:
                    return None     # a heading inside the Inhaltsübersicht
                title_idx = block_title(i)
                unit, number = text.split()[0].lower(), text.split()[1]
                level = _ZU_UNITS_LEVEL[unit]
                struct = [row for row in state["struct"] if row[0] < level]
                label = _ws(" ".join(self.stream[k].text
                                     for k in [i, *title_idx]))
                struct.append((level, f"{unit}-{number}", label))
                state["struct"] = struct
                act = state["act"]
                targets = []
                if act:
                    target = _norm(None, act, ctx=self.ctx,
                                   act_basis="artikel_heading",
                                   source_text=label)
                    target["structure"] = {unit: number}
                    targets.append(target)
                key = f"artikel-{state['artikel']}:" + ":".join(
                    row[1] for row in struct)
                return ((title_idx[-1] if title_idx else i) + 1,
                        _Frag("entwurf", key, [i, *title_idx],
                              [f"Artikel {state['artikel']}",
                               *[row[2] for row in struct]],
                              targets=targets, relation="drafts",
                              default_act=act))
            m = re.fullmatch(r"§\s*(\d+[a-z]?)", text)
            if m:
                if state["in_toc"]:
                    state["in_toc"] = False
                    reclaim_structure(current)
                number = m.group(1)
                title_idx = block_title(i, max_lines=3)
                act = state["act"]
                ref = _Ref("para", [("para", number)])
                target = _norm(ref, act, ctx=self.ctx,
                               act_basis="artikel_heading" if act
                               else "unresolved", source_text=f"§ {number}")
                target["draft_locator"] = {"artikel": state["artikel"]}
                label = _ws(" ".join(self.stream[k].text
                                     for k in [i, *title_idx]))
                return ((title_idx[-1] if title_idx else i) + 1,
                        _Frag("entwurf",
                              f"artikel-{state['artikel']}:para-{number}",
                              [i, *title_idx],
                              [f"Artikel {state['artikel']}",
                               *[row[2] for row in state["struct"]], label],
                              targets=[target], relation="drafts",
                              default_act=act))
            return None

        def on_line(i: int, current) -> bool:
            if self.stream[i].text == "Inhaltsübersicht" and current is not None:
                current.skip_mentions = True
                current.notes.append("contains_table_of_contents")
                if state["stamm"]:
                    state["in_toc"] = True
            return False

        def on_gap() -> None:
            state["struct"] = []
            state["in_toc"] = False

        self.loop(section, detect, on_line=on_line, on_gap=on_gap,
                  preamble=self.preamble(section, "entwurf"))

    def begruendung(self, section: _Section) -> None:
        state: dict[str, Any] = {"part": None, "stack": [], "gap": False}

        def starts(item: _Item) -> bool:
            return bool(_ZU_START.match(item.text))

        def artikel_entry() -> dict[str, Any] | None:
            return next((row for row in state["stack"]
                         if row["unit"] == "artikel"), None)

        def detect(i: int, current):
            item = self.stream[i]
            text = item.text
            if re.fullmatch(r"A\.\s+Allgemeiner\s+Teil", text):
                state.update(part="A", stack=[], gap=False)
                return i + 1, _Frag("begruendung", "allgemeiner-teil", [i],
                                    ["A. Allgemeiner Teil"])
            if re.fullmatch(r"B\.\s+Besonderer\s+Teil", text):
                state.update(part="B", stack=[], gap=False)
                return i + 1, _Frag("begruendung", "besonderer-teil", [i],
                                    ["B. Besonderer Teil"])
            if state["part"] in {"A", None} and item.indent < 4:
                m = _ROMAN_HEADING.match(text)
                if m and not text.endswith((",", ";")):
                    number = m.group("num").lower().replace(".", "-")
                    prefix = ["A. Allgemeiner Teil"] if state["part"] == "A" else []
                    return i + 1, _Frag("begruendung",
                                        f"allgemeiner-teil:{number}", [i],
                                        [*prefix, _ws(text)])
            if item.indent >= 4 or not _ZU_START.match(text):
                return None
            idx = self.heading_lines(i, starts_heading=starts)
            heading, _ = self.compose(idx, paragraphs=False)
            parsed = parse_zu_heading(heading)
            if parsed is None:
                return None
            return idx[-1] + 1, self.zu_fragment(idx, parsed, state)

        def on_gap() -> None:
            state["stack"] = [row for row in state["stack"] if row["level"] == 0]
            state["gap"] = True

        self.loop(section, detect, on_gap=on_gap,
                  preamble=self.preamble(section, "begruendung"))

    def zu_fragment(self, idx: list[int], parsed: dict[str, Any],
                    state: dict[str, Any]) -> _Frag:
        units = parsed["units"]
        first_unit, first_values = units[0]
        level = parsed["level"]
        entry = {"level": level, "unit": first_unit, "units": units,
                 "label": parsed["label"], "title": parsed["title"]}
        if first_unit == "artikel":
            act = act_from_title(parsed["title"] or "")
            number = first_values[0]
            if act:
                self.ctx.artikel_acts.setdefault(number, act)
            entry["act"] = act or self.ctx.artikel_acts.get(number)
            entry["stamm"] = bool(parsed["title"]) and not re.match(
                r"(?:Änderung|Aufhebung|Inkrafttreten|Außerkrafttreten|"
                r"Folgeänderung|Bekanntmachung|Einschränkung|Übergang)",
                parsed["title"] or "")
            state["gap"] = False
        stack = [row for row in state["stack"] if row["level"] < level]
        stack.append(entry)
        state["stack"] = stack
        artikel = next((row for row in stack if row["unit"] == "artikel"), None)
        act = artikel.get("act") if artikel else None
        para = next((row for row in stack if row["unit"] == "para"), None)
        targets: list[dict[str, Any]] = []
        notes: list[str] = []
        confidence = "high"
        if first_unit == "artikel":
            if act:
                target = _norm(None, act, ctx=self.ctx,
                               act_basis="artikel_heading",
                               source_text=parsed["title"] or act)
                target["draft_locator"] = {"artikel": first_values[0]}
                targets.append(target)
        elif first_unit in _STRUCTURAL:
            if act:
                target = _norm(None, act, ctx=self.ctx,
                               act_basis="artikel_heading",
                               source_text=parsed["label"])
                target["structure"] = {u: v[0] for u, v in units}
                targets.append(target)
        elif para is not None:
            refined = [(u, v) for u, v in units if u != "para"]
            below_para = [row for row in stack if row["level"] > para["level"]
                          and row is not entry]
            prefix: list[tuple[str, str]] = []
            for row in below_para:
                for u, values in row["units"]:
                    if u in _HIERARCHY:
                        prefix.append((u, values[0]))
            for number in para["units"][0][1]:
                combos: list[list[tuple[str, str]]] = [[("para", number),
                                                        *prefix]]
                for unit, values in (refined if entry is not para else []):
                    if unit not in _HIERARCHY:
                        continue
                    combos = [[*c, (unit, value)] for c in combos
                              for value in values]
                for parts in combos:
                    target = _norm(_Ref("para", parts), act, ctx=self.ctx,
                                   act_basis="artikel_heading" if act
                                   else "unresolved",
                                   source_text=parsed["label"])
                    target["draft_locator"] = ({"artikel": artikel["units"][0][1][0]}
                                               if artikel else None)
                    targets.append(target)
        else:
            locator: dict[str, Any] = {}
            if artikel:
                locator["artikel"] = artikel["units"][0][1][0]
            for row in stack:
                if row["unit"] in {"artikel", *_STRUCTURAL}:
                    continue
                for u, values in row["units"]:
                    locator[u] = values if len(values) > 1 else values[0]
            if act and not (artikel or {}).get("stamm"):
                target = _norm(None, act, ctx=self.ctx,
                               act_basis="artikel_heading",
                               source_text=parsed["label"])
                target["draft_locator"] = locator
                target["resolution"] = "amended_provision_not_named_in_heading"
                targets.append(target)
                confidence = "medium"
            else:
                notes.append("target_not_resolvable_from_heading")
        if state["gap"]:
            confidence = "medium"
        key_rows = stack
        if para is not None:
            key_rows = [row for row in stack if row["unit"] not in _STRUCTURAL]
        key = ":".join(
            "-".join([_UNIT_SLUG[u], *values])
            for row in key_rows for u, values in row["units"])
        path = (["B. Besonderer Teil"] if state["part"] == "B" else []) + \
            [row["label"] for row in stack]
        frag = _Frag("begruendung", key, idx, path, targets=targets,
                     confidence=confidence, default_act=act, notes=notes,
                     context_gap=state["gap"])
        return frag

    def bundesrat(self, section: _Section) -> None:
        def starts(item: _Item) -> bool:
            return bool(_BR_HEADING.match(item.text)) and item.indent <= 3

        def detect(i: int, current):
            item = self.stream[i]
            m = _BR_HEADING.match(item.text)
            if not m or item.indent > 3:
                return None
            zu_col = item.indent + m.start("rest")
            idx = self.heading_lines(i, starts_heading=starts, zu_col=zu_col)
            heading, _ = self.compose(idx, paragraphs=False)
            heading = _ws(heading)
            rest = _BR_HEADING.match(heading).group("rest")
            targets, notes = heading_targets(rest, self.ctx)
            number = m.group("num")
            acts = {t["act"] for t in targets if t["act"]}
            frag = _Frag("stellungnahme_bundesrat", f"ziffer-{number}", idx,
                         [section.title or "Stellungnahme des Bundesrates",
                          f"Ziffer {number}"], targets=targets,
                         default_act=acts.pop() if len(acts) == 1 else None,
                         keep_empty=True, notes=notes)
            frag.extra["ziffer"] = number
            return idx[-1] + 1, frag

        def on_line(i: int, current) -> bool:
            if current is not None and current.key.startswith("ziffer-") and \
                    current.just_heading_idx is None and \
                    _BR_JUSTIFICATION.fullmatch(self.stream[i].text):
                current.just_heading_idx = [i]
                current.just_body = []
                return True
            return False

        self.loop(section, detect, on_line=on_line,
                  preamble=self.preamble(section, "stellungnahme_bundesrat"))

    def gegenaeusserung(self, section: _Section) -> None:
        def starts(item: _Item) -> bool:
            return bool(_GA_START.match(item.text))

        def detect(i: int, current):
            item = self.stream[i]
            if item.indent > 3 or not _GA_START.match(item.text):
                return None
            idx = self.heading_lines(i, starts_heading=starts)
            heading, _ = self.compose(idx, paragraphs=False)
            heading = _ws(heading)
            m = _GA_HEADING.match(heading)
            if not m:
                return None
            numbers = re.findall(r"\d+[a-z]?", m.group("nums"))
            if "bis" in m.group("nums") and len(numbers) == 2 and \
                    all(n.isdigit() for n in numbers):
                numbers = [str(n) for n in range(int(numbers[0]),
                                                 int(numbers[1]) + 1)]
            paren = (m.group("paren") or "").strip()
            if paren.endswith(")"):
                paren = paren[:-1]
            targets, notes = heading_targets(paren, self.ctx) if paren \
                else ([], [])
            if paren and _paren_delta("(" + paren + ")") != 0:
                notes.append("source_parenthetical_unbalanced")
            acts = {t["act"] for t in targets if t["act"]}
            frag = _Frag("gegenaeusserung_bundesregierung",
                         "ziffer-" + "-".join(numbers), idx,
                         [section.title or "Gegenäußerung der Bundesregierung",
                          "Zu Ziffer " + ", ".join(numbers)],
                         targets=targets,
                         default_act=acts.pop() if len(acts) == 1 else None,
                         keep_empty=True, notes=notes)
            responds = [f"{self.doc_id}:stellungnahme-bundesrat:ziffer-{n}"
                        for n in numbers]
            frag.extra["ziffer"] = numbers[0] if len(numbers) == 1 else numbers
            frag.extra["responds_to"] = [r for r in responds if r in self.ids]
            return idx[-1] + 1, frag

        self.loop(section, detect,
                  preamble=self.preamble(section,
                                         "gegenaeusserung_bundesregierung"))

    def coarse(self, section: _Section, role: str) -> None:
        """Normenkontrollrat, Beschlussempfehlung, Bericht: coarse fragments
        at Roman-numeral (and, for the latter two, letter) headings."""
        letters = role in {"beschlussempfehlung", "bericht"}

        def detect(i: int, current):
            item = self.stream[i]
            if item.indent > 3 or item.text.endswith((",", ";")):
                return None
            m = _ROMAN_HEADING.match(item.text)
            if m is None and letters:
                m = _VORBLATT_HEADING.match(item.text)
            if m is None:
                return None
            number = m.group("num").lower().replace(".", "-")
            return i + 1, _Frag(role, number, [i],
                                [p for p in [section.title, _ws(item.text)] if p])

        self.loop(section, detect, preamble=self.preamble(section, role))

    # -- run ---------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        self.build_stream()
        sections = self.detect_sections()
        for section in sections:
            if section.role == "vorblatt":
                self.vorblatt(section)
            elif section.role == "entwurf":
                self.entwurf(section)
            elif section.role == "begruendung":
                self.begruendung(section)
            elif section.role == "stellungnahme_bundesrat":
                self.bundesrat(section)
            elif section.role == "gegenaeusserung_bundesregierung":
                self.gegenaeusserung(section)
            elif section.role in {"stellungnahme_normenkontrollrat",
                                  "beschlussempfehlung", "bericht"}:
                self.coarse(section, section.role)
        return {"document": self.document(),
                "sections": self.section_rows(sections),
                "fragments": self.fragments}

    def document(self) -> dict[str, Any]:
        meta = self.meta
        source = meta.get("source") or {}
        extraction = meta.get("extraction") or {}
        present = sorted(self.page_info)
        missing = [k + 1 for k, text in enumerate(self.pages) if text is None]
        by_role: dict[str, int] = {}
        relation_counts: dict[str, int] = {}
        for row in self.fragments:
            by_role[row["role"]] = by_role.get(row["role"], 0) + 1
            for relation in row["relations"]:
                relation_counts[relation["type"]] = \
                    relation_counts.get(relation["type"], 0) + 1
        detected_title = self.detected.get("title")
        warnings = list(self.warnings)
        if meta.get("title") and detected_title and \
                _ws(meta["title"]) != detected_title:
            warnings.append(f"title on page 1 ({detected_title!r}) differs "
                            f"from document_meta title")
        return {
            "id": self.doc_id,
            "drucksache": self.drucksache,
            "chamber": meta.get("chamber") or "BT",
            "type": meta.get("type"),
            "initiator": meta.get("initiator"),
            "date": meta.get("date"),
            "title": meta.get("title") or detected_title,
            "source": {
                "url": source.get("url") or meta.get("pdf_url"),
                "pdf_sha256": source.get("pdf_sha256") or meta.get("pdf_sha256"),
                "page_count": source.get("page_count") or meta.get("page_count")
                or len(self.pages),
                "observed_at": source.get("observed_at"),
                "observed_at_basis": source.get("observed_at_basis"),
            },
            "extraction": {
                "tool": "pdftotext",
                "mode": "-layout, one invocation per page",
                "pdftotext_version": extraction.get("pdftotext_version"),
                "offset_unit": "unicode_codepoints_of_page_text",
                "pages": [self.page_info[p] for p in present],
                "pages_missing": missing,
            },
            "procedure": meta.get("procedure"),
            "outcome": meta.get("outcome"),
            "procedure_stage": self.stage,
            "adoption_status": self.adoption_status,
            "adoption_basis": self.adoption_basis,
            "draft_created_acts": sorted(self.ctx.draft_acts),
            "artikel_acts": dict(sorted(self.ctx.artikel_acts.items(),
                                        key=lambda kv: _natural(kv[0]))),
            "detected": self.detected,
            "unassigned_text": [
                dict(span, reason="continues a passage whose heading is on "
                                  "an unextracted page")
                for span in self.spans(self.orphans)],
            "counts": {"fragments": len(self.fragments), "by_role": by_role,
                       "relations": relation_counts},
            "body_normalization": (
                "layout whitespace collapsed to one space; soft line breaks "
                "joined with a space; paragraph breaks kept as '\\n'; a "
                "line-end hyphen before a lower-case continuation is removed "
                "(Bezeich-/nung -> Bezeichnung) unless it is a suspended "
                "compound (Halb- und); no other rewriting"),
            "parser_version": PARSER_VERSION,
            "warnings": warnings,
        }


def _natural(value: str) -> tuple[int, str]:
    m = re.match(r"(\d+)(.*)", value)
    return (int(m.group(1)), m.group(2)) if m else (10 ** 6, value)


def _anlage_role(title: str) -> str:
    if title.startswith("Entwurf eines"):
        return "entwurf"
    if title.startswith("Stellungnahme des Nationalen Normenkontrollrat"):
        return "stellungnahme_normenkontrollrat"
    if title.startswith("Stellungnahme des Bundesrat"):
        return "stellungnahme_bundesrat"
    if title.startswith("Gegenäußerung der Bundesregierung"):
        return "gegenaeusserung_bundesregierung"
    return "unrecognized"


def _relation_target(norm: dict[str, Any]) -> dict[str, Any]:
    keys = ("canonical_id", "citation", "act", "unit", "number", "absatz",
            "satz", "nummer", "buchstabe", "doppelbuchstabe", "numbering_basis",
            "new_in_draft", "act_basis",
            "correspondence_to_enacted_norm_verified")
    return {k: norm.get(k) for k in keys}


def relation_row(fragment: dict[str, Any], *, relation: str,
                 norm: dict[str, Any], confidence: str,
                 from_mention: bool) -> dict[str, Any]:
    """One relation with its evidence, derived from the fragment itself.

    Heading-derived relations (``explains``/``drafts``) point at the
    heading span; ``mentions`` point at the mention's own span.  The store
    keeps only (relation, norm index, confidence) and rebuilds the rest with
    this function, so parser output and served output cannot drift apart.
    """
    if from_mention:
        span = norm["span"]
        quoted = norm["quoted_context"]
        derived = norm["part"]
    else:
        heading_span = fragment["heading_span"]
        span = {"pdf_page": heading_span["pdf_page"],
                "start": heading_span["start"], "end": heading_span["end"]}
        quoted = fragment["heading"][:200]
        derived = "heading"
    return {
        "type": relation,
        "source_id": fragment["id"],
        "target": _relation_target(norm),
        "evidence": {
            "source_id": fragment["id"],
            "span": span,
            "quoted_context": quoted,
            "derived_by": "parser",
            "derived_from": derived,
            "legal_effect_verified": False,
            "confidence": confidence,
        },
    }


# Keys every norm dict (target or mention) carries; ``None``/``False``
# values can be omitted in storage and restored exactly.
NORM_DEFAULTS: dict[str, Any] = {
    "citation": None, "source_text": None, "act": None, "act_basis": None,
    "unit": None, "number": None, "absatz": None, "satz": None,
    "nummer": None, "buchstabe": None, "doppelbuchstabe": None,
    "halbsatz": None, "new_in_draft": False, "following": False,
    "range": None, "numbering_basis": None, "canonical_id": None,
    "correspondence_to_enacted_norm_verified": False,
}


def mention_context(text: str, start: int, end: int) -> str:
    """Up to 200 characters around a mention (80 before, 80 after)."""
    return text[max(0, start - 80):min(len(text), end + 80)][:200]


def compact_norm(norm: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in norm.items()
            if not (k in NORM_DEFAULTS and v is NORM_DEFAULTS[k])}


def expand_norm(stored: dict[str, Any]) -> dict[str, Any]:
    return {**NORM_DEFAULTS, **stored}


def _path_units(norm: dict[str, Any]) -> list[tuple[str, str]]:
    parts = [(norm["unit"], norm["number"])]
    for unit in ("absatz", "satz", "nummer", "buchstabe", "doppelbuchstabe",
                 "halbsatz"):
        if norm.get(unit):
            parts.append((unit, norm[unit]))
    return parts


def _covered_by_target(mention: dict[str, Any],
                       targets: list[dict[str, Any]]) -> bool:
    """A mention of a target norm (or of a part/whole of it) stays
    ``explains``; act-level targets never swallow mentions."""
    if not mention.get("act") or mention.get("unit") not in {"para", "art"}:
        return False
    m_parts = _path_units(mention)
    for target in targets:
        if target.get("act") != mention["act"] or \
                target.get("unit") != mention["unit"]:
            continue
        t_parts = _path_units(target)
        shorter = min(len(m_parts), len(t_parts))
        if m_parts[:shorter] == t_parts[:shorter]:
            return True
    return False


def procedure_stage(meta: dict[str, Any]) -> str:
    if meta.get("procedure_stage"):
        return str(meta["procedure_stage"])
    doc_type = str(meta.get("type") or "").lower()
    initiator = str(meta.get("initiator") or "")
    if "gesetzentwurf" in doc_type:
        kind = {"Bundesregierung": "regierungsentwurf",
                "Bundesrat": "bundesratsentwurf"}.get(
            initiator, "fraktionsentwurf" if initiator else "entwurf")
        return f"bundestag_first_passage/{kind}"
    if "beschlussempfehlung" in doc_type:
        return "bundestag_committee/beschlussempfehlung"
    return "unknown"


def adoption(meta: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    outcome = meta.get("outcome") or {}
    procedure = meta.get("procedure") or {}
    status = outcome.get("status")
    if status == "promulgated":
        value = ADOPTION_PROMULGATED
    elif status in {"rejected", "withdrawn", "lapsed"}:
        value = f"procedure_{status}"
    else:
        value = "procedure_outcome_unknown"
    basis = {
        "derived_from": "document_meta.outcome",
        "procedure_outcome": status,
        "enacted_as": outcome.get("enacted_title"),
        "ausfertigung_date": outcome.get("ausfertigung_date"),
        "promulgation": outcome.get("promulgation"),
        "dip_vorgang_id": procedure.get("dip_vorgang_id"),
        "passage_correspondence_verified": False,
        "official_justification_of_enacted_norm": False,
        "note": ("The procedure outcome is recorded for the Drucksache as a "
                 "whole. Whether this passage, proposal or draft numbering "
                 "was carried into the enacted text has not been verified; "
                 "it is not presented as the official justification of an "
                 "enacted norm."),
    }
    return value, basis


def segment_drucksache(pages: list[str], *, drucksache: str,
                       document_meta: dict, act_slugs: dict[str, str]) -> dict:
    """Segment one Drucksache.

    ``pages[i]`` is the exact ``pdftotext -layout`` text of PDF page
    ``i + 1`` (``None`` for a page that was not extracted: fragments touching
    it are marked incomplete).  ``act_slugs`` maps act abbreviations
    (``"SGB XIV"``) to gesetze-im-internet slugs (``"sgb_14"``).
    """
    if not re.fullmatch(r"\d{1,4}/\d{1,6}", drucksache or ""):
        raise ValueError(f"not a Drucksache number: {drucksache!r}")
    parser = _Parser(pages, drucksache=drucksache, meta=document_meta or {},
                     act_slugs=act_slugs or {})
    return parser.run()
