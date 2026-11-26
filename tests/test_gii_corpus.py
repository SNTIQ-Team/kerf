from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from fetch_gii import (  # noqa: E402
    CORPUS,
    PRACTICE_EXPANSION_2026_07,
    PRACTICE_EXPANSION_2026_09,
    PRACTICE_EXPANSION_2026_11,
    require_complete_corpus,
)


def test_practice_expansion_is_large_and_not_silently_shrunk() -> None:
    assert len(PRACTICE_EXPANSION_2026_07) == 74
    assert len(PRACTICE_EXPANSION_2026_11) == 56
    assert "urhg" in PRACTICE_EXPANSION_2026_11
    assert len(CORPUS) == 256
    assert len(CORPUS - PRACTICE_EXPANSION_2026_07 - PRACTICE_EXPANSION_2026_09
               - PRACTICE_EXPANSION_2026_11) == 51


def test_september_expansion_keeps_social_rules_and_adds_everyday_domains() -> None:
    assert len(PRACTICE_EXPANSION_2026_09) == 75
    assert not (PRACTICE_EXPANSION_2026_09 & PRACTICE_EXPANSION_2026_07)
    assert {
        "bshg_76dv", "bshg_88abs2dv_1988", "versmedv", "schwbawv",
        "sozsichabkg_ukr", "hgb", "vvg_2008", "uklag", "rdg",
        "stvg", "stvo_2013", "fev_2010", "ustg_1980", "heizkostenv",
        "ifsg", "pflbg", "zag_2018",
    } <= PRACTICE_EXPANSION_2026_09


def test_practice_expansion_covers_priority_domains() -> None:
    required = {
        "migration": {
            "asylzbv_2026", "azrg-dv", "bmg", "pauswg", "deuf_v",
        },
        "social_and_housing": {
            "algiiv_2008", "sozhidav_2019", "wogv", "wofg", "pflegezg",
        },
        "family_and_work": {
            "famfg", "muschg_2018", "arbgg", "betrvg", "aentg_2009",
        },
        "courts_and_procedure": {
            "bverfgg", "gvg", "vwvg", "bdsg_2018", "fgo", "ao_1977",
        },
    }
    for slugs in required.values():
        assert slugs <= PRACTICE_EXPANSION_2026_07


def test_default_refresh_rejects_a_shrunken_official_catalog() -> None:
    links = {slug: f"https://example.test/{slug}.xml.zip"
             for slug in CORPUS - {"bverfgg"}}

    try:
        require_complete_corpus(CORPUS, links)
    except RuntimeError as exc:
        assert "bverfgg" in str(exc)
    else:
        raise AssertionError("a missing configured act must abort the refresh")


def test_explicit_selection_cannot_replace_daily_data_and_keeps_its_own_cache(tmp_path, monkeypatch):
    import io
    import json
    import zipfile
    from datetime import date
    from unittest.mock import Mock

    import common
    import fetch_gii
    import requests

    monkeypatch.setattr(common, "SNAPSHOTS", tmp_path / "data" / "snapshots")
    monkeypatch.setattr(fetch_gii, "ROOT", tmp_path)
    monkeypatch.setattr(fetch_gii, "CORPUS", {"hgb", "bgb"})
    master = "<items>" + "".join(
        f"<item><title>{slug}</title><link>https://www.gesetze-im-internet.de/{slug}/xml.zip</link></item>"
        for slug in sorted(fetch_gii.CORPUS)) + "</items>"

    def get(url, **_kwargs):
        response = requests.Response()
        response.status_code = 200
        if url == fetch_gii.TOC_URL:
            response._content = master.encode()
        else:
            slug = url.split("/")[-2]
            xml = (f'<dokumente doknr="A-{slug}" builddate="20260101"><norm doknr="N-{slug}">'
                   f'<metadaten><jurabk>{slug.upper()}</jurabk><enbez>§ 1</enbez></metadaten>'
                   f'<textdaten><text><Content><P>{slug}</P></Content></text></textdaten></norm></dokumente>')
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as package:
                package.writestr(slug + ".xml", xml)
            response._content = stream.getvalue()
        response._content_consumed = True
        return response

    http = Mock()
    http.get = get
    monkeypatch.setattr(fetch_gii, "Http", lambda **_kwargs: http)
    monkeypatch.setattr(sys, "argv", ["fetch_gii"])
    assert fetch_gii.main() == 0
    data = tmp_path / "data"
    original = {path: path.read_bytes() for path in data.rglob("*") if path.is_file()}
    today = date.today().isoformat()
    for selection, expected in [("hgb", {"hgb"}), ("hgb, bgb", {"hgb", "bgb"}), ("bgb,hgb", {"hgb", "bgb"})]:
        monkeypatch.setattr(sys, "argv", ["fetch_gii", "--slugs", selection])
        assert fetch_gii.main() == 0
        roots = list((data / "gii_probes").iterdir())
        matching = [root for root in roots if {
            json.loads(row)["slug"] for row in (root / "snapshots" / today / "acts.jsonl").read_text().splitlines()
        } == expected]
        assert len(matching) == 1
        assert (matching[0] / "gii_import_cache" / "hgb.json.gz").is_file()
        assert (matching[0] / "reader-source" / "fed_hgb.xml").is_file()
    assert len(roots) == 2
    monkeypatch.setattr(sys, "argv", ["fetch_gii", "--slugs", "bgb,unknown"])
    assert fetch_gii.main() == 1
    assert len(list((data / "gii_probes").iterdir())) == 2
    assert all(p.read_bytes() == old for p, old in original.items())
