"""FNA-based discovery of 2023+ amending documents (recht.bund.de index).

The FNA values are the official field of real recht.bund.de pages, e.g.
BGBl. 2026 I Nr. 107 ("860-2, 860-2, 860-2, 860-3, …") and BGBl. 2024 I Nr. 1
("neu: 202-5-25").
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from backfill_bgbl_history import (  # noqa: E402
    fna_index_candidates, learn_act_fna, split_fna)
from index_bgbl_issues import fna_list  # noqa: E402


def test_split_fna_separates_new_acts():
    assert split_fna("860-2, 860-2, 860-3") == (["860-2", "860-3"], [])
    assert split_fna("neu: 202-5-25") == ([], ["202-5-25"])
    assert fna_list("neu: 202-5-25") == ["202-5-25"]


def _doc(doc_id, fna):
    return {"document_id": doc_id, "official_fields": {"FNA": fna}}


def test_act_fna_is_the_number_shared_by_most_of_its_documents():
    docs = [_doc("a", "860-2, 860-12"), _doc("b", "860-2"),
            _doc("c", "860-2, 860-3, 860-12"), _doc("d", "860-12, 860-2")]
    links = {("sgb_2", "a"), ("sgb_2", "b"), ("sgb_2", "c"),
             ("sgb_12", "a"), ("sgb_12", "c"), ("sgb_12", "d")}
    learned = learn_act_fna(docs, links)
    assert learned["sgb_2"] == {"860-2"}
    # 860-12 and 860-2 tie for SGB XII: both are kept (over-capture is
    # harmless, the article heading decides)
    assert "860-12" in learned["sgb_12"]


def test_index_candidates_match_corpus_acts_and_skip_known(tmp_path):
    index = tmp_path / "index"
    index.mkdir()
    rows = [
        {"year": 2024, "issue": 5, "status": "ok",
         "document_id": "bgbl-1-2024-5", "title": "Gesetz zur Änderung des "
         "Zweiten Buches Sozialgesetzbuch", "publication_date": "2024-01-10",
         "execution_date": "2024-01-05", "fna_raw": "860-2"},
        {"year": 2024, "issue": 6, "status": "ok",
         "document_id": "bgbl-1-2024-6", "title": "Verordnung",
         "publication_date": "2024-01-11", "fna_raw": "neu: 860-2-99"},
        {"year": 2024, "issue": 7, "status": "missing"},
        {"year": 2024, "issue": 8, "status": "ok",
         "document_id": "bgbl-1-2024-8", "title": "Bekannt",
         "publication_date": "2024-01-12", "fna_raw": "860-2"},
    ]
    (index / "bgbl1-2024.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    acts = [{"slug": "sgb_2", "jurabk": "SGB 2", "long_title":
             "Sozialgesetzbuch (SGB) Zweites Buch (II) - Grundsicherung für "
             "Arbeitsuchende"}]
    documents = {"x": _doc("x", "860-2"), "y": _doc("y", "860-2")}
    found, learned = fna_index_candidates(
        index, acts, documents, {("sgb_2", "x"), ("sgb_2", "y")},
        "2023-01-01", "2026-12-31", {"bgbl-1-2024-8"})
    assert learned == {"sgb_2": {"860-2"}}
    assert [row["document_id"] for row in found] == ["bgbl-1-2024-5"]
    procedure = found[0]["retrospective_procedures"][0]
    assert procedure["discovery"] == "recht.bund.de FNA index"
    assert [a["slug"] for a in procedure["matched_acts"]] == ["sgb_2"]
    assert found[0]["official_pdf_url"].startswith(
        "https://www.recht.bund.de/bgbl/1/2024/5/")
