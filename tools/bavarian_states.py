"""Verified full BAYERN.RECHT captures; archival dates are not effective dates.

Offline only. The persistent source store retains complete captured HTML and
canonical act JSON as immutable content-addressed gzip objects. Every public
refresh reparses the retained HTML and compares the complete canonical state.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, NavigableString, Tag

from tools.official_states import canonical_json_bytes, _atomic_write, _deterministic_gzip

DATE_BASIS = 'archive_capture_not_effective_date'
MAX_SOURCE_BYTES = 16 * 1024 * 1024
SHA = re.compile(r'[0-9a-f]{64}')


class BavarianStateError(ValueError):
    """A capture, source identity, or immutable state failed verification."""


def _clean(value: str) -> str:
    return ' '.join(value.split())


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _empty() -> dict:
    return {'schema_version': 1, 'source_store': 'bavarian_states',
            'source_policy': {'official_only': True, 'archived_official_html': True,
                              'effective_dates_inferred': False},
            'observations': [], 'objects': {}, 'total_observations': 0,
            'total_states': 0}


def _source_identity(original_url: str, archive_url: str,
                     archive_timestamp: str, document_key: str) -> datetime:
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', document_key):
        raise BavarianStateError('invalid document key')
    parts = urlsplit(original_url)
    if parts.scheme not in {'http', 'https'} or parts.hostname not in {
            'www.gesetze-bayern.de', 'gesetze-bayern.de'} or parts.username or parts.password or \
            parts.query or parts.fragment or parts.path.lower() != \
            f'/Content/Document/{document_key}/true'.lower():
        raise BavarianStateError('source is not the official full document')
    try:
        if parts.port not in {None, 80, 443} or not re.fullmatch(r'\d{14}', archive_timestamp):
            raise ValueError('invalid port/timestamp')
        captured = datetime.strptime(archive_timestamp, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise BavarianStateError('invalid capture timestamp') from exc
    if archive_url != f'https://web.archive.org/web/{archive_timestamp}id_/{original_url}':
        raise BavarianStateError('archive identity differs from original URL/capture')
    return captured


def _render(node) -> str:
    """Keep all source characters, inserting only semantic whitespace."""
    if isinstance(node, NavigableString):
        return re.sub(r'\s+', ' ', str(node))
    if not isinstance(node, Tag):
        return ''
    if node.name not in {'div', 'dl', 'dt', 'dd', 'sup', 'sub', 'a', 'b', 'i',
                         'strong', 'em', 'span', 'br', 'hr', 'p'}:
        raise BavarianStateError(f'unsupported archived body element: {node.name}')
    if node.name in {'br', 'hr'}:
        return '\n\n'
    if node.name == 'dl':
        parts, pending = [], ''
        for child in node.children:
            if isinstance(child, NavigableString) and not child.strip():
                continue
            if not isinstance(child, Tag) or child.name not in {'dt', 'dd'}:
                raise BavarianStateError('unexpected definition-list child')
            if child.name == 'dt':
                if pending:
                    raise BavarianStateError('unpaired list label')
                pending = _render(child).strip()
            else:
                if not pending:
                    raise BavarianStateError('unlabelled definition-list item')
                parts.append(pending + ' ' + _render(child).strip())
                pending = ''
        if pending:
            raise BavarianStateError('unpaired list label')
        return '\n\n' + '\n\n'.join(parts) + '\n\n'
    pieces = []
    for child in node.children:
        if isinstance(child, Tag) and child.name == 'sup' and 'satznr' in child.get('class', []):
            prior = _clean(''.join(pieces))
            prefix = '\n\n' if prior and not re.fullmatch(r'\(\d+[a-z]?\)', prior) else ' '
            pieces.append(prefix + _clean(child.get_text()) + ' ')
        else:
            pieces.append(_render(child))
    value = ''.join(pieces)
    if node.name in {'div', 'p'}:
        return '\n\n' + value.strip() + '\n\n'
    if node.name in {'sup', 'sub'}:
        return ' ' + value.strip() + ' '
    return value


def parse_capture(html: bytes, *, act_id: str, jurabk: str, document_key: str,
                  original_url: str, archive_url: str, archive_timestamp: str,
                  retrieved_at: str) -> tuple[dict, dict]:
    captured = _source_identity(original_url, archive_url, archive_timestamp, document_key)
    if not re.fullmatch(r'by_[a-z0-9][a-z0-9_-]*', act_id) or not jurabk:
        raise BavarianStateError('invalid Bavarian act identity')
    if not isinstance(html, bytes) or len(html) > MAX_SOURCE_BYTES or b'</html>' not in html[-100:].lower():
        raise BavarianStateError('incomplete or oversized HTML capture')
    try:
        text = html.decode('utf-8')
        datetime.fromisoformat(retrieved_at.replace('Z', '+00:00'))
    except (UnicodeError, ValueError) as exc:
        raise BavarianStateError('invalid source encoding/retrieval time') from exc
    soup = BeautifulSoup(text, 'html.parser')
    content = soup.select_one('#content')
    metadata = soup.select_one('#doc-metadata b')
    full_pdf = soup.select_one(f'#doc-toolbar a[href="/Content/Pdf/{document_key}?all=True"]')
    if content is None or metadata is None or _clean(metadata.get_text()) != jurabk or full_pdf is None:
        raise BavarianStateError('official full-view identity/metadata missing')
    title = content.select_one('.titelzeile')
    stand = content.select_one('.standangabe')
    if title is None or jurabk not in title.get_text():
        raise BavarianStateError('historical title missing or wrong act')
    toc = []
    prefix = f'/Content/Document/{document_key}-'
    for link in content.select('.subdoc a[href]'):
        href = link['href']
        if href.startswith(prefix) and re.fullmatch(r'\d+[a-z]?', href[len(prefix):]):
            toc.append(href[len(prefix):])
    norms, labels, hierarchy = [], [], {}
    for container in content.select('.cont'):
        for heading in container.select('.gltitel'):
            if not re.fullmatch(r'h[1-6]', heading.name):
                raise BavarianStateError('unexpected grouping heading')
            level = int(heading.name[1])
            hierarchy = {k: v for k, v in hierarchy.items() if k < level}
            hierarchy[level] = _clean(heading.get_text(' ', strip=True))
        heading = container.select_one('.paraheading')
        if heading is None:
            continue
        label = heading.select_one('.paranr')
        norm_title = heading.select_one('.paratitel')
        name = _clean(label.get_text(' ', strip=True)) if label else ''
        match = re.fullmatch(r'(?:Art\.|§)\s*(\d+[a-z]?)', name)
        if match is None:
            raise BavarianStateError(f'unsupported norm label: {name}')
        labels.append(match.group(1))
        fragments, raw = [], []
        for child in container.children:
            if child is heading or isinstance(child, NavigableString) and not child.strip():
                continue
            if isinstance(child, Tag) and 'clearer' in child.get('class', []):
                continue
            fragments.append(_render(child))
            raw.append(child.get_text() if isinstance(child, Tag) else str(child))
        body = '\n\n'.join(_clean(line) for line in ''.join(fragments).split('\n') if line.strip())
        if re.sub(r'\s', '', body) != re.sub(r'\s', '', ''.join(raw)):
            raise BavarianStateError(f'source characters changed for {name}')
        norms.append({'enbez': name, 'titel': _clean(norm_title.get_text(' ', strip=True)) if norm_title else '',
                      'text': body, 'glied': list(hierarchy.values())})
    if not toc or len(set(toc)) != len(toc) or toc != labels:
        raise BavarianStateError('full-document TOC does not match all norm bodies in order')
    observed = captured.date().isoformat()
    state = {'id': act_id, 'jurabk': jurabk, 'juris': 'DE-BY',
             'title': _clean(title.get_text(' ', strip=True)),
             'stand': _clean(stand.get_text(' ', strip=True)) if stand else '',
             # Capture time belongs to the observation, not the content identity.
             'build': '', 'norm_count': len(norms), 'norms': norms}
    observation = {'act_id': act_id, 'jurabk': jurabk, 'document_key': document_key,
                   'source': 'BAYERN.RECHT', 'source_store': 'bavarian_states',
                   'observed_at': observed, 'captured_at': captured.isoformat().replace('+00:00', 'Z'),
                   'retrieved_at': retrieved_at, 'archive_timestamp': archive_timestamp,
                   'date_basis': DATE_BASIS, 'verification': 'exact',
                   'source_url': original_url, 'original_url': original_url,
                   'archive_url': archive_url, 'source_html_sha256': _sha(html),
                   'state_sha256': _sha(canonical_json_bytes(state)), 'norm_count': len(norms)}
    return state, observation


def _path(kind: str, digest: str) -> Path:
    if not isinstance(digest, str) or SHA.fullmatch(digest) is None:
        raise BavarianStateError('invalid object digest')
    suffix = 'json.gz' if kind == 'objects' else 'html.gz'
    return Path(kind) / 'sha256' / digest[:2] / f'{digest}.{suffix}'


def _load(store: Path) -> dict:
    path = store / 'manifest.json'
    if not path.exists():
        return _empty()
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise BavarianStateError('invalid manifest') from exc
    if not isinstance(value, dict) or value.get('schema_version') != 1 or value.get('source_store') != 'bavarian_states' or \
            not isinstance(value.get('observations'), list) or not isinstance(value.get('objects'), dict):
        raise BavarianStateError('invalid Bavarian manifest schema')
    return value


def _read_verified(store: Path, kind: str, digest: str) -> bytes:
    try:
        with gzip.open(store / _path(kind, digest), 'rb') as handle:
            data = handle.read(MAX_SOURCE_BYTES + 1)
    except (OSError, ValueError) as exc:
        raise BavarianStateError('source object unavailable') from exc
    if len(data) > MAX_SOURCE_BYTES or _sha(data) != digest:
        raise BavarianStateError('source object hash mismatch')
    return data


def _verified_states(store: Path, manifest: dict) -> list[tuple[dict, dict]]:
    rows, seen = [], set()
    for obs in manifest['observations']:
        if not isinstance(obs, dict):
            raise BavarianStateError('invalid observation')
        try:
            raw = _read_verified(store, 'sources', obs['source_html_sha256'])
            state, rebuilt = parse_capture(raw, **{k: obs[k] for k in (
                'act_id', 'jurabk', 'document_key', 'original_url', 'archive_url', 'archive_timestamp', 'retrieved_at')})
            key = (obs['act_id'], obs['archive_timestamp'])
            if obs != rebuilt or key in seen:
                raise BavarianStateError('observation differs from retained source')
            seen.add(key)
            canonical = _read_verified(store, 'objects', obs['state_sha256'])
            if canonical != canonical_json_bytes(state):
                raise BavarianStateError('state differs from retained HTML')
        except KeyError as exc:
            raise BavarianStateError('incomplete observation') from exc
        rows.append((state, obs))
    if set(manifest['objects']) != {obs['state_sha256'] for _, obs in rows}:
        raise BavarianStateError('manifest object inventory mismatch')
    if manifest.get('total_states') != len(manifest['objects']) or manifest.get('total_observations') != len(rows):
        raise BavarianStateError('manifest counts mismatch')
    return rows


def capture_html(store: Path, html: bytes, **kwargs) -> dict:
    store = Path(store)
    state, observation = parse_capture(html, **kwargs)
    manifest = _load(store)
    _verified_states(store, manifest)
    for row in manifest['observations']:
        if (row['act_id'], row['archive_timestamp']) == (observation['act_id'], observation['archive_timestamp']):
            if row['state_sha256'] != observation['state_sha256'] or row['source_html_sha256'] != observation['source_html_sha256']:
                raise BavarianStateError('conflicting immutable capture identity')
            return row
    digest = observation['state_sha256']
    for kind, value, identity in [('sources', html, observation['source_html_sha256']),
                                  ('objects', canonical_json_bytes(state), digest)]:
        path = store / _path(kind, identity)
        if path.exists():
            if _read_verified(store, kind, identity) != value:
                raise BavarianStateError('immutable object collision')
        else:
            _atomic_write(path, _deterministic_gzip(value))
    manifest['observations'].append(observation)
    manifest['observations'].sort(key=lambda x: (x['act_id'], x['archive_timestamp']))
    manifest['objects'][digest] = {'path': _path('objects', digest).as_posix(),
                                  'canonical_bytes': len(canonical_json_bytes(state))}
    manifest['total_observations'] = len(manifest['observations'])
    manifest['total_states'] = len(manifest['objects'])
    _atomic_write(store / 'manifest.json', canonical_json_bytes(manifest))
    return observation


def publish_states(store: Path, output: Path) -> dict:
    """Verify every HTML/state pair before publishing; no network or date inference."""
    store, output = Path(store), Path(output)
    manifest = _load(store)
    rows = _verified_states(store, manifest)
    for state, observation in rows:
        digest = observation['state_sha256']
        target = output / _path('objects', digest)
        _atomic_write(target, _deterministic_gzip(canonical_json_bytes(state)))
        target.chmod(0o644)  # Public release is read by the separate API service user.
    if rows:
        _atomic_write(output / 'manifest.json', canonical_json_bytes(manifest))
        (output / 'manifest.json').chmod(0o644)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--html', type=Path, required=True)
    for name in ('act-id', 'jurabk', 'document-key', 'original-url', 'archive-url', 'archive-timestamp', 'retrieved-at'):
        parser.add_argument('--' + name, required=True)
    args = vars(parser.parse_args())
    html, store = args.pop('html'), args.pop('store')
    print(json.dumps(capture_html(store, html.read_bytes(), **args), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
