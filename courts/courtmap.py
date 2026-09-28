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
  basis TEXT NOT NULL CHECK(basis IN ('ecli_code','name_active','name_dissolved','ambiguous','unmapped')),
  candidates TEXT NOT NULL DEFAULT '[]',
  snapshot_id TEXT
);
"""


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


def store_mapping(conn: sqlite3.Connection, payload: dict) -> dict:
    records, snapshot = records_from_payload(payload)
    if not records:
        raise ValueError("Amtsgraph court mapping without records")
    mapping = build_map(records)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM amtsgraph_courts")
    conn.executemany("""INSERT INTO amtsgraph_courts VALUES(?,?,?,?,?,?,?,?,?,?,?,
                          strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))""",
                     [(r["court_id"], r.get("official_name") or "", r.get("xjustiz_id"), r.get("ecli_court_code"),
                       r.get("kind"), r.get("land_code"), r.get("status"), r.get("unit_type"), r.get("parent_court_id"),
                       json.dumps(r.get("aliases") or [], ensure_ascii=False), snapshot) for r in records])
    conn.execute("DELETE FROM court_map")
    conn.executemany("INSERT INTO court_map VALUES(?,?,?,?,?)",
                     [(slug, m["court_id"], m["basis"], json.dumps(m["candidates"]), snapshot)
                      for slug, m in sorted(mapping.items())])
    # Corpus slugs without any Amtsgraph record stay explicit.
    conn.execute("""INSERT OR IGNORE INTO court_map(court_slug, court_id, basis, candidates, snapshot_id)
                    SELECT DISTINCT court_slug, NULL, 'unmapped', '[]', ? FROM decisions""", (snapshot,))
    conn.execute("COMMIT")
    return apply_to_decisions(conn) | {"records": len(records), "slugs_mapped": sum(1 for m in mapping.values()
                                                                                    if m["court_id"]),
                                       "snapshot_id": snapshot}


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
