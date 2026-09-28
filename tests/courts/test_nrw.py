"""NRWE: real captured decisions and result list."""
import pytest

from courts.casenumbers import case_key, id_component, split_case_numbers
from courts.detect import classify_page
from courts.paragraphs import assemble, locate_quote
from courts.sources.nrw import parse_document, parse_result_list
from courts.store import finalise

MULTI = "L_9_SO_202_26_B_ER_und_L_9_SO_203_26_B_Beschluss_20260813"


def doc(fixture, name):
    markup = fixture(f"nrw/{name}.html").decode("utf-8")
    return finalise(parse_document(markup, f"https://nrwe.justiz.nrw.de/sgs/lsg_nrw/j2026/{name}.html", name))


def test_combined_docket_split_and_raw_kept_verbatim(fixture):
    d = doc(fixture, MULTI)
    assert d.case_numbers_raw == "L 9 SO 202/26 B ER und L 9 SO 203/26 B"
    assert d.case_numbers == ["L 9 SO 202/26 B ER", "L 9 SO 203/26 B"]
    assert d.ecli == "ECLI:DE:LSGNRW:2026:0813.L9SO202.26B.ER.UN.00"
    assert (d.court_id, d.jurisdiction, d.land) == ("lsg-nw", "sozial", "nw")
    assert d.decision_type == "Beschluss" and d.decision_date == "2026-08-13"


def test_eight_dockets_and_truncated_ecli(fixture):
    d = doc(fixture, "L_3_R_485_25_B_multi_Beschluss_20250722")
    assert len(d.case_numbers) == 8
    assert d.case_numbers[0] == "L 3 R 485/25 B" and d.case_numbers[-1] == "L 3 R 504/25 B"
    assert d.case_numbers_raw.endswith("L 3 R 503/25 B und L 3 R 504/25 B")
    # The published ECLI names only two dockets; it is kept as published.
    assert d.ecli == "ECLI:DE:LSGNRW:2025:0722.L3R485.25B.L3R486.00"


@pytest.mark.parametrize("raw,expected", [
    ("L 7 AS 1065/25 B ER / L 7 AS 1066/25 B", ["L 7 AS 1065/25 B ER", "L 7 AS 1066/25 B"]),
    ("2 W 233/01 + 2 W 236/01", ["2 W 233/01", "2 W 236/01"]),
    ("19 U 104/90    (vormals: 19 U 147/86)", ["19 U 104/90", "19 U 147/86"]),
    ("VerfGH 101/26.VB-2 und VerfGH 105/26-VB-2", ["VerfGH 101/26.VB-2", "VerfGH 105/26-VB-2"]),
    ("1 B 19/16, 1 B 19/16, 1 PKH 55/16", ["1 B 19/16", "1 PKH 55/16"]),
    ("2 BvR 1/20 u.a.", ["2 BvR 1/20"]),
    ("C-621/24", ["C-621/24"]),
])
def test_split_case_numbers(raw, expected):
    assert split_case_numbers(raw) == expected


def test_case_key_matches_bverwg_dot_style():
    assert case_key("1 B 19.16") == case_key("1 B 19/16") == "1B19/16"
    assert id_component("L 7 AS 1065/25 B ER") == "L_7_AS_1065-25_B_ER"


def test_sections_tenor_and_numbered_reasons(fixture):
    d = doc(fixture, MULTI)
    names = [s.name for s in d.sections]
    assert names == ["tenor", "gruende"]
    assert all(s.authorship == "court" for s in d.sections)
    paras, numbering = assemble(d.ecli, d.sections)
    assert numbering == "rn"
    ids = [p.id.split("#")[1] for p in paras]
    assert ids[:3] == ["tenor-1", "tenor-2", "rn-1"]
    assert locate_quote(paras, "Die zulässige Beschwerde der Antragstellerin ist unbegründet") == [f"{d.ecli}#rn-8"]
    assert d.procedural[0]["court"] == "Sozialgericht Dortmund" and d.procedural[0]["case_number"] == "S 43 SO 251/26"


def test_numbered_list_wrappers_do_not_become_empty_rn(fixture):
    d = doc(fixture, "15_K_4583_24_Sonstige_20260731")
    paras, _ = assemble(d.ecli, d.sections)
    rns = {p.rn for p in paras if p.rn}
    assert "20" not in rns and "21" in rns  # Rn 20 wraps a list whose item is Rn 21
    assert "20" in d.metadata["rn_without_own_text"]
    assert all(p.text.strip() for p in paras)


def test_short_decision_with_status_like_az_is_not_an_error_page(fixture):
    # Regression: "1 A 406/26.A" contains "406" and must not look like HTTP 406.
    body = fixture("nrw/1_A_406_26_A_Beschluss_20260904.html")
    assert classify_page(body, status=200) is None
    d = finalise(parse_document(body.decode("utf-8"), "https://nrwe.justiz.nrw.de/x.html", "x"))
    assert d.case_numbers == ["1 A 406/26.A"]


def test_result_list(fixture):
    total, items = parse_result_list(fixture("nrw/result_lsg_2025_2026_desc_p1.html.gz").decode("utf-8"))
    assert total == 471 and len(items) == 100
    multi = next(i for i in items if i["source_doc_id"].endswith(MULTI))
    assert multi["listed_case_numbers"] == "L 9 SO 202/26 B ER und L 9 SO 203/26 B"
    assert multi["listed_date"] == "2026-08-13" and multi["priority"] == 0
    assert multi["listed_court"] == "lsg-nw"
    dates = [i["listed_date"] for i in items]
    assert dates == sorted(dates, reverse=True)
