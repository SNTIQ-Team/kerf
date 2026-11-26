"""Citation extraction: norm identities, decision mentions, parser-only relation types."""
import sqlite3

import pytest

from courts.citations import (FORBIDDEN_FOR_PARSER, PARSER_RELATIONS, Relation, extract_decisions,
                              extract_norms)
from courts.paragraphs import assemble
from courts.sources.base import Capture
from courts.sources.rii import RiiAdapter

DOC = "jb-WBRE201600193"


def targets(text):
    return [r.target_id for r in extract_norms(text, "p")]


@pytest.fixture
def rii_paragraphs(fixture):
    item = {"source_doc_id": DOC, "url": "https://x", "listed_date": "2016-03-16", "listed_court": "bverwg"}
    parsed = RiiAdapter().parse(Capture(fixture(f"rii/{DOC}.zip"), "https://x", "application/zip", "zip"), item)
    paras, _ = assemble(parsed.ecli, parsed.sections)
    return {p.id.split("#")[1]: p for p in paras}


def test_ivm_chain_from_real_rn11(rii_paragraphs):
    rels = extract_norms(rii_paragraphs["rn-11"].text, "rn-11")
    assert [r.target_id for r in rels] == [
        "de/bund/vwgo/para/166/abs/1/satz/1", "de/bund/zpo/para/114/abs/1/satz/1", "de/bund/zpo/para/121/abs/1"]
    assert all(r.detail["in_conjunction_with"] for r in rels)
    assert rels[0].detail["chain"].startswith("§ 166 Abs. 1 Satz 1 VwGO i.V.m. § 114")


def test_renamed_act_resolves_to_same_work(rii_paragraphs):
    rel = extract_norms(rii_paragraphs["rn-1"].text, "rn-1")[0]
    assert rel.target_id == "de/bund/asylvfg_1992/para/24/abs/4"
    assert rel.detail["act"] == "AsylVfG" and rel.detail["act_resolution"] == "act_index"
    assert rel.detail["kerf_act_id"] == "fed_asylvfg_1992"


@pytest.mark.parametrize("text,expected", [
    ("§ 7 Abs. 1 Satz 1 Nr. 2 Buchst. a SGB II", ["de/bund/sgb_2/para/7/abs/1/satz/1/nr/2/buchst/a"]),
    ("§ 7 Abs. 1 Satz 1 Nr. 2 lit. a SGB II", ["de/bund/sgb_2/para/7/abs/1/satz/1/nr/2/buchst/a"]),
    ("Art. 19 Abs. 4 Satz 1 GG", ["de/bund/gg/art/19/abs/4/satz/1"]),
    ("§ 37 Abs. 2 Nr. 3 SGB XIV", ["de/bund/sgb_14/para/37/abs/2/nr/3"]),
    ("§§ 44 bis 46 SGB X", ["de/bund/sgb_10/para/44", "de/bund/sgb_10/para/45", "de/bund/sgb_10/para/46"]),
    ("§ 86b Abs. 2 Satz 2 SGG i.V.m. §§ 920 Abs. 2, 294 ZPO",
     ["de/bund/sgg/para/86b/abs/2/satz/2", "de/bund/zpo/para/920/abs/2", "de/bund/zpo/para/294"]),
    ("§§ 60, 61 Abs. 1 Satz 2, 66 SGB I", ["de/bund/sgb_1/para/60", "de/bund/sgb_1/para/61/abs/1/satz/2",
                                           "de/bund/sgb_1/para/66"]),
    ("§ 7 Abs. 1 und 2 SGB II", ["de/bund/sgb_2/para/7/abs/1", "de/bund/sgb_2/para/7/abs/2"]),
    ("Art. 6 Abs. 1 Buchst. e DSGVO", ["eu/32016R0679/art/6/abs/1/buchst/e"]),
    ("Art. 23 Abs. 2 der Richtlinie 2005/85/EG", ["eu/32005L0085/art/23/abs/2"]),
    ("Art. 4 der Verordnung (EG) Nr. 883/2004", ["eu/32004R0883/art/4"]),
    ("§ 1 BayVwVfG", ["de/by/bayvwvfg/para/1"]),
    ("§ 35 VwVfG NRW", ["de/nw/vwvfg/para/35"]),
    ("Czybulka, in: Sodan/Ziekow, VwGO, 4. Aufl. 2014, § 132 Rn. 65", []),  # no act after the §: skipped
])
def test_norm_identities(text, expected):
    assert targets(text) == expected


def test_following_sections_marker():
    rel = extract_norms("nach §§ 3 ff. SGB X", "p")[0]
    assert rel.target_id == "de/bund/sgb_10/para/3" and rel.detail["following"] == "ff"


def test_unknown_act_is_unresolved_not_guessed():
    rel = extract_norms("§ 12 KAG", "p")[0]
    assert rel.target_id is None and rel.detail["act_resolution"] == "unresolved"
    assert rel.target_raw == "§ 12 KAG"


def test_norm_quote_detection():
    assert extract_norms("§ 24 Abs. 4 AsylG lautet: „Das Bundesamt hat …“", "p")[0].relation == "quotes"


def test_patent_register_in_a_cited_docket():
    # VG Hamburg, 11 E 3588/09, Rn. 20; the second docket is published in JURE109006024.
    for cite, docket in [
        ("BPatG, Beschl. v. 14.1.2003 – 24 W (pat) 50/02", "24 W (pat) 50/02"),
        ("Bundespatentgericht, Beschluss vom 18.05.2010 - 3 ZA (pat) 1/09", "3 ZA (pat) 1/09"),
    ]:
        text = "(" + cite + "), Rn. 4."
        hit, = extract_decisions(text, "p", own_court_id=None)
        assert hit.detail["court_id"] == "bpatg"
        assert hit.detail["case_number"] == docket
        assert text[hit.span_start:hit.span_end] == cite
        assert hit.target_id.endswith("/" + docket.replace(" ", "_").replace("/", "-"))

    assert not extract_decisions("BGH, Beschluss vom 18.05.2010 - 3 ZA (pat) 1/09", "p", own_court_id=None)
    assert not extract_decisions("BPatG, Beschluss vom 18.05.2010 - 3 W (Anmerkung) 1/09", "p", own_court_id=None)
    assert not extract_decisions("BPatG wurde erwähnt. DPMA, Beschluss vom 18.05.2010 - 3 W (pat) 1/09",
                                 "p", own_court_id=None)


def test_decision_mentions_from_real_rn5(rii_paragraphs):
    rels = extract_decisions(rii_paragraphs["rn-5"].text, "rn-5", own_court_id="bverwg",
                             own_case_keys={"1B19/16", "1PKH55/16"})
    got = [(r.relation, r.target_id) for r in rels]
    assert got == [("mentions", "court:bverwg/2014-04-01/1_B_1.14"),
                   ("mentions", "court:bverwg/2015-03-10/1_B_7.15"),
                   ("mentions", "court:bverwg/2015-10-27/1_B_55.15"),
                   ("mentions", "court:bverwg/1992-06-29/3_B_102.91")]
    ev = rels[0].evidence()
    assert ev["paragraph_id"] == "rn-5" and ev["derived_by"] == "parser" and ev["legal_effect_verified"] is False
    assert "1 B 1.14" in ev["quoted_context"]


def test_quote_procedural_self_and_ecli():
    text = ("„Ein Anspruch besteht nur, wenn die Voraussetzungen vorliegen“ (BSG, Urteil vom 12.5.2017 - "
            "B 7 AY 1/16 R - Rn. 20). Die Beschwerde gegen den Beschluss des Sozialgerichts Köln vom "
            "3. Februar 2025 - S 12 AS 123/25 ER - ist zulässig. Vgl. Senatsurteil vom 17. März 2016 - "
            "B 4 AS 18/15 R -; ECLI:DE:BVerfG:2012:ls20120718.1bvl001010.")
    rels = extract_decisions(text, "p", own_court_id="bsg")
    got = [(r.relation, r.target_id) for r in rels]
    assert ("quotes", "court:bsg/2017-05-12/B_7_AY_1-16_R") in got
    assert ("procedural_reference", "court:sg-koeln/2025-02-03/S_12_AS_123-25_ER") in got
    assert ("mentions", "court:bsg/2016-03-17/B_4_AS_18-15_R") in got
    assert ("mentions", "ECLI:DE:BVerfG:2012:ls20120718.1bvl001010") in got
    quote = next(r for r in rels if r.relation == "quotes")
    assert quote.quoted_context.startswith("„Ein Anspruch")


def test_cjeu_identifier_keeps_the_printed_span_and_uses_the_full_target():
    from courts.citations import extract_decisions

    text = ("Urteil, EU:C:2016:764, Rn. 28, eu:t:2005:57 und EU:F:2010:80; "
            "ECLI:EU:C:2021:629und juris; ECLI:EU:C:2015:780Urteil. "
            "ECLI:DE:BVerfG:2012:ls20120718.1bvl001010. NichtEU:C:2018:19. "
            "ECLI:EU:C:2018:ABC.")
    hits = extract_decisions(text, "source#rn-4", own_court_id=None)
    assert [(x.target_raw, x.target_id) for x in hits] == [
        ("EU:C:2016:764", "ECLI:EU:C:2016:764"),
        ("eu:t:2005:57", "ECLI:EU:T:2005:57"),
        ("EU:F:2010:80", "ECLI:EU:F:2010:80"),
        ("ECLI:EU:C:2021:629", "ECLI:EU:C:2021:629"),
        ("ECLI:EU:C:2015:780", "ECLI:EU:C:2015:780"),
        ("ECLI:DE:BVerfG:2012:ls20120718.1bvl001010", "ECLI:DE:BVerfG:2012:ls20120718.1bvl001010"),
    ]
    assert all(text[x.span_start:x.span_end] == x.target_raw and x.detail["ecli"] == x.target_id for x in hits)


def test_parser_cannot_emit_legal_effect_relations():
    assert PARSER_RELATIONS == {"mentions", "quotes", "procedural_reference"}
    for forbidden in FORBIDDEN_FOR_PARSER | {"applies", "interprets", "supports"}:
        with pytest.raises(ValueError):
            Relation(forbidden, "norm", "de/bund/gg/art/1", "Art. 1 GG", "p", 0, 1, "", 0.9)


def test_database_rejects_parser_legal_effect_relations():
    from courts.store import connect, init_db
    conn = connect()
    init_db(conn)
    insert = ("INSERT INTO relations(decision_id, relation, target_kind, target_id, target_raw, quoted_context, "
              "origin, derived_by, legal_effect_verified, confidence) VALUES('d', ?, 'norm', 'x', 'x', '', "
              "'decision_text', ?, ?, 0.5)")
    for relation, derived_by, verified in [("applies", "parser", 0), ("mentions", "parser", 1),
                                           ("interprets", "model", 1)]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(insert, (relation, derived_by, verified))
    conn.execute(insert, ("interprets", "model", 0))  # model-assisted, unverified: allowed by the schema


@pytest.mark.parametrize("text,land,expected", [
    ("nach § 45 SGB X. Danach", None, ["de/bund/sgb_10/para/45"]),           # Roman book before a full stop
    ("Art. 42 Abs. 1 Satz 1 Buchst. h VO (EU) Nr. 604/2013", None, ["eu/32013R0604/art/42/abs/1/satz/1/buchst/h"]),
    ("Art. 101 BV", "by", ["de/by/bv/art/101"]),                            # Bavarian constitution in a Bavarian court
    ("Art. 101 BV", "nw", [None]),                                          # elsewhere "BV" stays unresolved
])
def test_norm_identities_in_context(text, land, expected):
    assert [r.target_id for r in extract_norms(text, "p", land=land)] == expected


def test_trailing_annotation_is_not_part_of_the_docket():
    rels = extract_decisions("(BVerfG Beschluss vom 17.12.2014 - 1 BvL 21/12 mwN)", "p", own_court_id="bsg")
    assert [r.target_id for r in rels] == ["court:bverfg/2014-12-17/1_BvL_21-12"]


@pytest.mark.parametrize("name,court_id", [
    ("Schleswig-Holsteinisches Verwaltungsgericht", "vg-sh"),     # live juris SH
    ("Oberverwaltungsgericht des Saarlandes", "ovg-sl"),           # live juris SL
    ("Verwaltungsgericht Hamburg", "vg-hamburg"),                  # city-state seats keep their name
    ("Verwaltungsgericht Bremen", "vg-bremen"),
    ("Oberverwaltungsgericht des Landes Sachsen-Anhalt", "ovg-st"),
])
def test_court_ids_of_new_laender(name, court_id):
    from courts.courtids import resolve
    assert resolve(name).court_id == court_id


@pytest.mark.parametrize("text,target,basis", [
    ("§ 5 SÜG", "de/bund/s_g/para/5", "gii_catalog"),        # GII writes umlauts as '_' (not Soldatengesetz 'sg')
    ("§ 7 SG", "de/bund/sg/para/7", "gii_catalog"),
    ("§ 24 AsylG", "de/bund/asylvfg_1992/para/24", "alias"),  # renamed act, same work
    ("§ 35 BauGB", "de/bund/bbaug/para/35", "alias"),
    ("§ 1 AÜG", "de/bund/a_g/para/1", "act_index"),
])
def test_norms_resolve_through_kerfs_canonical_resolver(text, target, basis):
    rel = extract_norms(text, "p")[0]
    assert (rel.target_id, rel.detail["act_resolution"]) == (target, basis)


def test_missing_kerf_data_fails_loudly(monkeypatch, tmp_path):
    from courts.acts import KerfDataUnavailable, kerf_data_dir
    import courts.acts as acts
    monkeypatch.setenv("KERF_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(acts, "ROOT", tmp_path)
    monkeypatch.setattr(acts.subprocess, "run", lambda *a, **k: type("R", (), {"stdout": ""})())
    with pytest.raises(KerfDataUnavailable):
        kerf_data_dir()


def test_check_norm_ids_flags_stale_ids(fixture):
    from courts.acts import check_norm_ids
    from courts.store import connect, init_db
    from courts.sync import sync_source
    from tests.courts.test_store_sync_read import FakeHttp, FixtureAdapter, item
    conn = connect()
    init_db(conn)
    sync_source(FixtureAdapter(fixture, [item()]), conn, FakeHttp())
    assert check_norm_ids(conn)["not_canonical"] == 0
    conn.execute("UPDATE relations SET target_id='de/bund/sgb14/para/37' WHERE id=(SELECT min(id) FROM relations "
                 "WHERE target_kind='norm')")
    report = check_norm_ids(conn)
    assert report["not_canonical"] == 1 and "de/bund/sgb14/para/37" in report["not_canonical_examples"]


@pytest.mark.parametrize("written,expected", [
    ("Art. 15 Abs. 1 EuGVVO", None),
    ("Art. 15 der Verordnung (EG) Nr. 44/2001", "eu/32001R0044/art/15"),
    ("Art. 17 der Verordnung (EU) Nr. 1215/2012", "eu/32012R1215/art/17"),
    ("Art. 15 Brüssel-I-VO", "eu/32001R0044/art/15"),
    ("Art. 17 Brüssel-Ia-VO", "eu/32012R1215/art/17"),
    ("Art. 17 BrüIaVO", "eu/32012R1215/art/17"),
])
def test_brussels_i_instruments(written, expected):
    [relation] = extract_norms(written)
    assert relation.target_id == expected
    if expected is None:
        assert relation.detail["act_resolution"] == "ambiguous"
        assert relation.target_raw == written
