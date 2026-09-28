"""Historical Bavarian norm versions and change records (real fixtures).

Fixtures under tests/fixtures/bavarian_history/ are verbatim excerpts of
captured bytes: the #content element of archived BAYERN.RECHT pages
(web.archive.org id_ captures), the .tx-gvbl element of a
www.verkuendung-bayern.de GVBl detail page, one page of pdftotext output of a
GVBl issue PDF, an excerpt of an archived /Content/Zip XML, and verbatim rows
of Lexgraph's BAYERN.RECHT snapshot of 2026-09-17.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from tools import bavarian_history as bh

FIX = Path(__file__).resolve().parent / "fixtures" / "bavarian_history"


def _read(name: str) -> bytes:
    return (FIX / name).read_bytes()


# ------------------------------------------------------------------ identity

def test_identity_ignores_whitespace_and_soft_hyphens():
    a = bh.norm_identity("§ 1", "Titel", "(1) 1Satz eins.\n(2) Satz\xa0zwei.")
    b = bh.norm_identity("§ 1", "Titel", "(1)1Satz eins. (2) Sat­z zwei.")
    assert a == b
    assert a != bh.norm_identity("§ 1", "Titel", "(1) 1Satz eins.")


# ------------------------------------------------------------------ archive

def test_archive_roundtrip_verifies_hash(tmp_path):
    digest = bh.put_raw("gvbl", b"<html>x</html>", "html", tmp_path)
    assert bh.get_raw("gvbl", digest, "html", tmp_path) == b"<html>x</html>"
    path = bh.raw_path("gvbl", digest, "html", tmp_path)
    assert path.name == f"{digest}.html.gz" and path.parent.name == digest[:2]
    bh._atomic_write(path, bh._gzip(b"tampered"))
    with pytest.raises(bh.HistoryArchiveError):
        bh.get_raw("gvbl", digest, "html", tmp_path)
    row = bh.append_ledger("gvbl", {"act_id": None, "status": "ok"}, tmp_path)
    assert row["schema"] == "lexgraph-history-capture/1"
    assert bh.read_ledger("gvbl", tmp_path) == [row]
    with pytest.raises(bh.HistoryArchiveError):
        bh.put_raw("other", b"x", "html", tmp_path)


# ------------------------------------------------------- BAYERN.RECHT pages

def test_norm_page_text_matches_xml_flattening_and_act_level_date():
    parsed = bh.parse_bayern_html(_read("dvasyl-12-20240516120303.html"),
                                  "BayDVAsyl", "DVAsyl", "norm")
    assert parsed["warnings"] == []
    fields = parsed["metadata"]["fields"]
    assert fields["Text gilt ab"] == {"raw": "01.12.2023", "date": "2023-12-01"}
    [norm] = parsed["norms"]
    assert norm["enbez"] == "§ 12"
    current = json.loads(_read("dvasyl-norm12-20260917.jsonl").decode("utf-8"))
    # the archived page flattens exactly like today's XML-derived text
    assert norm["text"] == current["text"]
    assert norm["titel"] == current["titel"]


def test_footnote_is_inlined_like_the_xml():
    parsed = bh.parse_bayern_html(_read("agvwgo-14-20260311190206.html"),
                                  "BayAGVwGO", "AGVwGO", "norm")
    [norm] = parsed["norms"]
    assert "in Kraft.*)Diese Vorschrift betrifft" in norm["text"]
    assert "[Amtl. Anm.:]" not in norm["text"]
    assert norm["text"].count("Diese Vorschrift") == 1


def test_2016_page_generation_and_in_kraft_ab_label():
    parsed = bh.parse_bayern_html(_read("agvwgo-14-20160212232903.html"),
                                  "BayAGVwGO", "AGVwGO", "norm")
    assert parsed["metadata"]["fields"]["in Kraft ab"]["date"] == "2015-11-01"
    [norm] = parsed["norms"]
    assert norm["enbez"] == "Art. 14"
    assert norm["text"].startswith("(1) Soweit nicht anderes bestimmt wird")
    assert "\n(2) Unberührt bleiben" in norm["text"]


def test_archived_zip_xml_excerpt():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("bayportalnorm/BayAGVwGO.xml",
                         _read("agvwgo-20190411054044-excerpt.xml"))
    parsed = bh.parse_zip(buf.getvalue(), "BayAGVwGO", "AGVwGO")
    assert parsed["builddate"] == "11.04.2019_00:01"
    assert parsed["inkraft"] == "2015-11-01"
    [norm] = parsed["norms"]
    # old exports: an empty <para.titel/> precedes the real one
    assert norm["titel"] == "(Zu §§ 2, 3 Abs. 1, § 184 VwGO)"
    assert "empty first para.titel" in parsed["warnings"]
    assert norm["text"].startswith("(1) 1Das Oberverwaltungsgericht")
    assert [v["gvbl_citation"] for v in parsed["aenderungsverlauf"]] == [
        "GVBl. S. 408", "GVBl. S. 210"]
    assert parsed["stand"]["entries"][0]["type"] == "Vollzitat"


# ---------------------------------------------------------------- GVBl

def test_gvbl_detail_page_dates_sections_and_commands():
    doc = bh.parse_gvbl_detail(_read("gvbl-2023-616.html"))
    assert doc["gazette"]["reference"] == "GVBl. 2023 S. 616"
    assert (doc["issue"], doc["issue_date"]) == (22, "2023-11-30")
    assert doc["signature"] == {"raw": "München, den 15. November 2023",
                                "date": "2023-11-15"}
    assert doc["title_date"] == "2023-11-15"
    section = bh.section_for(doc, "by_dvasyl", "1")
    assert section["heading"] == "§ 1 Änderung der Asyldurchführungsverordnung"
    commands = bh.extract_commands(section, "§")["commands"]
    assert [(c["nummer"], [t["norm"] for t in c["targets"]]) for c in commands] == [
        ("1", ["§ 12"]), ("2", ["§ 22"]), ("3", ["§ 23"]), ("4", ["§ 29a"])]
    clause = bh.commencement_section(doc)["text"]
    rules = bh.parse_commencement(clause, doc["issue_date"])
    assert bh.in_force_for(rules, "1", "1")["date"] == "2023-12-01"
    # "Abweichend von Satz 1 tritt § 3 mit Wirkung vom 1. März 2020 in Kraft."
    third = bh.in_force_for(rules, "3", None)
    assert third["date"] == "2020-03-01"
    assert third["status"] == "official_commencement_clause"
    assert "mit Wirkung vom 1. März 2020" in third["clause"]


def test_gvbl_issue_pdf_page_text():
    page = _read("gvbl-2007-10-page325.txt").decode("utf-8")
    doc = bh.parse_gvbl_pdf_pages([page], 325, 325, "2007-05-09")
    assert doc["title"] == "Gesetz zur Änderung des Gesetzes zur Ausführung der Sozialgesetze"
    assert doc["signature"]["date"] == "2007-05-09"
    rules = bh.parse_commencement(bh.commencement_section(doc)["text"], None)
    result = bh.in_force_for(rules, "1", None)
    assert result["date"] == "2007-05-01"          # "mit Wirkung vom 1. Mai 2007"
    commands = bh.extract_commands(bh.section_for(doc, "by_agsg", "1"), "Art.")
    assert {t["norm"] for c in commands["commands"] for t in c["targets"]} == {"Art. 95"}
    # a date the page does not print is never accepted
    missing = bh.parse_gvbl_pdf_pages([page], 325, 325, "2007-05-10")
    assert "act heading date not found on its page" in missing["warnings"]


@pytest.mark.parametrize(("clause", "promulgated", "article", "nummer", "expected"), [
    # GVBl 2009 S. 640 (pdftotext), enumerated exceptions
    ("(1) 1Dieses Gesetz tritt am 1. Januar 2010 in Kraft. 2Abweichend von Satz 1 tritt\n"
     "1. § 2 mit Wirkung vom 1. September 2009 und\n2. § 1 Nr. 7 mit Wirkung vom "
     "28. Dezember 2009 in Kraft.", "2009-12-29", "2", None, "2009-09-01"),
    ("(1) 1Dieses Gesetz tritt am 1. Januar 2010 in Kraft. 2Abweichend von Satz 1 tritt\n"
     "1. § 2 mit Wirkung vom 1. September 2009 und\n2. § 1 Nr. 7 mit Wirkung vom "
     "28. Dezember 2009 in Kraft.", "2009-12-29", "1", "7", "2009-12-28"),
    ("(1) 1Dieses Gesetz tritt am 1. Januar 2010 in Kraft. 2Abweichend von Satz 1 tritt\n"
     "1. § 2 mit Wirkung vom 1. September 2009 und\n2. § 1 Nr. 7 mit Wirkung vom "
     "28. Dezember 2009 in Kraft.", "2009-12-29", "1", "3", "2010-01-01"),
    ("Dieses Gesetz tritt am Tag nach seiner Verkündung in Kraft.", "2025-07-15", "1", None,
     "2025-07-16"),
])
def test_commencement_rules(clause, promulgated, article, nummer, expected):
    rules = bh.parse_commencement(clause, promulgated)
    assert bh.in_force_for(rules, article, nummer)["date"] == expected


def test_commencement_without_known_nummer_stays_unknown():
    clause = ("(1) 1Dieses Gesetz tritt am 1. Januar 2010 in Kraft. 2Abweichend von Satz 1 "
              "tritt\n1. § 2 mit Wirkung vom 1. September 2009 und\n2. § 1 Nr. 7 mit Wirkung "
              "vom 28. Dezember 2009 in Kraft.")
    result = bh.in_force_for(bh.parse_commencement(clause, None), "1", None)
    assert result["date"] is None and result["status"] == "unknown"
    assert "Nummer of this change is not known" in result["basis"]


def test_relative_commencement_needs_the_issue_date():
    rules = bh.parse_commencement("Diese Verordnung tritt am Tag nach ihrer Verkündung in Kraft.",
                                  None)
    result = bh.in_force_for(rules, None, None)
    assert result["date"] is None and "promulgation date unknown" in result["basis"]


# ------------------------------------------------------------ version log

@pytest.mark.parametrize(("description", "expected"), [
    ("Art. 117 und 118 Abs. 1 Sätze 2 und 3, Abs. 2 und 3 aufgeh.",
     [("Art. 117", "aufgehoben"), ("Art. 118", "geändert")]),
    ("Art. 112 bis 114 neu gef., 115, 116 aufgeh",
     [("Art. 112", "neugefasst"), ("Art. 113", "neugefasst"), ("Art. 114", "neugefasst"),
      ("Art. 115", "aufgehoben"), ("Art. 116", "aufgehoben")]),
    ("IÜ, Art. 3 Satz 1 geänd., Art. 109a eingef.",
     [("Art. 3", "geändert"), ("Art. 109a", "eingefügt")]),
    ("Teil 2a (Art. 4) eingef.", [("Art. 4", "eingefügt")]),
    ("Art. 1 Abs. 1 Satz 3 angef., Art. 2 Abs. 3 aufgeh.",
     [("Art. 1", "geändert"), ("Art. 2", "geändert")]),
    ("§§ 23, 30 geänd.", [("§ 23", "geändert"), ("§ 30", "geändert")]),
    ("Art. 66b", [("Art. 66b", "unbekannt")]),
    ("mehrfach geänd.", []),
])
def test_ffn_descriptors(description, expected):
    parsed = bh.parse_ffn_descriptor(description, "Art.")
    assert [(t["norm"], t["change_type"]) for t in parsed["targets"]] == expected
    assert parsed["unspecified"] == (description == "mehrfach geänd.")


def _snapshot(tmp_path: Path) -> Path:
    day = tmp_path / "snapshots" / "bayern_recht" / "2026-09-17"
    day.mkdir(parents=True)
    (day / "versions.jsonl").write_bytes(_read("dvasyl-versions-20260917.jsonl"))
    (day / "acts.jsonl").write_bytes(_read("dvasyl-acts-20260917.jsonl"))
    (day / "norms.jsonl").write_bytes(_read("dvasyl-norm12-20260917.jsonl"))
    return day


def test_version_log_joins_aenderungsverlauf_and_ffn(tmp_path, monkeypatch):
    monkeypatch.setenv("LEXGRAPH_SNAPSHOTS", str(tmp_path / "snapshots"))
    _snapshot(tmp_path)
    log = bh.load_version_log("by_dvasyl")
    by_page = {e["page"]: e for e in log}
    entry = by_page[616]
    assert entry["xml_description"] == ("§ 1 V zur Änderung der Asyldurchführungsverordnung "
                                        "und weiterer Rechtsvorschriften vom 15.11.2023")
    assert [f["description"] for f in entry["ffn"]] == ["mehrfach geänd."]
    assert entry["article"]["articles"] == ["1"]
    assert entry["gvbl_candidates"][0]["year"] == 2023
    # the ffn says "Urteil v. 14.04.2021", the aenderungsverlauf "vom 26.10.2021"
    kinds = [f["kind"] for f in by_page[631]["findings"]]
    assert kinds == ["version_log_date_disagreement"]


# ---------------------------------------------------------- end to end

def _capture(root: Path, name: str, document: str, timestamp: str) -> None:
    data = _read(name)
    digest = bh.put_raw("wayback_bayern", data, "html", root)
    original = f"https://www.gesetze-bayern.de/Content/Document/{document}"
    bh.append_ledger("wayback_bayern", {
        "act_id": "by_dvasyl", "kind": "norm", "document": document,
        "original_url": original,
        "archive_url": f"https://web.archive.org/web/{timestamp}id_/{original}",
        "capture_timestamp": timestamp, "captured_at": bh.wayback_captured_at(timestamp),
        "cdx_digest": None, "http_status": 200, "media_type": "text/html", "ext": "html",
        "sha256": digest, "bytes": len(data), "fetched_at": "2026-09-28T00:00:00Z",
        "status": "ok", "error": None}, root)


def test_end_to_end_change_bracketed_by_captures(tmp_path, monkeypatch):
    root = tmp_path / "archive"
    monkeypatch.setenv("LEXGRAPH_SNAPSHOTS", str(tmp_path / "snapshots"))
    _snapshot(tmp_path)
    current = json.loads(_read("dvasyl-norm12-20260917.jsonl").decode("utf-8"))
    acts = tmp_path / "main" / "web" / "data" / "acts"
    acts.mkdir(parents=True)
    (acts / "by_dvasyl.json").write_text(json.dumps(
        {"id": "by_dvasyl", "norms": [current]}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(bh, "MAIN_CHECKOUT", tmp_path / "main")
    _capture(root, "dvasyl-12-20231001095317.html", "BayDVAsyl-12", "20231001095317")
    _capture(root, "dvasyl-12-20240516120303.html", "BayDVAsyl-12", "20240516120303")
    html = _read("gvbl-2023-616.html")
    digest = bh.put_raw("gvbl", html, "html", root)
    bh.append_ledger("gvbl", {
        "act_id": "by_dvasyl", "kind": "gvbl_detail",
        "original_url": "https://www.verkuendung-bayern.de/gvbl/2023-616/",
        "archive_url": None, "capture_timestamp": None, "captured_at": "2026-09-28T00:00:00Z",
        "cdx_digest": None, "http_status": 200, "media_type": "text/html", "ext": "html",
        "sha256": digest, "bytes": len(html), "fetched_at": "2026-09-28T00:00:00Z",
        "status": "ok", "error": None, "gvbl_year": 2023, "gvbl_page": 616}, root)

    result = bh.build_act_history("by_dvasyl", root, as_of="2026-09-28")

    assert result["validation"]["identity_rate"] == 1.0
    versions = [v for v in result["versions"] if v["canonical_id"] == "de/by/dvasyl/para/12"]
    assert len(versions) == 2
    old, new = versions
    assert old["source_role"] == "archived_capture"
    assert new["source_role"] == "official_consolidation"   # also today's text
    assert new["verification"]["cross_source"] == "agree"
    assert old["temporal"]["known_at"] == "2023-10-01"
    assert old["temporal"]["consolidation_dates"] == ["2022-03-01"]
    [change] = [c for c in result["changes"] if c["norm_id"] == "de/by/dvasyl/para/12"]
    assert change["change_type"] == "geändert"
    assert change["amending_act"]["gazette"]["reference"] == "GVBl. 2023 S. 616"
    assert change["amending_act"]["article"] == "1"
    assert change["amending_act"]["nummer"] == "1"
    assert change["dates"]["adopted"]["date"] == "2023-11-15"
    assert change["dates"]["adopted"]["status"] == "official"
    assert change["dates"]["promulgated"]["date"] == "2023-11-30"
    assert change["dates"]["in_force"]["date"] == "2023-12-01"
    assert change["dates"]["in_force"]["status"] == "official_commencement_clause"
    assert change["state"] == "in_force" and change["pending"] is False
    effect = change["text_effect"]
    assert effect["verification"] == "bracketed_by_captures"
    assert (effect["before_version_id"], effect["after_version_id"]) == (
        old["version_id"], new["version_id"])
    assert new["temporal"]["valid_from"] == "2023-12-01"
    assert new["change"]["change_id"] == change["change_id"]
    assert old["temporal"]["valid_until_exclusive"] == "2023-12-01"
    # entries without a captured GVBl document keep unknown dates with a reason
    unknown = [c for c in result["changes"] if c["dates"]["adopted"]["date"] is None]
    assert unknown and all(c["dates"]["adopted"]["basis"] for c in unknown)

    out = tmp_path / "out"
    bh.write_outputs([result], out)
    assert bh.load_changes(out, norm_id="de/by/dvasyl/para/12")[0]["change_id"] == \
        change["change_id"]
    assert len(bh.load_versions(out, act_id="by_dvasyl")) == len(result["versions"])
    assert bh.load_summary(out)["acts"]["by_dvasyl"]["counts"]["versions"] == \
        len(result["versions"])


# ------------------------------------------- omnibus acts, decisions, editions

# verbatim lines of GVBl 2019 S. 98 § 1 (Abs. 294–296); an unbalanced „ is
# prepended to show that quote tracking elsewhere cannot hide the container
GV_2019_98 = [
    "„(1) … unbalanced quoted wording from an earlier Absatz",
    "(294) In § 1 Abs. 1, § 2 Abs. 1 Satz 1 und § 3 der Verordnung über die Führung der "
    "Dienstaufsicht über die Gerichte der Sozialgerichtsbarkeit und über die Festsetzung der "
    "Zahl und die Berufung der Sozialrichter und Landessozialrichter in der in der Bayerischen "
    "Rechtssammlung (BayRS 33-4-A) veröffentlichten bereinigten Fassung, die durch § 1 Nr. 329 "
    "der Verordnung vom 22. Juli 2014 (GVBl. S. 286) geändert worden ist, werden jeweils die "
    "Wörter „Arbeit und Soziales, Familie und Integration“ durch die Wörter „Familie, Arbeit "
    "und Soziales“ ersetzt.",
    "(295) Das Gesetz zur Ausführung der Verwaltungsgerichtsordnung (AGVwGO) in der Fassung der "
    "Bekanntmachung vom 20. Juni 1992 (GVBl. S. 162, BayRS 34-1-I), das zuletzt durch Art. 10b "
    "Abs. 4 des Gesetzes vom 23. Juni 2015 (GVBl. S. 178) geändert worden ist, wird wie folgt "
    "geändert:",
    "1. In Art. 3 Satz 2, Art. 4 und Art. 9 Abs. 1 Satz 2 werden jeweils die Wörter „Bau und "
    "Verkehr“ durch die Wörter „Sport und Integration“ ersetzt.",
    "2. In Art. 17 Satz 2 werden die Wörter „Bau und Verkehr, soweit erforderlich im "
    "Einvernehmen mit dem Staatsministerium der Finanzen, für Landesentwicklung und Heimat“ "
    "durch die Wörter „Sport und Integration, soweit erforderlich im Einvernehmen mit dem "
    "Staatsministerium der Finanzen und für Heimat“ ersetzt.",
    "(296) In § 1 Abs. 1 Satz 1, § 2 Abs. 3 Satz 1 und 4 und § 6 der Verordnung über die "
    "Landesanwaltschaft Bayern (LABV) vom 29. Juli 2008 (GVBl. S. 554, BayRS 34-3-I), die "
    "zuletzt durch § 1 Nr. 331 der Verordnung vom 22. Juli 2014 (GVBl. S. 286) geändert worden "
    "ist, werden jeweils die Wörter „Bau und Verkehr“ durch die Wörter „Sport und Integration“ "
    "ersetzt.",
]


def test_omnibus_absatz_container():
    section = {"number": "1", "prefix": "§", "title": "", "items": GV_2019_98}
    result = bh.extract_commands(section, "Art.", None, "295")
    assert [(c["nummer"], [t["norm"] for t in c["targets"]]) for c in result["commands"]] == [
        ("1", ["Art. 3", "Art. 4", "Art. 9"]), ("2", ["Art. 17"])]
    # "Art. 10b Abs. 4 des Gesetzes vom …" in the intro cites the last change,
    # it is not a target
    assert all(t["norm"] != "Art. 10b" for c in result["commands"] for t in c["targets"])


def test_repeal_list_takes_the_verb_of_the_section():
    # GVBl 2013 S. 174, § 1 "Aufhebung von Vorschriften", Nr. 51 (verbatim)
    section = {"number": "1", "prefix": "§", "title": "Aufhebung von Vorschriften", "items": [
        "Es werden aufgehoben:",
        "51. Art. 117 und 118 Abs. 1 Sätze 2 und 3, Abs. 2 und 3 des Gesetzes zur Ausführung "
        "der Sozialgesetze (AGSG) vom 8. Dezember 2006 (GVBl S. 942, BayRS 86-7-A), zuletzt "
        "geändert durch § 2 des Gesetzes vom 18. Dezember 2012 (GVBl S. 714),",
        "52. Art. 45 Abs. 3 des Gesetzes über die Rechtsverhältnisse der nichtbundeseigenen "
        "Eisenbahnen und der Seilbahnen in Bayern (Bayerisches Eisenbahn- und Seilbahngesetz – "
        "BayESG) in der Fassung der Bekanntmachung vom 9. August 2003 (GVBl S. 598, BayRS "
        "932-1-W), geändert durch Gesetz vom 23. Juli 2010 (GVBl S. 324),"]}
    [command] = bh.extract_commands(section, "Art.", "51", None)["commands"]
    assert [(t["norm"], t["change_type"]) for t in command["targets"]] == [
        ("Art. 117", "aufgehoben"), ("Art. 118", "geändert")]


def test_published_normenkontrolle_decision():
    # GVBl 2021 S. 631 (verbatim Entscheidungsformel)
    doc = {"text": (
        "Entscheidungsformel:\n§ 23 Abs. 2 Satz 3, 2. Halbs. und § 23 Abs. 2 Satz 5 der Verordnung "
        "zur Durchführung des Asylgesetzes, des Asylbewerberleistungsgesetzes, des "
        "Aufnahmegesetzes und des § 12a des Aufenthaltsgesetzes (Asyldurchführungsverordnung - "
        "DVAsyl) vom 1. Oktober 2019 (GVBI. 613), zuletzt geändert durch Verordnung vom 17. Juni "
        "2020 (GVBI. 321), werden für unwirksam und unvereinbar mit Art. 3 Abs. 1, Abs. 2 Satz 1 "
        "und Abs. 3 Satz 1 GG erklärt.\nMünchen, den 29. Oktober 2021")}
    assert bh.decision_targets(doc, "§") == [
        {"norm": "§ 23", "change_type": "geändert", "operation": "für unwirksam erklärt (Teil)"}]


def test_predecessor_edition_is_split_off():
    base = {"archive_url": "x", "known_at": "2016-01-01"}
    old = {**base, "fassung": "2002-06-04", "captured_at": "2016-02-20T09:55:11Z"}
    new = {**base, "fassung": "2016-08-16", "captured_at": "2024-05-16T12:03:03Z"}
    undated = {**base, "fassung": None, "captured_at": "2021-09-18T21:55:38Z"}
    keep, other = bh.split_editions("by_dvasyl", [old, new, undated])
    assert keep == [new, undated] and other == [old]
