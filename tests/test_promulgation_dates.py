"""Adoption and promulgation dates from real BGBl excerpts, both eras.

tests/fixtures/promulgation_excerpts.json holds verbatim pieces (first page,
commencement article and signature) of electronic BGBl documents (2023+)
and printed-issue PDFs (bgbl.de, until 2022), including PDFs that contain
the neighbouring acts of the same issue.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from commencement import find_commencement_article  # noqa: E402
from promulgation_dates import (  # noqa: E402
    extract_adopted, extract_promulgated, extract_promulgation_dates,
    issue_headers,
)

EXCERPTS = {row["document_id"]: row for row in json.loads(
    (Path(__file__).parent / "fixtures" / "promulgation_excerpts.json")
    .read_text(encoding="utf-8"))}


def excerpt(document_id: str) -> tuple[str, dict]:
    row = EXCERPTS[document_id]
    return "\n".join(piece["text"] for piece in row["pieces"]), dict(
        row["meta"])


def dates(document_id: str) -> dict:
    text, meta = excerpt(document_id)
    located = find_commencement_article(
        text, legacy=EXCERPTS[document_id]["era"] == "bgbl.de")
    return extract_promulgation_dates(
        text, meta, commencement_end=located["end"] if located else None)


def check(result: dict, source: str) -> str:
    return next(row["status"] for row in result["cross_checks"]
                if row["source"] == source)


def test_electronic_bgbl_masthead_and_heading():
    result = dates("bgbl-1-2023-217")
    adopted, promulgated = result["adopted"], result["promulgated"]
    assert (adopted["date"], adopted["status"]) == ("2023-08-16", "official")
    assert adopted["basis"] == "BGBl document heading 'Vom 16. August 2023'"
    assert (promulgated["date"], promulgated["status"]) == (
        "2023-08-18", "official")
    assert promulgated["basis"] == (
        "BGBl issue masthead 'Ausgegeben zu Bonn am 18. August 2023 Nr. 217'")
    assert check(adopted, "BGBl signature block") == "agree"
    assert check(adopted, "DIP") == "agree"
    assert check(adopted, "recht.bund.de") == "agree"
    assert check(promulgated, "DIP") == "agree"
    assert all(row["status"] == "agree" for row in result["findings"])


def test_evidence_is_verbatim_with_offsets_and_digest():
    text, meta = excerpt("bgbl-1-2026-107")
    result = extract_adopted(text, meta)
    heading = result["evidence"][0]
    assert text[heading["start"]:heading["end"]] == heading["snippet"]
    assert heading["snippet"].strip() == "Vom 16. April 2026"
    assert heading["text_sha256"] == meta["text_sha256"]
    assert heading["pdf_sha256"] == meta["pdf_sha256"]
    assert heading["page_index"] == 0
    assert "Dreizehntes Gesetz" not in heading["snippet"]
    promulgated = extract_promulgated(text, meta)
    snippet = promulgated["evidence"][0]["snippet"]
    assert snippet == "Ausgegeben zu Bonn am 22. April 2026 Nr. 107"


def test_printed_date_kept_and_metadata_disagreement_reported():
    # BGBl. 2024 I Nr. 101 prints "Ausgegeben zu Bonn am 26. März 2024";
    # DIP and recht.bund.de metadata say 25.03.2024.
    result = dates("bgbl-1-2024-101")
    promulgated = result["promulgated"]
    assert promulgated["date"] == "2024-03-26"
    assert check(promulgated, "DIP") == "disagree"
    assert check(promulgated, "recht.bund.de") == "disagree"
    disagreements = [row for row in result["findings"]
                     if row["status"] == "disagree"]
    assert {(row["source"], row["source_value"]) for row in disagreements} == {
        ("DIP", "2024-03-25"), ("recht.bund.de", "2024-03-25")}


def test_printed_issue_binds_the_header_of_the_start_page():
    # Bürgergeld-Gesetz starts on page 2328 of issue Nr. 51; the header is
    # printed at the foot of that page.
    result = dates("bgbl-1-2022-s2328")
    promulgated = result["promulgated"]
    assert promulgated["date"] == "2022-12-20"
    assert promulgated["issue"] == "51"
    assert "page 2328" in promulgated["basis"]
    assert promulgated["evidence"][0]["snippet"].startswith(
        "2328 Bundesgesetzblatt Jahrgang 2022 Teil I Nr. 51")
    assert result["adopted"]["date"] == "2022-12-16"


def test_preceding_act_on_the_first_page_does_not_leak():
    # BGBl. I 2000 S. 1394 begins with the end of the preceding act and its
    # signature "Berlin, den 27. September 2000".
    text, _meta = excerpt("bgbl-1-2000-s1394")
    assert "Berlin, den 27. September 2000" in text
    result = dates("bgbl-1-2000-s1394")
    assert result["adopted"]["date"] == "2000-09-29"
    assert check(result["adopted"], "BGBl signature block") == "agree"
    signature = result["adopted"]["evidence"][1]["snippet"]
    assert signature == "Berlin, den 29. September 2000"
    assert result["promulgated"]["date"] == "2000-09-30"


def test_following_act_on_the_last_page_does_not_leak():
    # BGBl. I 2000 S. 1590 ends with the title "Erste Verordnung zur Änderung
    # der Telekom-Arbeitszeitverordnung 2000 Vom 16. November 2000".
    text, _meta = excerpt("bgbl-1-2000-s1590")
    assert "Vom 16. November 2000" in text
    result = dates("bgbl-1-2000-s1590")
    assert result["adopted"]["date"] == "2000-11-20"
    assert check(result["adopted"], "DIP") == "agree"
    assert check(result["adopted"], "BGBl signature block") == "agree"
    assert result["promulgated"]["date"] == "2000-11-29"


def test_kerned_and_split_legacy_headers():
    kerned = dates("bgbl-1-2006-s1091")["promulgated"]
    assert (kerned["date"], kerned["issue"]) == ("2006-05-05", "22")
    split = dates("bgbl-1-2003-s2190")["promulgated"]
    assert (split["date"], split["issue"]) == ("2003-11-19", "55")
    assert "a\nm 19. November 2003" in split["evidence"][0]["snippet"]


def test_wrong_issue_number_is_not_accepted():
    text, meta = excerpt("bgbl-1-2022-s2328")
    meta["issue"] = "52"
    result = extract_promulgated(text, meta)
    assert result["date"] is None
    assert result["reason"] == "issue_number_mismatch"


def test_missing_evidence_stays_unknown():
    result = extract_promulgation_dates("Artikel 1\nÄnderung des BGB\n", {})
    assert result["adopted"]["status"] == "unknown"
    assert result["adopted"]["reason"] == "no_unique_title_date_line_on_first_page"
    assert result["promulgated"]["reason"] == "no_issue_header_found"
    assert result["adopted"]["date"] is None
    assert result["promulgated"]["date"] is None


def test_issue_headers_carry_page_numbers():
    text, _meta = excerpt("bgbl-1-2006-s1091")
    headers = [row for row in issue_headers(text)
               if row["kind"] == "running_header"]
    assert headers and headers[0]["page"] == "1091"
    assert headers[0]["issue"] == "22"
