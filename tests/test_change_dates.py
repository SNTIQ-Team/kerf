"""resolve_change_dates and the resolver CLI on real BGBl excerpts."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tools"))

from backfill_bgbl_history import parse_retrospective_article_sections  # noqa: E402
from change_dates import (  # noqa: E402
    DocumentTextError, load_document_text, resolve_change_dates,
)

FIXTURES = Path(__file__).parent / "fixtures"
EXCERPTS = {row["document_id"]: row for row in json.loads(
    (FIXTURES / "promulgation_excerpts.json").read_text(encoding="utf-8"))}
ARTICLES = {(row["document_id"], row["article"]): row for row in json.loads(
    (FIXTURES / "amendment_articles.json").read_text(encoding="utf-8"))}
COMMENCEMENT = {row["document_id"]: row for row in json.loads(
    (FIXTURES / "commencement_articles.json").read_text(encoding="utf-8"))}


def excerpt(document_id: str) -> tuple[str, dict]:
    row = EXCERPTS[document_id]
    return "\n".join(piece["text"] for piece in row["pieces"]), dict(
        row["meta"])


def test_all_dates_for_one_item_of_the_fachkräfteeinwanderung():
    text, meta = excerpt("bgbl-1-2023-217")
    dip = COMMENCEMENT["bgbl-1-2023-217"]["dip_entry_into_force"]
    result = resolve_change_dates(text, "1", {"nummer": "18",
                                              "buchstabe": "b"},
                                  meta=meta, dip_rows=dip)
    assert result["query"] == "Artikel 1 Nummer 18 Buchstabe b"
    assert (result["adopted"]["date"], result["adopted"]["status"]) == (
        "2023-08-16", "official")
    assert (result["promulgated"]["date"],
            result["promulgated"]["status"]) == ("2023-08-18", "official")
    in_force = result["in_force"]
    assert (in_force["date"], in_force["status"]) == (
        "2023-08-19", "computed_from_commencement_clause")
    assert "Artikel 1 Nummer 18 Buchstabe b, Nummer 30 und 31" in \
        in_force["clause"]
    kinds = [row["kind"] for row in in_force["evidence"]]
    assert kinds == ["commencement_clause", "masthead"]
    assert in_force["dip_cross_check"]["status"] == "agree"
    assert result["applies_from"] == {"date": None, "status": "not_stated",
                                      "basis": None}
    assert result["commencement_article"]["article"] == "12"
    assert {"in_force_cross_check"} <= {row["kind"]
                                        for row in result["findings"]}


def test_computed_date_is_withheld_when_promulgation_is_disputed():
    # BGBl. 2024 I Nr. 101: masthead 26. März 2024, metadata 25.03.2024.
    text, meta = excerpt("bgbl-1-2024-101")
    computed = resolve_change_dates(text, "4", None, meta=meta)
    assert computed["in_force"]["status"] == "unknown"
    assert computed["in_force"]["reason"] == "promulgation_date_disputed"
    assert computed["in_force"]["computed_from_printed_promulgation"] == \
        "2024-03-27"
    # An explicit calendar date does not depend on the promulgation day.
    fixed = resolve_change_dates(text, "2", {"nummer": "6"}, meta=meta)
    assert (fixed["in_force"]["date"], fixed["in_force"]["status"]) == (
        "2024-07-01", "official_commencement_clause")
    listed = resolve_change_dates(text, "5b", None, meta=meta)
    assert listed["in_force"]["date"] == "2025-01-01"


def test_legacy_issue_end_to_end():
    text, meta = excerpt("bgbl-1-2022-s2328")
    result = resolve_change_dates(text, "1", {"nummer": "2",
                                              "buchstabe": "a"},
                                  meta=meta, legacy=True)
    assert result["adopted"]["date"] == "2022-12-16"
    assert result["promulgated"]["date"] == "2022-12-20"
    assert (result["in_force"]["date"], result["in_force"]["status"]) == (
        "2023-07-01", "official_commencement_clause")


def test_document_rows_are_read_by_digest(tmp_path):
    text, meta = excerpt("bgbl-1-2026-107")
    payload = text.encode("utf-8")
    relative = "data/bgbl_documents/texts/excerpt.txt"
    (tmp_path / relative).parent.mkdir(parents=True)
    (tmp_path / relative).write_bytes(payload)
    row = {**meta, "text_object": relative,
           "text_sha256": hashlib.sha256(payload).hexdigest(),
           "dip_entry_into_force": COMMENCEMENT["bgbl-1-2026-107"][
               "dip_entry_into_force"]}
    result = resolve_change_dates(row, "1a", None, roots=[tmp_path])
    assert result["in_force"]["date"] == "2029-01-01"
    assert result["in_force"]["dip_cross_check"]["status"] == "agree"
    assert load_document_text(row, [tmp_path]) == text
    with pytest.raises(DocumentTextError):
        load_document_text({**row, "text_sha256": "0" * 64}, [tmp_path])
    with pytest.raises(DocumentTextError):
        load_document_text({**row, "text_object": "../escape.txt"},
                           [tmp_path])


def test_cli_writes_resolutions_and_summary(tmp_path):
    from resolve_change_dates import main

    _text, meta = excerpt("bgbl-1-2023-217")
    first = EXCERPTS["bgbl-1-2023-217"]["pieces"][0]["text"]
    head = first[:first.index("Artikel 1")]          # masthead and title
    article = ARTICLES[("bgbl-1-2023-217", "1")]["text"]
    commencement = EXCERPTS["bgbl-1-2023-217"]["pieces"][2]["text"]
    # Real pieces of one document: masthead/title, Artikel 1 (prefix),
    # Artikel 12 and the signature.
    text = head + "\n" + article + "\n" + commencement
    payload = text.encode("utf-8")
    relative = "data/bgbl_documents/texts/fixture.txt"
    (tmp_path / relative).parent.mkdir(parents=True)
    (tmp_path / relative).write_bytes(payload)
    section = next(row for row in parse_retrospective_article_sections(text)[0]
                   if row["article"] == "1")
    document = {**meta, "text_object": relative,
                "text_sha256": hashlib.sha256(payload).hexdigest(),
                "dip_entry_into_force": COMMENCEMENT["bgbl-1-2023-217"][
                    "dip_entry_into_force"]}
    candidate = {
        "id": "fed-bgbl-candidate:fixture", "document_id": meta["document_id"],
        "slug": "aufenthg_2004", "act_id": "fed_aufenthg_2004",
        "long_title": "Aufenthaltsgesetz", "amending_article": "1",
        "article_text_sha256": hashlib.sha256(
            section["text"].encode("utf-8")).hexdigest(),
        "collective_subsection": None,
        "publication_date": meta["publication_date"],
        "effective_at": None,
        "effective_date_status": "unresolved_sub_article_scope"}
    snapshot = tmp_path / "data" / "snapshots" / "bgbl_history_backfill" / \
        "2026-01-01"
    snapshot.mkdir(parents=True)
    (snapshot / "documents.jsonl").write_text(
        json.dumps(document, ensure_ascii=False) + "\n", encoding="utf-8")
    (snapshot / "candidates.jsonl").write_text(
        json.dumps(candidate, ensure_ascii=False) + "\n", encoding="utf-8")
    out = tmp_path / "out"
    assert main(["--data-root", str(tmp_path), "--legacy-archive",
                 str(tmp_path / "no-legacy"), "--out-dir", str(out)]) == 0
    rows = [json.loads(line) for line in
            (out / "resolutions.jsonl").read_text().splitlines()]
    by_label = {row["item_label"]: row for row in rows}
    assert by_label["Nummer 1 Buchstabe a"]["dates"]["in_force"]["date"] == \
        "2023-11-18"
    assert by_label["Nummer 1 Buchstabe a"]["change_type"] == "eingefügt"
    assert all(row["dates"]["adopted"]["date"] == "2023-08-16"
               for row in rows)
    assert all(row["article_in_force"]["status"] == "unknown"
               for row in rows)            # Artikel 1 as a whole is split
    summary = json.loads((out / "summary.json").read_text())
    assert summary["items"]["total"] == len(rows)
    assert summary["items"]["resolved"] == len(rows)
    assert summary["article_level"]["before"] == {
        "recht.bund.de:unresolved_sub_article_scope": 1}
    assert (out / "documents.jsonl").read_text().count("\n") == 1
    assert (out / "unresolved.jsonl").read_text() == ""
