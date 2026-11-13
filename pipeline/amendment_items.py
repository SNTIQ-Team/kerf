"""Position and change type of every amendment command in an article.

``backfill_bgbl_history.parse_amendment_commands`` returns one record per
top-level item (``1.``, ``1a.``, ``2.`` …) with an integer ``item`` ordinal
that existing consumers (conformance fixtures, candidate rows, attested
legacy snapshots) depend on.  This module is a *wrapper*: it leaves that
function and its output untouched and adds

* ``position`` -- the address inside the amending article::

      {"absatz": None, "nummer": "5", "buchstabe": "b",
       "doppelbuchstabe": None, "dreifachbuchstabe": None}

  (``absatz`` is the subsection of a collective "Folgeänderungen" article);
* ``change_type`` -- from the command verb: ``neugefasst`` ("wird wie folgt
  gefasst"), ``ersetzt`` ("wird durch den folgenden … ersetzt"),
  ``eingefügt`` ("eingefügt"/"angefügt"/"vorangestellt"), ``aufgehoben``
  ("aufgehoben"/"gestrichen"), ``geändert`` ("wie folgt geändert", word
  replacement/insertion/deletion inside a unit) or ``unbekannt``;
* ``target_unit`` and ``norm_change_type`` -- the unit the verb acts on and
  what that means for the enclosing norm (inserting "Absatz 3" into § 7 is
  ``eingefügt`` for the Absatz but ``geändert`` for § 7).

Keeping this outside the attested parser files (archive_receipts
PARSER_FILES) means adding the fields does not invalidate existing legacy
attestations.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

from backfill_bgbl_history import (
    ARTICLE_ID_PATTERN, normalize_legal_name, parse_amendment_commands,
)
from bgbl_text import MappedText, normalise_lines
from patch_parser import QUOTE_RE, _item_follows, _split_items, parse_command

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from api.page_furniture import strip_page_furniture  # noqa: E402

CHANGE_TYPES = ("geändert", "eingefügt", "neugefasst", "aufgehoben",
                "ersetzt", "unbekannt")
POSITION_KEYS = ("absatz", "nummer", "buchstabe", "doppelbuchstabe",
                 "dreifachbuchstabe")
REF_ORDER = ("para", "article", "absatz", "satz", "nummer", "buchstabe")


def _compact(value: object) -> str:
    return " ".join(str(value or "").split())


def command_body(section: dict) -> tuple[str, dict, str | None]:
    """The operative body exactly as ``parse_amendment_commands`` sees it.

    Returns ``(body, parent_ref, parent_scope)``.  Mirrors lines of
    ``parse_amendment_commands`` so labels align with its items.
    """
    text = str(section.get("text") or "")
    lines = text.splitlines()
    if lines and re.fullmatch(
            rf"\s*Artikel\s+(?:{ARTICLE_ID_PATTERN}|[IVXLCDM]+)\s*",
            lines[0], flags=re.IGNORECASE):
        lines = lines[1:]
    if lines and normalize_legal_name(lines[0]) == normalize_legal_name(
            section.get("heading")):
        lines = lines[1:]
    body = "\n".join(lines)
    body, _removed = strip_page_furniture(body.replace("\f", "\n"))
    preamble = re.search(
        r"\b(?:wird|werden)\b[\s\S]{0,260}?"
        r"\b(?:wie\s+folgt\s+geändert|geändert|aufgehoben|gestrichen)\b"
        r"\s*[:.]?",
        body, flags=re.IGNORECASE)
    if preamble:
        operative = body[preamble.end():].lstrip()
        if operative:
            body = operative
        elif re.search(r"\baufgehoben\b", preamble.group(0),
                       flags=re.IGNORECASE):
            body = preamble.group(0)
    parent_ref: dict[str, str] = {}
    parent_scope: str | None = None
    first_item = re.search(r"(?m)^[ \t]*(?:0a?|1)\.[ \t]+", body)
    if first_item:
        header = body[:first_item.start()].strip()
        if re.search(r"\bwird\b.{0,120}\bwie\s+folgt\s+geändert\b",
                     _compact(header), flags=re.IGNORECASE):
            parent = parse_command(header)
            candidate_ref = parent.get("ref") or {}
            if isinstance(candidate_ref, dict) and any(
                    candidate_ref.get(key) for key in ("para", "article")):
                parent_ref = {str(key): str(value)
                              for key, value in candidate_ref.items()
                              if value is not None}
                parent_scope = _compact(header)
    return body, parent_ref, parent_scope


_PREAMBLE_RE = re.compile(
    r"\b(?:wird|werden)\b[\s\S]{0,260}?"
    r"\b(?:wie\s+folgt\s+geändert|geändert|aufgehoben|gestrichen)\b"
    r"\s*[:.]?", re.IGNORECASE)


def itemisation_body(section: dict) -> tuple[MappedText, dict, str | None]:
    """Operative body for itemisation as an offset-mapped normalised text.

    Same boundaries as :func:`command_body`, but the preamble is found on a
    de-hyphenated, letter-spacing-collapsed copy: legacy layers break the
    preamble verb ("wird wie folgt ge-\nändert:"), and a search on the raw
    text then skips to item 1's own "wie folgt geändert" and swallows it.
    Offsets map to ``section["text"]``.
    """
    text = str(section.get("text") or "")
    lines = text.splitlines(keepends=True)
    offset = 0
    if lines and re.fullmatch(
            rf"\s*Artikel\s+(?:{ARTICLE_ID_PATTERN}|[IVXLCDM]+)\s*",
            lines[0], flags=re.IGNORECASE):
        offset += len(lines[0])
        lines = lines[1:]
    if lines and normalize_legal_name(lines[0]) == normalize_legal_name(
            section.get("heading")):
        offset += len(lines[0])
    mapped = normalise_lines(text[offset:], offset=offset, original=text)
    preamble = _PREAMBLE_RE.search(mapped.text)
    if preamble:
        rest = mapped.text[preamble.end():]
        lead = len(rest) - len(rest.lstrip())
        if rest.strip():
            mapped = mapped.slice(preamble.end() + lead, len(mapped.text))
        elif re.search(r"\baufgehoben\b", preamble.group(0), re.IGNORECASE):
            mapped = mapped.slice(preamble.start(), preamble.end())
    parent_ref: dict[str, str] = {}
    parent_scope: str | None = None
    first_item = re.search(r"(?m)^[ \t]*(?:0a?|1)\.[ \t]+", mapped.text)
    if first_item:
        header = mapped.text[:first_item.start()].strip()
        if re.search(r"\bwird\b.{0,120}\bwie\s+folgt\s+geändert\b",
                     _compact(header), flags=re.IGNORECASE):
            parent = parse_command(header)
            candidate_ref = parent.get("ref") or {}
            if isinstance(candidate_ref, dict) and any(
                    candidate_ref.get(key) for key in ("para", "article")):
                parent_ref = {str(key): str(value)
                              for key, value in candidate_ref.items()
                              if value is not None}
                parent_scope = _compact(header)
    return mapped, parent_ref, parent_scope


def _quote_spans(body: str) -> list[tuple[int, int]]:
    return [match.span() for match in QUOTE_RE.finditer(body)]


def split_numbered(body: str) -> list[tuple[str, str]]:
    """``_split_items`` with the item labels kept: ``[(label, text)]``."""
    spans = _quote_spans(body)
    cuts, previous = [], None
    for match in re.finditer(r"(?m)^[ \t]{0,3}(\d{1,3}[a-z]?)\.[ \t]+", body):
        if not _item_follows(previous, match.group(1)):
            continue
        if any(a < match.start() < b for a, b in spans):
            continue
        cuts.append(match)
        previous = match.group(1)
    if not cuts:
        return [(None, body)] if _compact(body) else []
    rows = []
    for index, match in enumerate(cuts):
        end = cuts[index + 1].start() if index + 1 < len(cuts) else len(body)
        rows.append((match.group(1), body[match.end():end]))
    return rows


def _letter_label(width: int) -> str:
    return (rf"(?m)^[ \t]{{0,3}}((?P<letter>[a-z])(?P=letter){{{width - 1}}})"
            rf"[ \t]?\)[ \t]+")


def _numbered_spans(text: str) -> list[tuple[str, int, int]]:
    """``[(label, start, end)]`` of top-level items (``_split_items`` rules)."""
    spans = _quote_spans(text)
    cuts, previous = [], None
    for match in re.finditer(r"(?m)^[ \t]{0,3}(\d{1,3}[a-z]?)\.[ \t]+", text):
        if not _item_follows(previous, match.group(1)):
            continue
        if any(a < match.start() < b for a, b in spans):
            continue
        cuts.append(match)
        previous = match.group(1)
    if not cuts:
        return [(None, 0, len(text))] if _compact(text) else []
    return [(match.group(1), match.end(),
             cuts[index + 1].start() if index + 1 < len(cuts) else len(text))
            for index, match in enumerate(cuts)]


def _lettered_spans(text: str, start: int, end: int, width: int
                    ) -> tuple[int, list[tuple[str, int, int]]]:
    """Lettered sub-items inside ``text[start:end]``.

    Returns ``(container_end, [(label, start, end)])``; letters must run
    consecutively from ``a``; quotations never contain structure.
    """
    segment = text[start:end]
    spans = _quote_spans(segment)
    cuts = []
    expected = "a"
    for match in re.finditer(_letter_label(width), segment):
        if any(a < match.start() < b for a, b in spans):
            continue
        if match.group("letter") != expected:
            continue
        cuts.append(match)
        expected = chr(ord(expected) + 1)
    if not cuts:
        return end, []
    rows = [(match.group(1), start + match.end(),
             start + (cuts[index + 1].start() if index + 1 < len(cuts)
                      else len(segment)))
            for index, match in enumerate(cuts)]
    return start + cuts[0].start(), rows


def split_lettered(text: str, width: int) -> tuple[str, list[tuple[str, str]]]:
    """``(container_text, [(label, text)])`` for ``a)``/``aa)``/``aaa)``."""
    container_end, rows = _lettered_spans(text, 0, len(text), width)
    return text[:container_end], [(label, text[a:b]) for label, a, b in rows]


# ------------------------------------------------------------- change type

_TEXT_OBJECT = (r"(?:die\s+Wörter|das\s+Wort|die\s+Angaben?|die\s+Wortfolge|"
                r"die\s+Bezeichnung|die\s+Zahl|die\s+Zahlen|das\s+Datum|"
                r"die\s+Zeichen|das\s+Zeichen|das\s+Komma|ein\s+Komma|"
                r"der\s+Punkt|ein\s+Semikolon|das\s+Semikolon|die\s+Klammer|"
                r"die\s+Verweisung|der\s+Klammerzusatz|die\s+Paragraphenangabe|"
                r"die\s+Absatzbezeichnung|die\s+Satzbezeichnung|"
                r"die\s+Nummerierung|die\s+Buchstabenbezeichnung)")
_UNIT_PATTERNS = (
    ("norm", r"§§?\s*\d+[a-z]*|(?<![A-Za-zäöü])Artikel\s+\d+[a-z]*|"
             r"Paragraphen?"),
    ("absatz", r"Absatz|Absätze"),
    ("satz", r"Satz|Sätze"),
    ("nummer", r"Nummer|Nummern"),
    ("buchstabe", r"Buchstabe|Buchstaben"),
    ("ueberschrift", r"Überschrift"),
    ("inhaltsuebersicht", r"Inhaltsübersicht|Inhaltsverzeichnis"),
    ("anlage", r"Anlage|Anhang|Tabelle|Muster"),
    ("gliederung", r"Abschnitt|Unterabschnitt|Kapitel|Teil|Titel|Buch"),
    ("text", _TEXT_OBJECT),
)
_UNIT_RE = re.compile("|".join(f"(?P<{name}>{pattern})"
                               for name, pattern in _UNIT_PATTERNS))
_VERBS = (
    ("neugefasst", r"(?:wird|werden)\s+(?:[^.:„]{0,120}?\s)?(?:wie\s+folgt|"
                   r"folgendermaßen)\s+(?:neu\s+)?gefasst|erhalten?\s+"
                   r"folgende\s+Fassung|"
                   r"(?:wird|werden)\s+neu\s+gefasst"),
    ("ersetzt", r"durch\s+(?:(?:den|die|das|einen|eine)\s+)?folgenden?\s+"
                r"(?:§§?|(?:Absatz|Absätze|Satz|Sätze|Nummer|Nummern|"
                r"Buchstaben?|Artikel|Anlagen?|Tabellen?|Abschnitt|Kapitel|"
                r"Unterabschnitt|Teil|Titel|Anhang|Muster)\b).{0,120}?"
                r"ersetzt"),
    ("geändert", r"(?:wird|werden)\s+wie\s+folgt\s+geändert"),
    ("eingefügt", r"(?:wird|werden)\b.{0,400}?\b(?:eingefügt|angefügt|"
                  r"vorangestellt|angehängt)"),
    ("aufgehoben", r"(?:wird|werden|ist|sind)\b.{0,200}?\b(?:aufgehoben|"
                   r"gestrichen)"),
    ("ersetzt_text", r"durch\b.{0,300}?\bersetzt"),
    ("umnummeriert", r"(?:wird|werden)\s+(?:der|die|das)\s+(?:neue[rn]?\s+)?"
                     r"(?:§§?|Absatz|Absätze|Satz|Sätze|Nummer|Nummern|"
                     r"Buchstaben?)\b|umnummeriert|(?:wird|werden)\s+"
                     r"(?:Absatz|Absätze|Satz|Nummer|Nummern|§)\s+\d"),
)
_VERB_RES = [(name, re.compile(pattern, re.S)) for name, pattern in _VERBS]
_NEW_UNIT_RE = re.compile(
    r"(?:folgende[rsn]?|die\s+folgenden|der\s+folgende|das\s+folgende)\s+"
    r"(?P<kind>§§?|Absatz|Absätze|Satz|Sätze|Nummer|Nummern|Buchstaben?|"
    r"Artikel)\s*(?P<first>\d+[a-z]*|[a-z](?=\s))?(?:\s*(?:bis|und)\s*"
    r"(?P<last>\d+[a-z]*|[a-z](?=\s)))?")


def _unit_before(text: str, end: int) -> str | None:
    units = [match for match in _UNIT_RE.finditer(text, 0, end)]
    if not units:
        return None
    return next(name for name, value in units[-1].groupdict().items()
                if value)


def classify_change(raw: str) -> dict:
    """``change_type``/``target_unit``/``norm_change_type`` of one command."""
    text = _compact(QUOTE_RE.sub("„Q“", str(raw or "")))
    change_type = "unbekannt"
    verb_start = None
    # The outermost command verb is the earliest one ("Die Inhaltsübersicht
    # wird wie folgt geändert: … b) … wird wie folgt gefasst"); equal
    # starts are decided by the pattern order.
    matches = [(match.start(), rank, name, match)
               for rank, (name, regex) in enumerate(_VERB_RES)
               for match in [regex.search(text)] if match]
    if matches:
        _start, _rank, name, match = min(matches, key=lambda row: row[:2])
        change_type = name
        verb_start = match.start()
        if name in {"eingefügt", "aufgehoben"}:
            # Position of the verb itself, not of "wird".
            tail = re.search(r"(?:eingefügt|angefügt|vorangestellt|"
                             r"angehängt|aufgehoben|gestrichen)",
                             text[match.start():])
            verb_start = match.start() + (tail.start() if tail else 0)
    unit = _unit_before(text, verb_start) if verb_start is not None else None
    if re.search(r"\bAngaben?\s+zu(?:m|r)?\s+(?:den\s+)?(?:§|Artikel|"
                 r"Abschnitt|Unterabschnitt|Kapitel|Teil|Anlage)", text):
        unit = "inhaltsuebersicht"
    if change_type == "ersetzt_text":
        change_type = "geändert"
        unit = "text"
    elif change_type == "umnummeriert":
        change_type = "geändert"
    elif change_type in {"eingefügt", "aufgehoben"} and unit == "text":
        # Words/figures inserted into or struck from an existing unit.
        change_type = "geändert"
    elif change_type == "ersetzt" and unit == "text":
        change_type = "geändert"
    new_units = []
    if change_type in {"eingefügt", "ersetzt", "neugefasst"}:
        for match in _NEW_UNIT_RE.finditer(text):
            if match.group("first"):
                new_units.append({"kind": match.group("kind"),
                                  "first": match.group("first"),
                                  "last": match.group("last")})
    if unit == "norm":
        norm_change_type = change_type
    elif change_type == "unbekannt":
        norm_change_type = "unbekannt"
    else:
        norm_change_type = "geändert"
    return {"change_type": change_type, "target_unit": unit,
            "norm_change_type": norm_change_type, "new_units": new_units}


# --------------------------------------------------------------- itemise


def _merge_ref(parent: dict, child: dict) -> dict:
    """Inherit the parent address above the child's most general level."""
    if not child:
        return dict(parent)
    ranks = [REF_ORDER.index(key) for key in child if key in REF_ORDER]
    top = min(ranks) if ranks else len(REF_ORDER)
    merged = {key: value for key, value in parent.items()
              if key in REF_ORDER and REF_ORDER.index(key) < top}
    if "para" in child or "article" in child:
        merged = {}
    if parent.get("toc"):
        # Everything under "Die Inhaltsübersicht wird wie folgt geändert"
        # edits the table of contents, whatever § its entry names.
        merged["toc"] = True
    merged.update(child)
    return merged


def _position(absatz: str | None, nummer: str | None = None,
              buchstabe: str | None = None,
              doppelbuchstabe: str | None = None,
              dreifachbuchstabe: str | None = None) -> dict:
    return {"absatz": absatz, "nummer": nummer, "buchstabe": buchstabe,
            "doppelbuchstabe": doppelbuchstabe,
            "dreifachbuchstabe": dreifachbuchstabe}


def position_label(position: dict) -> str:
    parts = []
    for key, label in (("absatz", "Absatz"), ("nummer", "Nummer"),
                       ("buchstabe", "Buchstabe"),
                       ("doppelbuchstabe", "Doppelbuchstabe"),
                       ("dreifachbuchstabe", "Dreifachbuchstabe")):
        if position.get(key):
            parts.append(f"{label} {position[key]}")
    return " ".join(parts)


def _expand_abbreviations(text: str) -> str:
    """"§ 10 Abs. 1 Nr. 2" -> "§ 10 Absatz 1 Nummer 2" for address parsing
    (``patch_parser._ref`` only knows the written-out forms)."""
    text = re.sub(r"\bAbs\.\s?(?=\d)", "Absatz ", text)
    text = re.sub(r"\bNr\.\s?(?=\d)", "Nummer ", text)
    return re.sub(r"\bBuchst\.\s?(?=[a-z]\b)", "Buchstabe ", text)


def _record(mapped: MappedText, start: int, end: int, *, ref: dict,
            position: dict, ordinal: int, top_level_index: int,
            containers: list[str], parent_scope: str | None) -> dict:
    """One leaf: verbatim original wording, parsed from normalised text."""
    raw = mapped.verbatim(start, end).strip() if end > start else ""
    normalised = mapped.text[start:end]
    command = parse_command(_expand_abbreviations(normalised))
    change = classify_change(normalised)
    return {
        "ordinal": ordinal,
        "top_level_index": top_level_index,
        "item": position,
        "item_label": position_label(position),
        "operation": command["operation"],
        **change,
        "ref": _merge_ref(ref, command.get("ref") or {}),
        "raw": raw,
        "raw_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "containers": [_compact(text) for text in containers if
                       _compact(text)],
        "parent_scope": parent_scope,
    }


def itemise_amendment_commands(section: dict, *,
                               absatz: str | None = None) -> list[dict]:
    """Leaf-level commands of one amending article with their positions.

    ``absatz``: the collective subsection (``collective_subsection`` of a
    scoped candidate) when the article is a Folgeänderungen article.
    A numbered item with lettered sub-items yields one record per leaf
    (``Nummer 1 Buchstabe a``, ``… b``); the item's own introductory text
    is kept in ``containers``.  Splitting runs on a line-preserving
    normalised copy (letter-spaced PDF runs such as ``2 .I n§1A b s .1``
    collapsed), while every ``raw`` is the verbatim original span.
    """
    mapped, parent_ref, parent_scope = itemisation_body(section)
    text = mapped.text
    leaves: list[dict] = []
    ordinal = 0

    def ref_of(start: int, end: int) -> dict:
        return parse_command(_expand_abbreviations(text[start:end])).get(
            "ref") or {}

    for top_index, (label, a, b) in enumerate(_numbered_spans(text), start=1):
        container_end, letters = _lettered_spans(text, a, b, 1)
        if not letters:
            ordinal += 1
            leaves.append(_record(mapped, a, b, ref=parent_ref,
                                  position=_position(absatz, label),
                                  ordinal=ordinal, top_level_index=top_index,
                                  containers=[], parent_scope=parent_scope))
            continue
        container = text[a:container_end]
        container_ref = _merge_ref(parent_ref, ref_of(a, container_end))
        for letter, la, lb in letters:
            inner_end, doubles = _lettered_spans(text, la, lb, 2)
            if not doubles:
                ordinal += 1
                leaves.append(_record(
                    mapped, la, lb, ref=container_ref,
                    position=_position(absatz, label, letter),
                    ordinal=ordinal, top_level_index=top_index,
                    containers=[container], parent_scope=parent_scope))
                continue
            inner = text[la:inner_end]
            inner_ref = _merge_ref(container_ref, ref_of(la, inner_end))
            for double, da, db in doubles:
                innermost_end, triples = _lettered_spans(text, da, db, 3)
                if not triples:
                    ordinal += 1
                    leaves.append(_record(
                        mapped, da, db, ref=inner_ref,
                        position=_position(absatz, label, letter, double),
                        ordinal=ordinal, top_level_index=top_index,
                        containers=[container, inner],
                        parent_scope=parent_scope))
                    continue
                innermost = text[da:innermost_end]
                triple_ref = _merge_ref(inner_ref, ref_of(da, innermost_end))
                for triple, ta, tb in triples:
                    ordinal += 1
                    leaves.append(_record(
                        mapped, ta, tb, ref=triple_ref,
                        position=_position(absatz, label, letter, double,
                                           triple),
                        ordinal=ordinal, top_level_index=top_index,
                        containers=[container, inner, innermost],
                        parent_scope=parent_scope))
    # Drop pure boilerplate with neither an operation nor an address, as
    # parse_amendment_commands does for its top-level items.
    return [leaf for leaf in leaves
            if not (leaf["operation"] == "other" and not leaf["ref"]
                    and leaf["change_type"] == "unbekannt")]


def annotate_commands(commands: list[dict], section: dict, *,
                      absatz: str | None = None) -> list[dict]:
    """Copies of ``parse_amendment_commands`` records with added fields.

    Adds ``position``, ``change_type``, ``target_unit``,
    ``norm_change_type`` and ``leaf_count``; never alters existing keys.
    ``position_status`` is ``aligned`` when the labelled split reproduces
    the command's raw text exactly, else ``unaligned`` (position null).
    """
    body, _parent_ref, _scope = command_body(section)
    labelled = split_numbered(body)
    plain = _split_items(body)
    leaves = itemise_amendment_commands(section, absatz=absatz)
    counts: dict[int, int] = {}
    for leaf in leaves:
        counts[leaf["top_level_index"]] = counts.get(
            leaf["top_level_index"], 0) + 1
    # ``item`` is the printed number (``1a``), not the position; locate the
    # command's top-level position by its label.  An unnumbered body has the
    # single command 1.
    ordinal_of = {str(label): index for index, (label, _text)
                  in enumerate(labelled, start=1) if label is not None}
    rows = []
    for command in commands:
        row = dict(command)
        item = command.get("item")
        index = ordinal_of.get(str(item), 0) if ordinal_of else (
            1 if str(item) == "1" else 0)
        aligned = 0 < index <= len(labelled) and len(labelled) == len(plain) \
            and labelled[index - 1][1].strip() == str(command.get("raw") or
                                                      "").strip()
        if aligned:
            row["position"] = _position(absatz, labelled[index - 1][0])
            row["position_status"] = "aligned"
        else:
            row["position"] = _position(absatz)
            row["position_status"] = "unaligned"
        row.update({key: value for key, value in classify_change(
            command.get("raw") or "").items() if key != "new_units"})
        row["leaf_count"] = counts.get(index, 0)
        rows.append(row)
    return rows


def itemised_commands_with_summary(section: dict, *,
                                   absatz: str | None = None) -> dict:
    """Convenience: both views for one article."""
    commands, affected = parse_amendment_commands(section)
    return {"commands": annotate_commands(commands, section, absatz=absatz),
            "leaves": itemise_amendment_commands(section, absatz=absatz),
            "affected_norms": affected}
