"""Fail-closed replay of German amendment commands on one norm text.

Used to *verify* that an amendment explains the text change between two
captured versions (``replay_verified``) and to *reconstruct* the version
valid between two amendments that fall between the same two captures.  The
grammar is deliberately small; anything outside it raises
``ReplayUnsupported`` and the caller keeps the weaker attribution.  A
reconstruction is only ever accepted when replaying the remaining commands
reproduces the next captured official text exactly (whitespace-free
identity), so a parsing mistake cannot produce an accepted text.

Supported (each optionally scoped by ``In § n Absatz a Satz s …``):
word replacement (``… „X“ durch … „Y“ ersetzt``, ``jeweils``), word
deletion (``… „X“ gestrichen``), word insertion (``nach/vor dem Wort „X“ …
„Y“ eingefügt``), Absatz/Satz new wording (``wird wie folgt gefasst`` /
``wird durch den folgenden … ersetzt``), Absatz/Satz repeal, Absatz/Satz
insertion and appending, whole-norm new wording, and ``wird wie folgt
geändert: a) … b) …`` containers (nested ``aa)``).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from api.law_structure import parse_norm_structure
from api.page_furniture import strip_page_furniture

REPLAY_VERSION = "command-replay/1"


class ReplayUnsupported(ValueError):
    """The command is outside the supported grammar or its target is not
    unique in the addressed scope."""


# ------------------------------------------------------------------ text

_KEEP_HYPHEN_BEFORE = r"(?:und|oder|bzw|sowie|beziehungsweise)\b"


def clean_command(raw: str) -> str:
    """PDF command text → one line (page furniture and hyphenation)."""
    text, _ = strip_page_furniture(str(raw or ""))
    # recht.bund.de PDFs set the line-end hyphen after a space ("Luft-
    # verkehrs -\ngesetzes"); a real dash is an en dash, never "-".
    text = re.sub(rf"(?<=[a-zäöüß])[ \t]?-[ \t]*\n[ \t]*"
                  rf"(?!{_KEEP_HYPHEN_BEFORE})(?=[a-zäöüß])", "", text)
    text = re.sub(rf"(?<=\S)-\s*\n\s*(?={_KEEP_HYPHEN_BEFORE})", "- ", text)
    text = re.sub(r"(?<=\S)-\s*\n\s*(?=[a-zäöüß])", "-", text)
    return " ".join(text.split())


def _flex(fragment: str) -> re.Pattern[str]:
    """Whitespace-insensitive literal pattern for quoted wording."""
    tokens = [re.escape(ch) for ch in re.sub(r"\s+", "", fragment)]
    if not tokens:
        raise ReplayUnsupported("empty quoted wording")
    return re.compile(r"\s*".join(tokens))


# ----------------------------------------------------------------- scope

@dataclass
class Scope:
    absatz: str | None = None
    satz: str | None = None
    narrower: bool = False          # Nummer/Buchstabe below the Satz

    def child(self, other: "Scope") -> "Scope":
        return Scope(absatz=other.absatz or self.absatz,
                     satz=other.satz or (self.satz if not other.absatz
                                         else None),
                     narrower=other.narrower or self.narrower)


_SCOPE = re.compile(
    r"(?:§\s*\d+[a-z]?\s*)?"
    r"(?:(?:Absatz|Abs\.)\s*(?P<absatz>\d+[a-z]?)\s*)?"
    r"(?:(?:Satz)\s*(?P<satz>\d+)\s*)?"
    r"(?P<rest>(?:(?:Nummer|Nr\.|Buchstabe|Buchst\.|Doppelbuchstabe)\s*"
    r"\w+\s*)*)$")


def parse_scope(value: str | None) -> Scope:
    text = " ".join(str(value or "").split())
    text = re.sub(r"\s+(?:des|der)\s+.*$", "", text)
    if not text:
        return Scope()
    match = _SCOPE.fullmatch(text)
    if not match:
        raise ReplayUnsupported(f"unsupported scope {text!r}")
    return Scope(absatz=match.group("absatz"), satz=match.group("satz"),
                 narrower=bool(match.group("rest").strip()))


def _span(text: str, scope: Scope, *, bavarian: bool) -> tuple[int, int]:
    if scope.absatz is None and scope.satz is None:
        return 0, len(text)
    structure = parse_norm_structure(text, bavarian=bavarian)
    units = structure["units"]
    container = None
    if scope.absatz is not None:
        found = [u for u in units if u["type"] == "absatz"
                 and u.get("number") == scope.absatz]
        if len(found) != 1:
            raise ReplayUnsupported(f"Absatz {scope.absatz} not unique")
        container = found[0]
    elif len(units) == 1 and units[0].get("number") is None:
        container = units[0]
    elif len(units) == 1:
        container = units[0]
    if container is None:
        raise ReplayUnsupported("Satz without Absatz in a multi-Absatz norm")
    if scope.satz is None:
        return container["start"], container["end"]
    sentences = [c for c in container.get("children") or []
                 if c["type"] == "satz"]
    found = [c for c in sentences if c.get("number") == scope.satz]
    if len(found) != 1:
        raise ReplayUnsupported(f"Satz {scope.satz} not unique")
    return found[0]["start"], found[0]["end"]


# -------------------------------------------------------------- commands

_Q = r"„(?P<{name}>[^“]*)“"
_WORD = (r"(?:die\s+Angaben?|das\s+Wort|die\s+Wörter|die\s+Zahlen?|"
         r"der\s+Klammerzusatz|den\s+Klammerzusatz|die\s+Wörter\s+und\s+"
         r"(?:die\s+)?Angaben?|die\s+Angaben?\s+und\s+(?:die\s+)?Wörter|"
         r"das\s+Komma\s+und\s+die\s+Wörter|ein\s+Komma\s+und\s+die\s+"
         r"Wörter|der\s+Satzteil|den\s+Satzteil)")
_TAIL = r"\.?(?:\s+(?:Der|Die|Das)\s+bisherigen?\s+.*)?$"
_IN = r"(?:In\s+(?P<scope>[^„]*?)\s+)?"

_REPLACE = re.compile(
    rf"^{_IN}(?:wird|werden)\s+(?P<jeweils>jeweils\s+)?(?:{_WORD}\s+)?"
    rf"{_Q.format(name='old')}\s+(?:jeweils\s+)?durch\s+(?:{_WORD}\s+)?"
    rf"{_Q.format(name='new')}\s+ersetzt{_TAIL}")
_DELETE = re.compile(
    rf"^{_IN}(?:wird|werden)\s+(?P<jeweils>jeweils\s+)?(?:{_WORD}\s+)?"
    rf"{_Q.format(name='old')}\s+gestrichen{_TAIL}")
_INSERT_WORDS = re.compile(
    rf"^{_IN}(?:wird|werden)\s+(?P<jeweils>jeweils\s+)?(?P<where>nach|vor)\s+"
    rf"(?:dem\s+Wort|der\s+Angabe|den\s+Wörtern|dem\s+Komma\s+nach\s+dem\s+"
    rf"Wort|den\s+Angaben)\s+{_Q.format(name='anchor')}\s+(?:{_WORD}\s+)?"
    rf"{_Q.format(name='new')}\s+eingefügt{_TAIL}")
_UNIT = r"(?:§\s*\d+[a-z]?\s+)?"
_NEW = r":\s*„(?P<new>.*)“"
_ABSATZ_NEW = re.compile(
    rf"^{_UNIT}Absatz\s+(?P<n>\d+[a-z]?)\s+wird\s+(?:wie\s+folgt\s+gefasst|"
    rf"durch\s+den\s+folgenden\s+Absatz\s+\S+\s+ersetzt){_NEW}{_TAIL}")
_ABSATZ_REPEAL = re.compile(
    rf"^{_UNIT}Absatz\s+(?P<n>\d+[a-z]?)\s+wird\s+aufgehoben{_TAIL}")
_ABSATZ_INSERT = re.compile(
    rf"^Nach\s+{_UNIT}Absatz\s+(?P<n>\d+[a-z]?)\s+wird\s+(?:der\s+)?"
    rf"folgende(?:r)?\s+Absatz\s+(?P<m>\d+[a-z]?)\s+eingefügt{_NEW}{_TAIL}")
_ABSATZ_APPEND = re.compile(
    rf"^(?:Dem\s+§\s*\d+[a-z]?\s+wird|Es\s+wird)\s+(?:der\s+)?folgende(?:r)?"
    rf"\s+Absatz\s+(?P<m>\d+[a-z]?)\s+angefügt{_NEW}{_TAIL}")
_ABSATZ_APPEND2 = re.compile(
    rf"^Folgender\s+Absatz\s+(?P<m>\d+[a-z]?)\s+wird\s+angefügt{_NEW}"
    rf"{_TAIL}")
_SATZ_NEW = re.compile(
    rf"^{_IN}(?:{_UNIT}(?:Absatz\s+(?P<a>\d+[a-z]?)\s+)?)?(?:wird\s+)?"
    rf"Satz\s+(?P<s>\d+)\s+(?:wird\s+)?(?:wie\s+folgt\s+gefasst|durch\s+"
    rf"(?:den\s+folgenden\s+Satz|die\s+folgenden\s+Sätze)\s+ersetzt)"
    rf"{_NEW}{_TAIL}")
_SATZ_REPEAL = re.compile(
    rf"^{_IN}(?:{_UNIT}(?:Absatz\s+(?P<a>\d+[a-z]?)\s+)?)?(?:wird\s+)?"
    rf"Satz\s+(?P<s>\d+)\s+wird\s+(?:aufgehoben|gestrichen){_TAIL}")
_SATZ_APPEND = re.compile(
    rf"^Dem\s+(?:{_UNIT}Absatz\s+(?P<a>\d+[a-z]?)|§\s*\d+[a-z]?)\s+"
    rf"(?:wird\s+(?:der\s+)?folgende(?:r)?\s+Satz|werden\s+die\s+folgenden"
    rf"\s+Sätze)\s+angefügt{_NEW}{_TAIL}")
_SATZ_APPEND2 = re.compile(
    rf"^Folgender\s+Satz\s+wird\s+angefügt{_NEW}{_TAIL}")
_SATZ_INSERT = re.compile(
    rf"^{_IN}(?:wird\s+)?[Nn]ach\s+(?:{_UNIT}(?:Absatz\s+(?P<a>\d+[a-z]?)"
    rf"\s+))?Satz\s+(?P<s>\d+)\s+(?:wird\s+(?:der\s+)?folgende(?:r)?\s+"
    rf"Satz|werden\s+die\s+folgenden\s+Sätze)(?:\s+\d+(?:\s+(?:und|bis)"
    rf"\s+\d+)?)?\s+eingefügt{_NEW}{_TAIL}")
_NORM_NEW = re.compile(
    rf"^§\s*(?P<p>\d+[a-z]?)\s+wird\s+(?:wie\s+folgt\s+gefasst|durch\s+den\s+"
    rf"folgenden\s+§\s*\S+\s+ersetzt){_NEW}{_TAIL}")
_CONTAINER = re.compile(
    rf"^{_IN}(?P<head>(?:§\s*\d+[a-z]?\s*)?(?:Absatz\s+\d+[a-z]?\s*)?"
    rf"(?:Satz\s+\d+\s*)?)(?:wird|werden)\s+wie\s+folgt\s+geändert:\s*"
    rf"(?P<rest>.*)$")


def _split_items(rest: str, double: bool) -> list[str]:
    pattern = r"(?:^|(?<=[.:;“”]\s))([a-z]{2})\)\s" if double else \
        r"(?:^|(?<=[.:;“”]\s))([a-z])\)\s"
    marks = []
    expected = "aa" if double else "a"
    for match in re.finditer(pattern, rest):
        if match.group(1) == expected:
            marks.append(match)
            expected = (chr(ord(expected[0]) + 1) * (2 if double else 1))
    if not marks:
        raise ReplayUnsupported("container without enumerated items")
    parts = []
    for index, match in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else \
            len(rest)
        parts.append(rest[match.end():end].strip())
    return parts


def _new_wording(value: str) -> str:
    return " ".join(value.split()).strip()


@dataclass
class NormText:
    titel: str
    text: str
    bavarian: bool = False
    applied: list[str] = field(default_factory=list)


def _replace_in(norm: NormText, scope: Scope, old: str, new: str, *,
                every: bool) -> None:
    start, end = _span(norm.text, scope, bavarian=norm.bavarian)
    pattern = _flex(old)
    region = norm.text[start:end]
    hits = list(pattern.finditer(region))
    if not hits:
        raise ReplayUnsupported("replaced wording not found")
    if len(hits) > 1 and not every:
        raise ReplayUnsupported("replaced wording not unique in scope")
    replacement = _new_wording(new)
    out = region
    for hit in reversed(hits):
        out = out[:hit.start()] + replacement + out[hit.end():]
    norm.text = norm.text[:start] + out + norm.text[end:]


def _insert_words(norm: NormText, scope: Scope, anchor: str, new: str,
                  where: str, *, every: bool) -> None:
    start, end = _span(norm.text, scope, bavarian=norm.bavarian)
    region = norm.text[start:end]
    hits = list(_flex(anchor).finditer(region))
    if not hits or (len(hits) > 1 and not every):
        raise ReplayUnsupported("insertion anchor not unique in scope")
    addition = _new_wording(new)
    out = region
    for hit in reversed(hits):
        if where == "nach":
            glue = "" if addition[:1] in {",", ".", ";", ")"} else " "
            out = out[:hit.end()] + glue + addition + out[hit.end():]
        else:
            out = out[:hit.start()] + addition + " " + out[hit.start():]
    norm.text = norm.text[:start] + out + norm.text[end:]


def _absatz_unit(norm: NormText, number: str) -> dict[str, Any]:
    structure = parse_norm_structure(norm.text, bavarian=norm.bavarian)
    found = [u for u in structure["units"] if u["type"] == "absatz"
             and u.get("number") == number]
    if len(found) != 1:
        raise ReplayUnsupported(f"Absatz {number} not unique")
    return found[0]


def _last_absatz_end(norm: NormText) -> int:
    structure = parse_norm_structure(norm.text, bavarian=norm.bavarian)
    if not structure["numbered_absaetze"]:
        raise ReplayUnsupported("norm has no numbered Absätze")
    return structure["units"][-1]["end"]


def apply_command(norm: NormText, raw: str, scope: Scope | None = None
                  ) -> None:
    command = clean_command(raw) if "\n" in raw or "„" in raw else raw
    scope = scope or Scope()
    # "§ 7 wird wie folgt geändert: a) … b) …" (or nested "aa) …")
    if match := _CONTAINER.match(command):
        head = parse_scope(((match.group("scope") or "") + " " +
                            match.group("head")).strip())
        inner = scope.child(head)
        rest = match.group("rest")
        double = bool(re.match(r"aa\)\s", rest))
        for part in _split_items(rest, double):
            apply_command(norm, part, inner)
        return
    if match := _REPLACE.match(command):
        _replace_in(norm, scope.child(parse_scope(match.group("scope"))),
                    match.group("old"), match.group("new"),
                    every=bool(match.group("jeweils")))
    elif match := _DELETE.match(command):
        target = scope.child(parse_scope(match.group("scope")))
        _replace_in(norm, target, match.group("old"), "",
                    every=bool(match.group("jeweils")))
        norm.text = re.sub(r"\s+([,.;])", r"\1",
                           re.sub(r"  +", " ", norm.text))
    elif match := _INSERT_WORDS.match(command):
        _insert_words(norm, scope.child(parse_scope(match.group("scope"))),
                      match.group("anchor"), match.group("new"),
                      match.group("where"),
                      every=bool(match.group("jeweils")))
    elif match := _NORM_NEW.match(command):
        new = _new_wording(match.group("new"))
        head = re.match(r"^§\s*\d+[a-z]?\s+(?P<titel>.*?)\s*(?=\(1\)|$)", new)
        if not head or "(1)" not in new:
            raise ReplayUnsupported("whole-norm wording without Absatz 1")
        norm.titel = head.group("titel").strip()
        norm.text = new[head.end():].strip()
    elif match := _ABSATZ_NEW.match(command):
        unit = _absatz_unit(norm, match.group("n"))
        new = _new_wording(match.group("new"))
        if not new.startswith(f"({match.group('n')})"):
            raise ReplayUnsupported("new Absatz wording without its number")
        norm.text = norm.text[:unit["start"]] + new + norm.text[unit["end"]:]
    elif match := _ABSATZ_REPEAL.match(command):
        # GII keeps a repealed Absatz as "(n) (weggefallen)"
        unit = _absatz_unit(norm, match.group("n"))
        norm.text = (norm.text[:unit["start"]] +
                     f"({match.group('n')}) (weggefallen)" +
                     norm.text[unit["end"]:])
    elif match := _ABSATZ_INSERT.match(command):
        unit = _absatz_unit(norm, match.group("n"))
        new = _new_wording(match.group("new"))
        if not new.startswith(f"({match.group('m')})"):
            raise ReplayUnsupported("inserted Absatz without its number")
        norm.text = norm.text[:unit["end"]] + new + norm.text[unit["end"]:]
    elif match := (_ABSATZ_APPEND.match(command) or
                   _ABSATZ_APPEND2.match(command)):
        number = match.group("m")
        new = _new_wording(match.group("new"))
        if not new.startswith(f"({number})"):
            raise ReplayUnsupported("appended Absatz without its number")
        end = _last_absatz_end(norm)
        norm.text = norm.text[:end] + new + norm.text[end:]
    elif match := _SATZ_NEW.match(command):
        target = scope.child(parse_scope(match.group("scope"))).child(
            Scope(absatz=match.group("a"), satz=match.group("s")))
        start, end = _span(norm.text, target, bavarian=norm.bavarian)
        norm.text = norm.text[:start] + _new_wording(match.group("new")) + \
            norm.text[end:]
    elif match := _SATZ_REPEAL.match(command):
        target = scope.child(parse_scope(match.group("scope"))).child(
            Scope(absatz=match.group("a"), satz=match.group("s")))
        start, end = _span(norm.text, target, bavarian=norm.bavarian)
        norm.text = (norm.text[:start].rstrip() + " " +
                     norm.text[end:].lstrip()).strip()
    elif match := (_SATZ_APPEND.match(command) or
                   _SATZ_APPEND2.match(command)):
        target = scope.child(Scope(absatz=match.groupdict().get("a")))
        if target.absatz is None and target.satz is None:
            start, end = 0, len(norm.text)
        else:
            start, end = _span(norm.text, Scope(absatz=target.absatz),
                               bavarian=norm.bavarian)
        new = _new_wording(match.group("new"))
        norm.text = norm.text[:end].rstrip() + " " + new + norm.text[end:]
    elif match := _SATZ_INSERT.match(command):
        target = scope.child(parse_scope(match.group("scope"))).child(
            Scope(absatz=match.group("a"), satz=match.group("s")))
        start, end = _span(norm.text, target, bavarian=norm.bavarian)
        new = _new_wording(match.group("new"))
        norm.text = norm.text[:end].rstrip() + " " + new + " " + \
            norm.text[end:].lstrip()
    else:
        raise ReplayUnsupported(f"unsupported command: {command[:120]}")
    norm.applied.append(command[:200])


def apply_change(titel: str, text: str, commands: list[dict[str, Any]], *,
                 bavarian: bool = False) -> tuple[str, str]:
    """Apply one change record's commands (in order) to a norm text."""
    if not commands:
        raise ReplayUnsupported("change has no commands")
    norm = NormText(titel=titel or "", text=text or "", bavarian=bavarian)
    for command in commands:
        raw = str(command.get("raw") or "")
        if not raw.strip():
            raise ReplayUnsupported("empty command")
        cleaned = clean_command(raw)
        # single-command articles repeat the article heading first
        cleaned = re.sub(r"^(?:Weitere\s+)?Änderung\s+(?:des|der)\s+.*?\s+"
                         r"(?=(?:In\s+§|Dem\s+§|Nach\s+§|§\s*\d))", "",
                         cleaned, count=1)
        # "§ 7 Absatz 1 Satz 2 … des Zweiten Buches …, das zuletzt durch …
        # geändert worden ist, wird wie folgt gefasst" (single-command
        # articles): drop the act citation between the unit and the verb.
        cleaned = re.sub(
            r"^((?:In\s+)?§\s*\d+[a-z]?(?:\s+(?:Absatz|Satz|Nummer|"
            r"Buchstabe)\s+\w+)*)\s+(?:des|der)\s+[^„]*?(?:geändert\s+worden"
            r"\s+ist|vom\s+\d{1,2}\.\s+\w+\s+\d{4}[^„]*?),\s+", r"\1 ",
            cleaned)
        apply_command(norm, cleaned)
    return norm.titel, norm.text
