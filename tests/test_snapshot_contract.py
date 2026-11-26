import os
import subprocess
import sys
from fastapi.testclient import TestClient
from api import main
from api.server import server


def test_snapshot_precondition_survives_real_composition(monkeypatch, tmp_path):
    monkeypatch.setattr(main, 'DATA_DIR', tmp_path / 'web-data.release-fixture')
    client = TestClient(server, root_path='/kerf')
    response = client.get('/capabilities')
    assert response.headers['x-kerf-snapshot'] == 'web-data.release-fixture'
    assert response.json()['snapshot'] == 'web-data.release-fixture'
    response = client.get('/capabilities', headers={'If-Kerf-Snapshot':'other'})
    assert response.status_code == 409 and response.json()['status'] == 'stale_snapshot'
    assert client.get('/capabilities', headers={'If-Kerf-Snapshot':'web-data.release-fixture'}).status_code == 200


def test_worker_pins_generation_across_publication_symlink_switch(tmp_path):
    old=tmp_path/'web-data.release-old';new=tmp_path/'web-data.release-new'
    old.mkdir();new.mkdir();link=tmp_path/'web-data';link.symlink_to(old)
    code = '''
from api import main
from pathlib import Path
import os
link=Path(os.environ['KERF_DATA'])
assert main.DATA_DIR.name == 'web-data.release-old'
link.unlink();link.symlink_to(link.parent/'web-data.release-new')
assert main.DATA_DIR.name == 'web-data.release-old'
assert main.corpus_snapshot() == 'web-data.release-old'
'''
    subprocess.run([sys.executable, '-c', code], env={**os.environ,'KERF_DATA':str(link)}, check=True)
