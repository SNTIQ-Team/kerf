import hashlib
from pathlib import Path

import pytest

from pipeline.fetch_bayern_recht import CORPUS, parse_registered_act, write_snapshot
from pipeline.reader_source_cache import reader_act_id

ZIP = Path(__file__).parent / "fixtures/bayern/BayOrgGAG-20261118.zip"
REGISTER = {
    "doc_key": "BayOrgGAG", "bayrs_nr": "32-1-A", "title": "ArbGOrgG",
    "chain": [{"seq": 5, "description": "Überschrift und mehrfach geänd.",
               "gvbl_citation": "§ 4 G v. 09.01.2018, S. 2, 39", "date": "2018-01-09"}],
}


def test_current_bavarian_corpus_has_unique_public_and_source_ids():
    assert len(CORPUS) == len(set(CORPUS.values())) == len({reader_act_id("by", a) for a in CORPUS})
    assert {"AufnG", "BayEUG", "GO", "BayBO", "AGSGG", "BayBGG", "BaySchO"} <= CORPUS.keys()


def test_original_xml_identity_amendments_and_annex_table():
    raw = ZIP.read_bytes()
    a, norms, changes, _, xml = parse_registered_act(raw, REGISTER, "ArbGOrgG")
    assert a["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert a["xml_sha256"] == hashlib.sha256(xml).hexdigest()
    assert a["source_url"] == "https://www.gesetze-bayern.de/Content/Zip/BayOrgGAG"
    assert a["includes_xml_annexes"] and [n["enbez"] for n in norms] == ["Art. 1", "Art. 2", "Anlage"]
    assert norms[-1]["norm_kind"] == "annex"
    assert "Landesarbeitsgericht" in norms[-1]["text"] and "Nürnberg" in norms[-1]["text"]
    assert [v for v in changes if v["source"] == "ffn"] == [
        {**REGISTER["chain"][0], "jurabk": "ArbGOrgG", "source": "ffn"}]
    with pytest.raises(ValueError, match="abbreviation or BayRS"):
        parse_registered_act(raw, {**REGISTER, "bayrs_nr": "other"}, "ArbGOrgG")


def test_failed_ffn_or_zip_cannot_produce_snapshot_tables(tmp_path):
    with pytest.raises(ValueError, match="empty or ambiguous"):
        write_snapshot(tmp_path, ["GO"], lambda *_: b"<html>Security check</html>", "2026-11-18")
    assert not list(tmp_path.glob("*.jsonl"))
