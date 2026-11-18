"""Verify the published HTML derivatives and their treatment of hidden form state."""
import gzip
import hashlib
import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parents[2]
PROVENANCE = json.loads((ROOT / "conformance/fixtures/court-html-provenance.json").read_text())
FIELDS = {"__requestverificationtoken", "__viewstate", "__viewstategenerator"}


@pytest.mark.parametrize("record", PROVENANCE, ids=[Path(row["fixture"]).name for row in PROVENANCE])
def test_fixture_digest(record):
    body = gzip.decompress((ROOT / record["fixture"]).read_bytes())
    assert hashlib.sha256(body).hexdigest() == record["fixture_sha256"]
    assert record["fixture_sha256"] != record["capture_sha256"]


def test_archived_forms_only_contain_synthetic_state():
    for directory in ("conformance/fixtures", "tests/courts/fixtures", "tests/fixtures"):
        for path in (ROOT / directory).rglob("*.html*"):
            body = path.read_bytes()
            if path.suffix == ".gz":
                body = gzip.decompress(body)
            lower = body.lower()
            if not any(field.encode() in lower for field in FIELDS):
                continue
            state = "fixture-" + path.name.split(".html")[0] + "-state"
            expected = {"__requestverificationtoken": "", "__viewstategenerator": "fixture-generator",
                        "__viewstate": state}
            for tag in BeautifulSoup(body, "html.parser").find_all("input"):
                name = tag.get("name", "").lower()
                if name not in FIELDS:
                    continue
                if tag.get("value", "") != expected[name]:
                    pytest.fail(f"{path.relative_to(ROOT)} has an unexpected value for {name}")
