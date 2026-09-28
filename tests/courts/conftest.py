import gzip
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).with_name("fixtures")


def fixture_bytes(relpath: str) -> bytes:
    path = FIXTURES / relpath
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


@pytest.fixture(autouse=True)
def isolated_corpus(tmp_path, monkeypatch):
    """Never touch the live corpus in ~/.local/share/sntiq-legal from tests."""
    monkeypatch.setenv("SNTIQ_COURTS_HOME", str(tmp_path / "corpus"))
    # The btrfs space guard is exercised explicitly in test_packs_policy_space.py.
    monkeypatch.setenv("SNTIQ_COURTS_SPACE_GUARD", "off")
    return tmp_path / "corpus"


@pytest.fixture
def fixture():
    return fixture_bytes
