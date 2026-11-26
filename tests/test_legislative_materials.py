"""Legislative-materials fragments, tested against real extraction output.

The fixtures are the exact ``pdftotext -layout`` text of ten pages of
BT-Drs. 19/13824 (Regierungsentwurf, Soziales Entschädigungsrecht); every
other page is passed as ``None`` so the parser must treat it as unextracted.
Page 314 is the regression page for a competitor excerpt in which the
Gegenäußerung answer to Ziffer 9 swallowed the start of the Ziffer 10
heading.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.legislative_materials import (PARSER_VERSION, heading_targets,
                                       parse_citation, segment_drucksache,
                                       _Ctx)
from api.materials_routes import create_router, materials_for_norm
from api.page_furniture import find_page_furniture
from tools.build_materials import write_store

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "materials" / "bt-drs-19-13824"
SOURCES = json.loads((ROOT / "data" / "materials_sources.json")
                     .read_text(encoding="utf-8"))
DOC_ID = "bt-drs-19-13824"


def _meta() -> dict:
    return json.loads((FIXTURES / "meta.json").read_text(encoding="utf-8"))


def _pages() -> list[str | None]:
    meta = _meta()
    pages: list[str | None] = [None] * meta["page_count"]
    for number, row in meta["pages"].items():
        pages[int(number) - 1] = (FIXTURES / row["file"]).read_bytes() \
            .decode("utf-8")
    return pages


def _document_meta() -> dict:
    entry = dict(SOURCES["documents"][0])
    meta = _meta()
    entry["source"] = {"url": meta["url"], "pdf_sha256": meta["pdf_sha256"],
                       "page_count": meta["page_count"],
                       "observed_at": "2026-09-28T07:04:41+00:00",
                       "observed_at_basis": "test_fixture"}
    entry["extraction"] = {"pdftotext_version": meta["pdftotext_version"]}
    return entry


@pytest.fixture(scope="module")
def pages() -> list[str | None]:
    return _pages()


@pytest.fixture(scope="module")
def result(pages) -> dict:
    return segment_drucksache(pages, drucksache="19/13824",
                              document_meta=_document_meta(),
                              act_slugs=SOURCES["act_slugs"])


@pytest.fixture(scope="module")
def frags(result) -> dict[str, dict]:
    return {row["id"]: row for row in result["fragments"]}


def frag(frags, suffix: str) -> dict:
    return frags[f"{DOC_ID}:{suffix}"]


def raw(pages, span: dict) -> str:
    return pages[span["pdf_page"] - 1][span["start"]:span["end"]]


def squash(text: str) -> str:
    """Compare raw and normalised text modulo layout whitespace and
    line-end hyphenation."""
    return re.sub(r"[\s\-]", "", text)


# -- fixtures are the real, unmodified extraction ---------------------------

def test_fixture_pages_match_recorded_extraction_hashes():
    meta = _meta()
    assert meta["pdf_sha256"] == SOURCES["documents"][0]["pdf_sha256"]
    assert meta["page_count"] == 322
    for number, row in meta["pages"].items():
        data = (FIXTURES / row["file"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == row["text_sha256"], number


def test_document_records_page_hashes_versions_and_missing_pages(result):
    doc = result["document"]
    assert doc["id"] == DOC_ID and doc["parser_version"] == PARSER_VERSION
    pages = {row["pdf_page"]: row for row in doc["extraction"]["pages"]}
    assert sorted(pages) == [1, 144, 169, 186, 187, 285, 286, 312, 313, 314]
    assert pages[314]["printed_page"] == 314
    assert pages[314]["text_sha256"] == _meta()["pages"]["314"]["text_sha256"]
    assert pages[1]["printed_page"] is None          # page 1 prints no number
    assert doc["extraction"]["pdftotext_version"].startswith("pdftotext")
    assert len(doc["extraction"]["pages_missing"]) == 312
    assert doc["detected"]["title"] == (
        "Entwurf eines Gesetzes zur Regelung des Sozialen "
        "Entschädigungsrechts")
    assert doc["warnings"] == []


def test_sections_carry_role_page_ranges(result):
    starts = {row["role"]: row["pdf_page_start"] for row in result["sections"]}
    assert starts == {"vorblatt": 1, "begruendung": 144,
                      "stellungnahme_bundesrat": 285,
                      "gegenaeusserung_bundesregierung": 312}


# -- regression: Ziffer 9 / Ziffer 10 on page 314 ----------------------------

def test_ziffer_9_body_is_only_the_answer(frags, pages):
    row = frag(frags, "gegenaeusserung:ziffer-9")
    assert row["role"] == "gegenaeusserung_bundesregierung"
    assert row["heading"] == ("Zu Ziffer 9 (Zu Artikel 1, § 34 Absatz 3 "
                              "Satz 2 SGB XIV)")
    assert row["body"] == ("Die Bundesregierung stimmt dem Vorschlag des "
                           "Bundesrates zu.")
    assert "Zu Ziffer 10" not in row["body"] and "(" not in row["body"]
    assert row["page_start"] == row["page_end"] == 314
    assert row["heading_span"]["pdf_page"] == 314
    assert row["heading_span"]["printed_page"] == 314
    assert raw(pages, row["heading_span"]) == row["heading"]
    assert [raw(pages, span) for span in row["body_spans"]] == [row["body"]]
    assert row["text_sha256"] == hashlib.sha256(
        (row["heading"] + "\n" + row["body"]).encode()).hexdigest()


def test_ziffer_10_heading_and_body_are_separate(frags, pages):
    row = frag(frags, "gegenaeusserung:ziffer-10")
    assert row["heading"] == "Zu Ziffer 10 (Zu Artikel 1, § 35 SGB XIV)"
    assert row["body"].startswith(
        "Die Bundesregierung stimmt dem Vorschlag des Bundesrates nicht zu.")
    assert row["body"].endswith("psychotherapeutischen Angeboten herzustellen.")
    for leak in ("Zu Ziffer", "Zu Artikel 1", "§ 35 SGB XIV", "Ziffer 11"):
        assert leak not in row["body"]
    assert "Die Bundesregierung" not in row["heading"]
    assert raw(pages, row["heading_span"]) == row["heading"]
    assert squash("".join(raw(pages, s) for s in row["body_spans"])) == \
        squash(row["body"])
    # line-end hyphens are joined, soft line breaks become spaces
    assert "Möglichkeit einer zeitnahen Weiterbehandlung" in row["body"]
    assert "Wartezeiten" in row["body"] and "\n" not in row["body"]


def test_every_span_slices_back_to_the_raw_page_text(result, pages):
    for row in result["fragments"]:
        assert squash(raw(pages, row["heading_span"])) == \
            squash(row["heading"]), row["id"]
        body_raw = "".join(raw(pages, s) for s in row["body_spans"])
        assert squash(body_raw) == squash(row["body"]), row["id"]
        if "justification" in row:
            just = row["justification"]
            assert raw(pages, just["heading_span"]) == "Begründung:"
            assert squash("".join(raw(pages, s) for s in just["body_spans"])) \
                == squash(just["body"])
        for mention in row["mentions"]:
            text = row["body"] if mention["part"] == "body" \
                else row["justification"]["body"]
            source = text[mention["text_start"]:mention["text_end"]]
            assert source == mention["source_text"]
            assert squash(raw(pages, mention["span"])) == squash(source)


def test_no_page_furniture_in_any_heading_or_body(result):
    for row in result["fragments"]:
        texts = [row["heading"], row["body"]]
        if "justification" in row:
            texts.append(row["justification"]["body"])
        for text in texts:
            assert find_page_furniture(text) == [], row["id"]
            assert "Drucksache 19/13824" not in text
            assert not re.search(r"–\s*\d{1,3}\s*–", text), row["id"]


def test_page_one_masthead_is_furniture(result):
    page = next(row for row in result["document"]["extraction"]["pages"]
                if row["pdf_page"] == 1)
    kinds = [row["kind"] for row in page["furniture"]]
    assert kinds == ["drucksache_masthead"]
    vorblatt = next(row for row in result["fragments"]
                    if row["role"] == "vorblatt")
    assert vorblatt["heading"] == "A. Problem und Ziel"
    assert "09.10.2019" not in vorblatt["body"]
    assert vorblatt["complete"] is False     # continues on unextracted page 2


# -- targets vs. mentions ----------------------------------------------------

def test_ziffer_9_explains_draft_norm_with_unverified_correspondence(frags):
    row = frag(frags, "gegenaeusserung:ziffer-9")
    [target] = row["target_norms"]
    expected = {
        "citation": "§ 34 Absatz 3 Satz 2 SGB XIV", "act": "SGB XIV",
        "unit": "para", "number": "34", "absatz": "3", "satz": "2",
        "numbering_basis": "entwurf",
        "canonical_id": "de/bund/sgb_14/para/34/abs/3/satz/2",
        "correspondence_to_enacted_norm_verified": False,
        "new_in_draft": False,
    }
    assert {k: target[k] for k in expected} == expected
    [relation] = row["relations"]
    assert relation["type"] == "explains"
    assert relation["target"]["canonical_id"] == expected["canonical_id"]
    evidence = relation["evidence"]
    assert evidence["source_id"] == row["id"]
    assert evidence["span"] == {"pdf_page": 314,
                                "start": row["heading_span"]["start"],
                                "end": row["heading_span"]["end"]}
    assert evidence["derived_by"] == "parser"
    assert evidence["legal_effect_verified"] is False
    assert evidence["confidence"] == "high"
    assert len(evidence["quoted_context"]) <= 200


def test_ziffer_8_wrapped_multi_target_parenthetical(frags):
    row = frag(frags, "gegenaeusserung:ziffer-8")
    assert row["heading"] == ("Zu Ziffer 8 (Zu Artikel 1, § 34 Absatz 1 Satz 2 "
                              "– neu – , Absatz 2 Satz 1 SGB XIV)")
    targets = [(t["canonical_id"], t["new_in_draft"])
               for t in row["target_norms"]]
    assert targets == [("de/bund/sgb_14/para/34/abs/1/satz/2", True),
                       ("de/bund/sgb_14/para/34/abs/2/satz/1", False)]
    assert [r["type"] for r in row["relations"]] == ["explains", "explains"]
    assert row["body"] == ("Die Bundesregierung stimmt dem Vorschlag des "
                           "Bundesrates zu.")


def test_ziffer_1_body_references_are_mentions_not_explains(frags):
    row = frag(frags, "gegenaeusserung:ziffer-1")
    [target] = row["target_norms"]
    assert (target["act"], target["unit"], target["canonical_id"]) == \
        ("SGB XIV", None, "de/bund/sgb_14")
    assert target["scope"] == "allgemein"
    mentioned = {m["canonical_id"]: m for m in row["mentions"]}
    assert set(mentioned) == {"de/bund/sgb_14/para/117",
                              "de/bund/sgb_14/para/4/abs/4",
                              "de/bund/sgb_14/para/4/abs/5"}
    assert mentioned["de/bund/sgb_14/para/117"]["source_text"] == "§ 117"
    assert mentioned["de/bund/sgb_14/para/4/abs/5"]["source_text"] == \
        "§ 4 Absatz 4 und 5"
    assert all(m["act_basis"] == "context" for m in row["mentions"])
    by_type = {}
    for relation in row["relations"]:
        by_type.setdefault(relation["type"], []).append(
            relation["target"]["canonical_id"])
    assert by_type["explains"] == ["de/bund/sgb_14"]
    assert sorted(by_type["mentions"]) == sorted(mentioned)
    for relation in row["relations"]:
        if relation["type"] == "mentions":
            assert relation["evidence"]["confidence"] == "medium"
            assert relation["evidence"]["span"]["pdf_page"] == 312


def test_target_mentioned_again_in_body_stays_explains(frags):
    # Gegenäußerung Ziffer 5 targets §§ 13 und 14 and repeats them in the body
    row = frag(frags, "gegenaeusserung:ziffer-5")
    assert [t["canonical_id"] for t in row["target_norms"]] == [
        "de/bund/sgb_14/para/13", "de/bund/sgb_14/para/14"]
    assert "Die §§ 13 und 14 erfassen" in row["body"]
    mentioned = {m["canonical_id"] for m in row["mentions"]}
    assert not mentioned & {"de/bund/sgb_14/para/13", "de/bund/sgb_14/para/14"}
    assert "de/bund/sgb_14/para/138" in mentioned


def test_government_positions(frags):
    expected = {"1": "rejects", "2": "agrees", "9": "agrees", "10": "rejects",
                "11": "examines"}
    for number, position in expected.items():
        row = frag(frags, f"gegenaeusserung:ziffer-{number}")
        assert row["government_position"] == position, number
        basis = row["government_position_basis"]
        assert basis["derived_by"] == "parser"
        assert basis["legal_effect_verified"] is False


def test_gegenaeusserung_answer_across_a_page_break(frags, pages):
    row = frag(frags, "gegenaeusserung:ziffer-3")
    assert (row["page_start"], row["page_end"]) == (312, 313)
    assert [s["pdf_page"] for s in row["body_spans"]] == [312, 313]
    assert ("Antragsrecht für die Verwaltungsbehörde des Sozialen "
            "Entschädigungsrechts anstelle der/des Geschädigten") in row["body"]
    assert row["body"].endswith("gestellt werden.")
    assert row["responds_to"] == [f"{DOC_ID}:stellungnahme-bundesrat:ziffer-3"]


# -- Begründung ---------------------------------------------------------------

def test_begruendung_para_37_targets_sgb_xiv_via_artikel_heading(frags, pages):
    row = frag(frags, "begruendung:artikel-1:para-37")
    assert row["role"] == "begruendung"
    assert row["heading"] == "Zu § 37 (Vereinbarungen mit Traumaambulanzen)"
    assert row["section_path"] == ["B. Besonderer Teil", "Zu Artikel 1",
                                   "Zu § 37"]
    [target] = row["target_norms"]
    assert target["canonical_id"] == "de/bund/sgb_14/para/37"
    assert target["act"] == "SGB XIV" and target["numbering_basis"] == "entwurf"
    assert target["act_basis"] == "artikel_heading"
    assert [r["type"] for r in row["relations"]] == ["explains"]
    # the Artikel heading was read on page 169, pages 170-185 are missing
    assert "context_resolved_across_unextracted_pages" in row["notes"]
    assert row["relations"][0]["evidence"]["confidence"] == "medium"
    # § 37 is printed entirely on page 187 (checked against the PDF)
    assert row["page_start"] == row["page_end"] == 187
    assert row["complete"] is True
    assert row["body"].startswith("Um die Ansprüche berechtigter Personen")
    assert row["body"].endswith(
        "Hierdurch soll die Gewinnung geeigneter Traumaambulanzen erleichtert "
        "werden.")
    assert "Zu § 38" not in row["body"]
    assert "Vorschrift stellt klar" in row["body"]      # "Vor-\nschrift"
    assert raw(pages, row["heading_span"]) == row["heading"]


def test_begruendung_neighbours_end_at_the_next_heading(frags):
    para_36 = frag(frags, "begruendung:artikel-1:para-36")
    assert para_36["page_end"] == 186
    assert para_36["body"].endswith("die kürzeste Anfahrt hat.")
    para_38 = frag(frags, "begruendung:artikel-1:para-38")
    assert para_38["body"].startswith("Ein Großteil der Bundesländer")
    assert "Zu Abschnitt 4" not in para_38["body"]
    para_39 = frag(frags, "begruendung:artikel-1:para-39")
    assert para_39["section_path"][-2:] == ["Zu Abschnitt 4", "Zu § 39"]


def test_allgemeiner_teil_is_coarse_and_orphans_are_not_attributed(frags,
                                                                   result):
    row = frag(frags, "begruendung:allgemeiner-teil:i")
    assert row["section_path"] == ["A. Allgemeiner Teil",
                                   "I. Handlungsbedarf und Ziele"]
    assert row["target_norms"] == [] and row["complete"] is False
    viii = frag(frags, "begruendung:allgemeiner-teil:viii")
    assert viii["heading"] == "VIII. Befristung; Evaluierung"
    assert "B. Besonderer Teil" not in viii["body"]
    # the text on page 169 before "VIII." continues an unextracted section
    [orphan] = result["document"]["unassigned_text"]
    assert orphan["pdf_page"] == 169
    assert not any(r["id"].endswith(":begruendung:preamble")
                   for r in result["fragments"])


# -- Stellungnahme des Bundesrates -------------------------------------------

def test_bundesrat_ziffer_3_heading_proposal_and_justification(frags, pages):
    row = frag(frags, "stellungnahme-bundesrat:ziffer-3")
    assert row["role"] == "stellungnahme_bundesrat"
    assert row["heading"] == ("3. Zu Artikel 1 (§ 8 Absatz 3 Satz 3 – neu – "
                              "SGB XIV)")
    assert row["body"].startswith(
        "In Artikel 1 ist dem § 8 Absatz 3 folgender Satz anzufügen:\n„Die "
        "Verwaltungsbehörde kann")
    assert row["body"].endswith("einen Antrag auf Leistungen stellen.“")
    assert "Begründung" not in row["body"] and "Begründung" not in row["heading"]
    just = row["justification"]
    assert just["heading"] == "Begründung:"
    assert just["body"].startswith("Die Regelung in § 8 Absatz 3 entspricht im "
                                   "Wesentlichen der bisherigen Regelung in "
                                   "§ 65 des Bundesversorgungsgesetzes.")
    assert just["body"].endswith("einen Antrag zu stellen.")
    body_end = row["body_spans"][-1]["end"]
    assert body_end < just["heading_span"]["start"] < \
        just["body_spans"][0]["start"]
    [target] = row["target_norms"]
    assert target["canonical_id"] == "de/bund/sgb_14/para/8/abs/3/satz/3"
    assert target["new_in_draft"] is True
    [mention] = row["mentions"]
    assert (mention["act"], mention["number"], mention["part"]) == \
        ("BVG", "65", "justification")
    assert mention["canonical_id"] is None       # BVG is not on GII any more


def test_bundesrat_ziffer_2_source_typo_is_kept_and_noted(frags):
    row = frag(frags, "stellungnahme-bundesrat:ziffer-2")
    assert row["heading"] == ("2. Zu Artike1 (§ 5 Absatz 1 Satz 6 und 7 "
                              "SGB XIV)")
    assert [t["canonical_id"] for t in row["target_norms"]] == [
        "de/bund/sgb_14/para/5/abs/1/satz/6",
        "de/bund/sgb_14/para/5/abs/1/satz/7"]
    assert any("Artike1" in note for note in row["notes"])
    assert row["body"] == (
        "In Artikel 1 ist § 5 Absatz 1 wie folgt zu ändern:\n"
        "a) In Satz 6 ist der Punkt am Ende durch die Wörter „, soweit damit "
        "keine Schlechterstellung der Kinder und Jugendlichen verbunden ist.“ "
        "zu ersetzen.\nb) Satz 7 ist zu streichen.")


def test_bundesrat_preamble_mentions_grundgesetz(frags):
    row = frag(frags, "stellungnahme-bundesrat:preamble")
    assert row["heading"] == "Stellungnahme des Bundesrates"
    [mention] = row["mentions"]
    assert mention["canonical_id"] == "de/bund/gg/art/76/abs/2"
    assert mention["numbering_basis"] == "as_cited_at_document_date"


# -- adoption status -----------------------------------------------------------

def test_adoption_status_never_claims_adoption_of_a_passage(result):
    for row in result["fragments"]:
        assert row["procedure_stage"] == \
            "bundestag_first_passage/regierungsentwurf"
        assert row["adoption_status"] == \
            "procedure_promulgated_passage_correspondence_unverified"
        basis = row["adoption_basis"]
        assert basis["passage_correspondence_verified"] is False
        assert basis["official_justification_of_enacted_norm"] is False
        assert basis["promulgation"] == "BGBl. I S. 2652"
        assert basis["dip_vorgang_id"] == "251749"
        for target in row["target_norms"]:
            assert target["correspondence_to_enacted_norm_verified"] is False
        for relation in row["relations"]:
            assert relation["evidence"]["legal_effect_verified"] is False
            assert relation["type"] in {"explains", "mentions"}


# -- citation grammar ----------------------------------------------------------

CTX = _Ctx(dict(SOURCES["act_slugs"]), {"SGB XIV"}, {"1": "SGB XIV"})


def _ids(area: str) -> list[str | None]:
    targets, _ = heading_targets(area, CTX)
    return [t["canonical_id"] for t in targets]


def test_heading_grammar_ranges_lists_and_act_level_targets():
    assert _ids("Zu Artikel 1, §§ 1 bis 3 SGB XIV") == [
        "de/bund/sgb_14/para/1", "de/bund/sgb_14/para/2",
        "de/bund/sgb_14/para/3"]
    assert _ids("Zu Artikel 1 SGB XIV allgemein") == ["de/bund/sgb_14"]
    targets, _ = heading_targets(
        "Zu Artikel 1, § 148 Absatz 1a – neu -, 1b – neu -, 1c – neu – "
        "SGB XIV", CTX)
    assert [(t["absatz"], t["new_in_draft"]) for t in targets] == [
        ("1a", True), ("1b", True), ("1c", True)]
    assert _ids("Zu Artikel 1, § 127 Absatz 1 Nummer 2, 4 Buchstabe a, "
                "Nummer 5, § 128 Nummer 1 und 2 SGB XIV") == [
        "de/bund/sgb_14/para/127/abs/1/nr/2",
        "de/bund/sgb_14/para/127/abs/1/nr/4/buchst/a",
        "de/bund/sgb_14/para/127/abs/1/nr/5",
        "de/bund/sgb_14/para/128/nr/1", "de/bund/sgb_14/para/128/nr/2"]


def test_heading_grammar_multiple_artikel_groups_with_other_acts():
    # Gegenäußerung Ziffer 36 (page 321), whose parenthetical the source
    # leaves unbalanced.
    targets, _ = heading_targets(
        "Zu Artikel 6 Nummer 3 (§ 88 SVG), Artikel 16 Nummer 22 (§ 220 SGG), "
        "Artikel 56 Nummer 3 (§ 194 Absatz 6 VwGO", CTX)
    assert [(t["citation"], t["canonical_id"], t["draft_locator"]["artikel"])
            for t in targets] == [
        ("§ 88 SVG", None, "6"),
        ("§ 220 SGG", "de/bund/sgg/para/220", "16"),
        ("§ 194 Absatz 6 VwGO", "de/bund/vwgo/para/194/abs/6", "56")]
    assert all(t["numbering_basis"] == "as_cited_at_document_date"
               for t in targets)


def test_unbalanced_source_parenthetical_does_not_swallow_the_body():
    lines = [
        "Drucksache 19/13824                                 – 321 –"
        "             Deutscher Bundestag – 19. Wahlperiode",
        "", "",
        "Zu Ziffer 36 (Zu Artikel 6 Nummer 3 (§ 88 SVG), Artikel 16 Nummer 22 "
        "(§ 220 SGG), Artikel 56 Num-",
        "mer 3 (§ 194 Absatz 6 VwGO)",
        "Die Bundesregierung stimmt dem Vorschlag des Bundesrates in der Sache "
        "zu und wird im weiteren Verlauf des",
        "Gesetzgebungsverfahrens einen Formulierungsvorschlag vorlegen.",
        "", "",
        "Zu Ziffer 37 (Zu Artikel 9 (Nummer 6 – neu – (§ 25a Absatz 9 – neu – "
        "HHG), Artikel 12 Nummer 6 – neu",
        "– (§ 26 Absatz 4 – neu – StrRehaG)",
        "Zu Artikel 9 Nummer 6:",
        "Die Bundesregierung stimmt dem Vorschlag des Bundesrates nicht zu.",
    ]
    page = "\n".join(lines) + "\n\f"
    head = ("Deutscher Bundestag – 19. Wahlperiode – 320 – Drucksache 19/13824"
            "\n\n\nAnlage 4\n\n\nGegenäußerung der Bundesregierung zur "
            "Stellungnahme des Bundesrates\n\f")
    out = segment_drucksache([head, page], drucksache="19/13824",
                             document_meta={"type": "Gesetzentwurf",
                                            "initiator": "Bundesregierung"},
                             act_slugs=SOURCES["act_slugs"])
    rows = {r["id"].rsplit(":", 1)[1]: r for r in out["fragments"]}
    assert rows["ziffer-36"]["heading"].endswith(
        "Artikel 56 Nummer 3 (§ 194 Absatz 6 VwGO)")
    assert rows["ziffer-36"]["body"].startswith("Die Bundesregierung stimmt")
    assert rows["ziffer-36"]["government_position"] == "agrees"
    assert "source_parenthetical_unbalanced" in rows["ziffer-36"]["notes"]
    assert rows["ziffer-37"]["heading"].endswith("– neu – StrRehaG)")
    assert rows["ziffer-37"]["body"].startswith("Zu Artikel 9 Nummer 6:\n")
    assert rows["ziffer-37"]["government_position"] == "rejects"


def test_parse_citation_for_api_targets():
    [row] = parse_citation("§ 37 SGB XIV", SOURCES["act_slugs"])
    assert row["canonical_id"] == "de/bund/sgb_14/para/37"
    [row] = parse_citation("SGB XIV", SOURCES["act_slugs"])
    assert row["canonical_id"] == "de/bund/sgb_14"
    assert parse_citation("§ 37", SOURCES["act_slugs"]) == []


# -- API -----------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path, result, monkeypatch):
    monkeypatch.delenv("KERF_MATERIALS_DIR", raising=False)
    write_store(tmp_path, result, SOURCES["act_slugs"])
    return tmp_path


def _client(data_dir: Path) -> TestClient:
    app = FastAPI()
    app.include_router(create_router(lambda: data_dir))
    return TestClient(app)


def test_api_search_ok_envelope(store):
    res = _client(store).get("/materials", params={"drucksache": "19/13824",
                                                   "role": "gegenaeusserung_"
                                                           "bundesregierung",
                                                   "limit": 5})
    assert res.status_code == 200
    body = res.json()
    assert body["schema"] == "kerf-evidence-envelope/1"
    assert body["status"] == "ok" and body["total"] == 12
    assert len(body["evidence"]) == 5
    assert body["searched"] == [{"source": "materials:19/13824",
                                 "status": "ok", "count": 12}]
    item = body["evidence"][0]
    assert item["id"] == item["canonical_id"] == \
        f"{DOC_ID}:gegenaeusserung:preamble"
    assert item["source"]["url"] == (
        "https://dserver.bundestag.de/btd/19/138/1913824.pdf#page=312")
    assert item["source"]["role"] == "gegenaeusserung_bundesregierung"
    assert item["quality"]["source_exact"] is True
    assert item["quality"]["reconstruction_status"] == "not_reconstructed"
    assert item["temporal"] == {
        "procedure_stage": "bundestag_first_passage/regierungsentwurf",
        "document_date": "2019-10-09"}


def test_api_relation_filter_separates_explains_and_mentions(store):
    client = _client(store)
    explains = client.get("/materials", params={
        "target": "de/bund/sgb_14/para/34", "relation": "explains"}).json()
    assert explains["status"] == "ok"
    assert [e["id"].rsplit(":", 1)[1] for e in explains["evidence"]] == [
        "para-34", "ziffer-8", "ziffer-9"]
    ziffer_9 = explains["evidence"][2]
    assert ziffer_9["pinpoint"] == {
        "drucksache": "19/13824", "pdf_page": 314, "printed_page": 314,
        "pdf_page_end": 314,
        "heading": "Zu Ziffer 9 (Zu Artikel 1, § 34 Absatz 3 Satz 2 SGB XIV)"}
    assert [r["type"] for r in ziffer_9["matched_relations"]] == ["explains"]
    mentions = client.get("/materials", params={
        "target": "§ 117 SGB XIV", "relation": "mentions"}).json()
    assert [e["id"] for e in mentions["evidence"]] == [
        f"{DOC_ID}:gegenaeusserung:ziffer-1"]
    none = client.get("/materials", params={
        "target": "de/bund/sgb_14/para/117", "relation": "explains"}).json()
    assert none["status"] == "ok" and none["total"] == 0
    # a prefix matches whole path segments only (para/3 is not para/34)
    assert client.get("/materials", params={
        "target": "de/bund/sgb_14/para/3"}).json()["total"] == 0


def test_api_not_in_corpus_and_invalid_arguments(store):
    client = _client(store)
    res = client.get("/materials", params={"drucksache": "19/99999"})
    assert res.status_code == 404
    assert res.json()["status"] == "not_in_corpus"
    assert res.json()["total"] is None
    res = client.get("/materials", params={"drucksache": "19/13824",
                                           "foo": "1"})
    assert res.status_code == 422
    body = res.json()
    assert body["status"] == "invalid_argument"
    assert body["unsupported_arguments"] == ["foo"]
    res = client.get("/materials", params={"at": "2020-01-01"})
    assert res.status_code == 422
    assert res.json()["status"] == "unsupported_temporal_request"
    for params in ({"limit": "500"}, {"role": "kommentar"},
                   {"relation": "cites"}, {"target": "§ 37"}):
        res = client.get("/materials", params=params)
        assert res.status_code == 422, params
        assert res.json()["status"] == "invalid_argument"


def test_api_unavailable_when_store_missing(tmp_path):
    res = _client(tmp_path).get("/materials")
    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "unavailable" and body["total"] is None
    (tmp_path / "materials").mkdir()
    (tmp_path / "materials" / "index.json").write_text("{not json")
    res = _client(tmp_path).get("/materials")
    assert res.status_code == 503 and res.json()["status"] == "unavailable"


def test_store_is_one_sqlite_database_without_json_documents(store):
    files = sorted(p.name for p in (store / "materials").iterdir())
    assert files == ["index.json", "materials.sqlite"]
    index = json.loads((store / "materials" / "index.json").read_text())
    assert index["schema"] == "kerf-materials-index/2"
    database = store / "materials" / "materials.sqlite"
    assert index["database"]["sha256"] == \
        hashlib.sha256(database.read_bytes()).hexdigest()
    [entry] = index["documents"]
    assert entry["fragment_count"] == 31


def test_served_fragments_equal_parser_output(store, result):
    client = _client(store)
    served = []
    for offset in (0, 20):
        body = client.get("/materials", params={"drucksache": "19/13824",
                                                "limit": 20,
                                                "offset": offset}).json()
        served += [item["id"] for item in body["evidence"]]
    assert served == [row["id"] for row in result["fragments"]]
    for row in result["fragments"]:
        res = client.get(f"/materials/fragments/{row['id']}").json()
        assert res["fragment"] == row, row["id"]


def test_builder_output_is_deterministic_across_build_orders(tmp_path, pages,
                                                              result):
    other = segment_drucksache(pages, drucksache="19/13825",
                               document_meta=_document_meta(),
                               act_slugs=SOURCES["act_slugs"])
    # the running headers name 19/13824: recorded, not silently accepted
    assert other["document"]["warnings"]
    first, second, again = (tmp_path / "a", tmp_path / "b", tmp_path / "c")
    write_store(first, result, SOURCES["act_slugs"])
    write_store(first, other, SOURCES["act_slugs"])
    write_store(second, other, SOURCES["act_slugs"])
    write_store(second, result, SOURCES["act_slugs"])
    write_store(again, result, SOURCES["act_slugs"])
    write_store(again, other, SOURCES["act_slugs"])
    write_store(again, result, SOURCES["act_slugs"])       # rebuild: idempotent
    blobs = [(d / "materials" / name).read_bytes() for d in (first, second,
                                                            again)
             for name in ("materials.sqlite", "index.json")]
    assert blobs[0] == blobs[2] == blobs[4]
    assert blobs[1] == blobs[3] == blobs[5]
    index = json.loads(blobs[1])
    assert [d["drucksache"] for d in index["documents"]] == ["19/13824",
                                                           "19/13825"]
    res = _client(first).get("/materials", params={"drucksache": "19/13825"})
    assert res.json()["total"] == 31


def test_api_integrity_check_failed_on_sha_mismatch(store):
    client = _client(store)
    assert client.get("/materials").json()["status"] == "ok"
    path = store / "materials" / "materials.sqlite"
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01                           # same size
    path.write_bytes(bytes(data))
    res = client.get("/materials", params={"drucksache": "19/13824"})
    assert res.status_code == 503
    body = res.json()
    assert body["status"] == "integrity_check_failed"
    assert body["total"] is None
    assert body["evidence"] == []
    res = client.get(f"/materials/fragments/{DOC_ID}:gegenaeusserung:ziffer-9")
    assert res.json()["status"] == "integrity_check_failed"
    out = materials_for_norm(store, "de/bund/sgb_14/para/37")
    assert out["status"] == "integrity_check_failed" and out["explains"] == []


def test_api_unavailable_when_database_missing(store):
    (store / "materials" / "materials.sqlite").unlink()
    res = _client(store).get("/materials")
    assert res.status_code == 503
    assert res.json()["status"] == "unavailable"
    assert res.json()["total"] is None


def test_materials_dir_override(store, tmp_path_factory, monkeypatch):
    elsewhere = tmp_path_factory.mktemp("no-store-here")
    monkeypatch.setenv("KERF_MATERIALS_DIR", str(store / "materials"))
    res = _client(elsewhere).get("/materials", params={"limit": 1})
    assert res.json()["status"] == "ok" and res.json()["total"] == 31
    assert materials_for_norm(elsewhere, "de/bund/sgb_14/para/37")["status"] \
        == "ok"
    monkeypatch.setenv("KERF_MATERIALS_DIR", str(elsewhere))
    assert _client(store).get("/materials").status_code == 503


def test_api_fragment_route(store):
    client = _client(store)
    res = client.get(f"/materials/fragments/{DOC_ID}:gegenaeusserung:ziffer-9")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok" and body["total"] == 1
    assert body["fragment"]["body"] == ("Die Bundesregierung stimmt dem "
                                        "Vorschlag des Bundesrates zu.")
    assert body["evidence"][0]["government_position"] == "agrees"
    res = client.get(f"/materials/fragments/{DOC_ID}:gegenaeusserung:ziffer-99")
    assert res.status_code == 404
    assert res.json()["status"] == "not_found_in_searched_scope"
    res = client.get("/materials/fragments/bt-drs-19-1:x")
    assert res.status_code == 404 and res.json()["status"] == "not_in_corpus"
    res = client.get(f"/materials/fragments/{DOC_ID}:gegenaeusserung:ziffer-9",
                     params={"x": "1"})
    assert res.status_code == 422


def test_materials_for_norm_splits_explains_and_mentions(store, tmp_path_factory):
    out = materials_for_norm(store, "de/bund/sgb_14/para/37")
    assert out["status"] == "ok"
    explains = {row["id"].split(":", 1)[1] for row in out["explains"]}
    assert explains == {"begruendung:artikel-1:para-37",
                        "gegenaeusserung:ziffer-11"}
    assert out["mentions"] == [] and out["drafts"] == []
    assert out["searched"] == [{"source": "materials:19/13824",
                                "status": "ok", "count": 2}]
    four = materials_for_norm(store, "de/bund/sgb_14/para/4")
    assert [row["id"].split(":", 1)[1] for row in four["mentions"]] == [
        "stellungnahme-bundesrat:ziffer-1", "gegenaeusserung:ziffer-1"]
    # the Bundesrat cites "§ 4 SGB XIV-E" in its justification
    [br] = four["mentions"][0]["matched_relations"]
    assert br["evidence"]["derived_from"] == "justification"
    assert br["target"]["numbering_basis"] == "entwurf"
    assert four["explains"] == []
    assert out["totals"] == {"explains": 2, "mentions": 0, "drafts": 0}
    capped = materials_for_norm(store, "de/bund/sgb_14/para/37",
                                limit_per_relation=1)
    assert len(capped["explains"]) == 1 and capped["truncated"] is True
    assert capped["totals"]["explains"] == 2
    missing = materials_for_norm(tmp_path_factory.mktemp("empty"),
                                 "de/bund/sgb_14/para/37")
    assert missing["status"] == "unavailable"
    assert missing["explains"] == [] and missing["searched"]
