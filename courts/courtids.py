"""Stable court identities: ``<type>-<place>`` (e.g. ``lsg-nw``, ``vg-muenchen``).

Land-level courts use ISO 3166-2:DE subdivision codes (``nw`` for NRW);
joint courts use joined codes (``bebb`` Berlin-Brandenburg, ``nihb``
Niedersachsen-Bremen).  Federal courts are bare (``bverwg``).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class CourtInfo:
    court_id: str
    court_type: str
    jurisdiction: str
    land: str


FEDERAL = {
    "bverfg": ("Bundesverfassungsgericht", "verfassung"),
    "bgh": ("Bundesgerichtshof", "ordentlich"),
    "bverwg": ("Bundesverwaltungsgericht", "verwaltung"),
    "bfh": ("Bundesfinanzhof", "finanz"),
    "bag": ("Bundesarbeitsgericht", "arbeit"),
    "bsg": ("Bundessozialgericht", "sozial"),
    "bpatg": ("Bundespatentgericht", "patent"),
    "gmsogb": ("Gemeinsamer Senat der obersten Gerichtshöfe des Bundes", "ordentlich"),
}

# (regex over the folded long/short name, type, branch); longest first.
COURT_TYPES = [
    (r"landessozialgericht|\blsg\b", "lsg", "sozial"),
    (r"sozialgericht|\bsg\b", "sg", "sozial"),
    (r"oberverwaltungsgericht|\bovg\b", "ovg", "verwaltung"),
    (r"verwaltungsgerichtshof|\bvgh\b", "vgh", "verwaltung"),
    (r"verwaltungsgericht|\bvg\b", "vg", "verwaltung"),
    (r"oberstes landesgericht|\bobl?g\b", "oblg", "ordentlich"),
    (r"oberlandesgericht|\bolg\b", "olg", "ordentlich"),
    (r"kammergericht|\bkg\b", "olg", "ordentlich"),
    (r"landesarbeitsgericht|\blag\b", "lag", "arbeit"),
    (r"arbeitsgericht|\barbg\b", "arbg", "arbeit"),
    (r"landgericht|\blg\b", "lg", "ordentlich"),
    (r"amtsgericht|\bag\b", "ag", "ordentlich"),
    (r"finanzgericht|\bfg\b", "fg", "finanz"),
    (r"landesverfassungsgericht|verfassungsgerichtshof|staatsgerichtshof|verfassungsgericht|"
     r"\bverfgh\b|\bstgh\b|\blverfg\b", "verfgh", "verfassung"),
    (r"anwaltsgerichtshof|\bagh\b", "agh", "ordentlich"),
    (r"anwaltsgericht", "anwg", "ordentlich"),
    (r"truppendienstgericht", "tdg", "verwaltung"),
    (r"dienstgerichtshof|dienstgericht", "dg", "verwaltung"),
    (r"berufsgericht", "bg", "verwaltung"),
]

LANDS = {
    "baden-wuerttemberg": "bw", "bw": "bw", "bayern": "by", "bayerisch": "by",
    "berlin-brandenburg": "bebb", "berlin": "be", "brandenburg": "bb",
    "brandenburgisch": "bb", "bremen": "hb", "bremisch": "hb", "hamburg": "hh",
    "hamburgisch": "hh", "hessen": "he", "hessisch": "he",
    "mecklenburg-vorpommern": "mv", "niedersachsen-bremen": "nihb",
    "niedersachsen": "ni", "niedersaechsisch": "ni", "nds.": "ni",
    "nordrhein-westfalen": "nw", "nrw": "nw", "rheinland-pfalz": "rp",
    "saarland": "sl", "saarlaendisch": "sl", "sachsen-anhalt": "st", "sachsen": "sn",
    "saechsisch": "sn", "schleswig-holstein": "sh", "schleswig-holsteinisch": "sh",
    "thueringen": "th", "thueringer": "th", "mv": "mv", "lsa": "st", "saarlandes": "sl",
    "sachsen-anhalts": "st", "brandenburgs": "bb", "rheinland-pfaelzisch": "rp",
}
# Seat names that denote the Land-level court in citations.
SEATS = {
    ("ovg", "muenster"): "nw", ("ovg", "lueneburg"): "ni", ("ovg", "koblenz"): "rp",
    ("ovg", "bautzen"): "sn", ("ovg", "magdeburg"): "st", ("ovg", "schleswig"): "sh",
    ("ovg", "weimar"): "th", ("ovg", "greifswald"): "mv", ("ovg", "saarlouis"): "sl",
    ("vgh", "mannheim"): "bw", ("vgh", "kassel"): "he", ("vgh", "muenchen"): "by",
    ("lsg", "essen"): "nw", ("lsg", "celle"): "nihb", ("lsg", "stuttgart"): "bw",
    ("lsg", "muenchen"): "by", ("lsg", "darmstadt"): "he", ("lsg", "mainz"): "rp",
    ("lsg", "chemnitz"): "sn", ("lsg", "halle"): "st", ("lsg", "erfurt"): "th",
    ("lsg", "potsdam"): "bebb", ("lsg", "neubrandenburg"): "mv", ("lsg", "saarbruecken"): "sl",
}
LAND_LEVEL = {"lsg", "ovg", "vgh", "lag", "fg", "verfgh", "oblg"}
ALIASES = {
    "bayvgh": "vgh-by", "baylsg": "lsg-by", "bayoblg": "oblg-by", "bayverfgh": "verfgh-by",
    "vghbw": "vgh-bw", "hessvgh": "vgh-he", "nds. ovg": "ovg-ni", "ovgnrw": "ovg-nw",
    "kg": "olg-be", "kammergericht": "olg-be",
}


_TRANSLIT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "ae", "Ö": "oe", "Ü": "ue"})


def fold(value: str) -> str:
    """Lowercase, German transliteration (ü -> ue), other diacritics removed."""
    value = unicodedata.normalize("NFC", value).translate(_TRANSLIT)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(value.lower().split())


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(value)).strip("-")


def resolve(name: str, *, default_land: str | None = None) -> CourtInfo:
    """Map a court name (long or abbreviated) to a stable identity."""
    folded = re.sub(r"(gericht|gerichtshof|senat)(e?s)\b", r"\1", fold(name))
    compact = folded.replace(" ", "")
    for key, (_, branch) in FEDERAL.items():
        if compact == key or folded == fold(FEDERAL[key][0]):
            return CourtInfo(key, key, branch, "de")
    if compact.startswith("gemeinsamersenat"):
        return CourtInfo("gmsogb", "gmsogb", "ordentlich", "de")
    alias = ALIASES.get(folded) or ALIASES.get(compact)
    if alias:
        ctype, land = alias.split("-", 1)
        branch = next((b for p, t, b in COURT_TYPES if t == ctype), "ordentlich")
        return CourtInfo(alias, ctype, branch, land)
    for pattern, ctype, branch in COURT_TYPES:
        match = re.search(pattern, folded)
        if not match:
            continue
        rest = (folded[:match.start()] + " " + folded[match.end():])
        rest = re.sub(r"\b(fuer das land|fuer den freistaat|des landes|fuer|das|des|land|der|die|in|zu|am|freistaat)\b",
                      " ", rest)
        rest = " ".join(rest.replace("(", " ").replace(")", " ").split())
        land = _land_of(rest)
        if ctype in LAND_LEVEL and land:
            return CourtInfo(f"{ctype}-{land}", ctype, branch, land)
        if land and rest not in ("berlin", "bremen", "hamburg") and \
                re.sub(r"isch(?:es|er|en|e)\b", "isch", rest).strip() in LANDS:
            # A local court named only by its Land ("Schleswig-Holsteinisches Verwaltungsgericht"):
            # the Land's single court of that type.
            return CourtInfo(f"{ctype}-{land}", ctype, branch, land)
        if ctype == "oblg" and not rest:
            return CourtInfo("oblg-by", ctype, branch, "by")
        seat = slug(rest)
        seat_land = SEATS.get((ctype, seat))
        if seat_land:
            return CourtInfo(f"{ctype}-{seat_land}", ctype, branch, seat_land)
        if not seat:
            break
        return CourtInfo(f"{ctype}-{seat}", ctype, branch, land or default_land or "")
    return CourtInfo("court-" + slug(name), "unknown", "unknown", default_land or "")


def _land_of(rest: str) -> str | None:
    rest = re.sub(r"isch(?:es|er|en|e)\b", "isch", rest).strip()
    if rest in LANDS:
        return LANDS[rest]
    for word in rest.split():
        if word in LANDS:
            return LANDS[word]
    return None


PRIORITY_BRANCHES = ("sozial", "verwaltung")


def priority(jurisdiction: str | None) -> int:
    """Backfill order: social and administrative courts first."""
    return 0 if jurisdiction in PRIORITY_BRANCHES else 1
