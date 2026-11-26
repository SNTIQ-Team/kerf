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


def test_quoted_formex_articles_belong_to_the_amending_article():
    # Regulation 2024/1352 uses this shape to insert Art. 7c into Regulation 2019/816.
    xml = """<ACT><ENACTING.TERMS><DIVISION><ARTICLE><TI.ART>Artikel 1</TI.ART><ALINEA>
    <P>Folgender Artikel wird eingefügt:</P><QUOT.S><ARTICLE>
    <TI.ART><QUOT.START CODE="201E"/>Artikel 7c</TI.ART><STI.ART><P>Überschrift</P></STI.ART>
    <PARAG><NO.PARAG>(1)</NO.PARAG><ALINEA>Erster Satz.</ALINEA></PARAG>
    <PARAG><NO.PARAG>(2)</NO.PARAG><ALINEA>Zweiter Satz.<QUOT.END CODE="201C"/></ALINEA></PARAG>
    </ARTICLE></QUOT.S></ALINEA></ARTICLE>
    <ARTICLE><TI.ART>Artikel 7c</TI.ART><ALINEA>Eigener Artikel.</ALINEA></ARTICLE>
    </DIVISION></ENACTING.TERMS><ANNEX><ARTICLE><TI.ART>Artikel 99</TI.ART>
    <ALINEA>Mustereintrag.</ALINEA></ARTICLE></ANNEX></ACT>"""
    parsed = eu.parse_formex(xml.encode())
    assert [a["enbez"] for a in parsed["articles"]] == ["Art. 1", "Art. 7c"]
    assert "„Artikel 7c Überschrift (1) Erster Satz. (2) Zweiter Satz.“" in parsed["articles"][0]["text"]
    assert parsed["articles"][1]["text"] == "Eigener Artikel."
    assert parsed["annexes_skipped"] == 1


def test_formex_consolidation_omits_deleted_articles_and_inline_words():
    # 2013/36/EU keeps the repealed Arts. 28–32 inside a single deleted division.
    xml = b'''<CONS.ACT><ENACTING.TERMS><?CLG.MDFO ID="o1" IDREF="c1" ACTION="DELETED"?>
      <DIVISION><ARTICLE><TI.ART>Artikel 28</TI.ART><ALINEA>Alt</ALINEA></ARTICLE></DIVISION>
      <?CLG.MDFC ID="c1" IDREF="o1"?><ARTICLE><TI.ART>Artikel 33</TI.ART><ALINEA>
      Erwerb <?CLG.MDFO ID="o2" IDREF="c2" ACTION="DELETED"?>oder <HT>Kauf</HT>
      <?CLG.MDFC ID="c2" IDREF="o2"?>durch <?CLG.MDFO ID="o3" IDREF="c3" ACTION="REPLACED"?>
      Dritte<?CLG.MDFC ID="c3" IDREF="o3"?>.</ALINEA></ARTICLE></ENACTING.TERMS></CONS.ACT>'''
    articles = eu.parse_formex(xml)['articles']
    assert [(a['enbez'], a['text']) for a in articles] == [('Art. 33', 'Erwerb durch Dritte.')]


def test_formex_deletion_can_cross_division_and_contain_nested_changes():
    # The consolidation of Regulation 2016/1624 closes a deletion inside a division.
    xml = b'''<CONS.ACT><ENACTING.TERMS><?CLG.MDFO ID="o1" IDREF="c1" ACTION="DELETED"?>
      <DIVISION><ARTICLE><TI.ART>Artikel 1</TI.ART><ALINEA>Alt</ALINEA></ARTICLE>
      <?CLG.MDFO ID="o2" IDREF="c2" ACTION="DELETED"?>
      <ARTICLE><TI.ART>Artikel 2</TI.ART><ALINEA>Alt</ALINEA></ARTICLE>
      <?CLG.MDFC ID="c2" IDREF="o2"?><?CLG.MDFC ID="c1" IDREF="o1"?>
      <ARTICLE><TI.ART>Artikel 3</TI.ART><ALINEA>Erhalten.</ALINEA></ARTICLE></DIVISION>
      </ENACTING.TERMS></CONS.ACT>'''
    assert [a['enbez'] for a in eu.parse_formex(xml)['articles']] == ['Art. 3']


@pytest.mark.parametrize('closing', ['', '<?CLG.MDFC ID="c1" IDREF="other"?>'])
def test_formex_rejects_unpaired_deletion(closing):
    xml = '<CONS.ACT><?CLG.MDFO ID="o1" IDREF="c1" ACTION="DELETED"?>' + closing + '</CONS.ACT>'
    with pytest.raises(ValueError, match='deletion'):
        eu.parse_formex(xml.encode())


def test_formex_article_subdivisions_keep_titles_and_paragraphs_in_order():
    # Regulation 987/2009, Article 25, places its numbered paragraphs under A., B. and C.
    xml = b'''<ACT><ENACTING.TERMS><ARTICLE><TI.ART>Artikel 25</TI.ART><STI.ART>Aufenthalt</STI.ART>
      <SUBDIV><TITLE><TI><NP><NO.P>A.</NO.P><TXT>Verfahren</TXT></NP></TI></TITLE>
      <PARAG><NO.PARAG>(1)</NO.PARAG><ALINEA>Erster Absatz.</ALINEA></PARAG>
      <SUBDIV><TITLE><TI><NP><NO.P>I.</NO.P><TXT>Belege</TXT></NP></TI></TITLE>
      <ALINEA>Eine Bescheinigung.</ALINEA></SUBDIV></SUBDIV>
      <SUBDIV><TITLE><TI><NP><NO.P>B.</NO.P><TXT>Zahlung</TXT></NP></TI></TITLE>
      <PARAG><NO.PARAG>(2)</NO.PARAG><ALINEA>Weitere Angaben.</ALINEA></PARAG></SUBDIV>
      <ALINEA>Schluss.</ALINEA></ARTICLE></ENACTING.TERMS></ACT>'''
    assert eu.parse_formex(xml)['articles'] == [{
        'enbez': 'Art. 25', 'titel': 'Aufenthalt',
        'text': 'A. Verfahren\n(1) Erster Absatz.\nI. Belege\nEine Bescheinigung.\nB. Zahlung\n(2) Weitere Angaben.\nSchluss.',
    }]


def test_formex_quoted_paragraph_keeps_its_number_and_opening_quote():
    # Article 105 of the AI Act adds a numbered paragraph to Directive 2014/90/EU.
    # Its Formex puts the opening quotation mark inside the quoted paragraph number.
    xml = b'''<ACT><ARTICLE><TI.ART>Artikel 105</TI.ART><PARAG><NO.PARAG>(1)</NO.PARAG>
      <ALINEA>Folgender Absatz wird angefuegt:</ALINEA><ALINEA><QUOT.S>
      <PARAG><NO.PARAG><QUOT.START CODE="201E"/>(5)</NO.PARAG><ALINEA>Erster Satz.
      <QUOT.END CODE="201C"/></ALINEA></PARAG></QUOT.S></ALINEA></PARAG></ARTICLE></ACT>'''
    assert eu.parse_formex(xml)['articles'][0]['text'] == '(1) Folgender Absatz wird angefuegt:\n„(5) Erster Satz. “'


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


def test_legacy_html_keeps_the_article_and_drops_its_endnotes():
    # The German consolidated edition of 2003/8/EC links each body note to a source anchor.
    html = """<body><p>Artikel 23</p>
    <p style="font-weight: bold; text-align: center">Adressaten</p>
    <p>(1) Diese Richtlinie gilt für die genannten Personen (<a href="#E0001">1</a>).</p>
    <p>(2) Der zweite Absatz bleibt erhalten.</p>
    <a name="E0001"></a><p>(<a href="#src.E0001">1</a>) ABl. C 103 E vom 30.4.2002, S. 368.</p>
    </body>"""
    assert eu.parse_html_articles(html.encode())["articles"] == [{
        "enbez": "Art. 23", "titel": "Adressaten",
        "text": "(1) Diese Richtlinie gilt für die genannten Personen.\n(2) Der zweite Absatz bleibt erhalten.",
    }]


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


def _generation(tmp_path: Path, row=None) -> Path:
    from tools import build_law_history
    data = tmp_path / "web-data"
    (data / "acts").mkdir(parents=True)
    detail = eu_history.build(row if row is not None else _row())
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


@pytest.mark.parametrize(("end", "observed", "valid_until"), [
    ("2031-11-02", "2026-09-29", "2031-11-03"),
    ("2020-11-02", "2020-11-02", "2020-11-03"),
])
def test_whole_act_repeal_bounds_knowledge_at_retrieval(end, observed, valid_until):
    row = _row()
    row["dates"]["end_of_validity"] = end
    row["relations"]["repealed_by"] = [{"celex": "32024R1351"}]
    detail = eu_history.build(row)
    latest = detail["history"]["states"][-1]
    assert (latest["known_until"], latest["valid_until"]) == (observed, valid_until)
    assert detail["history"]["states"][0]["known_until"] == "2016-05-04"
    for version in detail["history"]["version"]:
        assert version["temporal"]["observed_until"] <= observed
        assert all(att["known_until"] <= observed for att in version["attestations"])


@pytest.mark.parametrize(("end", "repeals"), [("2031-11-02", []), ("2016-05-03", [{"celex": "example"}])])
def test_unconfirmed_act_end_preserves_source_date_without_closing_text(end, repeals):
    row = _row()
    row["dates"]["end_of_validity"] = end
    row["relations"]["repealed_by"] = repeals
    detail = eu_history.build(row)
    latest = detail["history"]["states"][-1]
    assert latest["known_until"] == "2026-09-29" and latest["valid_until"] is None
    assert detail["dates"]["end_of_validity"] is None
    finding = next(f for f in detail["history"]["finding"] if f["kind"] == "act_end_of_validity_unresolved")
    assert finding["reported_end_of_validity"] == end
    assert row["dates"]["end_of_validity"] == end


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


def test_civil_aid_display_title_comes_from_the_corrected_german_edition(monkeypatch):
    original = "32003L0008"
    corrected = "02003L0008-20030131"
    spec = next(s for s in eu.CORPUS if s["celex"] == original)
    old_title = "Richtlinie 2002/8/EG des Rates"
    new_title = "Richtlinie 2003/8/EG des Rates"
    meta = dict.fromkeys(("adopted", "signed", "published", "oj_ref", "in_force", "applies_from",
                          "entry_into_force_dates", "end_of_validity", "eli"))
    meta["title"] = old_title
    monkeypatch.setattr(eu, "work_metadata", lambda *_: meta)
    monkeypatch.setattr(eu, "consolidations", lambda *_: [{"celex": corrected, "date": "2003-01-31"}])
    monkeypatch.setattr(eu, "related", lambda *_: {})
    monkeypatch.setattr(eu, "manifestations", lambda *_: {})
    monkeypatch.setattr(eu, "fetch_edition", lambda _cellar, celex, _: {
        "celex": celex, "articles": [{"enbez": "Art. 1", "text": "Ziele"}]})

    class CellarTitle:
        def sparql(self, query):
            assert eu.celex_uri(corrected) in query and eu.DEU in query
            return [{"t": new_title}]

    row = eu.fetch_act(CellarTitle(), spec, lisbon=None)
    assert row["title"] == new_title
    assert row["source"]["published_title"] == old_title
    assert row["source"]["corrected_title_edition"] == corrected


def test_formex_nested_list_points_keep_their_printed_markers():
    # The German NIS2 Directive, Article 2(2)(a), puts its Roman points inside
    # a paragraph of the outer point. The marker is required to cite the text.
    xml = b"""<ACT><ENACTING.TERMS><ARTICLE><TI.ART>Artikel 2</TI.ART>
      <PARAG><NO.PARAG>2.</NO.PARAG><ALINEA><LIST><ITEM><NP><NO.P>a)</NO.P>
      <TXT>Einrichtungen:</TXT><P><LIST><ITEM><NP><NO.P>i)</NO.P><TXT>Netze;</TXT>
      </NP></ITEM><ITEM><NP><NO.P>ii)</NO.P><TXT>Dienste.</TXT></NP></ITEM></LIST></P>
      </NP></ITEM></LIST></ALINEA></PARAG></ARTICLE></ENACTING.TERMS></ACT>"""
    [article] = eu.parse_formex(xml)["articles"]
    assert article["text"] == "(2) a) Einrichtungen: i) Netze; ii) Dienste."


def test_future_consolidation_enters_the_reader_on_its_edition_date(tmp_path):
    row = _row()
    prior = row["editions"][-1]
    prior["articles"] = [
        {"enbez": "Art. 1", "titel": "", "text": "Current wording."},
        {"enbez": "Art. 2", "titel": "", "text": "Present until the change."},
    ]
    later = {**prior, "celex": "02016R0679-20270111", "date": "2027-01-11",
             "articles": [{"enbez": "Art. 1", "titel": "", "text": "Later wording."},
                          {"enbez": "Art. 3", "titel": "", "text": "New article."}]}
    row["editions"].append(later)
    current = eu_history.build(row)
    assert current["stand"] == prior["celex"]
    assert [(a["enbez"], a["text"]) for a in current["norms"]] == [
        ("Art. 1", "Current wording."), ("Art. 2", "Present until the change.")]
    assert any(s["stand"] == later["celex"] for s in current["history"]["states"])
    assert any(v["temporal"]["valid_from"] == "2027-01-11" for v in current["history"]["version"])
    later_detail = eu_history.build(row, as_of="2027-01-11")
    assert later_detail["stand"] == later["celex"]
    assert [(a["enbez"], a["text"]) for a in later_detail["norms"]] == [
        ("Art. 1", "Later wording."), ("Art. 3", "New article.")]
    from api.law_routes import create_router
    data = _generation(tmp_path, row)
    app = FastAPI()
    app.include_router(create_router(lambda: data, lambda: None, lambda: "fixture"))
    client = TestClient(app)
    path = "/norms/eu/32016R0679/art/3/at"
    for on in ("2026-09-29", "2026-12-31"):
        before = client.get(path, params={"date": on}).json()
        assert before["status"] == "not_in_corpus" and not before.get("evidence")
    after = client.get(path, params={"date": "2027-01-11"}).json()
    assert after["status"] == "ok" and after["evidence"][0]["text"] == "New article."
