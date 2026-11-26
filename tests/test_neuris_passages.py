import hashlib
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.decision_passages import create_router
from tools.decision_passages import build_index
from tools.official_decision_sources import extract_official, read_capture

PROFILE = "neuris-akn/1"
DOC = "JURE260000001"
URL = f"https://testphase.rechtsinformationen.bund.de/v1/rechtsprechung/{DOC}.xml"
ROW = {"id": "neuris-" + DOC.lower(), "court_short": "BSG", "az": "B 1 X 1/26 R",
       "date": "2026-01-01", "kind": "Beschluss", "document_number": DOC, "url": URL,
       "case_numbers": ["B 1 X 1/26 R"], "ecli": None}
XML = f'''<akomaNtoso xmlns="http://docs.oasis-open.org/legaldocml/ns/akn/3.0"
 xmlns:r="http://rechtsinformationen.bund.de/schema/ris/0.1"><judgment><meta>
 <identification><FRBRWork><FRBRthis value="{DOC}"/><FRBRdate name="entscheidungsdatum" date="2026-01-01"/>
 </FRBRWork></identification><proprietary><r:meta><r:dokumenttyp>Beschluss</r:dokumenttyp>
 <r:gericht><r:gerichtstyp>BSG</r:gerichtstyp></r:gericht><r:aktenzeichenListe><r:aktenzeichen>B 1 X 1/26 R</r:aktenzeichen>
 </r:aktenzeichenListe></r:meta></proprietary></meta><judgmentBody><motivation r:domainTerm="Gründe">
 <p>2. Eine Überschrift.</p><hcontainer name="Randnummer" eId="randnummer-9"><num>9</num>
 <content><p>Ein <i>wörtlicher</i> Satz.</p><p>Weiter.</p></content></hcontainer><heading>II.</heading>
 </motivation></judgmentBody></judgment></akomaNtoso>'''.encode()


def capture(folder: Path, blob=XML, url=URL):
    h = hashlib.sha256(blob).hexdigest()
    (folder / (h + ".source")).write_bytes(blob)
    (folder / (ROW["id"] + ".json")).write_text(json.dumps({
        "decision_id": ROW["id"], "profile": PROFILE, "url": url, "sha256": h,
        "observed_at": "2026-01-02T10:00:00+00:00", "media_type": "application/xml"}))


def test_source_rn_lookup_search_download_and_observation(tmp_path):
    capture(tmp_path)
    generation = tmp_path / "output"
    build_index([ROW], tmp_path, generation, observed_at="2026-01-03T00:00:00+00:00", official_cache=tmp_path)
    app = FastAPI()
    app.include_router(create_router(lambda: generation))
    client = TestClient(app)
    hit = client.get("/decision-passages", params={"decision_id": ROW["id"], "q": "wörtlicher"}).json()
    assert hit["total"] == 1
    path = f"/decisions/{ROW['id']}/passages/" + hit["passages"][0]["passage_id"]
    result = client.get(path).json()
    assert (result["paragraph_label"], result["source_anchor"]) == ("9", "randnummer-9")
    assert result["text"] == "Ein wörtlicher Satz. Weiter."
    assert result["xml_path"].endswith("/akn:hcontainer[1]")
    assert result["source"]["xml_sha256"] == hashlib.sha256(XML).hexdigest()
    data = client.get("/decision-passages", params={"decision_id": ROW["id"]}).json()
    assert [p["paragraph_label"] for p in data["passages"]] == [None, "9", None]
    assert client.get(f"/decisions/{ROW['id']}/source").content == XML
    observation = client.get("/decision-observations").json()["observations"][0]
    assert observation["document_type"] == "Beschluss"
    assert observation["ecli"] is None


@pytest.mark.parametrize("patch", [{"court_short": "BGH"}, {"date": "2026-01-02"}, {"case_numbers": ["X"]},
                                    {"ecli": "ECLI:DE:BSG:2026:xxx"}, {"kind": "Urteil"}, {"document_number": "OTHER"}])
def test_publication_must_match_source_identity(patch):
    with pytest.raises(ValueError):
        extract_official(XML, ROW | patch, PROFILE)


def test_duplicate_rn_or_unresolved_image_holds_release():
    for changed in [XML.replace(b"<num>9</num>", b"<num>9</num><num>10</num>"),
                    XML.replace(b"<p>Weiter.</p>", b'<p><img src="fig.jpg"/></p>')]:
        with pytest.raises(ValueError):
            extract_official(changed, ROW, PROFILE)


def test_source_url_must_name_same_publication(tmp_path):
    capture(tmp_path, url=URL.replace(DOC, "OTHER"))
    with pytest.raises(ValueError):
        read_capture(tmp_path, ROW)


def test_integration_holds_docket_and_ecli_collisions():
    from tools.neuris_passages import add_selected
    assert add_selected([], [ROW]) == [ROW]
    same_docket = dict(ROW, id="manual", case_numbers=["B 1 X 1/26 R"])
    same_ecli = dict(ROW, id="other", az="Other", case_numbers=["Other"], ecli="ECLI:DE:BSG:2026:ABC")
    for existing, new in ((same_docket, ROW), (same_ecli, dict(ROW, ecli="ECLI:DE:BSG:2026:abc"))):
        with pytest.raises(ValueError, match="conflicting identifiers"):
            add_selected([existing], [new])


def test_selected_missing_capture_fails_before_replacing_index(tmp_path):
    capture(tmp_path)
    root = tmp_path / "generation"
    opts = {"observed_at": "2026-01-03T00:00:00+00:00", "neuris_cache": tmp_path}
    build_index([ROW], tmp_path, root, **opts)
    previous = (root / "decision_passages.sqlite").read_bytes()
    (tmp_path / (ROW["id"] + ".json")).unlink()
    with pytest.raises(ValueError, match="lacks its source capture"):
        build_index([ROW], tmp_path, root, **opts)
    assert (root / "decision_passages.sqlite").read_bytes() == previous


def test_same_rn_with_two_source_anchors_is_held():
    start = XML.index(b'<hcontainer')
    end = XML.index(b'</hcontainer>') + len(b'</hcontainer>')
    numbered = XML[start:end]
    with pytest.raises(ValueError, match="duplicate NeuRIS Rn"):
        extract_official(XML[:end] + numbered.replace(b'randnummer-9', b'another-anchor') + XML[end:], ROW, PROFILE)


def test_unlisted_or_changed_selected_sources_are_held(tmp_path):
    from tools.neuris_passages import selected_snapshot
    cache = tmp_path / "official_decisions"
    cache.mkdir()
    capture(cache)
    (tmp_path / "decisions.jsonl").write_text(json.dumps(ROW) + "\n")
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({DOC: hashlib.sha256(XML).hexdigest()}))
    assert selected_snapshot(tmp_path) == [ROW]
    selection.write_text(json.dumps({DOC: "0" * 64}))
    with pytest.raises(ValueError, match="receipt differs"):
        selected_snapshot(tmp_path)
    selection.write_text(json.dumps({"UNREVIEWED": "0" * 64}))
    with pytest.raises(ValueError, match="incomplete"):
        selected_snapshot(tmp_path)
