"""Read one document or original from a pinned SNTIQ Kerf Hub dataset revision."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zlib
import time
from urllib.error import URLError, HTTPError
from http.client import IncompleteRead
from urllib.request import Request, urlopen

BASE = "https://huggingface.co/datasets/SNTIQ-Team/kerf/resolve"
MAX_MANIFEST = 4 * 1024**2
MAX_CATALOG = 64 * 1024**2
MAX_DOCUMENT = 64 * 1024**2


def fetch(url, limit, span=None):
    headers = {
        "User-Agent": "SNTIQ Kerf court archive reader",
        "Accept-Encoding": "identity",
    }
    if span is not None:
        start, length = span
        headers["Range"] = f"bytes={start}-{start + length - 1}"
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers=headers), timeout=45) as response:
                if span is not None:
                    expected = rf"bytes {start}-{start + length - 1}/\d+"
                    if response.status != 206 or not re.fullmatch(
                        expected, response.headers.get("Content-Range", "")
                    ):
                        raise ValueError(
                            "server did not return the exact requested byte range"
                        )
                payload = response.read(limit + 1)
            break
        except HTTPError as exc:
            if exc.code not in (429, 502, 503, 504) or attempt == 2:
                raise
            try:
                delay = min(30, max(1, int(exc.headers.get("Retry-After", 2**attempt))))
            except (TypeError, ValueError):
                delay = 2**attempt
            time.sleep(delay)
        except (URLError, TimeoutError, IncompleteRead):
            if attempt == 2:
                raise
            time.sleep(2**attempt)
    if len(payload) > limit:
        raise ValueError("response exceeded its allowed size")
    return payload


def unpack(payload, limit):
    stream = zlib.decompressobj(16 + zlib.MAX_WBITS)
    content = stream.decompress(payload, limit + 1)
    if (
        len(content) > limit
        or stream.unused_data
        or stream.unconsumed_tail
        or not stream.eof
    ):
        raise ValueError("invalid or oversized gzip member")
    return content


def verify(data, size, sha):
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
        raise ValueError("court source length or SHA-256 mismatch")


class CourtArchive:
    def __init__(self, revision):
        if not re.fullmatch("[0-9a-f]{40}", revision):
            raise ValueError("supply an immutable 40-character Hub commit")
        self.base = f"{BASE}/{revision}/court_archive"
        manifest = json.loads(fetch(self.base + "/manifest.json", MAX_MANIFEST))
        if manifest.get("schema") != "kerf-court-archive/1":
            raise ValueError("unknown archive schema")
        self.files = manifest["files"]
        entry = self.files["catalog.json.gz"]
        if not 0 < entry["size"] <= MAX_CATALOG:
            raise ValueError("invalid catalogue size")
        data = fetch(self.base + "/catalog.json.gz", entry["size"])
        verify(data, entry["size"], entry["sha256"])
        catalogue = json.loads(unpack(data, MAX_CATALOG))
        if (
            catalogue.get("public_database_sha256")
            != manifest["public_database_sha256"]
        ):
            raise ValueError("catalogue comes from a different public release")
        self.rows = {r["id"]: r for r in catalogue["records"]}
        if len(self.rows) != manifest["decisions"] or len(self.rows) != len(
            catalogue["records"]
        ):
            raise ValueError("unexpected court catalogue cardinality")

    def read(self, key, *, original=False):
        row = self.rows[key]
        name = row["pack"]
        if not re.fullmatch(r"packs/[a-z0-9-]+/\d{4}\.gz", name):
            raise ValueError("invalid court pack name")
        part = row["original" if original else "document"]
        off, length, size = part["offset"], part["length"], part["bytes"]
        if (
            off < 0
            or not 0 < length <= MAX_DOCUMENT
            or not 0 < size <= MAX_DOCUMENT
            or off + length > self.files[name]["size"]
        ):
            raise ValueError("invalid court frame extent")
        compressed = fetch(self.base + "/" + name, length, (off, length))
        verify(compressed, length, part["compressed_sha256"])
        content = unpack(compressed, size)
        verify(content, size, part["sha256"])
        if original:
            return content
        document = json.loads(content)
        if document["metadata"]["id"] != key:
            raise ValueError("court lookup returned a different document")
        return document


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("revision")
    ap.add_argument("decision_id")
    ap.add_argument("--original", action="store_true")
    args = ap.parse_args()
    archive = CourtArchive(args.revision)
    if args.decision_id not in archive.rows:
        ap.exit(1, "Decision absent from this archive revision.\n")
    result = archive.read(args.decision_id, original=args.original)
    if args.original:
        sys.stdout.buffer.write(result)
    else:
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
