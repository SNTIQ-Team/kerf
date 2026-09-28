"""Corpus court slugs -> Amtsgraph court ids (court:de:<XJustiz-ID>, court:eu:<ECLI code>)."""
import json

import pytest

from courts.courtmap import build_map, canonical_court_id, store_mapping

RECORDS = [
    {"court_id": "court:de:U8572", "official_name": "Bundesverwaltungsgericht", "ecli_court_code": "BVerwG",
     "status": "active", "unit_type": "court", "land_code": None, "aliases": [{"alias": "BVerwG"}]},
    {"court_id": "court:de:F6087", "official_name": "Bundesverwaltungsgericht", "ecli_court_code": None,
     "status": "dissolved", "unit_type": "court", "land_code": None, "aliases": []},
    {"court_id": "court:de:R6306", "official_name": "Landessozialgericht Nordrhein-Westfalen", "status": "active",
     "unit_type": "court", "land_code": "05", "aliases": [{"alias": "LSG NRW"}]},
    {"court_id": "court:de:R6205", "official_name": "Sozialgericht Dortmund", "status": "active",
     "unit_type": "court", "land_code": "05", "aliases": []},
    {"court_id": "court:de:R6802", "official_name": "Sozialgericht Dortmund", "status": "dissolved",
     "unit_type": "court", "land_code": "05", "aliases": []},
    {"court_id": "court:de:D8047", "official_name": "Bayerisches Landessozialgericht Zweigstelle Schweinfurt",
     "status": "active", "unit_type": "court", "land_code": "09", "aliases": []},
    {"court_id": "court:de:D6255", "official_name": "Bayerisches Landessozialgericht", "status": "active",
     "unit_type": "court", "land_code": "09", "aliases": []},
    {"court_id": "court:de:D3304", "official_name": "Amtsgericht Fürth", "status": "active", "unit_type": "court",
     "land_code": "09", "aliases": []},
    {"court_id": "court:de:M1105", "official_name": "Amtsgericht Fürth", "status": "active", "unit_type": "court",
     "land_code": "06", "aliases": []},
    {"court_id": "court:eu:C", "official_name": "Gerichtshof", "ecli_court_code": "C", "status": "active",
     "unit_type": "court", "land_code": None, "aliases": [{"alias": "EuGH"}]},
]


def test_mapping_priorities():
    m = build_map(RECORDS)
    assert m["bverwg"] == {"court_id": "court:de:U8572", "basis": "ecli_code", "candidates": []}
    assert m["lsg-nw"]["court_id"] == "court:de:R6306"
    assert m["sg-dortmund"] == {"court_id": "court:de:R6205", "basis": "name_active", "candidates": []}
    assert m["lsg-by"]["court_id"] == "court:de:D6255"          # the branch office is not a bench of its own
    assert m["eugh"]["court_id"] == "court:eu:C"
    assert m["ag-fuerth"]["basis"] == "ambiguous"                # Fürth (BY) and Fürth (HE): never guessed


def test_store_mapping_updates_decisions_and_keeps_the_slug(fixture):
    from courts.store import connect, init_db
    from courts.sync import sync_source
    from tests.courts.test_store_sync_read import ECLI, FakeHttp, FixtureAdapter, item
    conn = connect()
    init_db(conn)
    sync_source(FixtureAdapter(fixture, [item()]), conn, FakeHttp())
    assert conn.execute("SELECT court_id, court_slug FROM decisions").fetchone()[:] == ("bverwg", "bverwg")
    result = store_mapping(conn, {"snapshot_id": "amtsgraph-test", "records": RECORDS})
    assert result["decisions_mapped"] == 1
    row = conn.execute("SELECT id, court_id, court_slug FROM decisions").fetchone()
    assert tuple(row) == (ECLI, "court:de:U8572", "bverwg")
    assert canonical_court_id(conn, "ag-fuerth", "he") == "court:de:M1105"   # the Land settles a homonym
    assert canonical_court_id(conn, "ag-fuerth", None) == "ag-fuerth"
    from courts.read import open_db, search
    assert search(open_db(), "Mitteilung", court="court:de:U8572")["total"] == \
        search(open_db(), "Mitteilung", court="bverwg")["total"] > 0


def test_ecli_parts_land_and_federal_layouts():
    from courts.courtmap import ecli_parts
    land = ecli_parts("ECLI:DE:BAYVGH:2015:1117.12ZB15.1191.0A")
    assert (land["code"], land["date"], land["docket"]) == ("BAYVGH", "2015-11-17", "12ZB151191")
    federal = ecli_parts("ECLI:DE:BVerwG:2017:170817U3C18.15.0")
    assert federal["code"] == "BVerwG" and federal["date"] is None
    assert ecli_parts("ECLI:EU:C:2019:123") is None and ecli_parts("not an ecli") is None


def test_chamber_slugs_count_for_their_court_and_codes_are_never_guessed():
    import sqlite3
    from courts.courtmap import derive_ecli_codes, ecli_court_slugs
    from courts.model import Block, ParsedDecision, Section
    from courts.store import init_db, store_decision
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    for n, court in enumerate(["Amtsgericht Gießen"] * 8 + ["Amtsgericht Gießen Einzelrichter"] * 2):
        store_decision(conn, ParsedDecision(
            source_id="he", source_doc_id=f"d{n}", source_url=f"https://example.invalid/{n}", court_name=court,
            decision_date="2020-01-02", case_numbers_raw=f"4{n} C 1/20", case_numbers=[f"4{n} C 1/20"],
            decision_type="Urteil", ecli=f"ECLI:DE:AGGIESS:2020:0102.4{n}C1.20.00", land="he",
            sections=[Section("gruende", "court", "fixture", [Block("Text.", "1")])]),
            content_hash=f"h{n}", capture_path="-", media_type="text/html")
    conn.execute("INSERT INTO court_map(court_slug, court_id, basis) VALUES('ag-giessen', 'court:de:M1406', 'name_active')")
    report = derive_ecli_codes(conn)
    assert report["assigned"]["AGGIESS"]["identity"] == "court:de:M1406" and not report["ambiguous"]
    assert set(ecli_court_slugs(conn, "aggiess")) == {"ag-giessen", "ag-giessen-einzelrichter"}
    assert ecli_court_slugs(conn, "VGMUENC") == []
