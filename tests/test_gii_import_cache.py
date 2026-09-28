from __future__ import annotations

import gzip
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))
from gii_import_cache import GiiImportCache, ImportBudgetExceeded


def test_download_allows_official_image_annexes_but_has_a_hard_limit():
    import fetch_gii
    assert hasattr(fetch_gii, "bounded_zip_bytes")
    chunks = [b"x" * (1024**2)] * 19
    assert len(fetch_gii.bounded_zip_bytes(chunks)) == 19 * 1024**2
    with pytest.raises(ValueError, match="limit"):
        fetch_gii.bounded_zip_bytes([b"123", b"456"], limit=5)


def law(slug="hgb"):
    return {"slug": slug, "norm_count": 1}, [{"slug": slug, "enbez": "1", "text": "Text"}]


def test_cached_law_resumes_only_same_source_day(tmp_path):
    cache = GiiImportCache(tmp_path, max_bytes=10000, min_free_bytes=0)
    act, norms = law()
    cache.put("hgb", "2026-09-21", act, norms)
    assert cache.get("hgb", "2026-09-21") == (act, norms)
    assert cache.get("hgb", "2026-09-22") is None


def test_budget_cannot_be_exceeded_and_prior_law_remains_readable(tmp_path):
    cache = GiiImportCache(tmp_path, max_bytes=10000, min_free_bytes=0)
    cache.put("hgb", "2026-09-21", *law())
    size = cache.used_bytes()
    limited = GiiImportCache(tmp_path, max_bytes=size, min_free_bytes=0)
    with pytest.raises(ImportBudgetExceeded):
        limited.put("zvg", "2026-09-21", *law("zvg"))
    assert limited.used_bytes() == size
    assert limited.get("hgb", "2026-09-21") == law()
    assert limited.get("zvg", "2026-09-21") is None


def test_bad_cache_is_not_silently_reused(tmp_path):
    cache = GiiImportCache(tmp_path, max_bytes=10000, min_free_bytes=0)
    cache.put("hgb", "2026-09-21", *law())
    target = tmp_path / "hgb.json.gz"
    target.write_bytes(gzip.compress(b'{"day":"2026-09-21","act":{}}'))
    with pytest.raises(ValueError):
        cache.get("hgb", "2026-09-21")


def test_cache_rejects_traversal_and_wrong_act_identity(tmp_path):
    cache = GiiImportCache(tmp_path, max_bytes=10000, min_free_bytes=0)
    with pytest.raises(ValueError):
        cache.put("../outside", "2026-09-21", *law())
    with pytest.raises(ValueError):
        cache.put("hgb", "2026-09-21", *law("zvg"))
    assert not list(tmp_path.iterdir())


def test_cached_body_hash_must_match(tmp_path):
    cache = GiiImportCache(tmp_path, max_bytes=10000, min_free_bytes=0)
    cache.put("hgb", "2026-09-21", *law())
    target = tmp_path / "hgb.json.gz"
    payload = gzip.decompress(target.read_bytes()).replace(b'Text', b'Fake')
    target.write_bytes(gzip.compress(payload))
    with pytest.raises(ValueError, match="digest"):
        cache.get("hgb", "2026-09-21")
