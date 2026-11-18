import json
import unicodedata
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from fastapi.testclient import TestClient

from api import main
from tools.bavarian_guidance import migration_guidance


def test_migration_guidance_status_and_complete_text(tmp_path, monkeypatch):
    directory = Path(__file__).resolve().parents[1] / "data" / "bavarian_guidance"
    documents = migration_guidance(directory)
    (tmp_path / "bavarian_migration_guidance.json").write_text(json.dumps(documents))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "_CACHE", {})
    route = "/bavaria/migration-guidance"

    with TestClient(main.app) as client:
        index = client.get(route).json()
        assert index["total"] == 2
        assert {d["abbreviation"]: d["valid_until"] for d in index["documents"]} == {
            "BIR": "2026-12-31", "GesUVV": None}
        for metadata in index["documents"]:
            assert metadata["document_type"] == "Verwaltungsvorschrift"
            assert metadata["is_statute"] is False
            assert metadata["conflict_with_statute"] == "not_assessed"
            notice = metadata["legal_notice_en"]
            guidance = metadata["agent_guidance_en"]
            assert notice.isupper() and "NOT A STATUTE" in notice and "LAW PREVAILS" in notice
            assert "intended practice" in guidance and "actual practice" in guidance
            assert notice.isascii() and guidance.isascii()
            assert not any(key.endswith("_ru") for key in metadata)
            assert "text" not in metadata
            key = metadata["id"]
            response = client.get(f"{route}/{key}")
            assert response.status_code == 200
            doc = response.json()
            text = doc.pop("text")
            assert doc == metadata
            assert notice not in text and guidance not in text
            with ZipFile(directory / doc["source"]["captured_on"] / (key + ".zip")) as z:
                root = ET.fromstring(z.read(f"bayportalvv/{key}.xml"))
            source_text = "".join(root.find("textdaten/vv1.0/rumpf").itertext())
            assert "".join(unicodedata.normalize("NFKC", text).split()) == \
                "".join(unicodedata.normalize("NFKC", source_text).split())
        assert client.get(f"{route}/unknown").status_code == 404
