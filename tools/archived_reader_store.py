"""Immutable formatting evidence for a separately verified historical act CAS.

This store adds no editions or legal dates. Every source capture must reproduce
all target norm labels, titles and body characters; each publication reparses
retained HTML. The API additionally binds safe blocks to its resolved state.
"""
from __future__ import annotations
import copy
import gzip
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from tools.reader_blocks import _html_children, _exact_whitespace, flatten_nodes, text_hash, ReaderBlocksError

MAX_BYTES=16*1024*1024
DIGEST=re.compile(r'[a-f0-9]{64}')
ACT_ID=re.compile(r'(?:fed|by)_[a-z0-9][a-z0-9_-]*')
SOURCE=re.compile(r'https://www\.gesetze-im-internet\.de/[a-z0-9][a-z0-9_-]*/BJNR[0-9]+\.html')

class ArchivedReaderError(ValueError):
    """Historical structure or its source binding is unavailable/invalid."""


def _sha(raw):return hashlib.sha256(raw).hexdigest()
def _identity(aid,digest):
    if not isinstance(aid,str) or not ACT_ID.fullmatch(aid) or not isinstance(digest,str) or not DIGEST.fullmatch(digest):
        raise ArchivedReaderError('invalid act/state identity')

def _blob(kind,digest):
    if not isinstance(digest,str) or not DIGEST.fullmatch(digest):raise ArchivedReaderError('invalid source digest')
    return Path(kind)/'sha256'/digest[:2]/(digest+('.html.gz' if kind=='sources' else '.json.gz'))

def _write(path,raw):
    from tools.official_states import _atomic_write
    _atomic_write(path,raw);path.chmod(0o644)

def _read_blob(store,kind,digest):
    try:
        with gzip.open(store/_blob(kind,digest),'rb') as handle:raw=handle.read(MAX_BYTES+1)
    except (OSError,EOFError,ValueError) as exc:raise ArchivedReaderError('source object unavailable') from exc
    if len(raw)>MAX_BYTES or _sha(raw)!=digest:raise ArchivedReaderError('source object hash mismatch')
    return raw

def _json(path):
    try:
        if path.stat().st_size>MAX_BYTES:raise ArchivedReaderError('reader object too large')
        return json.loads(path.read_bytes())
    except (OSError,ValueError) as exc:raise ArchivedReaderError('reader object unavailable') from exc

def _provenance(source_url,archive_url,archive_timestamp,retrieved_at):
    if not isinstance(source_url,str) or not SOURCE.fullmatch(source_url):raise ArchivedReaderError('invalid official source URL')
    if not isinstance(archive_timestamp,str) or not re.fullmatch(r'\d{14}',archive_timestamp):raise ArchivedReaderError('invalid capture timestamp')
    try:
        datetime.strptime(archive_timestamp,'%Y%m%d%H%M%S')
        datetime.fromisoformat(retrieved_at.replace('Z','+00:00'))
    except (ValueError,AttributeError) as exc:raise ArchivedReaderError('invalid capture/retrieval date') from exc
    if archive_url!=f'https://web.archive.org/web/{archive_timestamp}id_/{source_url}':raise ArchivedReaderError('archive URL differs from source capture')


def build_gii_html_structure(html,state,*,source_url,archive_url,archive_timestamp,retrieved_at):
    from bs4 import BeautifulSoup
    from tools.official_states import canonical_json_bytes
    _provenance(source_url,archive_url,archive_timestamp,retrieved_at)
    digest=_sha(canonical_json_bytes(state));aid=state.get('id');_identity(aid,digest)
    if not aid.startswith('fed_'):raise ArchivedReaderError('GII capture requires a federal act')
    if not isinstance(html,bytes) or len(html)>MAX_BYTES or b'</html>' not in html[-100:].lower():raise ArchivedReaderError('incomplete/oversized captured HTML')
    targets=state.get('norms')
    if not isinstance(targets,list) or not targets:raise ArchivedReaderError('state has no norms')
    names=[n.get('enbez') for n in targets]
    if any(not isinstance(n,str) or not n for n in names) or len(set(names))!=len(names):raise ArchivedReaderError('ambiguous historical norm labels')
    soup=BeautifulSoup(html,'html.parser')
    abbreviation=soup.select_one('.jnamtabk');title=soup.select_one('.jnlangue')
    if abbreviation is None or abbreviation.get_text(strip=True).strip('() ')!=state.get('jurabk') or title is None or title.get_text(strip=True)!=state.get('title'):
        raise ArchivedReaderError('captured source belongs to another act')
    source_norms={}
    for container in soup.select('.jnnorm'):
        label=container.select_one('.jnheader .jnenbez')
        if label is None:continue
        name=label.get_text(strip=True)
        if name in source_norms:raise ArchivedReaderError('ambiguous source label')
        source_norms[name]=container
    if set(source_norms)!=set(names):raise ArchivedReaderError('captured complete norm inventory differs from state')
    indexed={};titles={}
    for index,target in enumerate(targets):
        name=target['enbez'];container=source_norms[name];heading=container.select_one('.jnentitel')
        title=heading.get_text(strip=True) if heading else ''
        if title!=(target.get('titel') or ''):raise ArchivedReaderError(f'historical title mismatch: {name}')
        body=copy.deepcopy(container)
        for note in body.select('.jnfussnote,.jnheader'):note.decompose()
        sources=body.select('.jnhtml')
        if len(sources)>1:raise ArchivedReaderError('ambiguous HTML body')
        source=sources[0] if sources else None
        nodes=[]
        if source is not None:
            for paragraph in source.select('.jurAbsatz'):paragraph.name='p'
            for span in source.find_all('span'):
                if 'font-weight:bold' in span.get('style','').replace(' ',''):span['class']=['b']
            try:nodes=_html_children(source,{})
            except (ReaderBlocksError,ValueError) as exc:raise ArchivedReaderError('unsupported captured structure') from exc
        try:
            if re.sub(r'\s','',flatten_nodes(nodes))!=re.sub(r'\s','',target['text']):raise ArchivedReaderError(f'historical body mismatch: {name}')
            nodes=_exact_whitespace(nodes,target['text'])
            if flatten_nodes(nodes)!=target['text']:raise ArchivedReaderError('body changed during normalization')
        except ReaderBlocksError as exc:raise ArchivedReaderError('invalid source structure') from exc
        indexed[str(index)]={'enbez':name,'text_sha256':text_hash(target['text']),'blocks':nodes}
        titles[str(index)]=title
    return {'schema_version':1,'act_id':aid,'state_sha256':digest,'source_url':source_url,
            'archive_url':archive_url,'archive_timestamp':archive_timestamp,'retrieved_at':retrieved_at,
            'source_html_sha256':_sha(html),'norm_titles':titles,'norms':{},'norms_by_index':indexed}


def _validate_payload(payload,act,state):
    aid=act.get('id');digest=state.get('state_sha256');_identity(aid,digest)
    if state.get('id')!=aid or state.get('jurabk')!=act.get('jurabk'):raise ArchivedReaderError('resolved state belongs to another act')
    if not isinstance(payload,dict) or payload.get('schema_version')!=1 or payload.get('act_id')!=aid or payload.get('state_sha256')!=digest:raise ArchivedReaderError('historical structure state mismatch')
    try:_provenance(**{k:payload[k] for k in ('source_url','archive_url','archive_timestamp','retrieved_at')})
    except (KeyError,TypeError) as exc:raise ArchivedReaderError('missing historical provenance') from exc
    if not isinstance(payload.get('source_html_sha256'),str) or not DIGEST.fullmatch(payload['source_html_sha256']):raise ArchivedReaderError('invalid source digest')
    if payload.get('norms')!={}:raise ArchivedReaderError('historical structures require indexed records')
    indexed=payload.get('norms_by_index');titles=payload.get('norm_titles');norms=state.get('norms')
    if not isinstance(norms,list) or not isinstance(indexed,dict) or not isinstance(titles,dict) or set(indexed)!=set(titles) or set(indexed)!={str(i) for i in range(len(norms))}:raise ArchivedReaderError('incomplete indexed historical structure')
    for index,norm in enumerate(norms):
        row=indexed[str(index)]
        if not isinstance(norm,dict) or not isinstance(norm.get('text'),str):raise ArchivedReaderError('invalid resolved norm body')
        if not isinstance(row,dict) or set(row)!={'enbez','text_sha256','blocks'} or row.get('enbez')!=norm.get('enbez') or titles[str(index)]!=(norm.get('titel')or'') or row.get('text_sha256')!=text_hash(norm['text']):raise ArchivedReaderError('historical norm identity/hash mismatch')
        try:actual=flatten_nodes(row['blocks'])
        except ReaderBlocksError as exc:raise ArchivedReaderError('unsafe archived reader blocks') from exc
        if actual!=norm['text']:raise ArchivedReaderError('historical block text differs from CAS')
    return payload


def store_capture(store,html,state,**provenance):
    from tools.official_states import canonical_json_bytes, _deterministic_gzip
    store=Path(store);payload=build_gii_html_structure(html,state,**provenance)
    path=store/'records'/payload['act_id']/(payload['state_sha256']+'.json')
    if path.exists():
        old=_json(path)
        if old!=payload:raise ArchivedReaderError('conflicting immutable archived formatting')
        return payload
    for kind,raw,digest in [('sources',html,payload['source_html_sha256']),('states',canonical_json_bytes(state),payload['state_sha256'])]:
        target=store/_blob(kind,digest)
        if target.exists() and _read_blob(store,kind,digest)!=raw:raise ArchivedReaderError('immutable source collision')
        if not target.exists():_write(target,_deterministic_gzip(raw))
    _write(path,canonical_json_bytes(payload));return payload


def publish_archived_readers(store,output):
    from tools.official_states import canonical_json_bytes
    store,output=Path(store),Path(output);verified=[]
    for path in sorted((store/'records').glob('*/*.json')):
        if path.is_symlink():raise ArchivedReaderError('archived reader record must not be a symlink')
        payload=_json(path);aid=payload.get('act_id');digest=payload.get('state_sha256');_identity(aid,digest)
        if path.parent.name!=aid or path.stem!=digest:raise ArchivedReaderError('reader record path differs from identity')
        raw=_read_blob(store,'sources',payload.get('source_html_sha256'));state=json.loads(_read_blob(store,'states',digest))
        rebuilt=build_gii_html_structure(raw,state,**{k:payload[k] for k in ('source_url','archive_url','archive_timestamp','retrieved_at')})
        if rebuilt!=payload:raise ArchivedReaderError('historical source does not reproduce stored structure')
        _validate_payload(payload,{'id':aid,'jurabk':state['jurabk']},{**state,'state_sha256':digest})
        verified.append(payload)
    for payload in verified:_write(output/payload['act_id']/(payload['state_sha256']+'.json'),canonical_json_bytes(payload))
    return {'published':len(verified),'states':[p['state_sha256'] for p in verified]}


def load_archived_reader(directory,act,state,*,requested_at):
    """State resolution is the API caller's responsibility, identical to Markdown."""
    try:
        if not isinstance(requested_at,str) or date.fromisoformat(requested_at).isoformat()!=requested_at:raise ValueError()
    except ValueError as exc:raise ArchivedReaderError('invalid requested date') from exc
    aid=act.get('id');digest=state.get('state_sha256');_identity(aid,digest)
    path=Path(directory)/aid/(digest+'.json')
    if not path.is_file():return None
    payload=_validate_payload(_json(path),act,state)
    return {k:v for k,v in payload.items() if k!='norm_titles'}|{'requested_at':requested_at}
