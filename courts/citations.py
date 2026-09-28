"""Parser-derived citation relations.

The parser only ever emits ``mentions``, ``quotes`` and
``procedural_reference`` (spec, "Relation vocabulary").  Legal effect
(applies/interprets/supports ...) is never inferred here: every relation
carries ``derived_by: parser`` and ``legal_effect_verified: false``.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import date

from .acts import ActIdentity, long_name_abbreviation, resolve_act
from .casenumbers import case_key, clean as clean_az, id_component
from .courtids import resolve as resolve_court

PARSER_RELATIONS = frozenset({"mentions", "quotes", "procedural_reference"})
FORBIDDEN_FOR_PARSER = frozenset({"applies", "interprets", "supports", "rejects_application_of",
                                  "distinguishes"})

WS = r"[ \t  \n]"
S = WS + "*"
NUM = r"\d{1,4}[a-z]{0,2}(?![\w])"
LEVELS = ("unit", "abs", "satz", "nr", "buchst", "extra")
SUB_KEYWORDS = [
    ("abs", r"(?:Abs\.?|Absatz|Absätze|Absätzen|Abss\.)"),
    ("satz", r"(?:Satz|Sätze|Sätzen|S\.)"),
    ("nr", r"(?:Nrn?\.|Nummer(?:n)?|Ziff\.|Ziffer(?:n)?)"),
    ("buchst", r"(?:Buchst\.?|Buchstabe(?:n)?|lit\.)"),
    ("extra", r"(?:Halbs\.|Halbsatz|Hs\.|HS|Alt\.|Alternative|Var\.|Variante|UAbs\.|Unterabs\.|Unterabsatz|Spiegelstrich)"),
]
HEAD_RE = re.compile(r"(?<![\w§])(?P<head>§§?|Artt?\.|Artikel|Art(?=" + WS + r"+\d))" + S + r"(?=\d)")
SUB_RE = re.compile(S + "(?:" + "|".join(f"(?P<{k}>{p})" for k, p in SUB_KEYWORDS) + ")" + S)
VALUE_RE = re.compile(r"(?P<v>\d{1,4}[a-z]{0,2}|[a-z]{1,2}\)?)(?![\w])")
FOLLOW_RE = re.compile(S + r"(?P<f>ff?\.|ff(?![\w]))")
LIST_SEP_RE = re.compile(S + r"(?P<sep>,|und|oder|bis|sowie|bzw\.|[-–])" + S)
IVM_RE = re.compile(S + r"(?:,\s*)?(?:i\.\s?V\.\s?m\.|iVm\.?|in Verbindung mit|i\.\s?S\.\s?v\.|iSv\.?)" + S)
CHAIN_SEP_RE = re.compile(S + r"(?:,|;|und|sowie|oder|bzw\.)" + S + r"(?=(?:§|Art))")
ARTICLE_WORDS = r"(?:des|der|dem|den|im|zum|zur|nach)"
EU_ACT_RE = re.compile(
    S + r"(?:" + ARTICLE_WORDS + S + r")?(?P<name>[\w-]*?(?:Richtlinie|richtlinie|Verordnung|verordnung|RL|VO))"
    + S + r"(?:\((?P<fam>EU|EG|EWG)\)" + S + r")?(?:Nr\.?" + S + r")?"
    r"(?P<a>\d{1,4})/(?P<b>\d{1,4})(?:/(?P<suffix>EU|EG|EWG))?")
ACT_TOKEN_RE = re.compile(
    S + r"(?:" + ARTICLE_WORDS + S + r")?"
    r"(?P<act>[A-ZÄÖÜ][A-Za-zÄÖÜäöüß]*(?:[-/][A-Za-zÄÖÜäöüß]+)*\.?)"
    r"(?P<book>" + WS + r"(?:[IVX]{1,5}|\d{1,2})(?![\w/])(?!\.\d))?"
    r"(?P<land>" + WS + r"(?:NRW|NW|BW|Bln|Bbg|LSA|M-V|MV|SH|RP|RLP|Saarl|Nds|HB|HH|Bay)\.?(?![\w]))?"
    r"(?P<year>" + WS + r"(?:19|20)\d{2}(?![\w/]))?")
LONG_NAME_RE = re.compile(S + ARTICLE_WORDS + S + r"(?P<name>(?:Bürgerlichen" + WS + r"Gesetzbuch(?:es|s)?|"
                          r"[A-ZÄÖÜ][a-zäöüß]+(?:gesetz(?:es|buch(?:es|s)?)?|ordnung)))(?![\w])")
NOT_ACTS = {"Rn", "Rdnr", "Randnr", "Abs", "Satz", "Nr", "S", "Art", "Anm", "Aufl", "Bd", "Kap", "Teil",
            "Buchst", "Alt", "Hs", "Var", "Ziff", "Tz", "Fn", "Vorb", "Vgl", "Vorbem", "Anl", "Anlage",
            "BGBl", "BT", "BR", "Drs", "GVBl", "GV", "Sp", "Rz", "Rdn", "Verf", "Az", "Beschluss", "Urteil",
            "Der", "Die", "Das", "Den", "Dem", "Des", "Diese", "Dieser", "Dies", "Es", "Er", "Sie", "Im",
            "In", "Nach", "Gemäß", "Mit", "Von", "Zu", "Für", "Ein", "Eine", "Einer", "Auch", "Aber",
            "Hier", "Dabei", "Danach", "Damit", "Denn", "Soweit", "Insoweit", "Entgegen", "Unter", "Bei",
            "Aus", "Auf", "Wie", "Wenn", "Ob", "Da", "So", "Zwar", "Jedoch", "Allerdings", "Sofern",
            "Eu", "EU", "EG", "Buchstabe", "Nummer", "Absatz", "Sätze", "Halbsatz", "Kläger", "Klägerin",
            "Beklagte", "Beklagten", "Antragsteller", "Antragstellerin", "Gericht", "Senat", "Kammer"}
QUOTE_INTRO_RE = re.compile(S + r"(?:lautet|lautete|bestimmt|bestimmte|heißt es|hieß es|hat folgenden Wortlaut|"
                            r"sieht vor|regelt)[^„“\"»]{0,60}?[:,]?" + S + r"[„“\"»]")


@dataclass
class Relation:
    relation: str
    target_kind: str                 # norm | decision
    target_id: str | None            # canonical identity (None = unresolved)
    target_raw: str
    paragraph_id: str | None
    span_start: int | None
    span_end: int | None
    quoted_context: str
    confidence: float
    detail: dict = field(default_factory=dict)
    derived_by: str = "parser"
    legal_effect_verified: bool = False

    def __post_init__(self) -> None:
        if self.relation not in PARSER_RELATIONS:
            raise ValueError(f"parser may not emit relation {self.relation!r}")

    def evidence(self) -> dict:
        return {"paragraph_id": self.paragraph_id, "quoted_context": self.quoted_context,
                "span": [self.span_start, self.span_end] if self.span_start is not None else None,
                "derived_by": self.derived_by, "legal_effect_verified": self.legal_effect_verified,
                "confidence": self.confidence}


def context_window(text: str, start: int, end: int, width: int = 160) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    return " ".join(text[left:right].split())


# --- norms -------------------------------------------------------------------

@dataclass
class _Item:
    unit_type: str
    levels: dict[str, list[str]] = field(default_factory=dict)
    ranges: dict[str, bool] = field(default_factory=dict)
    following: str | None = None
    extra_raw: list[str] = field(default_factory=list)
    last_level: str = "unit"


def _read_values(text: str, pos: int, level: str, single: bool = False) -> tuple[list[str], int, bool, str | None]:
    """Read '1', '1 und 2', '3 bis 5', '1, 2', 'a und b'; stop before § or keywords."""
    values: list[str] = []
    is_range = False
    following = None
    m = VALUE_RE.match(text, pos)
    if not m or (level != "buchst" and not m.group("v")[0].isdigit()) or \
            (level == "buchst" and m.group("v")[0].isdigit()):
        return values, pos, is_range, following
    values.append(m.group("v").rstrip(")"))
    pos = m.end()
    while not single:
        f = FOLLOW_RE.match(text, pos)
        if f and level == "unit":
            following = "ff" if f.group("f").startswith("ff") else "f"
            pos = f.end()
            break
        sep = LIST_SEP_RE.match(text, pos)
        if not sep:
            break
        nxt = VALUE_RE.match(text, sep.end())
        if not nxt:
            break
        value = nxt.group("v").rstrip(")")
        same_kind = value[0].isdigit() == values[-1][0].isdigit()
        if not same_kind:
            break
        if sep.group("sep") in ("bis", "-", "–"):
            is_range = True
        values.append(value)
        pos = nxt.end()
    return values, pos, is_range, following


def _expand(values: list[str], is_range: bool) -> tuple[list[str], bool]:
    if is_range and len(values) == 2 and values[0].isdigit() and values[1].isdigit():
        lo, hi = int(values[0]), int(values[1])
        if 0 < hi - lo <= 30:
            return [str(n) for n in range(lo, hi + 1)], True
    return values, False


def _act_at(text: str, pos: int, land: str | None = None) -> tuple[ActIdentity | None, str, int] | None:
    eu = EU_ACT_RE.match(text, pos)
    if eu:
        a, b = eu.group("a"), eu.group("b")
        kind = "L" if re.search(r"(?i)richtlinie|RL$", eu.group("name")) else "R"
        year, number = (a, b) if (len(a) == 4 and a[:2] in ("19", "20") and not (len(b) == 4 and b[:2] in ("19", "20") and "Nr" in eu.group(0))) else (b, a)
        if len(year) == 2:
            year = ("19" if int(year) > 50 else "20") + year
        celex = f"3{year}{kind}{int(number):04d}"
        return ActIdentity(f"eu/{celex}", "eu_citation"), eu.group(0).strip(), eu.end()
    long = LONG_NAME_RE.match(text, pos)
    if long:
        abbreviation = long_name_abbreviation(" ".join(long.group("name").split()))
        if abbreviation:
            return resolve_act(abbreviation, land), long.group(0).strip(), long.end()
    m = ACT_TOKEN_RE.match(text, pos)
    if not m:
        return None
    token = m.group("act").rstrip(".")
    if token in NOT_ACTS or len(token) < 2:
        return None
    uppers = sum(1 for c in token if c.isupper())
    looks_like_abbreviation = uppers >= 2 or re.search(r"(?:G|O|V|B|VO|RL|StV)$", token)
    if not looks_like_abbreviation:
        return None
    candidates = []
    if m.group("book"):
        candidates.append((token + m.group("book"), m.end("book")))
    if m.group("land"):
        candidates.append((token + m.group("land"), m.end("land")))
    candidates.append((token, m.end("act")))
    for raw, end in candidates:
        ref = resolve_act(" ".join(raw.split()), land)
        if ref.prefix or raw == token:
            year_end = m.end("year") if m.group("year") and end == m.end("act") else end
            return ref, " ".join(text[m.start("act"):year_end].split()), year_end
    return None


def _norm_id(base: str, unit_type: str, combo: dict[str, str]) -> str:
    parts = [base, unit_type, combo["unit"]]
    for level in ("abs", "satz", "nr", "buchst"):
        if combo.get(level):
            parts += [level, combo[level]]
    return "/".join(parts)


def _read_subs(text: str, item: _Item, item_start: int, cursor: int, plural: bool,
               pending: list) -> tuple[_Item, int, int]:
    """Sub-units, possibly several items sharing one head ("Abs. 1 Satz 1, Abs. 2").

    Under a plural head ("§§ 920 Abs. 2, 294 ZPO") a separator followed by a
    bare number after a sub-unit starts the next norm, not another Absatz.
    """
    while True:
        sub = SUB_RE.match(text, cursor)
        if sub and plural and item.last_level == "unit" and len(item.levels["unit"]) > 1 \
                and not item.ranges.get("unit"):
            # "§§ 60, 61 Abs. 1 SGB I": the Absatz belongs to § 61 only.
            head_units = item.levels["unit"][:-1]
            pending.append((_Item(item.unit_type, levels={"unit": head_units}), item_start, cursor))
            item.levels["unit"] = item.levels["unit"][-1:]
        if sub:
            level = next(k for k, _ in SUB_KEYWORDS if sub.group(k))
            if LEVELS.index(level) <= LEVELS.index(item.last_level) and level != "extra":
                pending.append((item, item_start, cursor))
                parent = _Item(item.unit_type)
                for lv in LEVELS[:LEVELS.index(level)]:
                    if lv in item.levels:
                        parent.levels[lv] = item.levels[lv][-1:]
                item, item_start = parent, sub.start()
            values, cursor2, is_range, _ = _read_values(text, sub.end(), level, single=plural)
            if not values:
                break
            if level == "extra":
                item.extra_raw.append(text[sub.start():cursor2].strip())
            else:
                item.levels[level], item.ranges[level] = values, is_range
                item.last_level = level
            cursor = cursor2
            sep = re.match(S + r",(?=" + S + r"(?:Abs|Satz|S\.|Nr|Buchst|lit))", text[cursor:])
            if sep:
                cursor += sep.end()
            continue
        if plural and item.last_level != "unit":
            sep = LIST_SEP_RE.match(text, cursor)
            nxt = VALUE_RE.match(text, sep.end()) if sep else None
            if nxt and nxt.group("v")[0].isdigit():
                pending.append((item, item_start, cursor))
                values, cursor2, is_range, following = _read_values(text, sep.end(), "unit")
                item = _Item(item.unit_type, following=following)
                item.levels["unit"], item.ranges["unit"] = values, is_range
                item_start, cursor = sep.end(), cursor2
                continue
        break
    return item, item_start, cursor


def extract_norms(text: str, paragraph_id: str | None = None, *, land: str | None = None) -> list[Relation]:
    relations: list[Relation] = []
    pos = 0
    chain_no = 0
    while True:
        head = HEAD_RE.search(text, pos)
        if not head:
            break
        chain_start = head.start()
        chain_no += 1
        pending: list[tuple[_Item, int, int]] = []
        resolved: list[tuple[_Item, int, int, ActIdentity | None, str]] = []
        cursor = head.start()
        in_conjunction = False
        while True:
            h = HEAD_RE.match(text, cursor)
            if h:
                unit_type = "para" if h.group("head").startswith("§") else "art"
                plural = h.group("head") in ("§§", "Artt.")
                item = _Item(unit_type)
                values, cursor2, is_range, following = _read_values(text, h.end(), "unit")
                if not values:
                    break
                item.levels["unit"], item.ranges["unit"] = values, is_range
                item.following = following
                cursor = cursor2
                item_start = h.start()
            else:
                break
            item, item_start, cursor = _read_subs(text, item, item_start, cursor, plural, pending)
            pending.append((item, item_start, cursor))
            act = _act_at(text, cursor, land)
            if act:
                ref, raw_act, cursor = act
                for it, s, e in pending:
                    resolved.append((it, s, cursor, ref, raw_act))
                pending = []
            ivm = IVM_RE.match(text, cursor)
            if ivm and HEAD_RE.match(text, ivm.end()):
                cursor = ivm.end()
                in_conjunction = True
                continue
            sep = CHAIN_SEP_RE.match(text, cursor)
            if sep and HEAD_RE.match(text, sep.end()):
                cursor = sep.end()
                continue
            break
        chain_end = max(cursor, head.end())
        chain_text = " ".join(text[chain_start:chain_end].split())
        quoted = bool(QUOTE_INTRO_RE.match(text, chain_end))
        for item, start, end, ref, raw_act in resolved:
            relations.extend(_item_relations(item, ref, raw_act, text, start, end, chain_text,
                                             paragraph_id, chain_no, in_conjunction, quoted))
        pos = max(chain_end, head.end())
    return relations


def _item_relations(item: _Item, ref: ActIdentity | None, raw_act: str, text: str, start: int, end: int,
                    chain_text: str, paragraph_id: str | None, chain_no: int, in_conjunction: bool,
                    quoted: bool) -> list[Relation]:
    level_values = []
    via_range = False
    for level in ("unit", "abs", "satz", "nr", "buchst"):
        if level in item.levels:
            values, expanded = _expand(item.levels[level], item.ranges.get(level, False))
            via_range |= expanded
            level_values.append([(level, v) for v in values])
    combos = list(itertools.islice(itertools.product(*level_values), 60))
    out = []
    for combo in combos:
        values = dict(combo)
        target = _norm_id(ref.prefix, item.unit_type, values) if ref and ref.prefix else None
        raw = f"{'§' if item.unit_type == 'para' else 'Art.'} {values['unit']}" + "".join(
            f" {lbl} {values[lv]}" for lv, lbl in (("abs", "Abs."), ("satz", "Satz"), ("nr", "Nr."),
                                                   ("buchst", "Buchst.")) if values.get(lv)) + f" {raw_act}"
        detail = {"act": raw_act, "act_prefix": ref.prefix if ref else None,
                  "act_resolution": ref.basis if ref else "unresolved",
                  "lexgraph_act_id": ref.act_id if ref else None, "unit_type": item.unit_type,
                  "chain": chain_text, "chain_no": chain_no}
        if item.following:
            detail["following"] = item.following
        if via_range:
            detail["via_range"] = True
        if item.extra_raw:
            detail["pinpoint_extra"] = item.extra_raw
        if in_conjunction:
            detail["in_conjunction_with"] = True
        out.append(Relation(
            relation="quotes" if quoted else "mentions", target_kind="norm", target_id=target,
            target_raw=raw, paragraph_id=paragraph_id, span_start=start, span_end=end,
            quoted_context=context_window(text, start, end),
            confidence=0.9 if target else 0.4, detail=detail))
    return out


# --- decisions ---------------------------------------------------------------

MONTHS = {"januar": 1, "jan": 1, "februar": 2, "feb": 2, "febr": 2, "märz": 3, "maerz": 3, "mär": 3,
          "mrz": 3, "april": 4, "apr": 4, "mai": 5, "juni": 6, "jun": 6, "juli": 7, "jul": 7,
          "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10,
          "november": 11, "nov": 11, "dezember": 12, "dez": 12}
DATE_RE = (r"(?P<d>\d{1,2})\.\s?(?:(?P<m>\d{1,2})\.|(?P<mn>Januar|Februar|März|April|Mai|Juni|Juli|August|"
           r"September|Oktober|November|Dezember|Jan\.|Febr?\.|Mär\.|Mrz\.|Apr\.|Jun\.|Jul\.|Aug\.|Sept?\.|"
           r"Okt\.|Nov\.|Dez\.))\s?(?P<y>\d{4}|\d{2})")
DECISION_REF_RE = re.compile(
    r"(?:(?P<dtype>Urteile?n?|Urt\.|Beschlüsse?n?|Beschl\.|Gerichtsbescheide?s?|Kammerbeschlüsse?n?|"
    r"Senatsurteile?n?|Senatsbeschlüsse?n?|Vorlagebeschlusse?s?|Teilurteile?s?|Entscheidung(?:en)?)"
    + S + r")?(?:vom|v\.)" + S + DATE_RE + S +
    r"(?:[-–—]|,)" + S + r"(?:Az\.?:?" + S + r"|Aktenzeichen:?" + S + r")?"
    r"(?P<az>[A-ZÄÖÜ0-9IVX][^\n;()]{2,45}?)(?=" + S + r"(?:[-–—](?:\s|$)|[,;)]|\s(?:und|sowie|juris|BVerwGE|BSGE|BVerfGE|BGHZ|BFHE|BAGE|NJW|NVwZ)\b|\.\s|$))")
AZ_SHAPE = re.compile(r"(?:\d\s?[/.]\s?\d{2,5}\b|\b[CTF]\s?[-‑–]\s?\d{1,4}/\d{2}\b|\d{1,4}/\d{2}\b)")
COURT_NAMES = (
    r"BVerfG|BGH|BVerwG|BFH|BAG|BSG|BPatG|EuGH|EGMR|GmS-OGB|GemS-OGB|BayVGH|BayLSG|BayObLG|BayVerfGH|"
    r"Bundesverfassungsgerichts?|Bundesgerichtshofs?e?s?|Bundesverwaltungsgerichts?|Bundesfinanzhofs?e?s?|"
    r"Bundesarbeitsgerichts?|Bundessozialgerichts?|Bundespatentgerichts?|Gerichtshofs? der Europäischen Union|"
    r"Europäischen Gerichtshofs? für Menschenrechte|"
    r"(?:(?:Bayerische[rns]?|Hessische[rns]?|Sächsische[rns]?|Thüringer|Niedersächsische[rns]?|"
    r"Schleswig-Holsteinische[rns]?|Hamburgische[rns]?|Saarländische[rns]?|Bay\.|Hess\.|Sächs\.|Thür\.|Nds\.)"
    + WS + r")?"
    r"(?:LSG|OVG|VGH|VG|SG|OLG|LG|AG|LAG|ArbG|FG|KG|VerfGH|StGH|Landessozialgerichts?|"
    r"Oberverwaltungsgerichts?|Verwaltungsgerichtshofs?e?s?|Verwaltungsgerichts?|Sozialgerichts?|"
    r"Oberlandesgerichts?|Landgerichts?|Amtsgerichts?|Landesarbeitsgerichts?|Arbeitsgerichts?|"
    r"Finanzgerichts?|Verfassungsgerichtshofs?e?s?)"
    r"(?:" + WS + r"(?:für das Land" + WS + r"|des Landes" + WS + r")?(?!Urteil|Beschluss|vom|v\.)"
    r"[A-ZÄÖÜ][\wÄÖÜäöüß.-]+(?:" + WS + r"(?:(?:am|an der|in der|im)" + WS + r")?"
    r"(?!Urteil|Beschluss|vom)[A-ZÄÖÜ][\wäöüß-]+)?)?")
COURT_RE = re.compile(r"(?<![\w-])(?P<court>" + COURT_NAMES + r")(?![\w])")
SELF_RE = re.compile(r"(?<![\w-])(?P<self>Senats?|Senatsurteile?s?|Senatsbeschlusse?s?|Kammer|Kammerbeschlusse?s?|"
                     r"Große[nr]? Senats?)(?![\w])")
ECLI_RE = re.compile(r"ECLI:[A-Z]{2}:[A-Za-z]+:\d{4}:[A-Za-z0-9.\-]*[A-Za-z0-9]")
PROCEDURAL_RE = re.compile(r"(?i)angefochten|angegriffen|Vorinstanz|vorinstanzlich|vorgehend|erstinstanzlich|"
                           r"Ausgangsgericht|zurückverwiesen|Zurückverweisung|Berufungsurteil|Beschwerde gegen|"
                           r"Berufung gegen|Revision gegen|Nichtzulassungsbeschwerde gegen|aufgehoben")
OPEN_QUOTES = "„“\"«»‚"
CLOSE_QUOTES = "“”\"»«‘’"


def parse_date(match: re.Match) -> str | None:
    day = int(match.group("d"))
    if match.group("m"):
        month = int(match.group("m"))
    else:
        month = MONTHS.get(match.group("mn").rstrip(".").lower(), 0)
    year = int(match.group("y"))
    if year < 100:
        year += 1900 if year > 50 else 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def synthetic_decision_id(court_id: str, iso_date: str, az: str) -> str:
    return f"court:{court_id}/{iso_date}/{id_component(az)}"


def extract_decisions(text: str, paragraph_id: str | None, *, own_court_id: str | None,
                      own_case_keys: set[str] = frozenset(), own_land: str | None = None) -> list[Relation]:
    relations: list[Relation] = []
    courts = [(m.start(), m.end(), m.group("court")) for m in COURT_RE.finditer(text)]
    selves = [(m.start(), m.end(), None) for m in SELF_RE.finditer(text)] if own_court_id else []
    anchors = sorted(courts + selves)
    for ref in DECISION_REF_RE.finditer(text):
        az = clean_az(ref.group("az")).rstrip(" -–—,.")
        # Trailing annotations are not part of the docket: "1 BvL 21/12 mwN", "… m.w.N.", "… a.a.O.".
        az = re.sub(r"\s+(?:m\.?\s?w\.?\s?N|mwN|a\.?\s?a\.?\s?O|juris|zit\.?|ff?)\.?$", "", az).rstrip(" -–—,.")
        if not AZ_SHAPE.search(az) or re.search(r"(?i)\b(juris|rn|rdnr|seite)\b", az):
            continue
        iso = parse_date(ref)
        if not iso:
            continue
        at = ref.start("d")
        before = [a for a in anchors if a[0] < at and at - a[1] <= 280]
        if not before:
            continue
        a_start, a_end, court_name = before[-1]
        if court_name is None:
            court_id = own_court_id
            court_label = "self"
        else:
            info = resolve_court(court_name.replace("Gerichtshof der Europäischen Union", "EuGH"),
                                 default_land=own_land)
            court_id, court_label = _court_id_fixups(court_name, info.court_id), court_name
        if not court_id or court_id.startswith("court-"):
            continue
        target = synthetic_decision_id(court_id, iso, az)
        # Only the clause that introduces this citation counts: cut the prefix at
        # the last "(", ";" or sentence end so that an earlier sentence's
        # "Vorinstanz" does not turn a plain citation into a procedural one.
        prefix = re.split(r"[(;]|\.\s+(?=[A-ZÄÖÜ])", text[max(0, a_start - 90):a_start])[-1]
        if case_key(az) in own_case_keys:
            relation = "procedural_reference"
        elif PROCEDURAL_RE.search(prefix) or PROCEDURAL_RE.search(text[a_end:ref.start()]):
            relation = "procedural_reference"
        else:
            relation = "mentions"
        quoted = _quote_before(text, a_start)
        if quoted and relation == "mentions":
            relation = "quotes"
        relations.append(Relation(
            relation=relation, target_kind="decision", target_id=target,
            target_raw=" ".join(text[a_start:ref.end()].split()), paragraph_id=paragraph_id,
            span_start=a_start, span_end=ref.end(),
            quoted_context=quoted or context_window(text, a_start, ref.end()),
            confidence=0.85,
            detail={"court": court_label, "court_id": court_id, "date": iso, "case_number": az,
                    "decision_type": (ref.group("dtype") or None), "case_key": case_key(az)}))
    for m in ECLI_RE.finditer(text):
        relations.append(Relation(
            relation="mentions", target_kind="decision", target_id=m.group(0), target_raw=m.group(0),
            paragraph_id=paragraph_id, span_start=m.start(), span_end=m.end(),
            quoted_context=context_window(text, m.start(), m.end()), confidence=0.97,
            detail={"ecli": m.group(0)}))
    return relations


def _court_id_fixups(name: str, court_id: str) -> str:
    folded = name.lower()
    if folded.startswith("gerichtshof") or folded == "eugh" or "europäischen union" in folded:
        return "eugh"
    if folded == "egmr" or "menschenrechte" in folded:
        return "egmr"
    return court_id


def _quote_before(text: str, court_start: int) -> str | None:
    """A quotation that closes right before '(' + citation: '„…“ (BVerwG, …)'."""
    head = text[max(0, court_start - 12):court_start]
    m = re.search("[" + re.escape(CLOSE_QUOTES) + r"]\s*\(\s*(?:so\s+)?$", head)
    if not m:
        return None
    close_at = court_start - len(head) + m.start()
    open_at = max(text.rfind(q, max(0, close_at - 700), close_at) for q in OPEN_QUOTES)
    if open_at < 0 or close_at - open_at < 15:
        return None
    return " ".join(text[open_at:close_at + 1].split())


def extract_all(text: str, paragraph_id: str | None, *, own_court_id: str | None,
                own_case_keys: set[str] = frozenset(), own_land: str | None = None) -> list[Relation]:
    return extract_norms(text, paragraph_id, land=own_land) + extract_decisions(
        text, paragraph_id, own_court_id=own_court_id, own_case_keys=own_case_keys, own_land=own_land)
