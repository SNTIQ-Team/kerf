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

CANONICAL_DOCUMENT = re.compile(r"jb-[A-Z0-9]{1,64}")
DOCUMENT_ID = re.compile(r"(?:jb-)?([A-Z0-9]{1,64})", re.IGNORECASE | re.ASCII)
ECLI = re.compile(r"ECLI:[A-Z]{2}:[A-Z0-9.]+:[0-9]{4}:[A-Z0-9.]+", re.IGNORECASE | re.ASCII)
MAX_FRAME = 64 * 1024**2
MAX_LOOKUP = 8 * 1024**2
MAX_MANIFEST = 4 * 1024**2


class MissingRecord(LookupError):
    """The verified catalogue or decision has no such item."""


class EcliIndexUnavailable(LookupError):
    """This release predates the optional ECLI index."""


def canonical_ecli(value):
    if not isinstance(value, str) or len(value) > 200 or not ECLI.fullmatch(value):
        raise ValueError("Expected a complete ECLI identifier")
    return value.upper()


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
        self.has_ecli_index = "ecli_index" in manifest
        self.ecli_index = manifest.get("ecli_index")
        if not any(p.startswith("lookup/") for p in self.files):
            raise ValueError("RII lookup files missing from the manifest")

    def _file(self, path):
        if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9/._-]+", path):
            raise ValueError("Invalid RII archive path")
        if path.startswith("/") or ".." in PurePosixPath(path).parts or "\\" in path:
            raise ValueError("Invalid RII archive path")
        return self.files[path]

    @lru_cache(maxsize=12)
    def _bucket(self, bucket, directory="lookup"):
        path = f"{directory}/{bucket}.json.gz"
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
        table = self._bucket(bucket)
        if document not in table:
            raise MissingRecord(document)
        entry = table[document]
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

    @lru_cache(maxsize=256)
    def metadata(self, document):
        # Cache only verified metadata. Whole decisions can be many megabytes.
        return self.decision(document)["metadata"]

    def decisions_by_ecli(self, ecli):
        identifier = canonical_ecli(ecli)
        if not self.has_ecli_index:
            raise EcliIndexUnavailable()
        # The publisher writes every bucket, including empty ones. A missing
        # file in an advertised index cannot establish that an ECLI is absent.
        if not isinstance(self.ecli_index, dict) or self.ecli_index.get("buckets") != 256:
            raise ValueError("Incomplete RII ECLI index")
        bucket = sha256(identifier.encode("ascii")).hexdigest()[:2]
        self._file(f"ecli/{bucket}.json.gz")
        table = self._bucket(bucket, "ecli")
        decisions = []
        if identifier in table:
            documents = table[identifier]
            if not isinstance(documents, list) or not documents or documents != sorted(set(documents)):
                raise ValueError("Invalid RII ECLI index entry")
            for document in documents:
                if not isinstance(document, str) or not CANONICAL_DOCUMENT.fullmatch(document):
                    raise ValueError("Invalid document number in the RII ECLI index")
                try:
                    metadata = self.metadata(document)
                except MissingRecord as exc:
                    raise ValueError("RII ECLI index refers to an absent decision") from exc
                if canonical_ecli(metadata["ecli"].strip()) != identifier:
                    raise ValueError("RII ECLI differs from the decision metadata")
                decisions.append(metadata)
        return {"ecli": identifier, "total": len(decisions), "decisions": decisions}

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
                try:
                    xml = archive.read(members[0])
                except RuntimeError as exc:
                    raise ValueError("Unsupported RII ZIP compression or encryption") from exc
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
