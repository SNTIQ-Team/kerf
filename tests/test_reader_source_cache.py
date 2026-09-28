from pathlib import Path
import pytest
from pipeline.reader_source_cache import save_reader_source, reader_act_id


def test_raw_source_capture_is_atomic_and_has_stable_public_identity(tmp_path):
    assert reader_act_id('fed', 'RuStAG') == 'fed_rustag'
    assert reader_act_id('fed', 'AZRG-DV') == 'fed_azrg_dv'
    assert reader_act_id('by', 'PAG') == 'by_pag'
    value = b'<dokumente><norm /></dokumente>'
    path = save_reader_source(tmp_path, 'fed_test', value)
    assert path.read_bytes() == value
    with pytest.raises(ValueError):
        save_reader_source(tmp_path, 'fed_test', b'not XML')
    assert path.read_bytes() == value
    assert not list(tmp_path.glob('.*'))


@pytest.mark.parametrize('act_id', ['../oops', 'fed_../oops', 'eu_test', 'fed_test/foo'])
def test_raw_source_capture_rejects_invalid_paths(tmp_path, act_id):
    with pytest.raises(ValueError):
        save_reader_source(tmp_path, act_id, b'<dokumente/>')
