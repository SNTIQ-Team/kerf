import copy
from datetime import date, timedelta
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from api import main
from api.act_archive import reviewed_section_markdown
from api.reviewed_norm_editions import FILENAME, validate

ROOT = Path(__file__).resolve().parents[1]
ID = "de/bund/sgb_8/para/42"
ACT = "fed_sgb_8"


@pytest.fixture
def package():
    return json.loads((ROOT / "data" / FILENAME).read_bytes())


@pytest.fixture
def configured(monkeypatch, tmp_path, package):
    rec = package["norms"][ID]
    act = {"id": ACT, "juris": "DE", "title": "SGB VIII", "jurabk": "SGB VIII",
           "slug": "sgb_8", "build": "2026-11-26", "norms": [
               {"enbez": "§ 41", "titel": "Another section", "text": "Different text."},
               {"enbez": rec["label"], "titel": rec["title"], "text": rec["editions"][-1]["text"]}]}
    (tmp_path / "acts").mkdir()
    (tmp_path / "acts" / f"{ACT}.json").write_text(json.dumps(act))
    (tmp_path / "wiki.json").write_text(json.dumps([act]))
    (tmp_path / FILENAME).write_text(json.dumps(package))
    monkeypatch.setattr(main, "DATA_DIR", tmp_path)
    monkeypatch.setattr(main, "_archive_head_fallback", lambda: "2026-11-26")
    return TestClient(main.app), act


def test_all_legal_boundaries_on_both_http_surfaces(configured, package):
    client, act = configured
    rows = package["norms"][ID]["editions"]
    assert len(rows) == 6
    for row in rows:
        dates = [row["valid_from"]]
        if row["valid_until_exclusive"]:
            dates.append(str(date.fromisoformat(row["valid_until_exclusive"]) - timedelta(days=1)))
        for day in dates:
            md = client.get(f"/acts/{ACT}/markdown.json", params={"at": day, "norm": "42"}).json()
            assert md["quality"]["complete"] and md["provenance"]["scope"] == "§ 42"
            assert md["provenance"]["text_sha256"] == row["text_sha256"]
            assert md["provenance"]["effective_from"] == row["valid_from"]
            assert md["provenance"]["sources"][0]["sha256"] == row["sources"][0]["sha256"]
            native = client.get(f"/norms/{ID}/at", params={"date": day}).json()
            assert native["status"] == "ok"
            item = native["evidence"][0]
            assert item["content_hash"]["text_sha256"] == row["text_sha256"]
            assert item["source"]["url"] == row["sources"][0]["url"]
            assert item["temporal"]["valid_until_exclusive"] == row["valid_until_exclusive"]


def test_wording_stays_within_scope_and_knowledge(configured, package):
    client, act = configured
    records = package["norms"]
    for query, day, as_of in [(None, "2000-01-01", None), ("§ 41", "2000-01-01", None),
                               ("Art. 42", "2000-01-01", None), ("§ 42", "1998-06-30", None),
                               ("§ 42", "2026-11-27", None),
                               ("§ 42", "2000-01-01", "2026-11-26T08:45:49Z")]:
        assert reviewed_section_markdown(act, query, day, as_of, records, "2026-11-30") is None
    for norm in [None, "§ 41"]:
        params = {"at": "2000-01-01"}
        if norm:
            params["norm"] = norm
        result = client.get(f"/acts/{ACT}/markdown.json", params=params)
        assert result.status_code == 200
        assert result.json()["provenance"].get("date_basis") != "reviewed_norm_edition"
    at = client.get(f"/norms/{ID}/at", params={"date": "2000-01-01",
                    "as_of": "2026-11-26T09:45:50+01:00"}).json()
    assert at["evidence"][0]["temporal"]["known_at"] == records[ID]["known_from"]
    md = client.get(f"/acts/{ACT}/markdown", params={"norm": "§ 42", "at": "2000-01-01", "download": True})
    assert md.status_code == 200 and "attachment" in md.headers["Content-Disposition"]


def test_missing_release_and_corrupt_data(configured, package, tmp_path):
    client, _ = configured
    target = tmp_path / FILENAME
    target.unlink()
    params = {"at": "2000-01-01", "norm": "§ 42"}
    old = client.get(f"/acts/{ACT}/markdown.json", params=params)
    assert old.status_code == 200 and old.json()["provenance"].get("date_basis") != "reviewed_norm_edition"
    bad = copy.deepcopy(package)
    bad["norms"][ID]["editions"][0]["text"] += " changed"
    target.write_text(json.dumps(bad))
    assert client.get(f"/acts/{ACT}/markdown", params=params).status_code == 503
    with pytest.raises(ValueError, match="noncontiguous"):
        package["norms"][ID]["editions"][1]["valid_from"] = "2005-10-02"
        validate(package)
