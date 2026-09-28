import gzip
import hashlib
import json
from pathlib import Path
import pytest
from tools.archived_reader_store import ArchivedReaderError, store_capture, publish_archived_readers, load_archived_reader
from tools.official_states import canonical_json_bytes

STATE={'id':'fed_pstg','jurabk':'PStG','title':'Personenstandsgesetz','norm_count':2,'norms':[
 {'enbez':'§ 1','titel':'Titel','text':'(1) Erster Text.(2) Zweiter Text.','glied':[]},
 {'enbez':'§ 2','titel':'Liste','text':'1.Erstens2.Zweitens','glied':[]}]}
HTML='''<html><body><div class="jnnorm"><div class="jnheader"><span class="jnlangue">Personenstandsgesetz</span><span class="jnamtabk">(PStG)</span></div></div>
<div class="jnnorm"><div class="jnheader"><span class="jnenbez">§ 1</span><span class="jnentitel">Titel</span></div><div class="jnhtml"><div class="jurAbsatz">(1) Erster Text.</div><div class="jurAbsatz">(2) Zweiter Text.</div></div><div class="jnfussnote">Later editorial note</div></div>
<div class="jnnorm"><div class="jnheader"><span class="jnenbez">§ 2</span><span class="jnentitel">Liste</span></div><div class="jnhtml"><dl><dt>1.</dt><dd>Erstens</dd><dt>2.</dt><dd>Zweitens</dd></dl></div></div></body></html>'''.encode()
SOURCE='https://www.gesetze-im-internet.de/pstg/BJNR012210007.html'
ARGS={'source_url':SOURCE,'archive_url':'https://web.archive.org/web/20260626173342id_/'+SOURCE,'archive_timestamp':'20260626173342','retrieved_at':'2026-09-27T12:00:00Z'}

def digest(state):return hashlib.sha256(canonical_json_bytes(state)).hexdigest()
def observed(state):return {**state,'state_sha256':digest(state),'observed_at':'2026-07-16','source':'GII'}
def act():return {'id':'fed_pstg','jurabk':'PStG','source_url':'https://www.gesetze-im-internet.de/pstg/'}

def test_persist_reverify_publish_and_load_exact_dated_blocks(tmp_path):
 s=tmp_path/'source';p=tmp_path/'public'
 store_capture(s,HTML,STATE,**ARGS)
 assert publish_archived_readers(s,p)['published']==1
 out=load_archived_reader(p,act(),observed(STATE),requested_at='2026-07-16')
 assert out['requested_at']=='2026-07-16' and out['state_sha256']==digest(STATE)
 assert len(out['norms_by_index'])==2 and out['norms_by_index']['0']['blocks'][0]['tag']=='p'
 assert 'Later editorial' not in json.dumps(out)
 assert (p/'fed_pstg'/f'{digest(STATE)}.json').stat().st_mode & 0o777 == 0o644
 assert load_archived_reader(p,act(),{**observed(STATE),'state_sha256':'f'*64},requested_at='2026-07-16') is None

@pytest.mark.parametrize('change',[lambda h:h.replace(b'Erster',b'Wrong'),lambda h:h.replace(b'PStG',b'BGB'),lambda h:h.replace(b'Liste',b'Wrong title'),lambda h:h[:-20]])
def test_wrong_or_incomplete_source_rejected(tmp_path,change):
 with pytest.raises(ArchivedReaderError):store_capture(tmp_path,change(HTML),STATE,**ARGS)
 assert not list(tmp_path.rglob('*.json'))

def test_tampered_source_fails_publication_before_writes(tmp_path):
 s=tmp_path/'source';p=tmp_path/'public';store_capture(s,HTML,STATE,**ARGS)
 source=next((s/'sources').rglob('*.gz'));source.write_bytes(gzip.compress(b'bad'))
 with pytest.raises(ArchivedReaderError):publish_archived_readers(s,p)
 assert not p.exists()


def test_wrong_archived_body_or_title_cannot_receive_formatting(tmp_path):
 s=tmp_path/'source';p=tmp_path/'public';store_capture(s,HTML,STATE,**ARGS);publish_archived_readers(s,p)
 changed=json.loads(json.dumps(observed(STATE)));changed['norms'][0]['text']='Different'
 with pytest.raises(ArchivedReaderError):load_archived_reader(p,act(),changed,requested_at='2026-07-16')
 changed=json.loads(json.dumps(observed(STATE)));changed['norms'][0]['titel']='Different'
 with pytest.raises(ArchivedReaderError):load_archived_reader(p,act(),changed,requested_at='2026-07-16')


def test_archived_request_requires_exact_date_and_safe_store_identity(tmp_path):
 with pytest.raises(ArchivedReaderError):load_archived_reader(tmp_path,act(),observed(STATE),requested_at='../../bad')
 with pytest.raises(ArchivedReaderError):load_archived_reader(tmp_path,{**act(),'id':'../other'},observed(STATE),requested_at='2026-07-16')


def test_bavarian_date_without_archived_structure_is_an_optional_miss(tmp_path):
 state={**observed(STATE),'id':'by_bayvwvfg','jurabk':'BayVwVfG'}
 assert load_archived_reader(tmp_path,{'id':'by_bayvwvfg','jurabk':'BayVwVfG'},state,requested_at='2016-01-29') is None


def test_runtime_loader_import_has_no_build_only_dependencies():
 import subprocess,sys
 code='''
import sys
class BlockBuildDependencies:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'bs4' or fullname.startswith('tools.official_states') or fullname.startswith('pipeline'):
            raise ImportError('build dependency forbidden: '+fullname)
sys.meta_path.insert(0,BlockBuildDependencies())
from tools.archived_reader_store import load_archived_reader
'''
 subprocess.run([sys.executable,'-c',code],check=True)


def test_actual_archived_pstg_full_inventory_and_changed_old_sections(tmp_path):
 fixture=Path(__file__).parent/'fixtures'
 raw=gzip.decompress((fixture/'pstg-20260626173342.html.gz').read_bytes())
 state_raw=gzip.decompress((fixture/'pstg-state-3a80d759.json.gz').read_bytes())
 assert hashlib.sha256(raw).hexdigest()=='a37ce984f840ae55ead1ed7b3b53e977ea41d57e80f75d98e9c9465667bb81f8'
 assert hashlib.sha256(state_raw).hexdigest()=='3a80d75965d3c1aa9f6f550bbd7edee54438d04fd6cce1e72a0a792c08a302ae'
 state=json.loads(state_raw);s=tmp_path/'source';p=tmp_path/'public'
 store_capture(s,raw,state,**ARGS)
 assert publish_archived_readers(s,p)['states']==[digest(state)]
 payload=load_archived_reader(p,act(),observed(state),requested_at='2026-07-16')
 assert len(payload['norms_by_index'])==86
 assert 'norm_titles' not in payload and 'source_annotations' not in json.dumps(payload)
 def tags(nodes):
  for node in nodes:
   if node['type']=='element':
    yield node['tag'];yield from tags(node['children'])
 rows={row['enbez']:row for row in payload['norms_by_index'].values()}
 toc=list(tags(rows['Inhaltsübersicht']['blocks']))
 assert toc.count('tr')==158 and toc.count('td')+toc.count('th')==268
 assert list(tags(rows['§ 44']['blocks'])).count('p')==3
 from tools.reader_blocks import flatten_nodes
 for index,norm in enumerate(state['norms']):
  assert flatten_nodes(payload['norms_by_index'][str(index)]['blocks'])==norm['text']
