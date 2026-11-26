"""The treaty edition CELEX identifies the legal snapshot separately from later corrections."""
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import fetch_eu_texts as eu


TEU = "02016M/TXT-20160901"
TFEU = "02016E/TXT-20160901"


def article(i, text):
    return f"<ARTICLE><TI.ART>Artikel {i}</TI.ART><ALINEA>{text}</ALINEA></ARTICLE>"


def tree(target="E", start="20160901", missing=False):
    full_names = {"E": "Konsolidierte Fassung des Vertrags über die Arbeitsweise der Europäischen Union",
                  "M": "Konsolidierte Fassung des Vertrags über die Europäische Union"}
    stop = {"E": 359, "M": 56}[target] - int(missing)
    articles = "".join(article(i, f"Article {i} wording") for i in range(1, stop))
    protocol = "<CONS.DOC.GEN><TITLE>Protokoll</TITLE><ENACTING.TERMS>" + article(1, "Protocol wording") + "</ENACTING.TERMS></CONS.DOC.GEN>"
    return (f'<CONS.OTH><INFO.CONSLEG START.DATE="{start}" CONSLEG.DATE="20170213" DATE.LAST.MOD="20170223"/>'
            '<CONS.OTH.DOC><CORRIG><NO.CELEX>12016E/TXTR(02)</NO.CELEX><BIB.INSTANCE.CONS>'
            '<DATE ISO="20161028"/></BIB.INSTANCE.CONS></CORRIG>'
            f'<CONS.DOC.GEN><TITLE><TI><P>{full_names[target]}</P></TI></TITLE>'
            f'<ENACTING.TERMS>{articles}</ENACTING.TERMS>{protocol}</CONS.DOC.GEN></CONS.OTH.DOC></CONS.OTH>').encode()


def test_scoped_treaty_articles_keep_dates_and_exclude_protocols():
    result = eu.parse_treaty_edition(tree(), TFEU)
    assert len(result["articles"]) == 358
    assert result["articles"][0]["text"] == "Article 1 wording"
    assert result["consolidation_date"] == "2016-09-01"
    assert result["formex_report_date"] == "2017-02-13"
    assert result["formex_last_modification"] == "2017-02-23"
    assert result["corrigenda"][0]["date"] == "2016-10-28"


def test_official_zip_keeps_only_the_main_treaty():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("treaty.xml", tree("M"))
        archive.writestr("treaty.doc.xml", b"<NOTICE/>")
    parsed = eu.parse_treaty_edition(output.getvalue(), TEU)
    assert len(parsed["articles"]) == 55
    assert parsed["articles"][0]["text"] == "Article 1 wording"


@pytest.mark.parametrize("raw,edition", [
    (tree("E"), TEU), (tree("E", start="20190501"), TFEU), (tree("E", missing=True), TFEU),
    (b"<ACT/>", TFEU), (tree("M"), "12016M"), (tree("M"), "02016M/TXT-20161301"),
])
def test_wrong_date_scope_identity_and_partial_editions_are_rejected(raw, edition):
    with pytest.raises(ValueError):
        eu.parse_treaty_edition(raw, edition)


def test_explicit_reader_never_writes_a_shared_snapshot(tmp_path, monkeypatch, capsys):
    from hashlib import sha256
    raw = tree()
    url = eu.CELLAR + "test.0001.01/DOC_2"
    monkeypatch.setattr(eu, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(eu, "manifestations", lambda _cellar, _celex: {TFEU: {"fmx4": "official"}})
    monkeypatch.setattr(eu, "manifestation_items", lambda _cellar, _manifestation: [url])
    monkeypatch.setattr(eu.Cellar, "get", lambda self, item: {
        "bytes": raw, "url": item, "sha256": sha256(raw).hexdigest(), "retrieved_at": "2026-11-26"})
    assert eu.main(["--treaty-edition", TFEU, "--article", "15"]) == 0
    response = json.loads(capsys.readouterr().out)
    assert response["article"]["enbez"] == "Art. 15"
    assert response["source"]["url"] == url and response["source"]["sha256"] == sha256(raw).hexdigest()
    assert "articles" not in response
    monkeypatch.setattr(eu, "manifestations", lambda *_: pytest.fail("offline lookup contacted CELLAR"))
    assert eu.main(["--treaty-edition", TFEU, "--offline", "--article", "15"]) == 0
    assert json.loads(capsys.readouterr().out) == response
    assert eu.main(["--treaty-edition", TFEU, "--article", "359"]) == 1
    assert not capsys.readouterr().out
