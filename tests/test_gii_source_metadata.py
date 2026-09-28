from tools import build_web_data as builder


def test_wiki_preserves_official_paths_separately_from_stable_public_ids(monkeypatch):
    acts = [
        {"slug": "stag", "jurabk": "RuStAG", "norm_count": 1},
        {"slug": "azrg-dv", "jurabk": "AZRG-DV", "norm_count": 1},
        {"slug": "vwdg-dv", "jurabk": "VWDG-DV", "norm_count": 1},
    ]
    monkeypatch.setattr(builder, "load", lambda source, name:
                        acts if (source, name) == ("gii", "acts.jsonl") else [])
    monkeypatch.setattr(builder, "load_decisions", lambda: [])
    monkeypatch.setattr(builder, "load_buzer_cross_checks", lambda: {})
    monkeypatch.setattr(builder, "load_by_diffs", lambda: [])
    index, details = builder.build_wiki()
    for item in index:
        original = next(row for row in acts if row["jurabk"] == item["jurabk"])
        expected = f"https://www.gesetze-im-internet.de/{original['slug']}/"
        assert item["source_url"] == expected
        assert details[item["id"]]["source_url"] == expected
    assert set(details) == {"fed_rustag", "fed_azrg_dv", "fed_vwdg_dv"}
