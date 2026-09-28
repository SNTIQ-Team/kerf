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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

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
    if len(parts) < 5 or parts[0] != "de" or parts[1] not in {"bund", *LAND_CODES}:
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
        if key not in _SUB_UNITS or not re.fullmatch(r"[0-9a-z]+(?:~\d+)?",
                                                      sub):
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


# --------------------------------------------------------------------------
# Act resolution: the one canonical implementation (operating principle 1).
#
# Used by /norms/resolve (api/norm_history.py) and by the courts corpus
# (courts/acts.py).  Identity prefixes follow the grammar above: federal acts
# are addressed by their GII slug, Bavarian acts by their Lexgraph slug, other
# Länder by ``de/<land>/<abbreviation key>``, EU acts by CELEX work and
# treaties by ``int/<name>``.  An abbreviation that cannot be resolved
# unambiguously is ``unresolved`` - never guessed.
# --------------------------------------------------------------------------

LAND_CODES = ("bw", "by", "be", "bb", "hb", "hh", "he", "mv", "ni", "nw", "rp",
              "sl", "sn", "st", "sh", "th")
# Renamed or irregularly slugged federal acts: abbreviation key -> GII slug.
FEDERAL_ALIASES = {
    "asylg": "asylvfg_1992", "asylvfg": "asylvfg_1992", "rustag": "stag",
    "freizuegeu": "freiz_gg_eu_2004", "freizgeu": "freiz_gg_eu_2004",
    "baugb": "bbaug", "weg": "woeigg", "egbgb": "bgbeg", "egzpo": "zpoeg",
    "egstgb": "stgbeg", "eggvg": "gvgeg", "eghgb": "hgbeg", "soldg": "sg",
}
EU_ACTS = {
    "dsgvo": "32016R0679", "eudsgvo": "32016R0679", "aeuv": "12016E", "euv": "12016M",
    "grch": "12016P", "grc": "12016P", "eugrch": "12016P", "dublinivo": "32013R0604",
    "dublin3vo": "32013R0604", "dublinverordnung": "32013R0604", "aufnahmerl": "32013L0033",
    "qualifikationsrl": "32011L0095", "asylverfahrensrl": "32013L0032",
    "rueckfuehrungsrl": "32008L0115", "unionsbuergerrl": "32004L0038",
    "freizuegigkeitsrl": "32004L0038", "eugvvo": "32012R1215", "brueiavo": "32019R1111",
}
INTERNATIONAL_ACTS = {"emrk": "int/emrk", "gfk": "int/gfk", "unbrk": "int/unbrk", "brk": "int/unbrk"}
LAND_PREFIXES = {"bay": "by", "bln": "be", "bbg": "bb", "brem": "hb", "hmb": "hh", "hess": "he",
                 "nds": "ni", "saechs": "sn", "thuer": "th", "saarl": "sl", "nrw": "nw", "lsa": "st"}
LAND_SUFFIXES = {"nrw": "nw", "nw": "nw", "bw": "bw", "bln": "be", "be": "be", "bbg": "bb", "lsa": "st",
                 "mv": "mv", "sh": "sh", "rp": "rp", "rlp": "rp", "saarl": "sl", "sl": "sl", "nds": "ni",
                 "hb": "hb", "hh": "hh", "he": "he", "th": "th", "sn": "sn", "by": "by", "st": "st"}
# Abbreviations that denote a Land act only in that Land's own decisions.
LAND_CONTEXT_ACTS = {
    "by": {"bv", "vfghg", "pag", "lstvg", "go", "gemo", "lkro", "bezo", "kag", "dschg", "bayvsg", "agvwgo"},
    "nw": {"gonrw", "polgnrw", "kagnrw", "schulgnrw", "lbgnrw", "dsgnrw"},
    "he": {"hgo", "hsog", "hbo", "hvwvfg", "hkag", "hschg", "hdsig", "hbg"},
    "ni": {"npog", "nbauo", "nkomvg", "ndsg", "nkag", "nschg", "nbg", "nvwvfg"},
    "hb": {"bremvwvfg", "brempolg", "bremlbo"},
    "bb": {"bbgbo", "bbgkverf", "bbgpolg", "bbgvwvfg"},
}
# Long names occasionally written instead of the abbreviation ("des Grundgesetzes").
ACT_LONG_NAMES = {
    "grundgesetz": "GG", "grundgesetzes": "GG", "verwaltungsgerichtsordnung": "VwGO",
    "sozialgerichtsgesetz": "SGG", "sozialgerichtsgesetzes": "SGG", "zivilprozessordnung": "ZPO",
    "buergerlichen gesetzbuchs": "BGB", "buergerlichen gesetzbuches": "BGB", "buergerliches gesetzbuch": "BGB",
    "strafgesetzbuch": "StGB", "strafgesetzbuchs": "StGB", "strafprozessordnung": "StPO",
    "aufenthaltsgesetz": "AufenthG", "aufenthaltsgesetzes": "AufenthG", "asylgesetz": "AsylG",
    "asylgesetzes": "AsylG", "verwaltungsverfahrensgesetz": "VwVfG", "verwaltungsverfahrensgesetzes": "VwVfG",
    "abgabenordnung": "AO", "einkommensteuergesetz": "EStG", "einkommensteuergesetzes": "EStG",
    "asylbewerberleistungsgesetz": "AsylbLG", "asylbewerberleistungsgesetzes": "AsylbLG",
}
_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def long_name_abbreviation(name: str) -> str | None:
    folded = " ".join((name or "").casefold().translate(_TRANSLIT).split())
    return ACT_LONG_NAMES.get(folded)


def act_key(abbreviation: str) -> str:
    """Folded abbreviation: lowercase, umlauts transliterated, SGB books arabic, no year/punctuation."""
    text = " ".join(unicodedata.normalize("NFC", abbreviation or "").casefold().split())
    text = re.sub(r"\s+(?:19|20)\d{2}$", "", text)
    sgb = re.fullmatch(r"sgb[\s.-]*([ivx]+|\d{1,2})", text)
    if sgb:
        token = sgb.group(1)
        number = {v: k for k, v in _ROMAN.items()}.get(token, token)
        return f"sgb{number}"
    text = text.translate(_TRANSLIT)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", text)


def gii_slug_candidates(abbreviation: str) -> list[str]:
    """GII slug spellings of an abbreviation: GII writes umlauts as '_' (AÜG -> a_g, SÜG -> s_g)."""
    raw = " ".join(unicodedata.normalize("NFC", abbreviation or "").casefold().split())
    raw = re.sub(r"\s+(?:19|20)\d{2}$", "", raw)
    sgb = re.fullmatch(r"sgb[\s.-]*([ivx]+|\d{1,2})", raw)
    if sgb:
        token = sgb.group(1)
        return [f"sgb_{({v: k for k, v in _ROMAN.items()}.get(token, token))}"]
    umlauts = re.sub(r"[äöüß]", "_", raw)           # kept: they are part of the GII slug
    separated = re.sub(r"[\s/.\-]+", "_", umlauts).strip("_")
    joined = re.sub(r"[\s/.\-]+", "", umlauts)
    return list(dict.fromkeys(re.sub(r"[^a-z0-9_]", "", v) for v in (separated, joined)))


@dataclass(frozen=True)
class ActIdentity:
    prefix: str | None          # "de/bund/sgb_14", "de/by/bayvwvfg", "eu/32016R0679", "int/emrk" or None
    basis: str                  # act_index|alias|gii_catalog|land_affix|land_context|eu|international|ambiguous|unresolved
    act_id: str | None = None   # Lexgraph act id when the act is in the deep corpus
    jurabk: str | None = None
    in_corpus: bool = False
    candidates: tuple[str, ...] = ()

    def norm_id(self, unit: str, number: str, sub: str = "") -> str | None:
        if not self.prefix:
            return None
        return f"{self.prefix}/{unit}/{number}" + (f"/{sub}" if sub else "")


class ActResolver:
    """Abbreviation -> act identity over Lexgraph's act index and the full GII catalogue."""

    def __init__(self, act_index: Iterable[dict[str, Any]] = (), catalog: Iterable[dict[str, Any]] = ()):
        self.act_index = [row for row in act_index if isinstance(row, dict)]
        self._by_alias: dict[str, list[dict[str, Any]]] = {}
        for row in self.act_index:
            for value in (row.get("jurabk") or "", re.sub(r"\s+\d{4}$", "", str(row.get("jurabk") or ""))):
                for key in exact_act_aliases(value) | {act_key(value)}:
                    if key and row not in self._by_alias.setdefault(key, []):
                        self._by_alias[key].append(row)
        self._by_slug: dict[str, dict[str, Any]] = {}
        for row in self.act_index:
            juris, slug = act_slug(row)
            if juris == "de/bund":
                self._by_slug[slug] = row
        self.catalog_slugs: set[str] = {str(r.get("abbrev") or r.get("slug") or "") for r in catalog} - {""}
        self._slug_keys: dict[str, set[str]] = {}
        for slug in self.catalog_slugs | set(self._by_slug):
            for key in {slug, re.sub(r"_(?:19|20)\d{2}$", "", slug)}:
                self._slug_keys.setdefault(key, set()).add(slug)

    @classmethod
    def from_data_dir(cls, data_dir: Path) -> "ActResolver":
        data_dir = Path(data_dir)
        index = json.loads((data_dir / "wiki.json").read_text(encoding="utf-8"))
        catalog_path = data_dir / "gii_catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8")).get("acts", []) \
            if catalog_path.exists() else []
        return cls(index, catalog)

    def _federal(self, slug: str, basis: str) -> ActIdentity:
        row = self._by_slug.get(slug)
        if row is not None:
            return ActIdentity(f"de/bund/{slug}", basis if basis != "gii_catalog" else "act_index",
                               row.get("id"), row.get("jurabk"), True)
        return ActIdentity(f"de/bund/{slug}", basis)

    def resolve_act(self, abbreviation: str, *, land: str | None = None) -> ActIdentity:
        key = act_key(abbreviation)
        if not key:
            return ActIdentity(None, "unresolved")
        if key in EU_ACTS:
            return ActIdentity(f"eu/{EU_ACTS[key]}", "eu")
        if key in INTERNATIONAL_ACTS:
            return ActIdentity(INTERNATIONAL_ACTS[key], "international")
        # 1. Lexgraph's deep corpus (federal and Bavarian acts with a jurabk).
        rows = self._by_alias.get(key, [])
        if len(rows) == 1:
            juris, slug = act_slug(rows[0])
            return ActIdentity(f"{juris}/{slug}", "act_index", rows[0].get("id"), rows[0].get("jurabk"), True)
        if len(rows) > 1:
            return ActIdentity(None, "ambiguous", candidates=tuple(sorted(r.get("id") or "" for r in rows)))
        # 2. Land acts by affix ("VwVfG NRW", "BayVwVfG") or by the deciding court's Land ("BV" in Bavaria).
        folded = re.sub(r"\s+(?:19|20)\d{2}$", "", " ".join((abbreviation or "").casefold().split()))
        parts = folded.replace("-", " ").split()
        if len(parts) >= 2 and parts[-1].strip(".") in LAND_SUFFIXES:
            return ActIdentity(f"de/{LAND_SUFFIXES[parts[-1].strip('.')]}/{act_key(' '.join(parts[:-1]))}",
                               "land_affix")
        for prefix, prefix_land in LAND_PREFIXES.items():
            head = (abbreviation or "")[len(prefix):len(prefix) + 1]
            if key.startswith(prefix) and len(key) > len(prefix) + 1 and abbreviation[:1].isupper() \
                    and head.isupper():
                return ActIdentity(f"de/{prefix_land}/{key}", "land_affix")
        if land and key in LAND_CONTEXT_ACTS.get(land, ()):
            return ActIdentity(f"de/{land}/{key}", "land_context")
        # 3. Renamed / irregular federal acts, then the full GII catalogue.
        if key in FEDERAL_ALIASES:
            return self._federal(FEDERAL_ALIASES[key], "alias")
        found: set[str] = set()
        for candidate in gii_slug_candidates(abbreviation):
            found |= self._slug_keys.get(candidate, set())
        if len(found) == 1:
            return self._federal(found.pop(), "gii_catalog")
        if len(found) > 1:
            exact = [s for s in found if s in gii_slug_candidates(abbreviation)]
            if len(exact) == 1:
                return self._federal(exact[0], "gii_catalog")
            return ActIdentity(None, "ambiguous", candidates=tuple(sorted(found)))
        return ActIdentity(None, "unresolved")
