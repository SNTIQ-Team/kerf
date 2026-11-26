import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from rii_dataset_card import add_to_card


def test_dataset_export_retains_verified_federal_edition(tmp_path):
    asset = tmp_path / "catalogue.xml.gz"
    asset.write_bytes(b"source")
    manifest = {
        "format": "kerf-rii-publications/1",
        "source_catalogue": {"observed": "2026-11-26T08:30:03+00:00"},
        "source_catalogue_count": 85155,
        "counts": {"documents": 1234, "paragraphs": 10001},
        "files": {asset.name: {"size": 6, "sha256": hashlib.sha256(b"source").hexdigest()}},
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    original = "---\nconfigs:\n  - config_name: court_passages\n---\n# Kerf\n\n## Reuse\n\nRights.\n"
    card = add_to_card(original, tmp_path)
    assert add_to_card(card, tmp_path) == card
    assert "1,234" in card and "10,001" in card and "85,155" in card
    assert "config_name: rii_paragraphs" in card and "config_name: court_passages" in card
    assert card.endswith("## Reuse\n\nRights.\n")
    asset.write_bytes(b"broken")
    with pytest.raises(ValueError, match="integrity"):
        add_to_card(original, tmp_path)
