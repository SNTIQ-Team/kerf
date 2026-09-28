"""Corpus court slugs -> Amtsgraph court ids (``court:de:<XJustiz-ID>``, ``court:eu:<ECLI code>``).

Source: Amtsgraph ``/v1/courts/mapping`` (XRepository GDS.Gerichte: name,
XJustiz-ID, ECLI court code, aliases, status).  Each Amtsgraph record's name
and aliases are run through the canonical ``courts.courtids.resolve`` to get
the corpus slug, so both sides use one court-identity implementation.
Priority: ECLI court code > active record > dissolved record.  Several
candidates at the same priority give ``ambiguous`` (no id is guessed).

decisions.court_id holds the Amtsgraph id when mapped (else the slug);
decisions.court_slug keeps the corpus slug (alias; used in synthetic decision
ids ``court:<slug>/<date>/<Az>`` and for citation linking).
"""
from __future__ import annotations

import json
import re
import sqlite3

from .courtids import resolve

MAPPING_URLS = ("https://api.sntiq.com/v1/courts/mapping", "https://api.sntiq.com/amtsgraph/courts/mapping")
AGS_LAND = {"01": "sh", "02": "hh", "03": "ni", "04": "hb", "05": "nw", "06": "he", "07": "rp", "08": "bw",
            "09": "by", "10": "sl", "11": "be", "12": "bb", "13": "mv", "14": "sn", "15": "st", "16": "th"}
ECLI_SLUGS = {"bgh": "bgh", "bverwg": "bverwg", "bsg": "bsg", "bag": "bag", "bfh": "bfh", "bpatg": "bpatg",
              "bverfg": "bverfg", "c": "eugh", "t": "eug"}
NOT_A_BENCH = re.compile(r"Zweigstelle|Au(?:ß|ss)enstelle|Außenkammer|auswärtige|Präsident|Geschäftsstelle|"
                         r"Grundbuchamt|Registergericht|Nachlassgericht|Mahngericht|Insolvenzgericht|Vollstreckung|"
                         r"Zentral|Service", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS amtsgraph_courts(
  court_id TEXT PRIMARY KEY, official_name TEXT NOT NULL, xjustiz_id TEXT, ecli_court_code TEXT, kind TEXT,
  land_code TEXT, status TEXT, unit_type TEXT, parent_court_id TEXT, aliases TEXT NOT NULL DEFAULT '[]',
  snapshot_id TEXT, fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS court_map(
  court_slug TEXT PRIMARY KEY,
  court_id TEXT,
  basis TEXT NOT NULL CHECK(basis IN ('ecli_code','name_active','name_dissolved','seat_in_land','ambiguous',
                                      'unmapped')),
  candidates TEXT NOT NULL DEFAULT '[]',
  snapshot_id TEXT,
  ecli_court_code TEXT,
  ecli_court_codes TEXT NOT NULL DEFAULT '[]',
  ecli_basis TEXT,
  ecli_evidence TEXT NOT NULL DEFAULT '{}'
);
"""
# Added 2026-09-28 (additive): ECLI court codes observed in the corpus (derive_ecli_codes).
ECLI_COLUMNS = (("ecli_court_code", "TEXT"), ("ecli_court_codes", "TEXT NOT NULL DEFAULT '[]'"),
                ("ecli_basis", "TEXT"), ("ecli_evidence", "TEXT NOT NULL DEFAULT '{}'"))


def records_from_payload(payload: dict) -> tuple[list[dict], str | None]:
    records = next((v for v in payload.values() if isinstance(v, list) and v and isinstance(v[0], dict)
                    and "court_id" in v[0]), [])
    return records, payload.get("snapshot_id")


def slugs_for(record: dict) -> tuple[set[str], set[str]]:
    """(slugs from the official name and ECLI code, additional slugs from aliases)."""
    land = AGS_LAND.get(str(record.get("land_code") or ""))

    def resolved(names):
        out = set()
        for name in names:
            if name and not NOT_A_BENCH.search(name):
                info = resolve(name, default_land=land)
                if info.court_type != "unknown":
                    out.add(info.court_id)
        return out
    primary = resolved([record.get("official_name") or ""])
    code = (record.get("ecli_court_code") or "").lower()
    if code in ECLI_SLUGS:
        primary.add(ECLI_SLUGS[code])
    return primary, resolved([a.get("alias") or "" for a in record.get("aliases", [])]) - primary


def build_map(records: list[dict]) -> dict[str, dict]:
    by_slug: dict[str, list[dict]] = {}
    by_alias: dict[str, list[dict]] = {}
    for record in records:
        if record.get("unit_type") not in (None, "court") or NOT_A_BENCH.search(record.get("official_name") or ""):
            continue
        primary, aliases = slugs_for(record)
        for slug in primary:
            by_slug.setdefault(slug, []).append(record)
        for slug in aliases:
            by_alias.setdefault(slug, []).append(record)
    mapping: dict[str, dict] = {}
    for slug in set(by_slug) | set(by_alias):
        candidates = by_slug.get(slug) or by_alias.get(slug, [])   # official names beat aliases
        ecli = [c for c in candidates if (c.get("ecli_court_code") or "").lower() in ECLI_SLUGS
                and ECLI_SLUGS[(c.get("ecli_court_code") or "").lower()] == slug]
        active = [c for c in candidates if c.get("status") == "active"]
        for basis, pool in (("ecli_code", ecli), ("name_active", active), ("name_dissolved", candidates)):
            unique = {c["court_id"]: c for c in pool}
            if len(unique) == 1:
                mapping[slug] = {"court_id": next(iter(unique)), "basis": basis, "candidates": []}
                break
            if len(unique) > 1:
                mapping[slug] = {"court_id": None, "basis": "ambiguous", "candidates": sorted(unique)}
                break
    return mapping


TYPE_KIND = {"vg": "verwaltungsgericht", "ovg": "oberverwaltungsgericht", "vgh": "oberverwaltungsgericht",
             "sg": "sozialgericht", "lsg": "landessozialgericht", "ag": "amtsgericht", "lg": "landgericht",
             "olg": "oberlandesgericht", "arbg": "arbeitsgericht", "lag": "landesarbeitsgericht", "fg": "finanzgericht",
             "verfgh": "verfassungsgericht", "oblg": "oberstes_landesgericht", "bg": "berufsgericht",
             "dg": "dienstgericht"}


def seat_in_land(slug: str, court_type: str | None, land: str | None, records: list[dict]) -> str | None:
    """Fallback for seat spellings ("Neustadt (Weinstraße)" vs "Neustadt an der Weinstraße", "VG des
    Saarlandes" vs "VG Saarlouis"): same court type and Land, and either the only such court in the Land or
    the same first seat word.  Anything else stays unmapped."""
    kind = TYPE_KIND.get(court_type or "")
    ags = {v: k for k, v in AGS_LAND.items()}.get(land or "")
    if not kind or not ags:
        return None
    pool = [r for r in records if r.get("kind") == kind and r.get("land_code") == ags and r.get("status") == "active"
            and r.get("unit_type") in (None, "court") and not NOT_A_BENCH.search(r.get("official_name") or "")]
    if len(pool) == 1:
        return pool[0]["court_id"]
    seat = slug.split("-", 1)[1].split("-")[0] if "-" in slug else ""
    hits = [r for r in pool if seat and resolve(r.get("official_name") or "", default_land=land).court_id
            .split("-", 1)[-1].split("-")[0] == seat]
    return hits[0]["court_id"] if len(hits) == 1 else None


def store_mapping(conn: sqlite3.Connection, payload: dict) -> dict:
    records, snapshot = records_from_payload(payload)
    if not records:
        raise ValueError("Amtsgraph court mapping without records")
    mapping = build_map(records)
    for slug, court_type, land in conn.execute(
            "SELECT court_slug, court_type, land FROM decisions GROUP BY court_slug").fetchall():
        if slug not in mapping:
            found = seat_in_land(slug, court_type, land, records)
            if found:
                mapping[slug] = {"court_id": found, "basis": "seat_in_land", "candidates": []}
    conn.execute("DROP TABLE IF EXISTS court_map")   # derived table: rebuilt with the current CHECK list
    conn.executescript(SCHEMA)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM amtsgraph_courts")
    conn.executemany("""INSERT INTO amtsgraph_courts VALUES(?,?,?,?,?,?,?,?,?,?,?,
                          strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))""",
                     [(r["court_id"], r.get("official_name") or "", r.get("xjustiz_id"), r.get("ecli_court_code"),
                       r.get("kind"), r.get("land_code"), r.get("status"), r.get("unit_type"), r.get("parent_court_id"),
                       json.dumps(r.get("aliases") or [], ensure_ascii=False), snapshot) for r in records])
    conn.execute("DELETE FROM court_map")
    conn.executemany("INSERT INTO court_map(court_slug, court_id, basis, candidates, snapshot_id) VALUES(?,?,?,?,?)",
                     [(slug, m["court_id"], m["basis"], json.dumps(m["candidates"]), snapshot)
                      for slug, m in sorted(mapping.items())])
    # Corpus slugs without any Amtsgraph record stay explicit.
    conn.execute("""INSERT OR IGNORE INTO court_map(court_slug, court_id, basis, candidates, snapshot_id)
                    SELECT DISTINCT court_slug, NULL, 'unmapped', '[]', ? FROM decisions""", (snapshot,))
    conn.execute("COMMIT")
    applied = apply_to_decisions(conn)
    codes = derive_ecli_codes(conn)
    return applied | {"records": len(records), "slugs_mapped": sum(1 for m in mapping.values() if m["court_id"]),
                      "snapshot_id": snapshot, "ecli_codes_assigned": codes["codes_assigned"]}


LAND_AGS = {v: k for k, v in AGS_LAND.items()}


def canonical_court_id(conn: sqlite3.Connection, slug: str, land: str | None = None) -> str:
    """Amtsgraph id for a corpus slug; an ambiguous name is settled by the decision's Land, else the slug."""
    try:
        row = conn.execute("SELECT court_id, basis, candidates FROM court_map WHERE court_slug = ?", (slug,)).fetchone()
    except sqlite3.OperationalError:
        return slug
    if row is None:
        return slug
    if row[0]:
        return row[0]
    if row[1] == "ambiguous" and land in LAND_AGS:
        ids = json.loads(row[2])
        hits = [r[0] for r in conn.execute(
            f"SELECT court_id FROM amtsgraph_courts WHERE land_code = ? AND court_id IN ({','.join('?' * len(ids))})",
            (LAND_AGS[land], *ids))]
        if len(hits) == 1:
            return hits[0]
    return slug


def apply_to_decisions(conn: sqlite3.Connection) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    updated = 0
    for decision_id, slug, land, current in conn.execute(
            "SELECT id, court_slug, land, court_id FROM decisions").fetchall():
        canonical = canonical_court_id(conn, slug, land)
        if canonical != current:
            conn.execute("UPDATE decisions SET court_id = ? WHERE id = ?", (canonical, decision_id))
            updated += 1
    conn.execute("COMMIT")
    mapped = conn.execute("SELECT count(*) FROM decisions WHERE court_id LIKE 'court:%'").fetchone()[0]
    total = conn.execute("SELECT count(*) FROM decisions").fetchone()[0]
    return {"decisions_updated": updated, "decisions_mapped": mapped, "decisions_total": total}


def fetch_payload(http) -> dict:
    last = None
    for url in MAPPING_URLS:
        try:
            response = http.get(url, timeout=120)
            return json.loads(response.body)
        except Exception as exc:  # noqa: BLE001 - try the alternate URL, then report
            last = exc
    raise RuntimeError(f"Amtsgraph court mapping unavailable: {last}")


# --- ECLI court codes observed in the corpus ------------------------------------------------------------
#
# Amtsgraph knows the ECLI court codes of the federal courts only.  Land courts' codes (BAYVGH, VGMUENC, OVGNRW
# …) are taken from ECLIs *observed* in the corpus, never guessed from a name:
#
# * ``decision_ecli`` - the published ECLI of a corpus decision of that court (NRW, the juris Länder portals);
# * ``citation`` - an ECLI cited in a court's reasons right after a citation of a named court whose date and
#   Aktenzeichen are the ones the ECLI encodes ("VGH München, Urteil vom 11. Mai 1984 - 25 B 82 A.2323
#   [ECLI:DE:BAYVGH:1984:0511.25B82A.2323.0A]"): date = year + MMDD of the ordinal, Az = its docket part.
#
# Evidence is grouped by court identity (the Amtsgraph id from court_map where mapped, else the slug), so a
# court's chamber slugs count for the court.  A code is assigned when one identity holds at least
# ECLI_DOMINANCE of its evidence; the rest is kept as ``conflicts``.  Otherwise it stays unassigned
# (``ambiguous`` in the report).  Every slug of the assigned identity carries the code.

ECLI_DOMINANCE = 0.9
CHAMBER_REST = re.compile(r"kammer|senat|einzelrichter|schwurgericht|berufsgericht|^\d+[a-z]?$")
LAND_ORDINAL = re.compile(r"^(?P<mm>\d{2})(?P<dd>\d{2})\.(?P<az>.+)\.[0-9A-Za-z]{1,3}$")


def _alnum(value: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (value or "").upper())


def ecli_parts(ecli: str) -> dict | None:
    """ECLI:DE:<code>:<year>:<ordinal> -> code, year, and for the Land format <MMDD>.<Az>.<suffix> date and
    docket key (alphanumerics of the Az part)."""
    parts = (ecli or "").strip().split(":")
    if len(parts) != 5 or parts[0].upper() != "ECLI" or parts[1].upper() != "DE" or not parts[3].isdigit():
        return None
    out = {"code": parts[2], "year": parts[3], "ordinal": parts[4], "date": None, "docket": None}
    m = LAND_ORDINAL.match(parts[4])
    if m and 1 <= int(m.group("mm")) <= 12 and 1 <= int(m.group("dd")) <= 31:
        out["date"] = f"{parts[3]}-{m.group('mm')}-{m.group('dd')}"
        out["docket"] = _alnum(m.group("az"))
    return out


def ecli_observations(conn: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    """(code, court slug, kind, ECLI) for every observation (see above)."""
    from .citations import extract_decisions
    out = []
    for ecli, slug in conn.execute("SELECT ecli, court_slug FROM decisions WHERE ecli GLOB 'ECLI:DE:*'"):
        parts = ecli_parts(ecli)
        if parts and slug:
            out.append((parts["code"], slug, "decision_ecli", ecli))
    rows = conn.execute("""SELECT r.decision_id, r.paragraph_ordinal, r.target_id, r.span_start, d.land
                           FROM relations r JOIN decisions d ON d.id = r.decision_id
                           WHERE r.target_id GLOB 'ECLI:DE:*' AND r.target_kind = 'decision'
                             AND r.span_start IS NOT NULL AND r.paragraph_ordinal IS NOT NULL""").fetchall()
    for decision_id, ordinal, ecli, start, land in rows:
        parts = ecli_parts(ecli)
        if not parts or not parts["date"]:
            continue
        row = conn.execute("SELECT text FROM paragraphs WHERE decision_id = ? AND ordinal = ?",
                           (decision_id, ordinal)).fetchone()
        if row is None:
            continue
        prefix = row[0][:start].rstrip(" \t\n[(")
        near = [r for r in extract_decisions(prefix, None, own_court_id=None, own_land=land)
                if r.detail.get("court_id") and r.span_end is not None
                and not prefix[r.span_end:].strip(" \t\n[(–-,;")]
        if not near:
            continue
        cited = max(near, key=lambda r: r.span_end)
        if cited.detail.get("date") == parts["date"] and _alnum(cited.detail.get("case_key")) == parts["docket"]:
            out.append((parts["code"], cited.detail["court_id"], "citation", ecli))
    return out


def ensure_ecli_columns(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    present = {r[1] for r in conn.execute("PRAGMA table_info(court_map)")}
    for name, decl in ECLI_COLUMNS:
        if name not in present:
            conn.execute(f"ALTER TABLE court_map ADD COLUMN {name} {decl}")
    conn.commit()


def derive_ecli_codes(conn: sqlite3.Connection, *, store: bool = True) -> dict:
    """Assign observed ECLI court codes to court_map rows; return the report (see above)."""
    observations = ecli_observations(conn)
    try:
        mapped = {slug: cid for slug, cid in conn.execute("SELECT court_slug, court_id FROM court_map")}
    except sqlite3.OperationalError:
        mapped = {}

    def identity(slug: str) -> str:
        if mapped.get(slug):
            return mapped[slug]
        # A chamber slug ("ag-giessen-einzelrichter", "olg-berlin-senat-familiensachen", "vg-kassel-3") is the
        # court's: the longest mapped prefix whose remainder names a chamber, senate or single judge.
        words = slug.split("-")
        for cut in range(len(words) - 1, 1, -1):
            base, rest = "-".join(words[:cut]), "-".join(words[cut:])
            if mapped.get(base) and CHAMBER_REST.search(rest):
                return mapped[base]
        return f"slug:{slug}"
    spellings: dict[str, dict[str, int]] = {}
    by_code: dict[str, dict[str, dict]] = {}
    for code, slug, kind, ecli in observations:
        key = code.upper()
        spellings.setdefault(key, {}).setdefault(code, 0)
        spellings[key][code] += 1
        group = by_code.setdefault(key, {}).setdefault(identity(slug), {"decision_ecli": 0, "citation": 0,
                                                                        "slugs": set(), "samples": []})
        group[kind] += 1
        group["slugs"].add(slug)
        if len(group["samples"]) < 3:
            group["samples"].append(ecli)
    assigned: dict[str, dict] = {}
    ambiguous: dict[str, dict] = {}
    for key, groups in by_code.items():
        code = max(spellings[key].items(), key=lambda kv: (kv[1], kv[0]))[0]
        weight = {ident: g["decision_ecli"] + g["citation"] for ident, g in groups.items()}
        total = sum(weight.values())
        top = max(weight, key=lambda ident: (weight[ident], ident))
        described = {ident: {"decision_ecli": g["decision_ecli"], "citation": g["citation"],
                             "slugs": sorted(g["slugs"])} for ident, g in groups.items()}
        if weight[top] / total >= ECLI_DOMINANCE:
            g = groups[top]
            assigned[code] = {"identity": top, "basis": "decision_ecli" if g["decision_ecli"] else "citation",
                              "decision_ecli": g["decision_ecli"], "citation": g["citation"],
                              "share": round(weight[top] / total, 3), "samples": g["samples"],
                              "evidence_slugs": sorted(g["slugs"]),
                              "conflicts": {i: d for i, d in described.items() if i != top}}
        else:
            ambiguous[code] = described
    per_slug: dict[str, dict[str, dict]] = {}
    slugs_of: dict[str, set[str]] = {}
    for slug, cid in mapped.items():
        slugs_of.setdefault(identity(slug), set()).add(slug)
    for code, a in assigned.items():
        for slug in slugs_of.get(a["identity"], set()) | set(a["evidence_slugs"]):
            if identity(slug) == a["identity"]:
                per_slug.setdefault(slug, {})[code] = a
    if store:
        ensure_ecli_columns(conn)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE court_map SET ecli_court_code = NULL, ecli_court_codes = '[]', ecli_basis = NULL, "
                     "ecli_evidence = '{}'")
        for slug, codes in sorted(per_slug.items()):
            ranked = sorted(codes, key=lambda c: (-(codes[c]["decision_ecli"] + codes[c]["citation"]), c))
            primary = codes[ranked[0]]
            evidence = {c: {k: codes[c][k] for k in ("basis", "decision_ecli", "citation", "share", "samples",
                                                     "conflicts")} for c in ranked}
            conn.execute("INSERT OR IGNORE INTO court_map(court_slug, court_id, basis, candidates) "
                         "VALUES(?, NULL, 'unmapped', '[]')", (slug,))
            conn.execute("UPDATE court_map SET ecli_court_code = ?, ecli_court_codes = ?, ecli_basis = ?, "
                         "ecli_evidence = ? WHERE court_slug = ?",
                         (ranked[0], json.dumps(ranked), primary["basis"], json.dumps(evidence, ensure_ascii=False),
                          slug))
        conn.execute("COMMIT")
    try:
        amtsgraph = {(r[0] or "").upper() for r in conn.execute(
            "SELECT ecli_court_code FROM amtsgraph_courts WHERE ecli_court_code IS NOT NULL")}
    except sqlite3.OperationalError:
        amtsgraph = set()
    return {"observations": len(observations),
            "observations_by_kind": {k: sum(1 for o in observations if o[2] == k) for k in ("decision_ecli", "citation")},
            "codes_observed": len(by_code), "codes_assigned": len(assigned), "codes_ambiguous": len(ambiguous),
            "codes_new_to_amtsgraph": sorted(c for c in assigned if c.upper() not in amtsgraph),
            "slugs_with_code": len(per_slug),
            "assigned": {c: {k: v for k, v in a.items() if k != "evidence_slugs"} for c, a in sorted(assigned.items())},
            "ambiguous": dict(sorted(ambiguous.items()))}


def ecli_court_slugs(conn: sqlite3.Connection, code: str) -> list[str]:
    """Corpus slugs whose court carries ``code`` (observed, see derive_ecli_codes); [] if unknown."""
    try:
        return [r[0] for r in conn.execute(
            "SELECT court_slug FROM court_map WHERE upper(ecli_court_code) = upper(?) OR EXISTS "
            "(SELECT 1 FROM json_each(court_map.ecli_court_codes) WHERE upper(value) = upper(?))", (code, code))]
    except sqlite3.OperationalError:
        return []
