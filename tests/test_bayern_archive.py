"""Bavarian acts from archived official copies (pipeline/fetch_bayern_archive.py,
tools/by_archive_history.py): integrity gate, page rendering, dated versions."""
from __future__ import annotations

import base64
import hashlib
import io
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import fetch_bayern_archive as archive  # noqa: E402
from api.history_identity import normalize_text_for_identity  # noqa: E402
from tools import by_archive_history  # noqa: E402

XML = """<?xml version="1.0" encoding="utf-8"?><byrecht-norm builddate="23.05.2023_12:05"><kopf>
<angaben.versunabh><ausfertigung><ausfertigungsdatum>2018-05-15</ausfertigungsdatum></ausfertigung>
</angaben.versunabh><angaben.versabh><inkraft>2018-05-25</inkraft><fassung><fundstelle.GVBl>
<jahr>2018</jahr><seite>S=230</seite></fundstelle.GVBl></fassung>
<gliederungsNr.BayRS>BayRS 204-1-I</gliederungsNr.BayRS>
<titelangaben>Bayerisches Datenschutzgesetz (<amtlicheAbk>BayDSG</amtlicheAbk>)</titelangaben>
<amtlicheAbk>BayDSG</amtlicheAbk></angaben.versabh></kopf><rumpf>
<einzelnorm><para.nr>Art. 12</para.nr><para.titel>Datenschutzbeauftragte</para.titel>
<jurAbsatz><absatz.nr>(1)</absatz.nr><absatz.text><p><satz.nr>1</satz.nr>Beauftragte erhalten
insbesondere<dl><dt>1.</dt><dd>Zugang zum Verzeichnis und</dd><dt>2.</dt><dd>Gelegenheit zur
Stellungnahme.</dd></dl></p></absatz.text></jurAbsatz></einzelnorm>
<einzelnorm><para.nr>Art. 20</para.nr><para.titel>Auskunft</para.titel>
<jurAbsatz><absatz.text><p>Alte Fassung.</p></absatz.text></jurAbsatz></einzelnorm>
</rumpf></byrecht-norm>"""
PAGE_TEXT = ("(1) 1Beauftragte erhalten insbesondere 1. Zugang zum Verzeichnis nach "
             "Art. 30 DSGVO und 2. Gelegenheit zur Stellungnahme.\n(2) 1Neu. 2Auch neu.")
FIXTURES = Path(__file__).parent / "fixtures" / "bavarian_history"


def _zip(xml: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("bayportalnorm/BayDSG.xml", xml)
    return buffer.getvalue()


def _digest(payload):
    return base64.b32encode(hashlib.sha1(payload).digest()).decode()


def test_archived_page_is_read_with_the_workstation_parser(tmp_path, monkeypatch):
    import gzip
    monkeypatch.setattr(archive, "PAGE_CACHE", tmp_path)
    raw = (FIXTURES / "agvwgo-14-20160212232903.html").read_bytes()
    (tmp_path / "BayAGVwGO-14-20160212232903.html.gz").write_bytes(gzip.compress(raw))
    page = archive.fetch_page("BayAGVwGO-14", "20160212232903", offline=True)
    assert page["valid_from"] == "2015-11-01"            # the page's own "in Kraft ab"
    assert page["text"].startswith("(1) Soweit nicht anderes bestimmt wird")
    assert "\n(2) Unberührt bleiben" in page["text"]
    assert page["archive_url"].startswith("https://web.archive.org/web/20160212232903id_/")


def test_corrupt_archived_zip_is_rejected_not_repaired(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "CACHE", tmp_path)
    good = _zip(XML)
    (tmp_path / "BayDSG-20230523085904.zip").write_bytes(good)
    ok = archive.fetch_zip("BayDSG", {"timestamp": "20230523085904", "digest": _digest(good)},
                           offline=True)
    assert ok["status"] == "ok" and ok["ausfertigung"] == "2018-05-15"
    assert ok["gvbl"] == "GVBl. 2018 S. 230" and ok["bayrs_nr"] == "204-1-I"
    broken = bytearray(good)
    broken[60] ^= 0xFF                       # inside the deflated member: CRC fails
    (tmp_path / "BayDSG-20260107083957.zip").write_bytes(bytes(broken))
    bad = archive.fetch_zip("BayDSG", {"timestamp": "20260107083957", "digest": _digest(broken)},
                            offline=True)
    assert bad["status"] == "rejected" and "norms" not in bad


def test_zip_state_plus_newer_dated_page_gives_two_versions(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "CACHE", tmp_path)
    good = _zip(XML)
    (tmp_path / "BayDSG-20230523085904.zip").write_bytes(good)
    capture = archive.fetch_zip("BayDSG", {"timestamp": "20230523085904", "digest": _digest(good)},
                                offline=True)
    valid, text = "2025-08-01", PAGE_TEXT
    row = {"key": "BayDSG", "jurabk": "BayDSG", "bayrs_nr": "204-1-I", "title": "BayDSG",
           "inkraft": "2018-05-25", "ausfertigung": "2018-05-15", "gvbl": "GVBl. 2018 S. 230",
           "permalink": "https://www.gesetze-bayern.de/Content/Document/BayDSG",
           "zip_captures": [capture, {"timestamp": "20260107083957", "status": "rejected",
                                      "archive_url": "https://web.archive.org/x", "reason": "CRC"}],
           "pages": [{"doc": "BayDSG-12", "enbez": "Art. 12", "number": "12",
                      "timestamp": "20250806155146", "valid_from": valid, "text": text,
                      "archive_url": "https://web.archive.org/y"}],
           "unincorporated_gvbl_events": [], "fetched_at": "2026-09-29T00:00:00+00:00"}
    detail = by_archive_history.build(row)
    assert detail["id"] == "by_baydsg" and detail["juris"] == "DE-BY"
    art12 = [v for v in detail["history"]["version"] if v["canonical_id"] == "de/by/baydsg/art/12"]
    assert [v["temporal"]["valid_from"] for v in art12] == [None, "2025-08-01"]
    # the page dates the act's consolidated version: in force at the latest then
    assert art12[1]["temporal"]["valid_from_status"] == "official_archived_in_force_at_latest"
    assert art12[0]["temporal"]["valid_until_exclusive"] is None     # the change may be earlier
    head = {n["enbez"]: n["text"] for n in detail["norms"]}
    assert normalize_text_for_identity(head["Art. 12"]) == normalize_text_for_identity(text)
    assert head["Art. 20"] == "Alte Fassung."           # no newer page: the export text
    kinds = {f["kind"] for f in detail["history"]["finding"]}
    assert "archived_capture_rejected" in kinds
    change = next(c for c in detail["history"]["change"] if c.get("norm_id") == "de/by/baydsg/art/12")
    assert change["dates"]["in_force"]["date"] is None
    assert change["dates"]["in_force"]["at_the_latest"] == "2025-08-01"
    assert change["dates"]["adopted"]["date"] is None     # no Änderungsverlauf entry: not guessed


def test_act_title_drops_date_and_fassung_tail():
    assert by_archive_history.act_title(
        "Bayerisches Datenschutzgesetz ( BayDSG ) Vom 15. Mai 2018 (GVBl. S. 230) BayRS 204-1-I",
        "BayDSG") == "Bayerisches Datenschutzgesetz (BayDSG)"
    assert by_archive_history.act_title(
        "Geschäftsordnung für den Bayerischen Landtag (BayLTGeschO)in der Fassung der "
        "Bekanntmachung vom 14. August 2009 (GVBl. S. 420)", "BayLTGeschO") == \
        "Geschäftsordnung für den Bayerischen Landtag (BayLTGeschO)"


def test_archived_and_daily_text_keep_their_urls_and_observation_dates(tmp_path):
    import json
    a = {"key": "BayProbe", "jurabk": "Probe", "title": "Gesetz (Probe)", "inkraft": "2024-05-01",
         "ausfertigung": "2018-01-12", "permalink": "https://www.gesetze-bayern.de/Content/Document/BayProbe"}
    original = "https://www.gesetze-bayern.de/Content/Zip/BayProbe"
    url = "https://web.archive.org/web/20240507080000id_/" + original
    old = {"enbez": "Art. 1", "titel": "Geltung", "text": "Erste Fassung."}
    capture = {**a, "timestamp": "20240507080000", "original_url": original, "archive_url": url,
               "sha256": "1"*64, "status": "ok", "norms": [old], "versions": []}
    row = {**a, "zip_captures": [capture], "pages": []}
    for day, digest, wording in [("2026-11-18", "2"*64, "Neue Fassung."),
                                  ("2026-11-19", "3"*64, "Dritte Fassung.")]:
        folder = tmp_path/day
        folder.mkdir()
        act = {**a, "norm_count": 1, "source_observed_on": day, "source_sha256": digest,
               "source_url": original, "builddate": day}
        (folder/"acts.jsonl").write_text(json.dumps(act))
        (folder/"norms.jsonl").write_text(json.dumps({**old, "key": a["key"], "text": wording}))
    daily = by_archive_history.daily_editions(tmp_path, {"BayProbe"})
    detail = by_archive_history.build(row, daily=daily["BayProbe"])
    versions = detail["history"]["version"]
    assert [v["text"] for v in versions] == ["Erste Fassung.", "Neue Fassung.", "Dritte Fassung."]
    assert [v["temporal"]["observed_from"] for v in versions] == ["2024-05-07", "2026-11-18", "2026-11-19"]
    assert all(v["temporal"]["valid_from"] is None for v in versions)
    assert versions[0]["attestations"][0]["samples"][0]["archive_url"] == url
    assert versions[-1]["attestations"][0]["samples"][0]["sha256"] == "3"*64
    assert detail["dates"]["in_force"] is None
    assert detail["norms"][0]["text"] == "Dritte Fassung."


def test_partial_refresh_keeps_other_acts_pages_and_verified_zip():
    capture = {"timestamp": "20220101010101", "status": "ok", "sha256": "a"*64}
    old = {"key": "Old", "pages": [{"doc": "Old-1", "timestamp": "20230102010101"}],
           "zip_captures": [capture]}
    untouched = {"key": "Other", "pages": [], "zip_captures": []}
    update = {"key": "Old", "pages": [],
              "zip_captures": [{"timestamp": capture["timestamp"], "status": "rejected"}]}
    merged = archive.retain_observations([old, untouched], [update])
    assert merged[0]["pages"] == old["pages"]
    assert merged[0]["zip_captures"] == [capture]
    assert merged[1] == untouched
