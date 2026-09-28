"""Canonical norm identities and source-URL resolution.

Identity grammar (see the SNTIQ legal MCP spec):

``de/bund/<gii-slug>/<unit>/<number>[/abs/<n>[/satz/<n>][/nr/<n>][/buchst/<x>]]``
``de/by/<act-slug>/<unit>/<number>[...]``

with explicit unit types ``para`` (§), ``art`` (Art./Artikel), ``anlage``,
``anhang`` and ``text/<slug>`` for unnumbered blocks such as the
Eingangsformel.  A bare number never becomes an identity or a URL: GII writes
``§ 37`` as ``__37.html`` but ``Art 1`` as ``art_1.html`` and the Präambel as
``pr_ambel.html``.  Per-norm source URLs are therefore resolved only through
the official table of contents captured by ``pipeline/fetch_gii_toc.py``.
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

_SUB_UNITS = ("abs", "satz", "nr", "buchst")
_UNITS = ("para", "art", "anlage", "anhang", "text")

_ROMAN = {1: "i", 2: "ii", 3: "iii", 4: "iv", 5: "v", 6: "vi", 7: "vii",
          8: "viii", 9: "ix", 10: "x", 11: "xi", 12: "xii", 13: "xiii",
          14: "xiv"}


class NormIdentityError(ValueError):
    """The requested identifier is malformed (``invalid_argument``)."""


def _slug(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("ß", "ss")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text or "unnamed"


def designator_unit(enbez: str | None) -> tuple[str, str]:
    """Map a source designator (``§ 37``, ``Art 1``, ``Anlage 2``) to a unit.

    Unknown shapes are kept as ``text/<slug>`` rather than guessed.
    """
    raw = " ".join(str(enbez or "").split())
    if match := re.fullmatch(r"§+\s*(\d+\s*[a-z]{0,3})", raw, re.I):
        return "para", match.group(1).replace(" ", "").casefold()
    if match := re.fullmatch(r"(?:Art(?:ikel)?\.?)\s*(\d+\s*[a-z]{0,3})",
                             raw, re.I):
        return "art", match.group(1).replace(" ", "").casefold()
    if match := re.fullmatch(r"Anlage(?:\s+([0-9IVXLC]+[a-z]?))?\b.*", raw,
                             re.I):
        return "anlage", (match.group(1) or "0").casefold()
    if match := re.fullmatch(r"Anhang(?:\s+(\S+))?.*", raw, re.I):
        return "anhang", _slug(match.group(1) or "0")
    return "text", _slug(raw)


def unit_label(unit: str, number: str) -> str:
    if unit == "para":
        return f"§ {number}"
    if unit == "art":
        return f"Art. {number}"
    if unit == "anlage":
        return "Anlage" if number == "0" else f"Anlage {number.upper()}"
    if unit == "anhang":
        return "Anhang" if number == "0" else f"Anhang {number.upper()}"
    return number


def act_slug(act: dict[str, Any]) -> tuple[str, str]:
    """Return ``(jurisdiction_path, slug)`` for an act index row."""
    act_id = str(act.get("id") or "")
    juris = str(act.get("juris") or "")
    if juris == "DE-BY" or act_id.startswith("by_"):
        return "de/by", act_id.removeprefix("by_")
    url = str(act.get("source_url") or "")
    match = re.fullmatch(
        r"https://www\.gesetze-im-internet\.de/([a-z0-9][a-z0-9_.-]*)/?", url)
    if match:
        return "de/bund", match.group(1)
    return "de/bund", act_id.removeprefix("fed_")


def act_prefix(act: dict[str, Any]) -> str:
    juris, slug = act_slug(act)
    return f"{juris}/{slug}"


def norm_rows_with_ids(act: dict[str, Any], norms: list[dict[str, Any]]
                       ) -> list[tuple[str, dict[str, Any]]]:
    """Attach canonical ids to one state's norm list.

    Repeated designators (e.g. several ``Anlage`` blocks) receive ordinal
    suffixes ``~2``, ``~3`` in document order and are flagged, instead of one
    silently overwriting another.
    """
    prefix = act_prefix(act)
    seen: dict[str, int] = {}
    result = []
    for row in norms:
        if not isinstance(row, dict):
            continue
        unit, number = designator_unit(row.get("enbez") or row.get("titel"))
        base = f"{prefix}/{unit}/{number}"
        seen[base] = seen.get(base, 0) + 1
        ident = base if seen[base] == 1 else f"{base}~{seen[base]}"
        result.append((ident, row))
    return result


def split_norm_id(value: str) -> tuple[str, str, str, str, str]:
    """``de/bund/sgb_14/para/37/abs/2`` → (juris, slug, unit, number, sub)."""
    raw = (value or "").strip().strip("/")
    parts = raw.split("/")
    if len(parts) < 5 or parts[0] != "de" or parts[1] not in {"bund", "by"}:
        raise NormIdentityError(
            "norm id must look like de/bund/<act>/<para|art|anlage|anhang|"
            "text>/<number>[/abs/<n>…]")
    juris = f"{parts[0]}/{parts[1]}"
    slug, unit, number = parts[2], parts[3], parts[4]
    if unit not in _UNITS:
        raise NormIdentityError(f"unknown unit type {unit!r}; use one of "
                                f"{', '.join(_UNITS)}")
    rest = parts[5:]
    if len(rest) % 2:
        raise NormIdentityError("sub-unit path must be key/value pairs")
    order = []
    for key, sub in zip(rest[::2], rest[1::2]):
        if key not in _SUB_UNITS or not re.fullmatch(r"[0-9a-z]+", sub):
            raise NormIdentityError(f"invalid sub-unit {key}/{sub}")
        order.append(key)
    if order != sorted(order, key=_SUB_UNITS.index):
        raise NormIdentityError("sub-units must follow abs/satz/nr/buchst")
    return juris, slug, unit, number, "/".join(rest)


def exact_act_aliases(value: str) -> set[str]:
    key = " ".join(str(value or "").casefold().split())
    keys = {key, key.replace(" ", "")}
    match = re.fullmatch(r"sgb\s*(\d+|[ivx]+)", key)
    if match:
        token = match.group(1)
        if token.isdigit() and int(token) in _ROMAN:
            keys |= {f"sgb {_ROMAN[int(token)]}", f"sgb{_ROMAN[int(token)]}"}
        else:
            number = {v: k for k, v in _ROMAN.items()}.get(token)
            if number:
                keys |= {f"sgb {number}", f"sgb{number}"}
    return keys


_CITATION = re.compile(
    r"^\s*(?P<unit>§+|Art(?:ikel|\.)?)\s*(?P<num>\d+(?-i:[a-z]{0,3})\b)"
    r"(?:\s+(?:Abs(?:atz|\.)?)\s*(?P<abs>\d+[a-z]?))?"
    r"(?:\s+(?:S(?:atz|\.)?)\s*(?P<satz>\d+))?"
    r"(?:\s+(?:Nr\.?|Nummer)\s*(?P<nr>\d+[a-z]?))?"
    r"(?:\s+(?:Buchst(?:abe|\.)?|lit\.?)\s*(?P<buchst>[a-z]{1,2}))?"
    r"\s+(?P<act>.+?)\s*$", re.I)


def parse_citation(value: str) -> dict[str, str] | None:
    """Parse ``§ 37 Abs. 2 Nr. 3 SGB XIV`` into its parts (no resolution)."""
    match = _CITATION.match(value or "")
    if not match:
        return None
    unit = "para" if match.group("unit").startswith("§") else "art"
    sub = []
    for key in ("abs", "satz", "nr", "buchst"):
        if match.group(key):
            sub.extend([key, match.group(key).casefold()])
    return {"unit": unit,
            "number": match.group("num").replace(" ", "").casefold(),
            "act": match.group("act").strip(), "sub": "/".join(sub)}


@lru_cache(maxsize=8)
def _load_toc(path_string: str, mtime: float) -> dict[str, Any]:
    with open(path_string, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(
            payload.get("acts"), dict):
        raise ValueError("GII TOC has no acts map")
    return payload


def load_gii_toc(*paths: Path) -> dict[str, Any] | None:
    """First readable TOC among ``paths`` (data plane, then bundled copy)."""
    for path in paths:
        try:
            if path.is_file():
                return _load_toc(str(path), path.stat().st_mtime)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return None


def gii_norm_url(toc: dict[str, Any] | None, slug: str, unit: str,
                 number: str) -> dict[str, Any]:
    """Resolve an official per-norm URL through the captured GII TOC.

    Returns ``{"url": str|None, "status": ..., "basis": ...}``; never
    constructs a URL from a number.
    """
    act_url = f"https://www.gesetze-im-internet.de/{slug}/"
    if toc is None:
        return {"url": None, "act_url": act_url,
                "status": "toc_unavailable",
                "basis": "no captured GII table of contents"}
    entry = (toc.get("acts") or {}).get(slug)
    if not isinstance(entry, dict):
        return {"url": None, "act_url": act_url, "status": "act_not_in_toc",
                "basis": "act slug absent from the captured GII TOC"}
    key = f"{unit}/{number}"
    href = (entry.get("norms") or {}).get(key)
    if not href or not re.fullmatch(r"[A-Za-z0-9_.-]+\.html", str(href)):
        return {"url": None, "act_url": act_url, "status": "norm_not_in_toc",
                "basis": f"no TOC link for {key} in the GII capture of "
                         f"{entry.get('fetched_at')}"}
    return {"url": act_url + href, "act_url": act_url, "status": "resolved",
            "basis": f"GII table of contents captured {entry.get('fetched_at')}"
                     f" (sha256 {str(entry.get('sha256') or '')[:12]}…)"}
