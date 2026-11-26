"""The DSA's application clause and CELLAR's conflicting work date are both traceable."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
import fetch_eu_texts as eu
from tools import eu_history


@pytest.mark.parametrize(("last_wording", "expected"), [
    ("Diese Verordnung gilt ab dem 17. Februar 2024.", "2024-02-17"),
    ("Artikel 24 gilt ab dem 16. November 2022.", None),
    (None, None),
    ("Diese Verordnung gilt ab dem 17. Februar 2025.", None),
])
def test_dsa_application_sources(monkeypatch, last_wording, expected):
    act = "32022R2065"
    consolidation = "02022R2065-20221027"
    spec = next(s for s in eu.CORPUS if s["celex"] == act)
    meta = dict.fromkeys(("adopted", "signed", "published", "oj_ref", "in_force", "end_of_validity", "eli"))
    meta.update(title="Digital Services Act", published="2022-10-27", applies_from="2022-02-17",
                entry_into_force_dates=["2022-02-17", "2022-11-16"])
    monkeypatch.setattr(eu, "work_metadata", lambda *args: meta)
    monkeypatch.setattr(eu, "related", lambda *args: {})
    monkeypatch.setattr(eu, "consolidations", lambda *args: [{"celex": consolidation, "date":"2022-10-27"}])
    monkeypatch.setattr(eu, "manifestations", lambda *args: {})

    def edition(_cellar, celex, _manifestations):
        if celex == consolidation and last_wording is None:
            raise RuntimeError("no German edition")
        wording = "Diese Verordnung gilt ab dem 17. Februar 2024." if celex == act else last_wording
        return {"articles": [{"enbez": "Art. 93", "titel": "Inkrafttreten und Anwendung",
                              "text": "(1) Inkrafttreten.\n(2) " + wording +
                                      "\nArtikel 24 gilt jedoch ab dem 16. November 2022."}],
                "celex": celex, "items": [{"url": "https://example.org/" + celex, "sha256": "a" * 64}]}

    monkeypatch.setattr(eu, "fetch_edition", edition)
    row = eu.fetch_act(None, spec, lisbon=None)
    assert row["dates"]["applies_from"] == expected
    evidence = row["dates"]["application_clause"]
    assert evidence["cellar_date"] == "2022-02-17"
    assert evidence["missing_editions"] == ([consolidation] if last_wording is None else [])
    assert {s["celex"] for s in evidence["editions"]} == ({act} if last_wording is None else {act, consolidation})
    first = evidence["editions"][0]
    assert first["quote"] == "(2) Diese Verordnung gilt ab dem 17. Februar 2024."
    assert first["sources"] == [{"url": "https://example.org/" + act, "sha256": "a" * 64}]
    history_date = eu_history._base_change(row, "")["dates"]["applies_from"]
    assert history_date["date"] == expected
    if expected:
        assert "Art. 93" in history_date["basis"] and "Abs. 2" in history_date["basis"]
    else:
        assert row["warnings"] and history_date["status"] == "unknown"
