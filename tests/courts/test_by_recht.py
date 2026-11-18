import gzip
from pathlib import Path

import pytest

from courts.errors import UpstreamFailure
from courts.sources.base import Capture
from courts.sources.by_recht import BayernRecht, parse_listing

ROOT = Path(__file__).resolve().parents[2]
DOC = "Y-300-Z-BECKRS-B-2025-N-12316"
URL = "https://www.gesetze-bayern.de/Content/Document/" + DOC


def listing(court="VG München"):
    return f"""<p>18 Treffer in 18 Gerichtsentscheidungen</p>
      <div><p class="hltitel"><a href="/Content/Document/{DOC}?hl=true">{court}: Ausländerrecht</a></p>
      <p class="hlSubTitel">Beschluss vom 04.04.2025 – M 10 E 25.1990</p></div>
      <a href="/Search/Page/2">Weiter</a>""".encode()


def test_list_and_original_agree_on_court_date_docket_and_document():
    hits, link, count = parse_listing(listing(), 1)
    assert count == 18 and link.endswith("/Page/2")
    row, = hits
    assert row["source_doc_id"] == DOC and row["url"] == URL
    data = gzip.decompress((ROOT / "conformance/fixtures/decisions/bayern-recht-B-2025-N-12316.html.gz").read_bytes())
    original = Capture(data, URL, "text/html", "html")
    decision = BayernRecht().parse(original, row)
    assert decision.source_id == "by-recht" and decision.land == "by"
    assert decision.sections and decision.case_numbers_raw == row["listed_case_numbers"]
    for mismatch in ({"listed_date": "2025-04-02"}, {"source_doc_id": "Y-300-Z-BECKRS-B-2025-N-1"},
                     {"listed_court": "vg-augsburg"}, {"listed_case_numbers": "other case"}):
        with pytest.raises(UpstreamFailure, match="identity_mismatch"):
            BayernRecht().parse(original, row | mismatch)


def test_federal_court_in_bavaria_is_out_of_scope():
    assert parse_listing(listing("BFH München"), 1)[0] == []
    with pytest.raises(UpstreamFailure, match="unexpected_representation"):
        parse_listing(b"<p>Temporarily unavailable</p>", 1)
