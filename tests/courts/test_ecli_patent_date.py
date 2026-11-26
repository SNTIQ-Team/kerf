import pytest

from courts.store import ecli_date_conflict, ecli_encoded_date


def test_jure269032429_preserves_the_published_patent_court_date():
    # Official XML for JURE269032429: 3 Ni 8/22 (EP), 1 July 2026.
    ecli = 'ECLI:DE:BPatG:2026:01072026B3Ni8.22EP.0'
    assert ecli_encoded_date(ecli) == '2026-07-01'
    assert ecli_date_conflict(ecli, '2026-07-01') is None
    assert ecli_date_conflict(ecli, '2026-07-02') is not None


@pytest.mark.parametrize(('ecli', 'expected'), [
    ('ECLI:DE:BPatG:2025:110325U3Ni8.22EP.0', '2025-03-11'),
    ('ECLI:DE:BPatG:2026:20260701B3Ni8.22EP.0', '2026-07-01'),
    ('ECLI:DE:BPatG:2012:20120711B1.0', '2012-07-11'),
    ('ECLI:DE:BPatG:1101:11011101B1.0', None),  # Both readings fit the header year.
    ('ECLI:DE:BPatG:2026:31022026B1.0', None),  # Neither reading is a calendar date.
])
def test_eight_digit_patent_dates_require_one_calendar_reading(ecli, expected):
    assert ecli_encoded_date(ecli) == expected


@pytest.mark.parametrize(('ecli', 'published_date'), [
    ('ECLI:DE:BSG:2026:200526BB11AL126R0', '2026-06-03'),
    ('ECLI:DE:BSG:2024:110624BB12BA224R0', '2026-05-05'),
    ('ECLI:DE:BSG:2025:010425BB7AS225R0', '2026-06-03'),
])
def test_social_court_date_conflicts_remain_visible(ecli, published_date):
    assert ecli_date_conflict(ecli, published_date) is not None
