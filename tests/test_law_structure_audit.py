"""Regression of the source-verified 2026-09-29 MCP audit, not visual block counts."""
import json
from pathlib import Path
import pytest
from api.law_structure import parse_norm_structure, find_unit, iter_units
F = json.loads((Path(__file__).parent/'fixtures/law_structure/audit_20260929.json').read_text())

def test_asyl74_ten_sentences_and_exact_addresses():
    s=parse_norm_structure(F['asyl74']['text'])
    assert [len(a['children']) for a in s['units']] == [4,4,2]
    assert s['counts']['satz']==10
    assert find_unit(s,'abs/1/satz/4')['text'].startswith('§ 58 der Verwaltungsgerichtsordnung')
    assert find_unit(s,'abs/2/satz/2')['text'].startswith('§ 87b Absatz 3')
    assert find_unit(s,'abs/2/satz/3')['text'].startswith('Der Kläger ist')
    assert find_unit(s,'abs/1/satz/5') is None

def test_bay39_spaced_lists_restart_per_sentence():
    s=parse_norm_structure(F['bay39']['text'],bavarian=True)
    assert s['counts']['nummer']==16  # 2+3 in Abs. 1, 3 in Abs. 3, 8 in Abs. 4
    assert find_unit(s,'abs/1/satz/2/nr/3')['text']=='ein unverhältnismäßiger Aufwand entsteht.'
    assert find_unit(s,'abs/1/satz/1/nr/2')['text'].startswith('Belange der öffentlichen Sicherheit')
    assert find_unit(s,'abs/4/satz/1/nr/8')['text']=='die kommunalen Spitzenverbände.'
    assert find_unit(s,'abs/1/satz/2/nr/4') is None
    for u in iter_units(s):
        assert 0 <= u['start'] <= u['end'] <= len(F['bay39']['text'])

@pytest.mark.parametrize('second',['§ 2 gilt entsprechend.','§§ 2 und 3 gelten entsprechend.','20 Personen sind zugelassen.','(Weitere Regeln gelten.)','Art. 3 gilt entsprechend.'])
def test_reference_or_number_can_start_a_sentence(second):
    s=parse_norm_structure('Dies gilt. '+second)
    assert s['counts']['satz']==2

@pytest.mark.parametrize('text',['Nach Art. 2 gilt dies.','Nach § 2 Abs. 1 Satz 1 gilt dies.','Dies gilt am 1. Januar 2026.','Dies ist z. B. zulässig.'])
def test_abbreviations_ordinals_dates_stay_inside_sentence(text):
    s=parse_norm_structure(text)
    assert s['counts']['satz']==1
    assert s['counts']['nummer']==0
