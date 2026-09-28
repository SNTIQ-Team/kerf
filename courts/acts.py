"""Act abbreviation -> norm identity base (``de/bund/<act>``, ``de/<land>/<act>``, ``eu/<celex>``).

The act key is the folded abbreviation without punctuation; SGB books use
Arabic numbers (``SGB XIV`` -> ``sgb14``), matching the spec example
``de/bund/sgb14/para/37/abs/2/nr/3``.  Federal membership is checked against
the official GII catalogue (``courts/data/acts.json``, regenerated with
``python -m courts.acts <gii catalog.jsonl> [acts.jsonl]``).  Unknown
abbreviations are *unresolved*, never guessed as federal.
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .courtids import fold

DATA = Path(__file__).with_name("data") / "acts.json"
ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8,
         "ix": 9, "x": 10, "xi": 11, "xii": 12, "xiii": 13, "xiv": 14}

# Established short names whose GII slug differs or that were renamed.
FEDERAL_ALIASES = {
    # abbreviation key (or GII slug key) -> canonical act key
    "asylvfg": "asylg", "rustag": "stag", "freizggeu": "freizuegeu",
    "freizueggeu": "freizuegeu", "freizgeu": "freizuegeu", "bbaug": "baugb",
    "woeigg": "weg", "bgbeg": "egbgb", "zpoeg": "egzpo", "stgbeg": "egstgb",
    "gvgeg": "eggvg", "hgbeg": "eghgb", "inso eg": "eginso",
}
# Long names occasionally used instead of the abbreviation.
LONG_NAMES = {
    "grundgesetz": "gg", "grundgesetzes": "gg", "verwaltungsgerichtsordnung": "vwgo",
    "sozialgerichtsgesetz": "sgg", "sozialgerichtsgesetzes": "sgg",
    "zivilprozessordnung": "zpo", "buergerlichen gesetzbuchs": "bgb",
    "buergerlichen gesetzbuches": "bgb", "buergerliches gesetzbuch": "bgb",
    "strafgesetzbuch": "stgb", "strafgesetzbuchs": "stgb", "strafprozessordnung": "stpo",
    "aufenthaltsgesetz": "aufenthg", "aufenthaltsgesetzes": "aufenthg",
    "asylgesetz": "asylg", "asylgesetzes": "asylg", "verwaltungsverfahrensgesetz": "vwvfg",
    "verwaltungsverfahrensgesetzes": "vwvfg", "abgabenordnung": "ao",
    "einkommensteuergesetz": "estg", "einkommensteuergesetzes": "estg",
    "asylbewerberleistungsgesetz": "asylblg", "asylbewerberleistungsgesetzes": "asylblg",
}
EU_ACTS = {
    "dsgvo": "32016R0679", "dsgvo2016": "32016R0679", "eudsgvo": "32016R0679",
    "aeuv": "12016E", "euv": "12016M", "grch": "12016P", "grc": "12016P",
    "eugrch": "12016P", "dublinivo": "32013R0604", "dublin3vo": "32013R0604",
    "dublinverordnung": "32013R0604", "aufnahmerl": "32013L0033", "qualifikationsrl": "32011L0095",
    "asylverfahrensrl": "32013L0032", "rueckfuehrungsrl": "32008L0115",
    "unionsbuergerrl": "32004L0038", "freizuegigkeitsrl": "32004L0038",
    "vo8832004": "32004R0883", "eugvvo": "32012R1215", "brueiavo": "32019R1111",
}
INTERNATIONAL = {"emrk": "int/emrk", "grk": "int/gfk", "gfk": "int/gfk",
                 "unbrk": "int/unbrk", "brk": "int/unbrk", "kinderrechtskonvention": "int/unkrk"}
# Land prefixes/suffixes in abbreviations (BayVwVfG, VwVfG NRW, SächsPolG, ...).
LAND_PREFIXES = {"bay": "by", "bln": "be", "bbg": "bb", "brem": "hb", "hmb": "hh",
                 "hess": "he", "nds": "ni", "saechs": "sn", "thuer": "th", "saarl": "sl",
                 "bw": "bw", "nrw": "nw", "rhpf": "rp", "lsa": "st"}
LAND_SUFFIXES = {"nrw": "nw", "nw": "nw", "bw": "bw", "bln": "be", "be": "be",
                 "bbg": "bb", "lsa": "st", "mv": "mv", "sh": "sh", "rp": "rp",
                 "rlp": "rp", "saarl": "sl", "sl": "sl", "nds": "ni", "hb": "hb",
                 "hh": "hh", "he": "he", "th": "th", "sn": "sn", "by": "by", "st": "st"}


@dataclass(frozen=True)
class ActRef:
    base: str | None        # identity prefix, e.g. "de/bund/vwgo"; None if unresolved
    key: str
    resolution: str         # gii_catalog | alias | eu | international | land_affix | unresolved
    gii_slug: str | None = None


def act_key(abbreviation: str) -> str:
    text = fold(abbreviation)
    text = re.sub(r"\s+(?:19|20)\d{2}$", "", text)            # "AsylVfG 1992"
    sgb = re.fullmatch(r"sgb[\s.-]*([ivx]+|\d{1,2})", text)
    if sgb:
        book = sgb.group(1)
        return f"sgb{ROMAN.get(book, book)}"
    return re.sub(r"[^a-z0-9]", "", text)


def slug_key(slug: str) -> str:
    slug = re.sub(r"_(?:19|20)\d{2}$", "", slug.lower())
    return slug.replace("_", "").replace("-", "")


@lru_cache(maxsize=1)
def _table() -> dict:
    try:
        return json.loads(DATA.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"federal": {}}


@lru_cache(maxsize=4096)
def resolve_act(abbreviation: str) -> ActRef:
    key = act_key(abbreviation)
    federal: dict[str, str] = _table()["federal"]
    if key in EU_ACTS:
        return ActRef(f"eu/{EU_ACTS[key]}", key, "eu")
    if key in INTERNATIONAL:
        return ActRef(INTERNATIONAL[key], key, "international")
    folded = fold(abbreviation)
    parts = folded.replace("-", " ").split()
    if len(parts) >= 2 and parts[-1].strip(".") in LAND_SUFFIXES:
        land = LAND_SUFFIXES[parts[-1].strip(".")]
        return ActRef(f"de/{land}/{act_key(' '.join(parts[:-1]))}", key, "land_affix")
    for prefix, land in LAND_PREFIXES.items():
        if key.startswith(prefix) and len(key) > len(prefix) + 1 and abbreviation[:1].isupper() \
                and abbreviation[len(prefix):len(prefix) + 1].isupper():
            return ActRef(f"de/{land}/{key}", key, "land_affix")
    canonical = FEDERAL_ALIASES.get(key, key)
    if canonical in federal:
        resolution = "gii_catalog" if canonical == key else "alias"
        return ActRef(f"de/bund/{canonical}", canonical, resolution, federal[canonical] or None)
    return ActRef(None, key, "unresolved")


def long_name_key(name: str) -> str | None:
    return LONG_NAMES.get(fold(name))


def build(catalog: Path, acts: Path | None = None) -> dict:
    """Regenerate the federal table from a GII catalog (+ optional jurabk list)."""
    federal: dict[str, str] = {}
    for line in catalog.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        slug = json.loads(line).get("abbrev") or ""
        if slug:
            federal.setdefault(slug_key(slug), slug)
    if acts is not None:
        for line in acts.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                if row.get("jurabk") and row.get("slug"):
                    federal[act_key(row["jurabk"])] = row["slug"]
    for alias, target in FEDERAL_ALIASES.items():
        federal.setdefault(target, federal.get(alias, ""))
    return {"source": "gesetze-im-internet.de catalogue (GII TOC) + GII jurabk",
            "generated_from": str(catalog.name), "federal": dict(sorted(federal.items()))}


if __name__ == "__main__":
    table = build(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(table, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    print(f"{len(table['federal'])} federal act keys -> {DATA}")
