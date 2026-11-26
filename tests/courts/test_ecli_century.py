import pytest

from courts.model import ParsedDecision
from courts.store import decision_identity, ecli_date_conflict, ecli_encoded_date


@pytest.mark.parametrize(('ecli', 'day', 'az'), [
    ('ECLI:DE:BVerwG:1961:021061B8B78.61.0', '1961-10-02', '8 B 78.61'),
    ('ECLI:DE:BVerwG:1995:021195B9B710.94.0', '1995-11-02', '9 B 710.94'),
    ('ECLI:DE:BVerwG:2007:030507U2C9.06.0', '2007-05-03', '2 C 9.06'),
])
def test_official_six_digit_ordinal_uses_the_ecli_year(ecli, day, az):
    parsed = ParsedDecision(source_id='bverwg', source_doc_id=ecli.split(':')[-1],
                            source_url='https://www.bverwg.de/de/' + ecli.split(':')[-1],
                            court_name='Bundesverwaltungsgericht', court_id='bverwg',
                            decision_date=day, case_numbers_raw=az, case_numbers=[az],
                            ecli=ecli, decision_type='Urteil' if 'U2C' in ecli else 'Beschluss', sections=[])
    assert ecli_encoded_date(ecli) == day
    assert decision_identity(parsed) == ecli
    assert ecli_date_conflict(ecli, day[:-2] + '01') is not None


def test_disagreeing_short_years_preserve_the_conflict_without_inventing_a_century():
    ecli = 'ECLI:DE:BVerwG:2000:311299B1B1.99.0'
    assert ecli_encoded_date(ecli) is None
    assert ecli_date_conflict(ecli, '2000-12-31') is not None
