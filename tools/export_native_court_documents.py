#!/usr/bin/env python3
"""Build byte-addressable decision texts from a frozen native Kerf corpus.

Run `python -m tools.export_native_court_documents SNAPSHOT.sqlite RELEASE_DIR`.
Every decision uses its native reader identity and its own verified gzip frame.
The release is complete only after manifest.json and locations.sqlite exist.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack, closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from courts.read import get_decision_by_id, open_db

SHARD_LIMIT = 128 * 1024**2
FRAME_LIMIT = 64 * 1024**2
SCHEMA = """
CREATE TABLE locations(
    id TEXT PRIMARY KEY, shard TEXT NOT NULL, start INTEGER NOT NULL,
    length INTEGER NOT NULL, raw_length INTEGER NOT NULL, sha256 TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE shards(path TEXT PRIMARY KEY, size INTEGER NOT NULL, sha256 TEXT NOT NULL) WITHOUT ROWID;
"""


def export_source(args):
    snapshot, target, source, expected = args
    if not re.fullmatch(r'[a-z0-9-]+', source):
        raise ValueError(f'Invalid source ID {source!r}')
    index_path = target / (source + '.sqlite')
    counts = dict(documents=0, paragraphs=0, held=0, held_paragraphs=0)
    with closing(open_db(snapshot)) as corpus, closing(sqlite3.connect(index_path)) as index, ExitStack() as files:
        index.executescript(SCHEMA)
        output = None
        number = 0
        for identifier, paragraph_count in corpus.execute(
                'SELECT id, paragraph_count FROM decisions WHERE source_id=? ORDER BY source_doc_id', (source,)):
            record = get_decision_by_id(corpus, identifier, with_relations=False)
            if record['status'] == 'ok':
                decision = record['decision']
                paragraphs = decision['paragraphs']
                if decision['id'] != identifier or len(paragraphs) != paragraph_count:
                    raise ValueError(f'Decision identity or paragraph count mismatch: {identifier}')
                for paragraph in paragraphs:
                    if hashlib.sha256(paragraph['text'].encode()).hexdigest() != paragraph['text_hash']:
                        raise ValueError(f'Paragraph hash mismatch: {paragraph["id"]}')
                counts['paragraphs'] += len(paragraphs)
            elif record['status'] == 'integrity_check_failed' and record['decision_id'] == identifier:
                counts['held'] += 1
                counts['held_paragraphs'] += paragraph_count
            else:
                raise ValueError(f'Could not resolve native decision {identifier}: {record["status"]}')
            raw = json.dumps(record, ensure_ascii=False, separators=(',', ':')).encode()
            frame = gzip.compress(raw, compresslevel=3, mtime=0)
            if max(len(raw), len(frame)) > FRAME_LIMIT:
                raise ValueError(f'Record exceeds reader limit: {identifier}')
            if output is None or output.tell() + len(frame) > SHARD_LIMIT:
                if output is not None:
                    index.execute('INSERT INTO shards VALUES (?,?,?)', (shard, output.tell(), digest.hexdigest()))
                    files.close()
                shard = f'packs/{source}-{number:04d}.pack'
                output = files.enter_context((target / shard).open('xb'))
                digest = hashlib.sha256()
                number += 1
            start = output.tell()
            output.write(frame)
            digest.update(frame)
            index.execute('INSERT INTO locations VALUES (?,?,?,?,?,?)',
                          (identifier, shard, start, len(frame), len(raw), hashlib.sha256(frame).hexdigest()))
            counts['documents'] += 1
        if output is not None:
            index.execute('INSERT INTO shards VALUES (?,?,?)', (shard, output.tell(), digest.hexdigest()))
        if counts['documents'] != expected:
            raise ValueError(f'Document count changed for {source}')
        index.commit()
    print(source, counts, flush=True)
    return source, counts


def export(snapshot: Path, destination: Path, workers: int):
    snapshot = snapshot.resolve(strict=True)
    destination = destination.resolve()
    if destination.exists():
        raise ValueError(f'Destination already exists: {destination}')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(open_db(snapshot)) as corpus:
        sources = corpus.execute('SELECT source_id, count(*) FROM decisions GROUP BY source_id ORDER BY count(*) DESC').fetchall()
        paragraphs = corpus.execute('SELECT count(*) FROM paragraphs').fetchone()[0]
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.court-export-') as stage_name:
        stage = Path(stage_name)
        target = stage / 'release'
        (target / 'packs').mkdir(parents=True)
        jobs = [(snapshot, target, name, count) for name, count in sources]
        with ProcessPoolExecutor(max_workers=workers) as pool:
            counts = dict(pool.map(export_source, jobs))
        if sum(s['paragraphs'] + s['held_paragraphs'] for s in counts.values()) != paragraphs:
            raise ValueError('Corpus-wide paragraph count mismatch')
        location_file = target / 'locations.sqlite'
        with closing(sqlite3.connect(location_file)) as index:
            index.executescript(SCHEMA)
            for source in sorted(counts):
                per_source = target / (source + '.sqlite')
                index.execute('ATTACH DATABASE ? AS src', (str(per_source),))
                with index:
                    for table in ('locations', 'shards'):
                        index.execute(f'INSERT INTO {table} SELECT * FROM src.{table}')
                index.execute('DETACH DATABASE src')
                per_source.unlink()
            if index.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('Index integrity check failed')
            shards = {p: dict(size=size, sha256=digest) for p, size, digest in
                      index.execute('SELECT path,size,sha256 FROM shards ORDER BY path')}
        with location_file.open('rb') as stream:
            index_hash = hashlib.file_digest(stream, 'sha256').hexdigest()
        manifest = dict(format='kerf-native-court-documents/1', documents=sum(n for _, n in sources),
                        paragraphs=paragraphs, sources=counts, locations_sha256=index_hash, shards=shards)
        (target / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True) + '\n')
        os.rename(target, destination)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('snapshot', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--workers', type=int, default=4)
    args = parser.parse_args()
    export(args.snapshot, args.destination, args.workers)
