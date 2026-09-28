"""Feed and Chronik must not misstate dates (live UI audit, findings 1 and 4).

The BGBl rows mirror the real GII Aktualitätendienst snapshot of
2026-09-28: BGBl. 2026 I Nr. 273 carries two acts, both RSS items dated
2026-09-27 (the day the service listed them, not the promulgation date).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import build_web_data as web_data  # noqa: E402

RSS = [
    {"event_id": "event:BGBl_1:2026-273:promulgation", "time": "2026-09-27",
     "eli": "https://www.recht.bund.de/eli/bund/BGBl_1/2026/273",
     "bgbl_citation": "BGBl. 2026 I Nr. 273",
     "title": "Verordnung zur Fortentwicklung der Vorbereitungsdienste für "
              "den gehobenen Dienst im Bundesnachrichtendienst"},
    {"event_id": "event:BGBl_1:2026-273:promulgation", "time": "2026-09-27",
     "eli": "https://www.recht.bund.de/eli/bund/BGBl_1/2026/273",
     "bgbl_citation": "BGBl. 2026 I Nr. 273",
     "title": "Verordnung über die Laufbahn des gehobenen Dienstes"},
    {"event_id": "event:BGBl_1:2026-228:promulgation", "time": "2026-09-27",
     "eli": "https://www.recht.bund.de/eli/bund/BGBl_1/2026/228",
     "bgbl_citation": "BGBl. 2026 I Nr. 228", "title": "Gesetz X"},
]
DOCUMENTS = [{"year": "2026", "issue": "228",
              "publication_date": "2026-07-29"}]


def _load(source, name):
    return {"bgbl_events": RSS, "bgbl_documents": DOCUMENTS}.get(source, [])


def test_bgbl_rows_carry_the_real_date_basis(monkeypatch):
    monkeypatch.setattr(web_data, "load", _load)
    monkeypatch.setattr(web_data, "load_decisions", lambda: [])
    rows = [row for row in web_data.build_feed() if row["source"] == "BGBl"]
    verified = [row for row in rows if row["url"].endswith("/228")]
    assert verified == [{
        "time": "2026-07-29", "juris": "DE", "source": "BGBl",
        "kind": "verkündet", "title": "BGBl. 2026 I Nr. 228 — Gesetz X",
        "url": "https://www.recht.bund.de/eli/bund/BGBl_1/2026/228",
        "badge": None, "time_basis": "official_publication_date"}]
    listed = [row for row in rows if row["url"].endswith("/273")]
    # Two acts in one issue are two rows with distinct titles, dated only as
    # the update service's listing, never as "verkündet".
    assert len(listed) == 2 and len({row["title"] for row in listed}) == 2
    assert {row["kind"] for row in listed} == {"gemeldet"}
    assert {row["time_basis"] for row in listed} == {
        "gii_update_service_item_date"}


def test_exact_duplicate_feed_rows_are_merged(monkeypatch):
    monkeypatch.setattr(web_data, "load",
                        lambda source, name: [RSS[0], RSS[0]]
                        if source == "bgbl_events" else [])
    monkeypatch.setattr(web_data, "load_decisions", lambda: [])
    assert len(web_data.build_feed()) == 1


def test_future_effective_amendments_are_not_labelled_in_force(monkeypatch):
    monkeypatch.setattr(web_data, "load", lambda source, name: [])
    monkeypatch.setattr(web_data, "TODAY", "2026-09-28")
    # Real event shape: GKG 2004 § 69b, BGBl. 2025 I Nr. 349, Art. 25,
    # effective 2036-01-01 per the DIP article commencement clause.
    event = {"id": "fed-bgbl-candidate:d0c35a8dd1fcfc8f8547",
             "act_id": "fed_gkg_2004", "jurabk": "GKG 2004",
             "document_id": "bgbl-1-2025-349", "amending_article": "25",
             "effective_at": "2036-01-01", "publication_date": "2025-12-22",
             "affected_norms": ["§ 69b"]}
    past = {**event, "id": "past", "document_id": "bgbl-1-2025-100",
            "effective_at": "2026-01-01"}
    git = web_data.build_git(historical_events=[event, past])
    rows = {row["event_id"]: row for row in git["commits"]
            if row.get("event_id")}
    future = rows["fed-bgbl-candidate:d0c35a8dd1fcfc8f8547"]
    assert future["msg"].startswith("Amtliche Änderung wird wirksam")
    assert future["future"] is True
    assert rows["past"]["msg"].startswith("Amtliche Änderung wirksam")
    assert rows["past"]["future"] is False
