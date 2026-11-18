from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from fetch_gvbl_index import page


def listing(next_offset=16):
    row = """<tr>
        <td data-label="Fundstelle"><a href="/gvbl/2026-698/">2026 S. 698</a></td>
        <td data-label="Verkündung">16.11.2026</td>
        <td data-label="Titel">Verordnung zur Änderung</td>
        <td data-label="Gl-Nr.">86-8-A/G, 800-21-86-A</td>
        <td data-label="Ausfertigung">28.10.2026</td></tr>"""
    link = (f'<a class="page-link" aria-label="Weiterblättern" '
            f'href="?offset={next_offset}#page-browser">&gt;</a>') if next_offset else ""
    return f'<table id="search-results-table"><tbody>{row}</tbody></table>{link}'


def test_index_follows_official_navigation_and_keeps_dates_separate():
    rows, offset = page(listing(17), 16)
    assert offset == 17
    assert rows[0]["time"] == "2026-11-16"
    assert rows[0]["ausfertigung_date"] == "2026-10-28"
    assert rows[0]["gliederungs_nr"] == "86-8-A/G, 800-21-86-A"
    assert rows[0]["permalink"] == "https://www.verkuendung-bayern.de/gvbl/2026-698/"
    assert page(listing(None), 1)[1] is None


def test_reject_skipped_page_and_foreign_source():
    with pytest.raises(ValueError):
        page(listing(40), 1)
    with pytest.raises(ValueError):
        page(listing(None).replace('href="/gvbl/', 'href="https://example.org/gvbl/'), 1)


def test_public_index_filters_whole_bayrs_number_and_publication_year(tmp_path, monkeypatch):
    import json
    from fastapi.testclient import TestClient
    from api import main

    def doc(date, refs):
        return {"time": date, "gliederungs_nr": refs}

    payload = {"source": "https://www.verkuendung-bayern.de/gvbl/",
               "index_date": "2026-11-18", "publications": [
                   doc("2024-10-01", "21-1-A, 13-2-B"), doc("2024-11-01", "21-1-AB"),
                   doc("2023-12-01", "21-1-A"), doc(None, "21-1-A")]}
    (tmp_path / "bavarian_gvbl.json").write_text(json.dumps(payload))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "_CACHE", {})
    with TestClient(main.app) as client:
        response = client.get("/bavaria/gvbl", params={"year": 2024, "bayrs": "21-1-a"})
        assert response.status_code == 200
        assert response.json()["publications"] == payload["publications"][:1]
        assert client.get("/bavaria/gvbl", params={"limit": 1, "offset": 2}).json()["total"] == 4
