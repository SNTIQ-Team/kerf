"""Pinned RII files preserve printed numbers and author provenance even without the upstream service."""
import gzip
import hashlib
import io
import json
import zipfile

from fastapi.testclient import TestClient
import pytest

from api.server import server as app
from api import rii_archive
from tools.read_rii_archive import main


def h(value):
    return hashlib.sha256(value).hexdigest()


def encode(obj):
    return json.dumps(obj,sort_keys=True).encode()


@pytest.fixture
def archive(monkeypatch):
    files = {}
    docs = ['jb-KORE123', 'jb-JURE456']
    by_bucket = {}
    originals = {}
    for i,key in enumerate(docs):
        raw = f'<dokument><doknr>{key[3:]}</doknr></dokument>'.encode()
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, 'w') as archive:
            archive.writestr(key + '.xml', raw)
            if i:
                archive.writestr('bild.png', b'illustration')
        whole_zip = payload.getvalue()
        packed = gzip.compress(raw) if i else whole_zip
        path = f'originals/bgh/2020-{i}.bin'
        files[path] = b'pad' + packed
        anchor = 'p-1' if i else 'rn-7'
        text = 'Published paragraph'
        url = 'https://www.rechtsprechung-im-internet.de/jportal/docs/bsjrs/' + key + '.zip'
        record = dict(original_pack=path, offset=3, length=len(packed),
                      packed_sha256=h(packed), zip_sha256=h(whole_zip), xml_sha256=h(raw),
                      source_zip=url, pack_format='gzip_xml' if i else 'zip', attachment_count=i)
        meta = dict(document=key, court='bgh', ecli='ECLI:DE:BGH:2020:ABC', paragraphs=1, printed_numbers=1-i, xml_bytes=len(raw), **record)
        passages = [dict(document=key, anchor=anchor, printed_number=None if i else '7', section='gruende',
                         authorship='court', text=text, text_sha256=h(text.encode()), position=1,
                         source_locators=['/dokument/gruende[1]/p[1]'])]
        encoded = encode({'metadata':meta,'passages':passages})
        frame = gzip.compress(encoded)
        frame_path = f'documents/bgh/{i}.bin'
        files[frame_path] = b'padding' + frame
        record['document_frame'] = dict(path=frame_path, offset=7, length=len(frame), packed_sha256=h(frame),
                                        bytes=len(encoded), sha256=h(encoded))
        by_bucket.setdefault(h(key.encode())[:2], {})[key] = record
        originals[key] = raw if i else whole_zip
    for bucket,table in by_bucket.items():
        files[f'lookup/{bucket}.json.gz'] = gzip.compress(encode(table))
    manifest = {'format':'kerf-rii-publications/1','files':{p:{'size':len(v),'sha256':h(v)} for p,v in files.items()}}
    files['manifest.json'] = encode(manifest)
    revision = 'a'*40
    def read(url, limit, span=None):
        assert '/'+revision+'/rii/' in url
        value = files[url.split('/rii/')[1]]
        if isinstance(value, Exception):raise value
        if span is None:return value
        return value[span[0]:span[0]+span[1]]
    monkeypatch.setattr(rii_archive, 'fetch', read)
    monkeypatch.setenv('KERF_RII_REVISION',revision)
    monkeypatch.setenv('KERF_RII_MANIFEST_SHA256',h(files['manifest.json']))
    rii_archive.open_archive.cache_clear()
    return TestClient(app), files, originals


def test_numbered_and_unnumbered_addresses(archive):
    client, _, _ = archive
    path = '/rii/decisions/jb-KORE123'
    assert client.get(path).json()['metadata']['printed_numbers']==1
    assert client.get('/rii/decisions/KORE123').json()['metadata']['document']=='jb-KORE123'
    assert client.get('/rii/decisions/jb-kore123').status_code==200
    hit = client.get(path+'/passages/rn-7')
    assert hit.status_code==200 and hit.json()['passage']['printed_number']=='7'
    assert hit.json()['passage']['source_locators']==['/dokument/gruende[1]/p[1]']
    assert hit.headers['x-kerf-rii-revision']=='a'*40
    assert hit.headers['cache-control'] == 'no-store'
    assert client.get(path+'/passages',params={'rn':'7'}).json()['total']==1
    base = '/rii/decisions/jb-JURE456/passages'
    assert client.get(base+'/p-1').json()['passage']['printed_number'] is None
    assert client.get(base+'/rn-1').status_code==404
    assert client.get(base,params={'rn':'1'}).json()['total']==0


def test_originals_keep_offered_format(archive):
    client, _, items=archive
    for key,mime in [('jb-KORE123','application/zip'),('jb-JURE456','application/xml')]:
        res=client.get('/rii/decisions/'+key+'/source')
        assert (res.status_code,res.content,res.headers['content-type'])==(200,items[key],mime)
        assert res.headers['x-kerf-source-sha256'] == h(items[key])
        assert res.headers['cache-control'] == 'no-store'


def test_absence_upstream_failure_and_bad_hash_are_distinct(archive):
    client, files, _=archive
    missing = client.get('/rii/decisions/jb-ABSENT')
    assert missing.status_code == 404
    assert missing.headers['cache-control'] == 'no-store'
    path='/rii/decisions/jb-KORE123'
    files['documents/bgh/0.bin']=OSError('upstream unavailable')
    assert client.get(path).status_code==503
    files['documents/bgh/0.bin']=b'x'*7000
    assert client.get(path).status_code==503
    files['originals/bgh/2020-0.bin']=b'wrong'
    assert client.get(path+'/source').status_code==503


def test_cli_uses_the_same_pinned_result_and_distinguishes_errors(archive, capsys):
    _, files, _ = archive
    assert main(['jb-KORE123','--anchor','rn-7']) == 0
    assert json.loads(capsys.readouterr().out)['passage']['printed_number'] == '7'
    assert main(['jb-JURE456','--passages','--query','paragraph']) == 0
    assert json.loads(capsys.readouterr().out)['passages'][0]['printed_number'] is None
    assert main(['jb-ABSENT']) == 1
    files['documents/bgh/0.bin'] = OSError('unavailable')
    assert main(['jb-KORE123']) == 2


def test_missing_published_file_is_a_service_error(archive):
    from urllib.error import HTTPError
    client, files, _ = archive
    key = 'jb-KORE123'
    bucket = h(key.encode())[:2]
    files[f'lookup/{bucket}.json.gz'] = HTTPError('https://huggingface.co/',404,'Not found',{},None)
    assert client.get('/rii/decisions/'+key).status_code == 503


def test_unpinned_manifest_is_not_served(archive, monkeypatch):
    client, _, _ = archive
    monkeypatch.setenv('KERF_RII_MANIFEST_SHA256','b'*64)
    result = client.get('/rii/decisions/jb-KORE123')
    assert result.status_code == 503
    assert result.headers['cache-control'] == 'no-store'


def test_present_but_invalid_catalogue_entry_is_a_service_error(archive, monkeypatch):
    client, _, _ = archive
    monkeypatch.setattr(rii_archive.RiiArchive, '_bucket', lambda self, key: {'jb-KORE123': None})
    assert client.get('/rii/decisions/jb-KORE123').status_code == 503
    assert client.get('/rii/decisions/jb-ABSENT').status_code == 404


@pytest.mark.parametrize('error', [RuntimeError('encrypted'), NotImplementedError('unsupported compression')])
def test_unreadable_zip_is_a_service_error(archive, monkeypatch, error):
    client, _, _ = archive
    def unsupported(*args, **kwargs):
        raise error
    monkeypatch.setattr(zipfile.ZipFile, 'read', unsupported)
    assert client.get('/rii/decisions/jb-KORE123/source').status_code == 503


IDENTIFIER = "ECLI:DE:BGH:2020:ABC"
OTHER = "ECLI:DE:BGH:2020:OTHER"
DOCUMENTS = ["jb-JURE456", "jb-KORE123"]


def enable_index(files, monkeypatch, entries, omit=None):
    manifest = json.loads(files["manifest.json"])
    manifest["ecli_index"] = {"buckets": 256}
    by_bucket = {f"{number:02x}": {} for number in range(256)}
    for identifier, documents in entries.items():
        by_bucket[h(identifier.encode())[:2]][identifier] = documents
    for bucket, table in by_bucket.items():
        if bucket == omit:
            continue
        path = f"ecli/{bucket}.json.gz"
        raw = gzip.compress(encode(table))
        files[path] = raw
        manifest["files"][path] = {"size": len(raw), "sha256": h(raw)}
    files["manifest.json"] = encode(manifest)
    monkeypatch.setenv("KERF_RII_MANIFEST_SHA256", h(files["manifest.json"]))
    rii_archive.open_archive.cache_clear()


def test_exact_ecli_returns_all_independently_checked_metadata(archive, monkeypatch, capsys):
    client, files, _ = archive
    enable_index(files, monkeypatch, {IDENTIFIER: DOCUMENTS})
    result = client.get("/rii/decisions", params={"ecli": IDENTIFIER.lower()})
    assert result.status_code == 200
    assert result.headers["cache-control"] == "no-store"
    payload = result.json()
    assert payload["revision"] == "a" * 40
    assert payload["total"] == 2 and payload["ecli"] == IDENTIFIER
    assert [row["document"] for row in payload["decisions"]] == DOCUMENTS
    assert all(row["ecli"] == IDENTIFIER for row in payload["decisions"])
    assert main(["--ecli", IDENTIFIER]) == 0
    assert json.loads(capsys.readouterr().out) == payload
    no_match = client.get("/rii/decisions", params={"ecli": OTHER})
    assert no_match.status_code == 200 and no_match.json()["decisions"] == []


def test_pre_index_release_retains_document_reads(archive, capsys):
    client, _, _ = archive
    result = client.get("/rii/decisions", params={"ecli": IDENTIFIER})
    assert result.status_code == 501 and result.headers["x-kerf-rii-revision"] == "a" * 40
    assert client.get("/rii/decisions/KORE123").status_code == 200
    assert main(["--ecli", IDENTIFIER]) == 2
    assert "no ECLI index" in capsys.readouterr().err
    assert client.get("/rii/decisions", params={"ecli": "free text"}).status_code == 422


@pytest.mark.parametrize("documents", [["jb-JURE456"], ["jb-ABSENT"], ["bad"], [], ["jb-KORE123", "jb-KORE123"]])
def test_index_cannot_assign_or_invent_an_ecli(archive, monkeypatch, documents):
    client, files, _ = archive
    enable_index(files, monkeypatch, {OTHER: documents})
    result = client.get("/rii/decisions", params={"ecli": OTHER})
    assert result.status_code == 503


def test_advertised_missing_bucket_cannot_establish_absence(archive, monkeypatch):
    client, files, _ = archive
    enable_index(files, monkeypatch, {}, omit=h(OTHER.encode())[:2])
    assert client.get("/rii/decisions", params={"ecli": OTHER}).status_code == 503


def test_repeat_ecli_lookup_reuses_only_verified_metadata(archive, monkeypatch):
    client, files, _ = archive
    enable_index(files, monkeypatch, {IDENTIFIER: DOCUMENTS})
    url = "/rii/decisions"
    assert client.get(url, params={"ecli": IDENTIFIER}).json()["total"] == 2
    files["documents/bgh/0.bin"] = OSError("temporarily offline")
    assert client.get(url, params={"ecli": IDENTIFIER}).json()["total"] == 2
    assert client.get("/rii/decisions/KORE123/passages/rn-7").status_code == 503
