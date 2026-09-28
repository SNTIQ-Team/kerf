"""Capture planning for the Bavarian history fetcher (no network)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import fetch_bavarian_history as fb  # noqa: E402

# verbatim rows of the web.archive.org CDX responses (2026-09-28)
ROWS = [
    ["http://www.gesetze-bayern.de:80/Content/Zip/BayAGSG", "20170521023155", "302",
     "text/html", "P73NNUK4KTDS2J2CMK4Z4K7W6UUJHD3H", "430"],
    ["https://www.gesetze-bayern.de/Content/Zip/BayAGSG", "20240224091357", "200",
     "application/zip", "Q6V3CI4TDJKUOS6CMGFGG45UIN73XSPM", "35493"],
    ["https://www.gesetze-bayern.de/Content/Zip/BayAGSGG", "20210420101950", "200",
     "application/zip", "JPTCJHZVYXM7SWRMCAE6Q6V3MP6AW5HI", "3199"],
    ["https://www.gesetze-bayern.de/Content/Document/BayAGSG/true", "20260617232000", "200",
     "text/html", "UXEJJ4ZRUFSJELEGGJTQA32FYOOBELAD", "40846"],
    ["https://www.gesetze-bayern.de/Content/Document/BayAGSG-80", "20250425180925", "200",
     "text/html", "HBSU3FEPZPTQD7N3WGPYK3U56PZ42AIR", "6633"],
    ["https://www.gesetze-bayern.de/Content/Document/BayAGSG-80", "20260607223454", "200",
     "text/html", "2HQ2MHSY4CADHH32YOABPJLRSHFJHIXW", "6621"],
    ["https://www.gesetze-bayern.de/Content/Document/BayAGSG-G12", "20190614003743", "302",
     "text/html", "3I42H3S6NNFQ2MSVX7XZKYAYSCX5QBYJ", "348"],
    ["https://www.gesetze-bayern.de/Content/Document/BayAGSG-112?contrast=change",
     "20211208023338", "200", "text/html", "X", "1"],
    ["http://www.gesetze-bayern.de:80/Content/Document/BayAGVwGO", "20160212031012", "302",
     "text/html", "MCFBSCEPSCFZ4J3CCCQIY6FXAMKHNQWA", "1"],
]


def test_classify_keeps_only_the_act_s_documents():
    assert fb.classify(ROWS[1][0], "BayAGSG") == ("zip", "BayAGSG")
    assert fb.classify(ROWS[2][0], "BayAGSG") is None          # BayAGSGG
    assert fb.classify(ROWS[3][0], "BayAGSG") == ("full", "BayAGSG")
    assert fb.classify(ROWS[4][0], "BayAGSG") == ("norm", "BayAGSG-80")
    assert fb.classify(ROWS[6][0], "BayAGSG") is None          # Gliederung page
    assert fb.classify(ROWS[7][0], "BayAGSG") is None          # presentation variant


def test_plan_orders_zip_first_and_groups_by_digest():
    plan = fb.plan_captures(ROWS, "BayAGSG")
    assert [p["kind"] for p in plan] == ["zip", "full", "norm", "norm"]
    assert plan[0]["captures"] == [("20240224091357", ROWS[1][0])]   # 302 dropped
    # newest first within a kind
    assert plan[2]["captures"][0][0] == "20260607223454"
    duplicate = ROWS[5][:1] + ["20260608000000"] + ROWS[5][2:]
    grouped = fb.plan_captures(ROWS + [duplicate], "BayAGSG")
    same = [p for p in grouped if p["cdx_digest"] == ROWS[5][4]]
    assert len(same) == 1 and len(same[0]["captures"]) == 2
