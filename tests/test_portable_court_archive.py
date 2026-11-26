import gzip
import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import read_court_archive as reader


class Response(io.BytesIO):
    def __init__(self, content, status, headers):
        super().__init__(content)
        self.status = status
        self.headers = headers


@pytest.mark.parametrize(
    "status,header", [(200, ""), (206, "bytes 0-2/20"), (206, "bytes 3-7/20")]
)
def test_a_hub_cache_cannot_substitute_a_different_byte_range(status, header):
    with patch.object(
        reader,
        "urlopen",
        return_value=Response(b"abc", status, {"Content-Range": header}),
    ):
        with pytest.raises(ValueError, match="exact requested byte range"):
            reader.fetch("https://example.org/part", 3, (3, 3))


def test_read_exact_requested_member():
    with patch.object(
        reader,
        "urlopen",
        return_value=Response(b"abc", 206, {"Content-Range": "bytes 3-5/20"}),
    ) as op:
        assert reader.fetch("https://example.org/part", 3, (3, 3)) == b"abc"
        assert op.call_args.args[0].get_header("Range") == "bytes=3-5"


def test_unpacked_extent_excludes_adjacent_decisions_and_partial_frames():
    one = gzip.compress(b"court")
    assert reader.unpack(one, 5) == b"court"
    for payload, size in [(one + one, 10), (one[:-1], 5), (one, 4)]:
        with pytest.raises(ValueError, match="gzip"):
            reader.unpack(payload, size)


def test_updating_card_replaces_the_previous_count_and_keeps_other_sections(tmp_path):
    import json
    from export_court_archive import add_to_card

    path = tmp_path / "manifest.json"

    def write(count):
        path.write_text(
            json.dumps(
                {
                    "schema": "kerf-court-archive/1",
                    "files": {},
                    "decisions": count,
                    "passages": 6,
                }
            )
        )

    write(2)
    base = "---\nlicense: other\nconfigs:\n  - config_name: original\n    data_files: data.jsonl\n---\n\n## Reuse\n\nRights.\n"
    card = add_to_card(base, tmp_path)
    assert add_to_card(card, tmp_path) == card
    write(3)
    updated = add_to_card(card, tmp_path)
    assert "mirrors 3 decisions" in updated and "mirrors 2 decisions" not in updated
    assert updated.count("config_name: court_passages") == 1 and updated.endswith(
        "Rights.\n"
    )


@pytest.mark.parametrize(
    "entry,expected",
    [
        (
            {
                "source_locator": "#point1 parent::td following-sibling::td",
                "source_anchor": "point1",
                "locator_type": "source_anchor_adjacent_cell",
            },
            ("source_anchor_adjacent_cell", "#point1 parent::td following-sibling::td"),
        ),
        (
            {
                "source_locator": "#readme_135772 > h1:nth-of-type(1)",
                "locator_type": "css",
            },
            ("css", "#readme_135772 > h1:nth-of-type(1)"),
        ),
        (
            {"xml_path": "/dokument/gruende/dl", "source_anchor": "rd_14"},
            ("xpath", "/dokument/gruende/dl"),
        ),
    ],
)
def test_passages_keep_the_source_expression_that_locates_the_text(entry, expected):
    from export_court_archive import passage_location

    assert passage_location(entry) == expected
