from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from fetch_gii import parse_law
from gii_html_parser import parse_gii_xml_zip
from gii_import_cache import GiiImportCache
from pipeline.fetch_gii_toc import parse_toc
from api.act_archive import render_markdown_snapshot
from api.norm_identity import gii_norm_url
from tools.reader_blocks import build_sidecar, flatten_nodes


def test_article_body_in_official_structural_record_reaches_readers():
    # Complete official ABAG package observed 2026-11-26 at
    # https://www.gesetze-im-internet.de/abag/xml.zip.
    package = ROOT / "tests/fixtures/history/gii-abag-20261126.xml.zip"
    assert hashlib.sha256(package.read_bytes()).hexdigest() == (
        "80639bbe4b52b50d898def9e003e50490964f1ec498e03cd6adee10e7bdca112")
    with ZipFile(package) as archive:
        xml = archive.read("BJNR377309001.xml")
    act, norms = parse_law(xml, "abag")
    assert act["norm_count"] == 6
    assert [n["enbez"] for n in norms] == ["Eingangsformel", "Art 2", "§ 1", "§ 2", "Art 3a", "Art 4"]
    expected = "Dieses Gesetz tritt mit Wirkung zum 31. Dezember 2001 in Kraft."
    assert norms[-1]["text"] == expected and norms[-1]["titel"] == "Inkrafttreten"
    assert norms[2]["gliederung"] == "Art 3"
    historic = parse_gii_xml_zip(package.read_bytes(), "abag")
    assert historic["complete"] and [(n["enbez"], n["text"]) for n in historic["norms"]] == [
        (n["enbez"], n["text"]) for n in norms]
    full = {"id": "fed_abag", "jurabk": "ABAG", "norms": norms, "juris": "DE",
            "source_url": "https://www.gesetze-im-internet.de/abag/", "versions": []}
    side = build_sidecar(xml, full, retrieved_at="2026-11-26")
    assert flatten_nodes(side["norms"]["Art 4"]["blocks"]) == expected
    product = render_markdown_snapshot(full, fallback_head="2026-11-26", norm="Art. 4")
    assert expected in product["markdown"]


def test_egbgb_source_link_retains_the_captured_document_fragment():
    # The source's EGBGB table also links the whole document on its own.
    doc = "BJNR006049896"
    fragment = doc + "BJNG035900377"
    href = f"{doc}.html#{fragment}"
    raw = (f'<a href="{doc}.html">Art 1</a><a href="{href}"><b>Art 50</b></a>'
           f'<a href="{doc}.html#BJNR000000000BJNG99">Art 2</a>').encode()
    assert parse_toc(raw) == {"art/50": href}
    toc = json.loads((ROOT / "api/data/gii_toc.json").read_bytes())
    assert gii_norm_url(toc, "bgbeg", "art", "50")["url"] == (
        "https://www.gesetze-im-internet.de/bgbeg/" + href)


def test_preceding_daily_parser_checkpoint_is_refetched(tmp_path):
    cache = GiiImportCache(tmp_path, min_free_bytes=0)
    original = {"day": "2026-11-26", "act": {"slug": "abag", "norm_count": 0}, "norms": []}
    def encode(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    legacy = {**original, "sha256": hashlib.sha256(encode(original)).hexdigest()}
    path = tmp_path / "abag.json.gz"
    path.write_bytes(gzip.compress(encode(legacy), mtime=0))
    assert cache.get("abag", original["day"]) is None
    assert json.loads(gzip.decompress(path.read_bytes())) == legacy
