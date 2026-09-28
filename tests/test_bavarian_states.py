"""Archived official HTML must be complete, attributable, and durable."""
import gzip
import hashlib
import json
from pathlib import Path

import pytest

from tools.bavarian_states import (
    BavarianStateError, capture_html, publish_states, parse_capture,
)

ORIGINAL = 'http://www.gesetze-bayern.de:80/Content/Document/BayVwVfG/true'
STAMP = '20160129083149'
ARCHIVE = f'https://web.archive.org/web/{STAMP}id_/{ORIGINAL}'
HTML = '''<!doctype html><html><body><div id="content">
<div id="doc-metadata"><b>BayVwVfG</b> in Kraft ab: 30.12.2015</div>
<div id="doc-toolbar"><a href="/Content/Pdf/BayVwVfG?all=True">PDF</a></div>
<div class="cont"><h1 class="titelzeile">Bayerisches Verwaltungsverfahrensgesetz (BayVwVfG)</h1>
<div class="standangabe">Zuletzt geändert 2015.</div>
<div class="subdoc"><a href="/Content/Document/BayVwVfG-1">Art. 1 Titel</a></div>
<div class="subdoc"><a href="/Content/Document/BayVwVfG-8a">§ 8a Leer</a></div></div>
<div class="cont"><h2 class="gltitel">Erster Teil</h2></div>
<div class="cont"><div class="paraheading"><div class="paranr">Art. 1</div><div class="paratitel">Titel</div></div>
<div class="absatz paratext">(1) <sup class="satznr">1</sup>Erster Satz.<sup class="satznr">2</sup>Zweiter Satz.</div>
<div class="absatz paratext">(2) Liste:</div><dl><dt>1.</dt><dd><div class="paratext">Eintrag<sup><a class="footnotelink-down" href="#FN1">1</a></sup>.</div><dl><dt>a)</dt><dd>Unterpunkt</dd></dl></dd></dl>
<hr class="fn"><div class="absatz fn"><a id="FN1"></a><sup>1</sup><b>[Amtl. Anm.:]</b> Anmerkung.</div></div>
<div class="cont"><div class="paraheading"><div class="paranr">§ 8a</div><div class="paratitel">(aufgehoben)</div></div></div>
</div></body></html>'''


def arguments():
    return dict(act_id='by_bayvwvfg', jurabk='BayVwVfG', document_key='BayVwVfG',
                original_url=ORIGINAL, archive_url=ARCHIVE, archive_timestamp=STAMP,
                retrieved_at='2026-09-27T10:00:00Z')


def test_full_capture_keeps_source_labels_paragraphs_notes_and_observation_semantics():
    state, obs = parse_capture(HTML.encode(), **arguments())
    assert state['norm_count'] == 2
    assert [n['enbez'] for n in state['norms']] == ['Art. 1', '§ 8a']
    text = state['norms'][0]['text']
    assert '(1) 1 Erster Satz.\n\n2 Zweiter Satz.' in text
    assert '\n\n(2) Liste:' in text
    assert '1. Eintrag 1 .' in text and 'a) Unterpunkt' in text
    assert '[Amtl. Anm.:] Anmerkung.' in text
    assert state['norms'][1]['text'] == ''
    assert state['norms'][0]['glied'] == ['Erster Teil']
    assert obs['observed_at'] == '2016-01-29'
    assert obs['archive_timestamp'] == STAMP and obs['archive_url'] == ARCHIVE
    assert obs['source_url'] == ORIGINAL
    assert obs['date_basis'] == 'archive_capture_not_effective_date'
    assert 'effective_at' not in obs


@pytest.mark.parametrize('html', [
    HTML[:-15], HTML.replace('BayVwVfG</b>', 'AGSG</b>'),
    HTML.replace('<div class="paranr">§ 8a</div>', '<div class="paranr">Art. 9</div>'),
    HTML.replace('/Content/Pdf/BayVwVfG?all=True', '/Content/Pdf/BayVwVfG'),
    HTML.replace('class="paranr">§ 8a', 'class="paranr">Art. 1'),
])
def test_incomplete_or_wrong_document_rejected(html):
    with pytest.raises(BavarianStateError):
        parse_capture(html.encode(), **arguments())


@pytest.mark.parametrize('field,value', [
    ('original_url', 'https://evil.example/Content/Document/BayVwVfG/true'),
    ('archive_url', ARCHIVE.replace(STAMP, '20170129083149')),
    ('archive_timestamp', '20169999000000'),
])
def test_provenance_cannot_claim_unrelated_capture(field, value):
    args = arguments(); args[field] = value
    with pytest.raises(BavarianStateError):
        parse_capture(HTML.encode(), **args)


def test_store_survives_refresh_with_verification_and_idempotence(tmp_path):
    store = tmp_path / 'source'; public = tmp_path / 'public'
    first = capture_html(store, HTML.encode(), **arguments())
    second = capture_html(store, HTML.encode(), **arguments())
    assert first == second
    manifest = publish_states(store, public)
    assert manifest['total_states'] == manifest['total_observations'] == 1
    digest = first['state_sha256']
    path = public / 'objects' / 'sha256' / digest[:2] / f'{digest}.json.gz'
    assert path.stat().st_mode & 0o777 == 0o644
    assert (public / 'manifest.json').stat().st_mode & 0o777 == 0o644
    raw = gzip.decompress(path.read_bytes())
    assert hashlib.sha256(raw).hexdigest() == digest
    assert json.loads(raw)['norm_count'] == 2
    source = next((store / 'sources').rglob('*.html.gz'))
    source.write_bytes(gzip.compress(b'corrupt'))
    with pytest.raises(BavarianStateError):
        publish_states(store, tmp_path / 'bad-public')
    assert not (tmp_path / 'bad-public').exists()


def test_conflicting_same_capture_is_not_overwritten(tmp_path):
    capture_html(tmp_path, HTML.encode(), **arguments())
    before = (tmp_path / 'manifest.json').read_bytes()
    with pytest.raises(BavarianStateError):
        capture_html(tmp_path, HTML.replace('Erster Satz.', 'Anderer Satz.').encode(), **arguments())
    assert (tmp_path / 'manifest.json').read_bytes() == before


def test_missing_store_does_not_remove_prior_publication(tmp_path):
    assert publish_states(tmp_path / 'missing', tmp_path / 'out')['observations'] == []


def test_real_2016_full_capture_preserves_all_norms_and_sentence_boundaries():
    raw = gzip.decompress((Path(__file__).parent / 'fixtures' / 'bayvwvfg-20160129083149.html.gz').read_bytes())
    assert hashlib.sha256(raw).hexdigest() == '7b6a7e52ec59de65ed1eb22516911fcb286349cf0a539c90baa176bd33597b40'
    state, obs = parse_capture(raw, **arguments())
    assert state['norm_count'] == 127
    norms = {row['enbez']: row for row in state['norms']}
    assert list(norms)[:3] == ['Art. 1', 'Art. 2', 'Art. 3']
    assert '§ 8a' in norms and 'Art. 8a' not in norms
    assert norms['Art. 98']['titel'] == '(aufgehoben)' and norms['Art. 98']['text'] == ''
    assert '\n\n2 Es ist einfach' in norms['Art. 10']['text']
    assert '\n\n2 Satz 1 gilt' in norms['Art. 29']['text']
    assert '\n\n(3) 1 Die Akteneinsicht' in norms['Art. 29']['text']
    assert '\n\n2 Die Behörde muß' in norms['Art. 61']['text']
    assert '\n\n3 Richtet sich die Vollstreckung' in norms['Art. 61']['text']
    assert '\n\n1 [Amtl. Anm.:] BGBl. FN 301-1' in norms['Art. 61']['text']
    assert '\n\n2 Ist der Widerspruch' in norms['Art. 80']['text']
    assert '\n\n3 Ist der Widerspruch' in norms['Art. 80']['text']
    assert '\n\n1. eines bestehenden' in norms['Art. 80']['text']
    assert '\n\na) eine natürliche Person' in norms['Art. 3']['text']
    assert obs['observed_at'] == '2016-01-29'


def test_public_builder_attaches_bavaria_without_overwriting_current_text():
    from tools.build_web_data import attach_bavarian_observations
    import copy
    current = {'by_bayvwvfg': {'id': 'by_bayvwvfg', 'jurabk': 'BayVwVfG', 'juris': 'DE-BY',
                              'norms': [{'enbez': 'Art. 1', 'text': 'Current text'}]}}
    norms = copy.deepcopy(current['by_bayvwvfg']['norms'])
    _, obs = parse_capture(HTML.encode(), **arguments())
    attach_bavarian_observations(current, {'observations': [obs]})
    assert current['by_bayvwvfg']['norms'] == norms
    assert current['by_bayvwvfg']['official_states'] == [obs]
    obs['jurabk'] = 'AGSG'
    with pytest.raises(ValueError, match='identity'):
        attach_bavarian_observations(current, {'observations': [obs]})


def test_real_2016_pag_full_capture_preserves_all_norms_and_sentences():
    raw = gzip.decompress((Path(__file__).parent / 'fixtures' / 'baypag-20160318102700.html.gz').read_bytes())
    assert hashlib.sha256(raw).hexdigest() == '34be566cf1769a5f3b210ccb908ebdb1235aa10e83397be6c926134a1368694c'
    original = 'http://gesetze-bayern.de:80/Content/Document/BayPAG/true'
    state, obs = parse_capture(raw, act_id='by_pag', jurabk='PAG', document_key='BayPAG',
                              original_url=original, archive_url=f'https://web.archive.org/web/20160318102700id_/{original}',
                              archive_timestamp='20160318102700', retrieved_at='2026-09-27T10:49:20Z')
    assert state['norm_count'] == 83
    norms = {row['enbez']: row for row in state['norms']}
    assert list(norms)[0] == 'Art. 1' and list(norms)[-1] == 'Art. 78'
    assert '\n\n(2) 1 Kommen zur Abwehr' in norms['Art. 5']['text']
    assert '\n\n2 Dem Betroffenen' in norms['Art. 5']['text']
    assert '\n\n2 Das Nähere' in norms['Art. 6']['text']
    assert '\n\n2 Dies gilt auch' in norms['Art. 7']['text']
    assert obs['state_sha256'] == 'dcf2ab11e0bb3849b90d0d8cd76f76004e5459ebbd6ec4ced3e4ee56e41e5cb0'
    assert obs['observed_at'] == '2016-03-18' and obs['date_basis'] == 'archive_capture_not_effective_date'


def test_identical_text_on_another_capture_date_reuses_state_identity():
    first, obs1 = parse_capture(HTML.encode(), **arguments())
    later = arguments()
    later['archive_timestamp'] = '20160229083149'
    later['archive_url'] = f"https://web.archive.org/web/20160229083149id_/{ORIGINAL}"
    second, obs2 = parse_capture(HTML.encode(), **later)
    assert obs1['observed_at'] != obs2['observed_at']
    assert first == second
    assert obs1['state_sha256'] == obs2['state_sha256']
