from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.reader_blocks import (  # noqa: E402
    ReaderBlocksError, build_sidecar, flatten_nodes, load_sidecar, source_url, text_hash,
)


def xml(body: str, *, enbez: str = "§ 1") -> bytes:
    return (f'<dokumente><norm><metadaten><enbez>{enbez}</enbez>'
            f'</metadaten><textdaten><text><Content>{body}</Content>'
            '</text></textdaten></norm></dokumente>').encode()


def act(text: str) -> dict:
    return {"id": "fed_testg", "norms": [{"enbez": "§ 1", "text": text}]}


def build(body: str, text: str) -> dict:
    return build_sidecar(xml(body), act(text), retrieved_at="2026-09-21T12:00:00Z")


def test_mixed_content_lists_tables_and_tails_are_preserved():
    body = ('lead<P>A <B>bold</B> tail</P>between<DL><DT>1.</DT>'
            '<DD><P>item</P></DD></DL><table><tbody><tr>'
            '<td colspan="2" rowspan="3" onclick="evil()">cell</td>'
            '</tr></tbody></table>end')
    sidecar = build(body, "leadA bold tailbetween1.itemcellend")
    nodes = sidecar["norms"]["§ 1"]["blocks"]
    assert flatten_nodes(nodes) == "leadA bold tailbetween1.itemcellend"
    assert nodes[1] == {"type": "element", "tag": "p", "children": [
        {"type": "text", "text": "A "},
        {"type": "element", "tag": "strong", "children": [{"type": "text", "text": "bold"}]},
        {"type": "text", "text": " tail"},
    ]}
    assert nodes[4]["children"][0]["children"][0]["children"][0]["attrs"] == {
        "colspan": 2, "rowspan": 3,
    }
    assert "onclick" not in json.dumps(nodes)


def test_unknown_wrappers_keep_text_and_no_executable_nodes():
    sidecar = build('<P><unknown>A</unknown><br/>tail</P>', "Atail")
    assert flatten_nodes(sidecar["norms"]["§ 1"]["blocks"]) == "Atail"
    assert "unknown" not in json.dumps(sidecar)
    # Script removal cannot silently certify different text as equivalent.
    assert build('<P>A<script>evil()</script>tail</P>', "Aevil()tail")["norms"] == {}


def test_export_rejects_changed_text_and_duplicate_norms():
    assert build('<P>New</P>', "Old")["norms"] == {}
    duplicate = xml('<P>A</P>').replace(b'</dokumente>', xml('<P>A</P>').split(b'<dokumente>')[1])
    assert build_sidecar(duplicate, act("A"), retrieved_at="now")["norms"] == {}


def test_official_doctype_and_cals_table():
    body = ('<table><tgroup><colspec colname="a"/><colspec colname="b"/>'
            '<thead><row><entry namest="a" nameend="b" morerows="1">Head<BR/>tail</entry>'
            '</row></thead></tgroup></table>')
    source = xml(body).replace(b'<dokumente>', b'<!DOCTYPE dokumente SYSTEM "https://www.gesetze-im-internet.de/dtd/1.01/gii-norm.dtd"><dokumente>')
    result = build_sidecar(source, act("Headtail"), retrieved_at="now")
    cell = result['norms']['§ 1']['blocks'][0]['children'][0]['children'][0]['children'][0]
    assert cell['tag'] == 'th'
    assert cell['attrs'] == {'colspan': 2, 'rowspan': 2}
    assert cell['children'][1] == {'type': 'element', 'tag': 'br', 'children': []}


def test_internal_entities_are_rejected():
    source = b'<!DOCTYPE dokumente [<!ENTITY foo "test">]>' + xml('<P>&foo;</P>')
    with pytest.raises(ReaderBlocksError):
        build_sidecar(source, act("test"), retrieved_at="now")


def test_paragraphs_split_around_block_children_for_valid_html():
    nodes = build('<P>Before<DL><DT>1.</DT><DD>item</DD></DL>after</P>',
                  'Before1.itemafter')['norms']['§ 1']['blocks']
    assert [node['tag'] for node in nodes] == ['p', 'dl', 'p']
    assert flatten_nodes(nodes) == 'Before1.itemafter'


def test_export_requires_local_act_before_network(tmp_path):
    from tools.reader_blocks import export_act
    class NoNetwork:
        def get(self, *args, **kwargs):
            pytest.fail('must not fetch unknown current acts')
    with pytest.raises(ReaderBlocksError):
        export_act(tmp_path, tmp_path / 'out', 'fed_missing', NoNetwork())


def test_export_cannot_replace_sidecar_with_mismatching_source(tmp_path):
    import io
    import zipfile
    from tools.reader_blocks import export_act
    (tmp_path / 'acts').mkdir()
    (tmp_path / 'acts' / 'fed_testg.json').write_text(json.dumps(act('Old')))
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w') as handle:
        handle.writestr('test.xml', xml('<P>New</P>'))
    class Response:
        url = 'https://www.gesetze-im-internet.de/testg/xml.zip'
        content = archive.getvalue()
        def raise_for_status(self): pass
    class Http:
        def get(self, *args, **kwargs): return Response()
    target = tmp_path / 'out'
    target.mkdir()
    old = target / 'fed_testg.json'
    old.write_text('unchanged')
    with pytest.raises(ReaderBlocksError):
        export_act(tmp_path, target, 'fed_testg', Http())
    assert old.read_text() == 'unchanged'


def test_duplicate_current_labels_are_omitted_not_arbitrarily_selected():
    current = act("A")
    current['norms'].extend([{'enbez':'Anlage','text':'first'}, {'enbez':'Anlage','text':'second'}])
    assert list(build_sidecar(xml('<P>A</P>'), current, retrieved_at='now')['norms']) == ['§ 1']


@pytest.mark.parametrize("act_id", ["../foo", "fed_../foo", "eu_reg", "fed_x?y", "fed_x/y", "fed_x\\y"])
def test_invalid_or_unsupported_ids(act_id):
    with pytest.raises(ReaderBlocksError):
        source_url(act_id)


@pytest.mark.parametrize("act_id,slug", [
    ("fed_rustag", "stag"), ("fed_azrg_dv", "azrg-dv"),
    ("fed_vwdg_dv", "vwdg-dv"),
])
def test_stable_public_ids_resolve_to_exact_gii_paths(act_id, slug):
    assert source_url(act_id) == f"https://www.gesetze-im-internet.de/{slug}/xml.zip"


def test_official_source_metadata_takes_precedence_without_allowing_other_origins():
    assert source_url("fed_old", "https://www.gesetze-im-internet.de/renamed-act/") == (
        "https://www.gesetze-im-internet.de/renamed-act/xml.zip")
    for url in ("https://example.test/new/", "https://www.gesetze-im-internet.de.evil.test/a/",
                "https://www.gesetze-im-internet.de/../secret/", "javascript:alert(1)"):
        with pytest.raises(ReaderBlocksError):
            source_url("fed_old", url)


def test_stored_blocks_are_rechecked_against_current_text(tmp_path):
    value = build('<P>Same</P>', "Same")
    (tmp_path / 'fed_testg.json').write_text(json.dumps(value))
    assert load_sidecar(tmp_path, act("Same"))["norms"]["§ 1"]["text_sha256"] == hashlib.sha256(b"Same").hexdigest()
    with pytest.raises(ReaderBlocksError):
        load_sidecar(tmp_path, act("Changed"))
    value["norms"]["§ 1"]["blocks"][0]["tag"] = "script"
    (tmp_path / 'fed_testg.json').write_text(json.dumps(value))
    with pytest.raises(ReaderBlocksError):
        load_sidecar(tmp_path, act("Same"))


def test_route_is_read_only_and_fails_closed(tmp_path, monkeypatch):
    from api import main
    data = tmp_path / 'data'
    (data / 'acts').mkdir(parents=True)
    readers = tmp_path / 'readers'
    readers.mkdir()
    act_path = data / 'acts' / 'fed_testg.json'
    act_path.write_text(json.dumps(act("Same")))
    monkeypatch.setattr(main, 'DATA_DIR', data)
    monkeypatch.setattr(main, 'READER_DIR', readers)
    client = TestClient(main.app)
    assert client.get('/acts/fed_testg/reader').status_code == 404
    (readers / 'fed_testg.json').write_text(json.dumps(build('<P>Same</P>', "Same")))
    response = client.get('/acts/fed_testg/reader')
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'no-cache'
    assert response.json()['norms']['§ 1']['blocks'][0]['tag'] == 'p'
    act_path.write_text(json.dumps(act("Changed")))
    assert client.get('/acts/fed_testg/reader').status_code == 404
    assert client.get('/acts/eu_test/reader').status_code == 404


def test_inline_source_markup_remains_inside_paragraph():
    body = '<P><SUP>1</SUP>A <I>word</I>, H<SUB>2</SUB>O and <U>under</U>.</P>'
    nodes = build(body, '1A word, H2O and under.')['norms']['§ 1']['blocks']
    assert len(nodes) == 1 and nodes[0]['tag'] == 'p'
    assert [node['tag'] for node in nodes[0]['children'] if node['type'] == 'element'] == ['sup', 'em', 'sub', 'u']
    assert flatten_nodes(nodes) == '1A word, H2O and under.'


def test_gii_explicit_rec_superscripts_are_sentence_markers_only():
    nodes = build('<P><SUP class="Rec">1</SUP>A<SUP>2</SUP>B<SUP class="Rec">x</SUP>C</P>',
                  '1A2BxC')['norms']['§ 1']['blocks'][0]['children']
    assert nodes[0]['attrs'] == {'role': 'sentence'}
    assert 'attrs' not in nodes[2]
    assert 'attrs' not in nodes[4]


def annotated_xml(body='<P>Current</P>', notes=None):
    if notes is None:
        notes = ('<Footnote FnZ="1" Postfix=")" Pos="exp">First <B>bold</B><BR/>line'
                 '<DL><DT>a)</DT><DD>detail</DD></DL></Footnote>'
                 '<Footnote FnZ="*" Pos="normal">Second</Footnote>')
    return xml(body).replace(b'</Content></text>',
        ('</Content><Footnotes>' + notes + '</Footnotes></text>').encode())


def test_gii_footnotes_are_separate_verified_annotations_not_canonical_body(tmp_path):
    value = build_sidecar(annotated_xml(), act('Current'), retrieved_at='now')
    norm = value['norms']['§ 1']
    assert flatten_nodes(norm['blocks']) == 'Current'
    assert norm['text_sha256'] == text_hash('Current')
    assert norm['source_annotations_text'] == '1) First boldlinea)detail* Second'
    assert norm['source_annotations_sha256'] == text_hash(norm['source_annotations_text'])
    assert flatten_nodes(norm['source_annotations']) == norm['source_annotations_text']
    first = norm['source_annotations'][0]['children']
    assert first[0]['children'][0] == {'type': 'element', 'tag': 'sup', 'attrs': {'role': 'footnote'}, 'children': [{'type': 'text', 'text': '1)'}]}
    assert first[1]['tag'] == 'dl'
    (tmp_path/'fed_testg.json').write_text(json.dumps(value))
    assert load_sidecar(tmp_path, act('Current'))['norms']['§ 1'] == norm
    assert build_sidecar(annotated_xml(), act('Changed'), retrieved_at='now')['norms'] == {}


def test_gii_alternate_fussnoten_preserve_pstg_application_and_constitutional_notes(tmp_path):
    notes = {
        '§ 12': '(+++ § 12 Abs. 3: Zur Nichtanwendung ab 1.7.2024 vgl. § 12a +++)',
        '§ 21': '§ 21 Abs. 1 Nr. 3 iVm § 22 Abs. 3: Nach Maßgabe der Entscheidungsformel mit Art. 2 Abs. 1 iVm  Art. 1 Abs 1 und mit Art. 3 Abs. 1 GG unvereinbar gem. BVerfGE v. 10.10.2017 I 3783 - 1 BvR 2019/16 -; der Gesetzgeber ist verpflichtet, bis zum 31.12.2018 eine verfassungsgemäße Regelung herbeizuführen. Zur Umsetzung der Anforderungen des BVerfG vgl. G v. 18.12.2018 I 2635 mWv 22.12.2018.',
    }
    parts = []
    for name, note in notes.items():
        parts.append(xml('<P>Current</P>', enbez=name).decode().removeprefix('<dokumente>').removesuffix('</dokumente>').replace(
            '</textdaten>', f'<fussnoten><Content><P>{note}</P></Content></fussnoten></textdaten>'))
    source = ('<dokumente>' + ''.join(parts) + '</dokumente>').encode()
    current = {'id': 'fed_testg', 'norms': [{'enbez': name, 'text': 'Current'} for name in notes]}
    value = build_sidecar(source, current, retrieved_at='now')
    for name, note in notes.items():
        norm = value['norms'][name]
        assert flatten_nodes(norm['blocks']) == 'Current'
        assert norm['text_sha256'] == text_hash('Current')
        assert norm['source_annotations_text'] == note
        assert flatten_nodes(norm['source_annotations']) == note
    (tmp_path/'fed_testg.json').write_text(json.dumps(value))
    assert load_sidecar(tmp_path, current)['norms'] == value['norms']


def test_alternate_notes_combine_with_footnotes_and_preserve_preformatted_whitespace():
    source = annotated_xml(notes='<Footnote FnZ="*" Pos="exp">First</Footnote>').replace(
        b'</textdaten>', b'<fussnoten><Content><P>Application<BR/>note</P><pre>  A    B\n  C    D</pre></Content></fussnoten></textdaten>')
    norm = build_sidecar(source, act('Current'), retrieved_at='now')['norms']['§ 1']
    assert norm['source_annotations_text'] == '* FirstApplicationnote  A    B\n  C    D'
    assert norm['source_annotations'][1]['children'][1]['tag'] == 'pre'
    assert norm['source_annotations'][1]['children'][0]['children'][1]['tag'] == 'br'


def test_alternate_notes_never_attach_act_header_to_an_arbitrary_norm():
    header = '<norm><metadaten><titel>Act header</titel></metadaten><textdaten><text><Content><P>Header body</P></Content></text><fussnoten><Content><P>Header note</P></Content></fussnoten></textdaten></norm>'
    source = xml('<P>Current</P>').replace(b'<dokumente>', ('<dokumente>' + header).encode())
    current = act('Current')
    current['norms'].insert(0, {'enbez': '', 'text': 'Header body'})
    value = build_sidecar(source, current, retrieved_at='now')
    assert 'source_annotations' not in value['norms']['§ 1']
    assert 'source_annotations' not in value['norms_by_index']['0']


@pytest.mark.parametrize('damage', ['text', 'hash', 'script', 'attrs', 'missing_hash'])
def test_gii_annotation_payload_fails_closed_on_corruption(tmp_path, damage):
    value = build_sidecar(annotated_xml(), act('Current'), retrieved_at='now')
    norm = value['norms']['§ 1']
    if damage == 'text':
        norm['source_annotations_text'] += ' altered'
    elif damage == 'hash':
        norm['source_annotations_sha256'] = '0' * 64
    elif damage == 'script':
        norm['source_annotations'][0]['tag'] = 'script'
    elif damage == 'attrs':
        norm['source_annotations'][0]['attrs'] = {'onclick': 'evil()'}
    else:
        del norm['source_annotations_sha256']
    (tmp_path/'fed_testg.json').write_text(json.dumps(value))
    with pytest.raises(ReaderBlocksError):
        load_sidecar(tmp_path, act('Current'))


@pytest.mark.parametrize('kind', ['text', 'nodes', 'depth'])
def test_source_annotations_have_bounded_size_and_depth(tmp_path, kind):
    value = build('<P>Current</P>', 'Current')
    if kind == 'text':
        nodes = [{'type': 'text', 'text': 'x' * 4_000_001}]
    elif kind == 'nodes':
        nodes = [{'type': 'text', 'text': ''}] * 50_001
    else:
        nodes = [{'type': 'text', 'text': ''}]
        for _ in range(66):
            nodes = [{'type': 'element', 'tag': 'div', 'children': nodes}]
    text = 'x' * 4_000_001 if kind == 'text' else ''
    value['norms']['§ 1'].update(source_annotations=nodes, source_annotations_text=text,
                                source_annotations_sha256=text_hash(text))
    (tmp_path/'fed_testg.json').write_text(json.dumps(value))
    with pytest.raises(ReaderBlocksError):
        load_sidecar(tmp_path, act('Current'))


def test_indexed_duplicate_norm_annotations_are_attached_only_to_the_matching_record(tmp_path):
    source = annotated_xml().replace(b'</dokumente>', b'') + annotated_xml('<P>Other</P>', '<Footnote>Other note</Footnote>').split(b'<dokumente>', 1)[1]
    current = {'id': 'fed_testg', 'norms': [{'enbez': '§ 1', 'text': 'Current'}, {'enbez': '§ 1', 'text': 'Other'}]}
    value = build_sidecar(source, current, retrieved_at='now')
    assert value['norms'] == {}
    assert value['norms_by_index']['1']['source_annotations_text'] == 'Other note'
    (tmp_path/'fed_testg.json').write_text(json.dumps(value))
    assert load_sidecar(tmp_path, current)['norms_by_index'] == value['norms_by_index']


def test_reader_route_returns_separate_annotations(tmp_path, monkeypatch):
    from api import main
    data = tmp_path/'data'
    (data/'acts').mkdir(parents=True)
    readers = tmp_path/'readers'
    readers.mkdir()
    (data/'acts/fed_testg.json').write_text(json.dumps(act('Current')))
    (readers/'fed_testg.json').write_text(json.dumps(build_sidecar(annotated_xml(), act('Current'), retrieved_at='now')))
    monkeypatch.setattr(main, 'DATA_DIR', data)
    monkeypatch.setattr(main, 'READER_DIR', readers)
    response = TestClient(main.app).get('/acts/fed_testg/reader')
    assert response.status_code == 200
    assert response.json()['norms']['§ 1']['source_annotations_text'] == '1) First boldlinea)detail* Second'


def test_bavarian_source_url_uses_validated_document_permalink():
    assert source_url('by_agsg', 'https://www.gesetze-bayern.de/Content/Document/BayAGSG') == 'https://www.gesetze-bayern.de/Content/Zip/BayAGSG'
    for bad in ('https://example.test/Content/Document/BayAGSG', 'https://www.gesetze-bayern.de/Content/Document/../BayAGSG'):
        with pytest.raises(ReaderBlocksError):
            source_url('by_agsg', bad)


def test_source_cache_can_rebuild_structure_offline_without_changing_text(tmp_path):
    from tools.reader_blocks import export_act
    (tmp_path / 'acts').mkdir()
    (tmp_path / 'acts' / 'fed_testg.json').write_text(json.dumps(act('1A')))
    cache = tmp_path / 'source'
    cache.mkdir()
    (cache / 'fed_testg.xml').write_bytes(xml('<P><SUP>1</SUP>A</P>'))
    class NoNetwork:
        def get(self, *args, **kwargs):
            pytest.fail('cached official XML should rebuild without network')
    export_act(tmp_path, tmp_path / 'out', 'fed_testg', NoNetwork(), source_cache=cache)
    value = load_sidecar(tmp_path / 'out', act('1A'))
    assert value['norms']['§ 1']['blocks'][0]['children'][0]['tag'] == 'sup'


def test_bavarian_builder_preserves_paragraph_sentence_and_nested_list_structure(tmp_path):
    from tools.reader_blocks import build_bayern_sidecar
    current = {'id': 'by_agsg', 'permalink': 'https://www.gesetze-bayern.de/Content/Document/BayAGSG',
               'norms': [{'enbez': 'Art. 7', 'text': '(1) 1A. 2B.\n(2) Items: 1. First a) nested'}]}
    source = b'''<byrecht-norm><einzelnorm><para.nr>Art. 7</para.nr><para.titel>Heading</para.titel><jurAbsatz>(1) <sup>1</sup>A. <sup>2</sup>B.</jurAbsatz><jurAbsatz>(2) Items: <liste><li><symbol>1.</symbol> <absatz>First <liste><li><symbol>a)</symbol> <absatz>nested</absatz></li></liste></absatz></li></liste></jurAbsatz></einzelnorm></byrecht-norm>'''
    value = build_bayern_sidecar(source, current, retrieved_at='now')
    (tmp_path / 'by_agsg.json').write_text(json.dumps(value))
    blocks = load_sidecar(tmp_path, current)['norms']['Art. 7']['blocks']
    assert flatten_nodes(blocks) == current['norms'][0]['text']
    assert blocks[0]['tag'] == 'div'
    tags = []
    def visit(nodes):
        for node in nodes:
            if node['type'] == 'element':
                tags.append(node['tag'])
                visit(node['children'])
    visit(blocks)
    assert tags.count('sup') == 2
    assert tags.count('dl') == tags.count('dt') == tags.count('dd') == 2
    changed = source.replace(b'nested', b'changed')
    assert build_bayern_sidecar(changed, current, retrieved_at='now')['norms'] == {}


def test_bavarian_html_sentence_markers_and_inline_footnotes_keep_exact_canonical_order():
    from tools.reader_blocks import build_bayern_html_sidecar
    current = {'id': 'by_bayvwvfg', 'permalink': 'https://www.gesetze-bayern.de/Content/Document/BayVwVfG',
               'norms': [{'enbez': 'Art. 10', 'text': '1Das A. 2Es B.'},
                         {'enbez': 'Art. 2', 'text': '(1) List: 1. Abgabenordnung1)BGBl. FN 610-1-3, 2. Next'}]}
    html = '''<html><body><div id="content">
      <div class="cont"><div class="paraheading"><div class="paranr">Art. 10</div></div>
      <div class="absatz paratext"><sup class="satznr">1</sup>Das A. <sup class="satznr">2</sup>Es B.</div></div>
      <div class="cont"><div class="paraheading"><div class="paranr">Art. 2</div></div>
      <div class="absatz paratext">(1) List:</div><dl><dt>1.</dt><dd><div>Abgabenordnung<sup><a class="footnotelink-down" href="#FN1">1)</a></sup>,</div></dd><dd class="clearer"></dd><dt>2.</dt><dd>Next</dd></dl>
      <hr class="fn"><div class="fn-container"><div class="fn"><a class="footnoteanchor" id="FN1"></a><sup><a class="footnotelink-up">1)</a></sup><b>[Amtl. Anm.:]</b> BGBl. FN 610-1-3</div></div></div>
      </div></body></html>'''
    value = build_bayern_html_sidecar(html, current, retrieved_at='now')
    assert set(value['norms']) == {'Art. 10', 'Art. 2'}
    assert value['source_url'] == current['permalink'] + '/true'
    sentence = value['norms']['Art. 10']['blocks'][0]['children'][0]
    assert sentence == {'type': 'element', 'tag': 'sup', 'attrs': {'role': 'sentence'}, 'children': [{'type': 'text', 'text': '1'}]}
    assert flatten_nodes(value['norms']['Art. 2']['blocks']) == current['norms'][1]['text']
    assert '"role": "footnote"' in json.dumps(value)
    assert 'clearer' not in json.dumps(value)


def test_sentence_and_footnote_roles_are_narrowly_validated():
    assert flatten_nodes([{'type': 'element', 'tag': 'sup', 'attrs': {'role': 'sentence'}, 'children': [{'type': 'text', 'text': '1'}]}]) == '1'
    for tag, role in [('sup', 'evil'), ('div', 'sentence'), ('small', 'evil')]:
        with pytest.raises(ReaderBlocksError):
            flatten_nodes([{'type': 'element', 'tag': tag, 'attrs': {'role': role}, 'children': []}])


def test_unlabelled_source_preamble_does_not_disable_labelled_reader_norms():
    current = act('A')
    current['norms'].append({'enbez': '', 'text': 'Preamble without a stable norm label'})
    assert list(build_sidecar(xml('<P>A</P>'), current, retrieved_at='now')['norms']) == ['§ 1']


def test_bavarian_generated_footnote_markers_are_not_added_to_canonical_text():
    from tools.reader_blocks import build_bayern_html_sidecar
    current = {'id': 'by_bayverf', 'permalink': 'https://www.gesetze-bayern.de/Content/Document/BayVerf',
               'norms': [{'enbez': 'Art. 13', 'text': '180Editorial note. Abgeordnete.'}]}
    html = '''<div id="content"><div class="cont"><div class="paraheading"><div class="paranr">Art. 13<sup>1)</sup></div></div>
    <div>180<sup><a class="footnotelink-down automatisch" href="#FN1">1</a></sup> Abgeordnete.</div>
    <div class="fn-container"><div class="fn"><a class="footnoteanchor" id="FN1"></a><sup><a class="footnotelink-up automatisch">1</a></sup><b>[Amtl. Anm.:]</b>Editorial note.</div></div></div></div>'''
    value = build_bayern_html_sidecar(html, current, retrieved_at='now')
    assert flatten_nodes(value['norms']['Art. 13']['blocks']) == current['norms'][0]['text']


def test_export_bavarian_html_from_offline_official_capture(tmp_path):
    from tools.reader_blocks import export_act
    current = {'id': 'by_agsg', 'permalink': 'https://www.gesetze-bayern.de/Content/Document/BayAGSG',
               'norms': [{'enbez': 'Art. 1', 'text': '1Same.'}]}
    (tmp_path / 'acts').mkdir()
    (tmp_path / 'acts/by_agsg.json').write_text(json.dumps(current))
    html_dir = tmp_path / 'html'
    html_dir.mkdir()
    (html_dir / 'BayAGSG.html').write_text('<div id="content"><div class="cont"><div class="paraheading"><div class="paranr">Art. 1</div></div><div><sup class="satznr">1</sup>Same.</div></div></div>')
    class NoNetwork:
        def get(self, *args, **kwargs):
            pytest.fail('must use the saved official page')
    export_act(tmp_path, tmp_path / 'out', 'by_agsg', NoNetwork(), html_directory=html_dir)
    assert load_sidecar(tmp_path / 'out', current)['norms']['Art. 1']['blocks'][0]['children'][0]['attrs']['role'] == 'sentence'


def test_export_retains_old_verified_structure_when_live_official_wording_changes(tmp_path):
    from tools.reader_blocks import export_act
    current = act('Old')
    (tmp_path / 'acts').mkdir()
    (tmp_path / 'acts/fed_testg.json').write_text(json.dumps(current))
    previous = tmp_path / 'previous'
    previous.mkdir()
    (previous / 'fed_testg.json').write_text(json.dumps(build('<P>Old</P>', 'Old')))
    cache = tmp_path / 'cache'
    cache.mkdir()
    (cache / 'fed_testg.xml').write_bytes(xml('<P>New</P>'))
    class NoNetwork:
        def get(self, *args, **kwargs):
            pytest.fail('must use the saved official XML')
    export_act(tmp_path, tmp_path / 'out', 'fed_testg', NoNetwork(), source_cache=cache, previous_reader_directory=previous)
    value = load_sidecar(tmp_path / 'out', current)
    assert flatten_nodes(value['norms']['§ 1']['blocks']) == 'Old'
    assert value['norms']['§ 1']['source_retrieved_at'] == '2026-09-21T12:00:00Z'


def test_repeated_norm_labels_have_hash_verified_indexed_structure(tmp_path):
    current = act('First')
    current['norms'].append({'enbez': '§ 1', 'text': 'Second'})
    source = xml('<P>First</P>').replace(b'</dokumente>', xml('<P>Second</P>').split(b'<dokumente>')[1])
    value = build_sidecar(source, current, retrieved_at='now')
    assert value['norms'] == {}
    assert sorted(value['norms_by_index']) == ['0', '1']
    (tmp_path / 'fed_testg.json').write_text(json.dumps(value))
    loaded = load_sidecar(tmp_path, current)
    assert loaded['norms_by_index']['1']['enbez'] == '§ 1'
    assert flatten_nodes(loaded['norms_by_index']['1']['blocks']) == 'Second'
    current['norms'].reverse()
    with pytest.raises(ReaderBlocksError):
        load_sidecar(tmp_path, current)


def test_bavarian_unlabelled_preamble_and_closing_formula_have_indexed_structure(tmp_path):
    from tools.reader_blocks import build_bayern_html_sidecar
    current = {'id': 'by_test', 'permalink': 'https://www.gesetze-bayern.de/Content/Document/BayTest',
               'norms': [{'enbez': '', 'text': 'Preamble'}, {'enbez': 'Art. 1', 'text': 'Body'}, {'enbez': '', 'text': 'München\nMinister'}]}
    html = '''<div id="content"><div class="cont"><div class="paraheading"><div class="paratitel">[Präambel]</div></div><div>Preamble</div></div>
    <div class="cont"><div class="paraheading"><div class="paranr">Art. 1</div></div><div>Body</div></div>
    <div class="cont"><div class="paraheading"></div><div>München</div><div><span class="b">Minister</span></div></div></div>'''
    value = build_bayern_html_sidecar(html, current, retrieved_at='now')
    assert sorted(value['norms_by_index']) == ['0', '2']
    (tmp_path / 'by_test.json').write_text(json.dumps(value))
    loaded = load_sidecar(tmp_path, current)
    assert flatten_nodes(loaded['norms_by_index']['2']['blocks']) == 'München\nMinister'
    assert loaded['norms_by_index']['2']['blocks'][1]['children'][0]['tag'] == 'strong'


def test_offline_refresh_preserves_old_matching_layout_and_uses_new_source(tmp_path):
    from tools.reader_blocks import refresh_cached_sidecars
    out = tmp_path / 'reader'
    sources = tmp_path / 'sources'
    sources.mkdir()
    (sources / 'fed_testg.xml').write_bytes(xml('<P><SUP>1</SUP>New</P>'))
    current = act('1New')
    result = refresh_cached_sidecars([current], out, sources)
    assert result['updated'] == 1
    assert load_sidecar(out, current)['norms']['§ 1']['blocks'][0]['children'][0]['tag'] == 'sup'
    (sources / 'fed_testg.xml').write_bytes(xml('<P>Different future edition</P>'))
    assert refresh_cached_sidecars([current], out, sources)['updated'] == 1
    assert load_sidecar(out, current)['norms']['§ 1']['source_retrieved_at']
    original = (out / 'fed_testg.json').read_bytes()
    changed = act('Unknown edition')
    result = refresh_cached_sidecars([changed], out, sources)
    assert result['updated'] == 0 and result['unavailable'] == ['fed_testg']
    assert (out / 'fed_testg.json').read_bytes() == original


def test_bavarian_footnote_body_keeps_its_own_superscript():
    from tools.reader_blocks import build_bayern_html_sidecar
    current = {'id': 'by_test', 'permalink': 'https://www.gesetze-bayern.de/Content/Document/BayTest',
               'norms': [{'enbez': 'Art. 1', 'text': 'Text1)Use m2.'}]}
    html = '''<div id="content"><div class="cont"><div class="paraheading"><div class="paranr">Art. 1</div></div><div>Text<sup><a class="footnotelink-down" href="#FN1">1)</a></sup></div>
    <div class="fn-container"><div class="fn"><a class="footnoteanchor" id="FN1"></a><sup><a class="footnotelink-up">1)</a></sup>Use m<sup>2</sup>.</div></div></div></div>'''
    value = build_bayern_html_sidecar(html, current, retrieved_at='now')
    assert flatten_nodes(value['norms']['Art. 1']['blocks']) == current['norms'][0]['text']


def test_gii_table_of_contents_heading_boundaries_are_preserved():
    body = '<TOC><Ident>Teil 1</Ident><Title>Allgemeines</Title><Subtitle>Abschnitt A</Subtitle><P>Text</P></TOC>'
    nodes = build(body, 'Teil 1AllgemeinesAbschnitt AText')['norms']['§ 1']['blocks']
    assert nodes[0]['tag'] == 'div'
    assert [child['tag'] for child in nodes[0]['children']] == ['h4', 'h4', 'h5', 'p']
    assert flatten_nodes(nodes) == 'Teil 1AllgemeinesAbschnitt AText'


def test_gii_preformatted_source_preserves_alignment_without_changing_canonical_text():
    body = '<P>Before<noindex><pre xml:space="preserve">  Fuel    kWh<BR/>  Coal    5.5</pre></noindex>after</P>'
    nodes = build(body, 'Before Fuel kWh Coal 5.5after')['norms']['§ 1']['blocks']
    assert [node['tag'] for node in nodes] == ['p', 'pre', 'p']
    assert flatten_nodes(nodes) == 'Before  Fuel    kWh  Coal    5.5after'
    assert nodes[1]['children'][1] == {'type': 'element', 'tag': 'br', 'children': []}
    assert 'attrs' not in nodes[1]
    with pytest.raises(ReaderBlocksError):
        flatten_nodes([{**nodes[1], 'attrs': {'style': 'position:fixed'}}])


def test_gii_titles_caption_their_direct_table_including_nested_tables():
    body = ('<table><Title>Outer</Title><tgroup><tbody><row><entry>'
            '<Title>Cell heading</Title><table><Title>Inner</Title>'
            '<tbody><tr><td>Value</td></tr></tbody></table>'
            '</entry></row></tbody></tgroup></table>')
    nodes = build(body, 'OuterCell headingInnerValue')['norms']['§ 1']['blocks']
    outer = nodes[0]
    assert outer['children'][0]['tag'] == 'caption'
    cell = outer['children'][1]['children'][0]['children'][0]
    assert cell['children'][0]['tag'] == 'h4'
    assert cell['children'][1]['tag'] == 'table'
    assert cell['children'][1]['children'][0]['tag'] == 'caption'
    assert flatten_nodes(nodes) == 'OuterCell headingInnerValue'


def test_gii_source_images_and_pdf_attachments_preserve_empty_text_and_safe_urls():
    body = '<P>A<IMG SRC="bgbl1_2016_j0162-1_0130.jpg" alt="Diagram" Width="1950" Height="979"/>B<FILE SRC="bgbl1_2022_j0500_0010.pdf"/></P>'
    nodes = build(body, 'AB')['norms']['§ 1']['blocks']
    assert flatten_nodes(nodes) == 'AB'
    image = nodes[0]['children'][1]
    assert image['tag'] == 'img'
    assert image['attrs'] == {'src': 'https://www.gesetze-im-internet.de/normengrafiken/bgbl1_2016/j0162-1_0130.jpg', 'alt': 'Diagram', 'width': 1950, 'height': 979}
    attachment = nodes[1]
    assert attachment['tag'] == 'source-file'
    assert attachment['attrs']['src'].endswith('/normengrafiken/bgbl1_2022/j0500_0010.pdf')
    assert attachment['children'] == []


@pytest.mark.parametrize('url', ['https://evil.test/image.jpg', 'https://www.gesetze-im-internet.de.evil.test/normengrafiken/bgbl1_2024/a.jpg', 'https://www.gesetze-im-internet.de/normengrafiken/bgbl1_2024/../a.jpg', 'javascript:alert(1)'])
def test_reader_media_rejects_untrusted_origins_and_paths(url):
    with pytest.raises(ReaderBlocksError):
        flatten_nodes([{'type': 'element', 'tag': 'img', 'attrs': {'src': url, 'alt': ''}, 'children': []}])


@pytest.mark.parametrize('node', [
    {'type': 'element', 'tag': [], 'children': []},
    {'type': 'element', 'tag': 'sup', 'attrs': {'role': []}, 'children': []},
    {'type': 'element', 'tag': 'img', 'attrs': {'src': []}, 'children': []},
])
def test_corrupt_reader_node_types_fail_closed_with_reader_error(node):
    with pytest.raises(ReaderBlocksError):
        flatten_nodes([node])
