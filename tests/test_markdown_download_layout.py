"""Downloads may add verified Absatz whitespace without changing machine Markdown."""
from __future__ import annotations

import hashlib
import json

import pytest

from api.act_archive import render_markdown_snapshot
from api.markdown_layout import apply_paragraph_layout, source_paragraph_layouts


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _text(text: str) -> dict:
    return {'type': 'text', 'text': text}


def _node(tag: str, *children: dict) -> dict:
    return {'type': 'element', 'tag': tag, 'children': list(children)}


def _payload(enbez: str, text: str, blocks: list[dict], *, indexed=False) -> dict:
    record = {'text_sha256': _sha(text), 'blocks': blocks}
    if indexed:
        return {'schema_version': 1, 'norms': {},
                'norms_by_index': {'0': {'enbez': enbez, **record}}}
    return {'schema_version': 1, 'norms': {enbez: record}, 'norms_by_index': {}}


# Actual PStG § 4 source paragraphs; canonical ingestion joins these P nodes.
PSTG4 = [
    '(1) Die Beurkundungen in einem Personenstandsregister sind nach ihrem Abschluss (§ 3 Abs. 2) in einem weiteren elektronischen Register (Sicherungsregister) zu speichern.',
    '(2) Das Sicherungsregister ist wie das Personenstandsregister am Ende des Jahres abzuschließen. Es ist nach Fortführung des Personenstandsregisters zu aktualisieren.',
]


@pytest.mark.parametrize('indexed', [False, True])
def test_actual_pstg4_download_separates_source_paragraphs(indexed):
    raw = ''.join(PSTG4)
    payload = _payload('§ 4', raw, [_node('p', _text(p)) for p in PSTG4], indexed=indexed)
    untouched = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    layouts = source_paragraph_layouts([payload])
    assert ('§ 4', _sha(raw)) in layouts
    assert len(PSTG4[0]) in layouts[('§ 4', _sha(raw))]
    assert apply_paragraph_layout('§ 4', raw, layouts) == '\n\n'.join(PSTG4)
    assert json.dumps(payload, ensure_ascii=False, sort_keys=True) == untouched


def test_bavarian_div_markers_and_sentence_superscripts_are_not_confused():
    first = '(1) 1Erster Satz. 2Zweiter Satz.'
    second = '\n(2) Dritter Satz.'
    raw = first + second
    blocks = [_node('div', _text('(1)'),
                    {'type': 'element', 'tag': 'sup', 'attrs': {'role': 'sentence'},
                     'children': [_text(' 1')]},
                    _text('Erster Satz. 2Zweiter Satz.')),
              _node('div', _text(second))]
    layouts = source_paragraph_layouts([_payload('Art. 29', raw, blocks)])
    assert apply_paragraph_layout('Art. 29', raw, layouts) == first + '\n\n' + second.lstrip()


def test_inline_parenthetical_references_never_create_paragraph_boundaries():
    raw = '(1) Nach Absatz (2) gilt dies. Die Nummer (3) bleibt.(2) Weiter im gleichen Quellabsatz.'
    layouts = source_paragraph_layouts([_payload('§ 1', raw, [_node('p', _text(raw))])])
    assert apply_paragraph_layout('§ 1', raw, layouts) == raw
    assert apply_paragraph_layout('§ 1', raw, {}) == raw


@pytest.mark.parametrize('container', ['table', 'pre', 'ul', 'ol', 'dl'])
def test_paragraph_like_markers_inside_tables_pre_and_lists_stay_untouched(container):
    first, excluded, last = '(1) Anfang.', '(99) Tabellen- oder Listenwert.', '(2) Ende.'
    raw = first + excluded + last
    blocks = [_node('p', _text(first)), _node(container, _node('p', _text(excluded))),
              _node('p', _text(last))]
    layouts = source_paragraph_layouts([_payload('§ 1', raw, blocks)])
    assert apply_paragraph_layout('§ 1', raw, layouts) == first + excluded + '\n\n' + last


def test_stale_or_internally_corrupted_source_hash_cannot_split_current_text():
    first, second = '(1) Anfang.', '(2) Ende.'
    raw = first + second
    payload = _payload('§ 1', raw, [_node('p', _text(first)), _node('p', _text(second))])
    payload['norms']['§ 1']['text_sha256'] = '0' * 64
    assert apply_paragraph_layout('§ 1', raw, source_paragraph_layouts([payload])) == raw
    payload['norms']['§ 1']['text_sha256'] = _sha(raw)
    payload['norms']['§ 1']['blocks'][0]['children'][0]['text'] = '(1) Fremder Text.'
    assert apply_paragraph_layout('§ 1', raw, source_paragraph_layouts([payload])) == raw
    valid = source_paragraph_layouts([_payload('§ 1', raw, [_node('p', _text(first)), _node('p', _text(second))])])
    changed = '(1) Historisch geändert.(2) Ende.'
    assert apply_paragraph_layout('§ 1', changed, valid) == changed


def test_only_existing_newline_markers_are_a_structure_free_fallback():
    raw = '(1) Alt.\n(2) Historischer Absatz.\n\n(3) Schon getrennt.\nFortsetzung (4).'
    expected = '(1) Alt.\n\n(2) Historischer Absatz.\n\n(3) Schon getrennt.\nFortsetzung (4).'
    assert apply_paragraph_layout('Art. 1', raw, {}) == expected
    assert apply_paragraph_layout('Art. 1', expected, {}) == expected


def test_verified_layout_is_idempotent_and_preserves_all_non_whitespace_characters():
    pieces = ['(1) Eins.', ' \t\n(1a) Zwei.', '\n\n(2) Drei.']
    raw = ''.join(pieces)
    layouts = source_paragraph_layouts([_payload('§ 1', raw, [_node('p', _text(p)) for p in pieces])])
    formatted = apply_paragraph_layout('§ 1', raw, layouts)
    assert formatted == '\n\n'.join(p.strip() for p in pieces)
    assert apply_paragraph_layout('§ 1', formatted, layouts) == formatted
    assert ''.join(formatted.split()) == ''.join(raw.split())


def test_conflicting_structure_for_same_label_and_body_is_not_arbitrarily_selected():
    first, second = '(1) Eins.', '(2) Zwei.'
    raw = first + second
    split = _payload('§ 1', raw, [_node('p', _text(first)), _node('p', _text(second))])
    joined = _payload('§ 1', raw, [_node('p', _text(raw))], indexed=True)
    for payloads in ([split, joined], [joined, split]):
        layouts = source_paragraph_layouts(payloads)
        assert ('§ 1', _sha(raw)) not in layouts
        assert apply_paragraph_layout('§ 1', raw, layouts) == raw
    assert apply_paragraph_layout('§ 1', raw, source_paragraph_layouts([split, split])) == first + '\n\n' + second


def test_opt_in_rendering_leaves_machine_markdown_and_canonical_body_unchanged():
    raw = ''.join(PSTG4)
    act = {'id': 'fed_pstg', 'jurabk': 'PStG', 'juris': 'DE',
           'title': 'Personenstandsgesetz', 'build': '20260927', 'versions': [],
           'norms': [{'enbez': '§ 4', 'titel': 'Sicherungsregister', 'text': raw}]}
    options = {'fallback_head': '2026-09-27', 'norm': '§ 4'}
    before = render_markdown_snapshot(act, **options)
    layouts = source_paragraph_layouts([_payload('§ 4', raw, [_node('p', _text(p)) for p in PSTG4])])
    download = render_markdown_snapshot(act, paragraph_layouts=layouts, **options)
    after = render_markdown_snapshot(act, **options)
    assert before['markdown'] == after['markdown']
    assert raw in before['markdown'] and '\n\n'.join(PSTG4) in download['markdown']
    assert 'paragraph_layout: separated\n' in download['markdown']
    assert 'paragraph_layout: separated' not in before['markdown']
    assert act['norms'][0]['text'] == raw
    assert before['source_exact'] is download['source_exact'] is True


def test_existing_newline_inside_known_pre_is_not_reclassified_as_a_paragraph():
    first, pre, second = '(1) Anfang.', '(8) Formzeile.\n(9) Zweite Formzeile.', '(2) Ende.'
    raw = first + pre + second
    payload = _payload('§ 1', raw, [_node('p', _text(first)),
                                  _node('pre', _text(pre)), _node('p', _text(second))])
    layouts = source_paragraph_layouts([payload])
    assert apply_paragraph_layout('§ 1', raw, layouts) == first + pre + '\n\n' + second


def _api_fixture(monkeypatch, *, text):
    from api import main
    act = {'id': 'fed_testg', 'jurabk': 'TestG', 'juris': 'DE',
           'title': 'Testgesetz', 'build': '20260927', 'versions': [],
           'norms': [{'enbez': '§ 1', 'titel': 'Titel', 'text': text}]}
    monkeypatch.setattr(main, '_load_act', lambda _aid: act)
    monkeypatch.setattr(main, '_archive_head_fallback', lambda: '2026-09-27')
    return main, act


def test_api_adds_layout_only_for_attachment_download(monkeypatch):
    from fastapi.testclient import TestClient
    first, second = '(1) Eins.', '(2) Zwei.'
    raw = first + second
    main, act = _api_fixture(monkeypatch, text=raw)
    monkeypatch.setattr(main, '_resolve_archive_states', lambda *args: (None, None, None))
    loads = []
    def load(directory, current):
        assert current is act
        loads.append('HEAD')
        return _payload('§ 1', raw, [_node('p', _text(first)), _node('p', _text(second))])
    monkeypatch.setattr(main, 'load_sidecar', load)
    client = TestClient(main.app)
    plain = client.get('/acts/fed_testg/markdown')
    explicit_false = client.get('/acts/fed_testg/markdown?download=false')
    assert plain.status_code == explicit_false.status_code == 200
    assert plain.text == explicit_false.text and loads == []
    attachment = client.get('/acts/fed_testg/markdown?download=true')
    assert attachment.status_code == 200 and loads
    assert first + '\n\n' + second in attachment.text
    assert raw in plain.text and 'paragraph_layout: separated' not in plain.text
    assert 'paragraph_layout: separated\n' in attachment.text
    assert attachment.headers['content-disposition'].startswith('attachment;')
    assert 'content-disposition' not in plain.headers
    assert act['norms'][0]['text'] == raw


@pytest.mark.parametrize('stale_historical_hash', [False, True])
def test_api_historical_download_uses_selected_body_hash_not_head(monkeypatch, stale_historical_hash):
    from fastapi.testclient import TestClient
    first, second = '(1) Alter Text.', '(2) Alter Schluss.'
    raw = first + second
    current = '(1) Heutiger Text.(2) Heutiger Schluss.'
    main, act = _api_fixture(monkeypatch, text=current)
    selected = {**act, 'observed_at': '2026-07-16', 'source': 'GII',
                'date_basis': 'retrieval_observation_not_effective_date',
                'state_sha256': 'a' * 64,
                'norms': [{'enbez': '§ 1', 'titel': 'Alter Titel', 'text': raw}]}
    monkeypatch.setattr(main, '_resolve_archive_states', lambda *args: (None, None, selected))
    current_payload = _payload('§ 1', current, [_node('p', _text(current))])
    monkeypatch.setattr(main, 'load_sidecar', lambda *args: current_payload)
    archive_calls = []
    def load(directory, current_act, state, *, requested_at):
        assert current_act is act and state['state_sha256'] == 'a' * 64
        assert state['norms'][0]['text'] == raw and requested_at == '2026-07-16'
        archive_calls.append(requested_at)
        result = _payload('§ 1', raw, [_node('p', _text(first)), _node('p', _text(second))], indexed=True)
        if stale_historical_hash:
            result['norms_by_index']['0']['text_sha256'] = _sha(current)
        return {**result, 'act_id': act['id'], 'state_sha256': state['state_sha256'], 'requested_at': requested_at}
    monkeypatch.setattr(main, 'load_archived_reader', load)
    client = TestClient(main.app)
    ordinary = client.get('/acts/fed_testg/markdown?at=2026-07-16')
    assert ordinary.status_code == 200 and archive_calls == []
    attachment = client.get('/acts/fed_testg/markdown?at=2026-07-16&download=true')
    assert attachment.status_code == 200 and archive_calls == ['2026-07-16']
    expected = raw if stale_historical_hash else first + '\n\n' + second
    assert expected in attachment.text and current not in attachment.text
    assert '## § 1 — Alter Titel' in ordinary.text and raw in ordinary.text
    assert ordinary.headers['x-lexgraph-state-sha256'] == attachment.headers['x-lexgraph-state-sha256'] == 'a' * 64
    assert ordinary.headers['x-lexgraph-source-exact'] == attachment.headers['x-lexgraph-source-exact'] == 'true'
    assert selected['norms'][0]['text'] == raw


def _whitespace_source_fixture():
    first, tail = '(1) Nach Absatz (2) bleibt dies.', '(2) Ende.'
    raw = first + '1.Listenwert' + tail
    blocks = [_node('p', _text('  ' + first + '\n')),
              _node('dl', _node('dt', _text('1. ')),
                    _node('dd', _text('Listenwert\n'))),
              _node('p', _text('\n' + tail))]
    return raw, first + '1.Listenwert\n\n' + tail, _payload('§ 1', raw, blocks)


def test_source_whitespace_can_align_only_to_hash_bound_canonical_body_without_mutation():
    raw, expected, payload = _whitespace_source_fixture()
    original = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    layouts = source_paragraph_layouts([payload], norm_bodies=[{'enbez': '§ 1', 'text': raw}])
    assert apply_paragraph_layout('§ 1', raw, layouts) == expected
    assert 'Absatz (2)' in expected and 'Absatz\n\n(2)' not in expected
    assert json.dumps(payload, sort_keys=True, ensure_ascii=False) == original
    assert apply_paragraph_layout('§ 1', expected, layouts) == expected


@pytest.mark.parametrize('corruption', ['source_letters', 'canonical_hash', 'canonical_label', 'record_hash'])
def test_whitespace_alignment_never_authorizes_changed_source_or_unbound_canonical_body(corruption):
    raw, _expected, payload = _whitespace_source_fixture()
    canonical = {'enbez': '§ 1', 'text': raw}
    if corruption == 'source_letters':
        payload['norms']['§ 1']['blocks'][0]['children'][0]['text'] = '  (1) Anderer Text.\n'
    elif corruption == 'canonical_hash':
        canonical['text'] = raw + ' '
    elif corruption == 'canonical_label':
        canonical['enbez'] = '§ 2'
    else:
        payload['norms']['§ 1']['text_sha256'] = 'f' * 64
    layouts = source_paragraph_layouts([payload], norm_bodies=[canonical])
    assert ('§ 1', _sha(raw)) not in layouts
    assert apply_paragraph_layout('§ 1', raw, layouts) == raw


def test_aligned_source_still_excludes_numbered_paragraph_shaped_list_children():
    first, item, last = '(1) Anfang.', '(9) Listeneintrag.', '(2) Ende.'
    raw = first + item + last
    payload = _payload('§ 1', raw, [
        _node('p', _text(first + '\n')),
        _node('dl', _node('dd', _node('p', _text(' ' + item + '\n')))),
        _node('p', _text(last)),
    ])
    layouts = source_paragraph_layouts([payload], norm_bodies=[{'enbez': '§ 1', 'text': raw}])
    assert apply_paragraph_layout('§ 1', raw, layouts) == first + item + '\n\n' + last
