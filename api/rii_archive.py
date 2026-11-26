"""Read published RII decisions from byte ranges in a pinned Kerf Hub release."""
from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
from pathlib import PurePosixPath
import io
import json
import re
import zipfile

from tools.read_court_archive import BASE, fetch, unpack, verify

DOCUMENT_ID = re.compile(r"(?:jb-)?([A-Z0-9]{1,64})", re.IGNORECASE | re.ASCII)
MAX_FRAME = 64 * 1024**2
MAX_LOOKUP = 8 * 1024**2
MAX_MANIFEST = 4 * 1024**2


class MissingRecord(LookupError):
    """The verified catalogue or decision has no such item."""


def document_id(value):
    match = DOCUMENT_ID.fullmatch(value)
    if not match:
        raise MissingRecord(value)
    return "jb-" + match[1].upper()


def bounded(value, maximum, *, minimum=1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid byte range in the RII manifest")
    return value


class RiiArchive:
    def __init__(self, revision: str, manifest_sha256: str):
        if not re.fullmatch(r"[0-9a-f]{40}", revision) or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
            raise ValueError("The RII release requires a pinned Hub revision and manifest hash")
        self.revision = revision
        self.manifest_sha256 = manifest_sha256
        self.base = f"{BASE}/{revision}/rii"
        raw = fetch(self.base + "/manifest.json", MAX_MANIFEST)
        if sha256(raw).hexdigest() != manifest_sha256:
            raise ValueError("RII manifest SHA-256 mismatch")
        manifest = json.loads(raw)
        if manifest["format"] != "kerf-rii-publications/1" or not isinstance(manifest["files"], dict):
            raise ValueError("Unsupported RII manifest")
        self.files = manifest["files"]
        if not any(p.startswith("lookup/") for p in self.files):
            raise ValueError("RII lookup files missing from the manifest")

    def _file(self, path):
        if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9/._-]+", path):
            raise ValueError("Invalid RII archive path")
        if path.startswith("/") or ".." in PurePosixPath(path).parts or "\\" in path:
            raise ValueError("Invalid RII archive path")
        return self.files[path]

    @lru_cache(maxsize=12)
    def _bucket(self, bucket):
        path = f"lookup/{bucket}.json.gz"
        if path not in self.files:
            return {}
        entry = self._file(path)
        size = bounded(entry["size"], MAX_LOOKUP)
        data = fetch(f"{self.base}/{path}", size)
        verify(data, size, entry["sha256"])
        table = json.loads(unpack(data, MAX_LOOKUP))
        if not isinstance(table, dict):
            raise ValueError("Invalid RII lookup table")
        return table

    def _entry(self, document):
        bucket = sha256(document.encode()).hexdigest()[:2]
        entry = self._bucket(bucket).get(document)
        if entry is None:
            raise MissingRecord(document)
        if not isinstance(entry, dict):
            raise ValueError("Invalid RII catalogue entry")
        return entry

    def _frame(self, path, offset, length, digest):
        total = bounded(self._file(path)["size"], 2**63 - 1)
        size = bounded(length, MAX_FRAME)
        start = bounded(offset, total - size, minimum=0)
        packed = fetch(f"{self.base}/{path}", size, (start, size))
        verify(packed, size, digest)
        return packed

    def decision(self, document):
        document = document_id(document)
        entry = self._entry(document)
        ref = entry["document_frame"]
        packed = self._frame(ref["path"], ref["offset"], ref["length"], ref["packed_sha256"])
        size = bounded(ref["bytes"], MAX_FRAME)
        raw = unpack(packed, size)
        verify(raw, size, ref["sha256"])
        result = json.loads(raw)
        meta, passages = result["metadata"], result["passages"]
        if (meta["document"] != document or meta["paragraphs"] != len(passages)
                or any(meta[k] != entry[k] for k in ("packed_sha256", "zip_sha256", "xml_sha256", "source_zip", "pack_format"))):
            raise ValueError("RII document frame does not match the catalogue entry")
        anchors = set()
        for p in passages:
            anchor = p["anchor"]
            if (p["document"] != document or not isinstance(anchor, str) or not anchor or anchor in anchors
                    or sha256(p["text"].encode()).hexdigest() != p["text_sha256"]):
                raise ValueError("Invalid RII passage identity or text")
            anchors.add(anchor)
        if sum(p["printed_number"] is not None for p in passages) != meta["printed_numbers"]:
            raise ValueError("RII printed number count differs from the frame metadata")
        return result

    def source(self, document):
        document = document_id(document)
        entry = self._entry(document)
        raw = self._frame(entry["original_pack"], entry["offset"], entry["length"], entry["packed_sha256"])
        if entry["pack_format"] == "zip" and entry["attachment_count"] == 0:
            verify(raw, len(raw), entry["zip_sha256"])
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                members = [m for m in archive.infolist() if not m.is_dir()]
                if (len(members) != 1 or PurePosixPath(members[0].filename).name.casefold() != document.casefold()+".xml"
                        or members[0].file_size > MAX_FRAME):
                    raise ValueError("Unexpected files in the RII decision ZIP")
                xml = archive.read(members[0])
            payload, extension = raw, "zip"
        elif entry["pack_format"] == "gzip_xml" and entry["attachment_count"] > 0:
            xml = unpack(raw, MAX_FRAME)
            payload, extension = xml, "xml"
        else:
            raise ValueError("Unsupported RII source representation")
        verify(xml, len(xml), entry["xml_sha256"])
        return payload, extension


@lru_cache(maxsize=2)
def open_archive(revision, manifest_sha256):
    return RiiArchive(revision, manifest_sha256)


def passage_for(decision, anchor):
    for passage in decision["passages"]:
        if passage["anchor"] == anchor:
            return passage
    raise MissingRecord(anchor)


def select_passages(decision, rn=None, query=None, offset=0, limit=50):
    rows = decision["passages"]
    if rn is not None:
        rows = [p for p in rows if p["printed_number"] == rn]
    if query is not None:
        term = query.casefold()
        rows = [p for p in rows if term in p["text"].casefold()]
    return {"total": len(rows), "offset": offset, "passages": rows[offset:offset+limit]}
