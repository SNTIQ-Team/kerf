"""EU/treaty texts: parsing, identity, version history and the norm routes.

Covers pipeline/fetch_eu_texts.py (Formex/XHTML/HTML -> one Unterabsatz per
line), tools/eu_history.py (editions -> versions + change records with dated
evidence), the shared identity (api.norm_identity) and log/at/diff over a real
``law_history.sqlite`` built by tools/build_law_history.py.
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import fetch_eu_texts as eu  # noqa: E402
from api.law_structure import find_unit, parse_norm_structure, sentence_units  # noqa: E402
from api.norm_identity import (ActResolver, eu_act_from_citation, parse_citation,  # noqa: E402
                               split_norm_id)
from tools import eu_history  # noqa: E402

FORMEX_OJ = """<?xml version="1.0" encoding="UTF-8"?><ACT><ENACTING.TERMS>
<ARTICLE IDENTIFIER="001"><TI.ART>Artikel 1</TI.ART><STI.ART>Gegenstand</STI.ART>
<ALINEA>Diese Verordnung regelt den Schutz<NOTE NOTE.ID="E1"><P>ABl. L 1.</P></NOTE> von Daten.</ALINEA>
<ALINEA>Sie gilt unmittelbar.</ALINEA></ARTICLE>
<ARTICLE IDENTIFIER="002"><TI.ART>Artikel 2</TI.ART><STI.ART>Auskunftsrecht</STI.ART>
<PARAG IDENTIFIER="002.001"><NO.PARAG>(1)</NO.PARAG><ALINEA><P>Die Person hat ein Recht auf folgende Informationen:</P>
<LIST TYPE="alpha"><ITEM><NP><NO.P>a)</NO.P><TXT>die Zwecke;</TXT></NP></ITEM>
<ITEM><NP><NO.P>b)</NO.P><TXT>die <QUOT.START CODE="201E"/>Kategorien<QUOT.END CODE="201C"/> der Daten.</TXT></NP></ITEM></LIST></ALINEA></PARAG>
<PARAG IDENTIFIER="002.002"><NO.PARAG>(2)</NO.PARAG><ALINEA>Eine Kopie ist unentgeltlich. Weitere Kopien kosten.</ALINEA></PARAG>
</ARTICLE></ENACTING.TERMS></ACT>"""
FORMEX_CONS = FORMEX_OJ.replace("<ACT>", '<CONS.ACT><INFO.CONSLEG START.DATE="20160504"/>').replace(
    "</ACT>", "</CONS.ACT>").replace("kosten.", "kosten ein Entgelt.").replace(
    "<ENACTING.TERMS>", "<GR.CORRIG><CORRIG><BIB.DATA><BIB.INSTANCE.CONS><DOCUMENT.REF.CONS>"
    "<COLL>L</COLL><NO.OJ>127</NO.OJ><YEAR>2018</YEAR><PAGE.FIRST>2</PAGE.FIRST>"
    "</DOCUMENT.REF.CONS><DATE ISO=\"20180523\">20180523</DATE></BIB.INSTANCE.CONS>"
    "<NO.CELEX>32016R0679R(02)</NO.CELEX></BIB.DATA></CORRIG></GR.CORRIG><ENACTING.TERMS>")
XHTML_ARTICLE = """<html><body><p class="ti-art">Artikel 267</p>
<p class="normal">Der Gerichtshof entscheidet im Wege der Vorabentscheidung</p>
<table><tr><td><p class="normal">a)</p></td><td><p class="normal">über die Auslegung der Verträge,</p></td></tr></table>
<table><tr><td><p class="normal">b)</p></td><td><p class="normal">über die Gültigkeit.</p></td></tr></table>
<p class="normal">Wird eine derartige Frage einem Gericht gestellt, so kann es vorlegen.</p>
<p class="normal">Wird eine derartige Frage in einem schwebenden Verfahren gestellt, so ist es verpflichtet.</p>
</body></html>"""
LEGACY_HTML = """<html><body><p>HAT FOLGENDE RICHTLINIE ERLASSEN:</p><p>KAPITEL I</p>
<p>Allgemeine Bestimmungen</p><p>Artikel 5</p><p>(1) Das Bestehen eines Massenzustroms wird festgestellt.</p>
<p>(2) Der Vorschlag enthält:</p><p>a) Beschreibung der Personengruppen;</p><p>b) Zeitpunkt.</p>
<p>Geschehen zu Brüssel am 20. Juli 2001.</p></body></html>"""


def test_formex_article_renders_absatz_unterabsatz_and_points():
    parsed = eu.parse_formex(FORMEX_OJ.encode())
    art1, art2 = parsed["articles"]
    assert art1 == {"enbez": "Art. 1", "titel": "Gegenstand",
                    "text": "Diese Verordnung regelt den Schutz von Daten.\nSie gilt unmittelbar."}
    assert art2["text"] == ("(1) Die Person hat ein Recht auf folgende Informationen: a) die Zwecke; "
                            "b) die „Kategorien“ der Daten.\n(2) Eine Kopie ist unentgeltlich. "
                            "Weitere Kopien kosten.")
    structure = parse_norm_structure(art2["text"], eu=True)
    assert find_unit(structure, "abs/1/buchst/b")["text"] == "die „Kategorien“ der Daten."
    assert find_unit(structure, "abs/2/satz/2")["text"] == "Weitere Kopien kosten."
    # the unnumbered article: two Unterabsätze counted as Absätze (German practice)
    first = parse_norm_structure(art1["text"], eu=True)
    assert [u["locator"] for u in first["units"]] == ["abs/1", "abs/2"]
    assert find_unit(first, "uabs/2")["text"] == "Sie gilt unmittelbar."


def test_consolidated_formex_lists_its_corrigenda_and_zipped_formex_is_read():
    parsed = eu.parse_formex(FORMEX_CONS.encode())
    assert parsed["consleg"]["start.date"] == "20160504"
    assert parsed["corrigenda"] == [{"celex": "32016R0679R(02)", "date": "2018-05-23",
                                     "oj_ref": "ABl. L 127/2018, S. 2"}]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("L_2026DE.toc.fmx.xml", "<TOC/>")
        archive.writestr("L_2026DE.000101.fmx.xml", FORMEX_OJ)
    assert [a["enbez"] for a in eu.parse_edition(buffer.getvalue(), "fmx4")["articles"]] == \
        ["Art. 1", "Art. 2"]


def test_treaty_xhtml_and_legacy_html_articles():
    art = eu.parse_html_articles(XHTML_ARTICLE.encode())["articles"][0]
    assert art["enbez"] == "Art. 267"
    lines = art["text"].split("\n")
    assert lines[0].endswith("a) über die Auslegung der Verträge, b) über die Gültigkeit.")
    structure = parse_norm_structure(art["text"], eu=True)
    assert find_unit(structure, "abs/3")["text"].startswith("Wird eine derartige Frage in einem")
    legacy = eu.parse_html_articles(LEGACY_HTML.encode())["articles"]
    assert legacy == [{"enbez": "Art. 5", "titel": "", "text":
                       "(1) Das Bestehen eines Massenzustroms wird festgestellt.\n"
                       "(2) Der Vorschlag enthält: a) Beschreibung der Personengruppen; b) Zeitpunkt."}]


@pytest.mark.parametrize("citation,celex", [
    ("RL 2001/55/EG", "32001L0055"), ("Richtlinie 64/221/EWG", "31964L0221"),
    ("VO (EU) 2016/679", "32016R0679"), ("Verordnung (EU) Nr. 604/2013", "32013R0604"),
    ("VO (EWG) Nr. 1408/71", "31971R1408"), ("Durchführungsbeschluss (EU) 2022/382", "32022D0382"),
])
def test_eu_citation_to_celex(citation, celex):
    assert eu_act_from_citation(citation) == celex


def test_eu_and_treaty_ids_and_aliases():
    assert split_norm_id("eu/32016R0679/art/15/abs/3") == ("eu", "32016R0679", "art", "15", "abs/3")
    assert split_norm_id("int/emrk/art/6/abs/3/buchst/c")[:4] == ("int", "emrk", "art", "6")
    assert parse_citation("Art. 6 Abs. 1 UAbs. 1 Buchst. f DSGVO")["sub"] == "abs/1/uabs/1/buchst/f"
    resolver = ActResolver([
        {"id": "eu_32016r0679", "juris": "EU", "celex": "32016R0679", "jurabk": "DSGVO",
         "aliases": ["DS-GVO", "GDPR"]},
        {"id": "eu_32001l0055", "juris": "EU", "celex": "32001L0055", "jurabk": "RL 2001/55/EG",
         "aliases": ["Massenzustrom-Richtlinie"]},
        {"id": "int_emrk", "juris": "INT", "jurabk": "EMRK"},
        {"id": "by_dvasyl", "juris": "DE-BY", "jurabk": "DVAsyl", "aliases": ["AsylDV"]}])
    for text, prefix, basis in (("DS-GVO", "eu/32016R0679", "eu"), ("GDPR", "eu/32016R0679", "eu"),
                                ("RL 2001/55/EG", "eu/32001L0055", "eu_citation"),
                                ("Massenzustrom-Richtlinie", "eu/32001L0055", "eu"),
                                ("EMRK", "int/emrk", "international"),
                                ("AsylDV", "de/by/dvasyl", "act_index")):
        identity = resolver.resolve_act(text)
        assert (identity.prefix, identity.basis, identity.in_corpus) == (prefix, basis, True), text
    # outside the text corpus: same identity, not in corpus (courts keep resolving it)
    assert resolver.resolve_act("RL 2013/33/EU").prefix == "eu/32013L0033"
    assert not resolver.resolve_act("RL 2013/33/EU").in_corpus


def _row() -> dict:
    oj = eu.parse_formex(FORMEX_OJ.encode())
    cons = eu.parse_formex(FORMEX_CONS.encode())
    item = [{"url": "https://publications.europa.eu/resource/cellar/x/DOC_1", "sha256": "a" * 64}]
    return {
        "act_id": "eu_32016r0679", "celex": "32016R0679", "jurabk": "DSGVO",
        "aliases": ["DS-GVO"], "family": "regulation", "title": "Verordnung (EU) 2016/679",
        "dates": {"adopted": "2016-04-27", "published": "2016-05-04", "oj_ref": "ABl. L 119 vom 4.5.2016",
                  "in_force": "2016-05-24", "applies_from": "2018-05-25", "end_of_validity": None},
        "relations": {"corrected_by": [], "repealed_by": [], "implicitly_repealed_by": [],
                      "amended_by": []},
        "editions": [
            {**oj, "kind": "oj", "celex": "32016R0679", "date": "2016-05-04",
             "published": "2016-05-04", "items": item},
            {**cons, "kind": "consolidated", "celex": "02016R0679-20160504", "date": "2016-05-04",
             "items": item}],
        "fetched_at": "2026-09-29T00:00:00+00:00", "warnings": [],
    }


def test_history_versions_and_dated_change_records():
    detail = eu_history.build(_row())
    versions = {(v["canonical_id"], v["sequence"]): v for v in detail["history"]["version"]}
    art1 = versions[("eu/32016R0679/art/1", 0)]
    assert ("eu/32016R0679/art/1", 1) not in versions      # identical OJ + consolidation: one version
    assert art1["temporal"]["valid_from"] == "2016-05-24"
    assert art1["temporal"]["valid_from_status"] == "official_eu_consolidation"
    oj, corrected = versions[("eu/32016R0679/art/2", 0)], versions[("eu/32016R0679/art/2", 1)]
    assert oj["source_role"] == "official_promulgation" and oj["temporal"]["valid_from"] is None
    assert corrected["temporal"]["valid_from"] == "2016-05-24"
    assert corrected["text"].endswith("Weitere Kopien kosten ein Entgelt.")
    changes = {c["change_id"]: c for c in detail["history"]["change"]}
    enacted = changes["eu_32016r0679:art-1:enacted"]
    assert {k: v["date"] for k, v in enacted["dates"].items()} == {
        "adopted": "2016-04-27", "promulgated": "2016-05-04", "in_force": "2016-05-24",
        "applies_from": "2018-05-25"}
    corrigendum = changes["eu_32016r0679:art-2:v1"]
    assert corrigendum["change_type"] == "berichtigt"
    assert corrigendum["dates"]["promulgated"]["date"] == "2018-05-23"
    assert corrigendum["amending_act"]["document_id"] == "32016R0679R(02)"
    assert [n["enbez"] for n in detail["norms"]] == ["Art. 1", "Art. 2"]


def _generation(tmp_path: Path) -> Path:
    from tools import build_law_history
    data = tmp_path / "web-data"
    (data / "acts").mkdir(parents=True)
    detail = eu_history.build(_row())
    (data / "acts" / "eu_32016r0679.json").write_text(json.dumps(detail, ensure_ascii=False))
    (data / "wiki.json").write_text(json.dumps([eu_history.index_row(detail)]))
    build_law_history.main(["--data-dir", str(data), "--documents", str(tmp_path / "none"),
                            "--bgbl-index", str(tmp_path / "none")])
    return data


def test_routes_resolve_text_log_diff_for_an_eu_article(tmp_path):
    from api.law_routes import create_router
    data = _generation(tmp_path)
    app = FastAPI()
    app.include_router(create_router(lambda: data, lambda: None, lambda: "fixture"))
    client = TestClient(app)
    resolved = client.get("/norms/resolve", params={"q": "Art. 2 Abs. 1 Buchst. b DS-GVO"}).json()
    assert resolved["status"] == "ok"
    assert resolved["evidence"][0]["canonical_id"] == "eu/32016R0679/art/2/abs/1/buchst/b"
    at = client.get("/norms/eu/32016R0679/art/2/abs/1/buchst/b/at", params={"date": "2024-01-01"}).json()
    assert at["status"] == "ok", at
    item = at["evidence"][0]
    assert item["sub_unit_text"] == "die „Kategorien“ der Daten."
    assert item["temporal"]["valid_from"] == "2016-05-24"
    assert item["structure"]["parser_version"].endswith("+eu-1")
    log = client.get("/norms/eu/32016R0679/art/2/log").json()
    assert log["status"] == "ok" and log["total"] == 2
    kinds = {e["change_type"] for e in log["events"]}
    assert {"erlassen", "berichtigt"} <= kinds
    diff = client.get("/norms/eu/32016R0679/art/2/diff",
                      params={"from": log["evidence"][1]["version_id"], "to": "head"}).json()
    assert diff["status"] == "ok" and diff["summary"].get("replace", 0) + diff["summary"].get(
        "insert", 0) >= 1
    missing = client.get("/norms/resolve", params={"q": "Art. 5 RL 2013/33/EU"}).json()
    assert missing["status"] == "not_in_corpus"


def test_repealed_act_answers_its_last_wording_only_with_the_verified_end(tmp_path):
    from api.law_routes import create_router
    row = _row()
    row["dates"]["end_of_validity"] = "2026-06-11"
    row["relations"]["repealed_by"] = [{"celex": "32024R1351", "published": "2024-05-22",
                                        "adopted": "2024-05-14", "oj_ref": "ABl. L, 2024/1351"}]
    from tools import build_law_history
    data = tmp_path / "web-data"
    (data / "acts").mkdir(parents=True)
    detail = eu_history.build(row)
    art2 = [v for v in detail["history"]["version"] if v["canonical_id"].endswith("/art/2")]
    assert art2[-1]["temporal"]["valid_until_exclusive"] == "2026-06-12"
    (data / "acts" / "eu_32016r0679.json").write_text(json.dumps(detail, ensure_ascii=False))
    (data / "wiki.json").write_text(json.dumps([eu_history.index_row(detail)]))
    build_law_history.main(["--data-dir", str(data), "--documents", str(tmp_path / "none"),
                            "--bgbl-index", str(tmp_path / "none")])
    app = FastAPI()
    app.include_router(create_router(lambda: data, lambda: None, lambda: "fixture"))
    client = TestClient(app)
    before = client.get("/norms/eu/32016R0679/art/2/at", params={"date": "2026-06-11"}).json()
    after = client.get("/norms/eu/32016R0679/art/2/at", params={"date": "2026-09-01"}).json()
    assert before["status"] == "ok"
    assert after["status"] == "temporally_unresolved"
    assert after["resolution"]["resolution"] == "after_verified_end"
    assert after["resolution"]["valid_until_exclusive"] == "2026-06-12"
    repeal = [c for c in detail["history"]["change"] if c["change_type"] == "aufgehoben"]
    assert repeal[0]["dates"]["in_force"]["date"] == "2026-06-12"
