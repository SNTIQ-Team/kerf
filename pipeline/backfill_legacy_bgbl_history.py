"""Bounded, resumable official BGBl event evidence for publications before 2023.

DIP supplies the exact official archive URL and publication citation. The
downloaded final BGBl PDF must agree with that citation before its amendment
articles are matched to the GII corpus. No historical consolidated bodies or
article commencement dates are inferred. Defaults cover 2006-2022; an earlier
--start and explicit --wahlperiode values can discover older available records.

The cumulative archive, including checkpoints and published inventories, has
a hard 400 MiB ceiling. A run has separate request, transfer and document
limits. Repeat the same command to resume. --rebuild is completely offline.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from pypdf.errors import DependencyError

from backfill_bgbl_history import (
    act_aliases, build_inventory, descriptor_matches, legal_name_forms, load_curated_acts,
    normalize_legal_name,
    parse_retrospective_article_sections,
)
from common import ROOT
from archive_receipts import (
    ArchiveReceiptError, attested_rows, parser_identity, receipted_hashes,
    row_digest,
)
from fetch_bgbl_documents import SourceIntegrityError, extract_pdf_text
from fetch_dip import BASE as DIP_BASE, PUBLIC_KEY

MIB = 1024 * 1024
ARCHIVE_LIMIT = 400 * MIB
ARCHIVE_ROOT = ROOT / "data" / "legacy_bgbl_history"
SNAPSHOT_ROOT = ROOT / "data" / "snapshots" / "bgbl_legacy_history_backfill"
SOURCE = "gii+dip+bgbl.de"
INTEGRITY_BASIS = "official_dip_url+publication_header+sha256"
PRIORITY_SLUGS = {
    "aufenthg_2004", "asylvfg_1992", "asylg", "asylblg", "azrg", "bvfg",
    "freiz_gg_eu_2004", "stag", "beschv_2013", "intv", "vwgo", "vwvfg",
    "sgg", "zpo", "stpo", "famfg", "fgo", "arbgg", "vwgvg", "sogg",
}
MONTHS = ("", "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember")


class BudgetExceeded(RuntimeError):
    """A resumable run has reached an explicit resource limit."""


class ResponseTooLarge(SourceIntegrityError):
    """One source exceeds its response bound; later sources may still fit."""


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _day(value: object) -> str:
    text = str(value or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError("invalid ISO date")
    return date.fromisoformat(text).isoformat()


def _official_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc not in {
            "www.bgbl.de", "search.dip.bundestag.de"}:
        raise SourceIntegrityError("unexpected official source origin")


def check_pdf_dependencies() -> None:
    from pypdf._crypt_providers import crypt_provider

    if crypt_provider[0] == "local_crypt_fallback":
        raise DependencyError("legacy BGBl requires pypdf[crypto]; install requirements.txt")


class BoundedHttp:
    """Stream every response with explicit total request and byte limits."""

    def __init__(self, *, max_requests: int, max_bytes: int, delay: float = 0.8,
                 session=None):
        if max_requests < 1 or max_bytes < 1 or delay < 0:
            raise ValueError("invalid HTTP limits")
        self.max_requests = max_requests
        self.max_bytes = max_bytes
        self.delay = delay
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = "Kerf/1.0 (official history import)"
        self.requests = 0
        self.downloaded_bytes = 0
        self._last = 0.0

    def get_bytes(self, url: str, *, params: dict | None = None,
                  max_bytes: int = 16 * MIB, redirects: int = 0) -> bytes:
        _official_url(url)
        run_remaining = self.max_bytes - self.downloaded_bytes
        remaining = min(max_bytes, run_remaining)
        if self.requests >= self.max_requests or remaining <= 0:
            raise BudgetExceeded("request or transfer budget reached")
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self.requests += 1
        self._last = time.monotonic()
        response = self.session.get(url, params=params, timeout=(15, 45),
                                    stream=True, allow_redirects=False)
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                if redirects >= 3:
                    raise SourceIntegrityError("too many official source redirects")
                target = urljoin(url, response.headers.get("Location", ""))
                _official_url(target)
                return self.get_bytes(target, max_bytes=max_bytes,
                                      redirects=redirects + 1)
            response.raise_for_status()
            length = response.headers.get("Content-Length")
            if length and int(length) > max_bytes:
                raise ResponseTooLarge(f"response declares {length} bytes; "
                                       f"per-response cap is {max_bytes}")
            if length and int(length) > remaining:
                raise BudgetExceeded("response exceeds remaining transfer budget")
            payload = bytearray()
            for block in response.iter_content(min(65536, remaining + 1)):
                if len(payload) + len(block) > remaining:
                    self.downloaded_bytes += remaining - len(payload)
                    if max_bytes <= run_remaining:
                        raise ResponseTooLarge(f"stream exceeds per-response cap of {max_bytes} bytes")
                    raise BudgetExceeded("stream exceeds remaining transfer budget")
                payload.extend(block)
                self.downloaded_bytes += len(block)
            return bytes(payload)
        finally:
            response.close()


class LegacyArchive:
    """Content addressed objects and one checkpoint under a cumulative cap."""

    def __init__(self, root: Path = ARCHIVE_ROOT, *,
                 max_bytes: int | None = ARCHIVE_LIMIT,
                 min_free_bytes: int = 128 * MIB, accounting_roots=()):
        # ``max_bytes=None`` is the workstation archive: the cumulative cap
        # protects the small VPS and has no meaning on the archive's home.
        if max_bytes is not None and not 0 < max_bytes <= ARCHIVE_LIMIT or \
                min_free_bytes < 0:
            raise ValueError("invalid archive limits")
        if max_bytes is None and Path(root).absolute().is_relative_to("/srv"):
            raise ValueError("an uncapped legacy archive is workstation-only")
        self.root = Path(root).absolute()
        self.max_bytes = max_bytes
        self.min_free_bytes = min_free_bytes
        self.accounting_roots = tuple(Path(p).absolute() for p in accounting_roots)
        if self.root.is_symlink():
            raise SourceIntegrityError("archive root is a symlink")
        self.root.mkdir(parents=True, exist_ok=True)

    def size_bytes(self) -> int:
        total = 0
        for root in (self.root, *self.accounting_roots):
            if root.is_symlink():
                raise SourceIntegrityError("archive accounting root is a symlink")
            if not root.exists():
                continue
            for parent, dirs, files in os.walk(root, followlinks=False):
                for name in (*dirs, *files):
                    path = Path(parent) / name
                    if path.is_symlink():
                        raise SourceIntegrityError("archive contains a symlink")
                total += sum((Path(parent) / name).stat().st_size for name in files)
        return total

    def reserve(self, size: int) -> None:
        if size < 0 or (self.max_bytes is not None and
                        self.size_bytes() + size > self.max_bytes):
            raise BudgetExceeded("cumulative source archive budget reached")
        if shutil.disk_usage(self.root).free < size + self.min_free_bytes:
            raise BudgetExceeded("disk free-space guard reached")

    def path(self, relative: str) -> Path:
        raw = Path(relative)
        if raw.is_absolute() or ".." in raw.parts or not raw.parts:
            raise SourceIntegrityError("object path escapes legacy archive")
        path = self.root / raw
        for candidate in (path, *path.parents):
            if candidate == self.root.parent:
                break
            if candidate.is_symlink():
                raise SourceIntegrityError("legacy object path contains a symlink")
        return path

    def write(self, relative: str, payload: bytes, *, immutable: bool = False) -> str:
        path = self.path(relative)
        if path.exists():
            previous = path.read_bytes()
            if previous == payload:
                return relative
            if immutable:
                raise SourceIntegrityError("corrupt immutable legacy object")
        # Include the temporary complete file while the old file still exists.
        self.reserve(len(payload))
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return relative

    def put(self, folder: str, payload: bytes, suffix: str) -> str:
        return self.write(f"{folder}/{_digest(payload)}{suffix}", payload,
                          immutable=True)

    def offloaded(self, relative: str, digest: str) -> bool:
        """True when the file is absent here but a workstation receipt holds
        exactly these bytes (see pipeline/archive_receipts.py)."""
        if self.path(relative).exists():
            return False
        key = f"{self.root.name}/{Path(relative).as_posix()}"
        offload_root = Path(os.environ.get(
            "KERF_OFFLOAD_ROOT", self.root.parent / "archive-offload"))
        return digest in receipted_hashes(offload_root).get(key, set())

    def read(self, relative: str, digest: str) -> bytes:
        if not re.fullmatch(r"[0-9a-f]{64}", str(digest or "")):
            raise SourceIntegrityError("invalid legacy source digest")
        payload = self.path(relative).read_bytes()
        if _digest(payload) != digest:
            raise SourceIntegrityError("legacy source digest mismatch")
        return payload


def load_checkpoint(archive: LegacyArchive) -> dict:
    path = archive.path("checkpoint.json")
    if not path.exists():
        return {"schema_version": 1, "discovery": {}, "documents": {}, "failures": {}}
    state = json.loads(path.read_bytes())
    if state.get("schema_version") != 1 or any(not isinstance(state.get(key), dict)
            for key in ("discovery", "documents", "failures")):
        raise SourceIntegrityError("invalid legacy checkpoint")
    return state


def save_checkpoint(archive: LegacyArchive, state: dict) -> None:
    archive.write("checkpoint.json", _json_bytes(state))


def cached_procedures(archive: LegacyArchive, state: dict, *, start: str, end: str,
                      wahlperioden: list[int]) -> list[dict]:
    found = {}
    seen_pages = set()
    for scope, progress in state["discovery"].items():
        scoped_start, scoped_end, period = scope.split(":")
        if int(period) not in wahlperioden or scoped_start > end or scoped_end < start:
            continue
        for page in progress["pages"]:
            if page["sha256"] in seen_pages:
                continue
            seen_pages.add(page["sha256"])
            payload = json.loads(gzip.decompress(archive.read(page["object"], page["sha256"])))
            for row in payload.get("documents") or []:
                found[str(row["id"])] = row
    return sorted(found.values(), key=lambda row: str(row["id"]))


def discover_procedures(http: BoundedHttp, key: str, archive: LegacyArchive,
                        state: dict, *, start: str, end: str,
                        wahlperioden: list[int]) -> list[dict]:
    found = {}
    for wp in wahlperioden:
        scope = f"{start}:{end}:{wp}"
        progress = state["discovery"].setdefault(scope, {
            "cursor": None, "complete": False, "pages": []})
        for page in progress["pages"]:
            payload = json.loads(gzip.decompress(archive.read(
                page["object"], page["sha256"])))
            for row in payload.get("documents") or []:
                found[str(row["id"])] = row
        while not progress["complete"]:
            params = {"f.vorgangstyp": "Gesetzgebung", "f.wahlperiode": wp,
                      "f.beratungsstand": "Verkündet", "f.datum.start": start,
                      "f.datum.end": end, "apikey": key}
            if progress["cursor"]:
                params["cursor"] = progress["cursor"]
            raw = http.get_bytes(f"{DIP_BASE}/vorgang", params=params,
                                 max_bytes=4 * MIB)
            payload = json.loads(raw)
            if not isinstance(payload.get("documents"), list):
                raise SourceIntegrityError("DIP returned no documents array")
            compressed = gzip.compress(raw, mtime=0)
            relative = archive.put("dip", compressed, ".json.gz")
            progress["pages"].append({"object": relative,
                                      "sha256": _digest(compressed)})
            for row in payload["documents"]:
                found[str(row["id"])] = row
            cursor = payload.get("cursor")
            progress["complete"] = (not payload["documents"] or not cursor or
                                     cursor == progress["cursor"])
            progress["cursor"] = cursor
            progress["num_found"] = payload.get("numFound")
            save_checkpoint(archive, state)
            print(f"DIP WP{wp}: {len(progress['pages'])} pages; "
                  f"{len(found)} procedures retained", flush=True)
    return sorted(found.values(), key=lambda row: str(row["id"]))


def legacy_source(promulgation: dict) -> dict:
    year = str(promulgation.get("jahrgang") or "")
    page = str(promulgation.get("seite") or "")
    issue = str(promulgation.get("heftnummer") or "")
    if not (year.isdigit() and 1949 <= int(year) <= 2022 and
            page.isdigit() and 0 < int(page) < 10000 and
            issue.isdigit() and 0 < int(issue) < 1000):
        raise SourceIntegrityError("invalid legacy publication reference")
    url = str(promulgation.get("pdf_url") or "")
    _official_url(url)
    parsed = urlparse(url)
    if parsed.netloc != "www.bgbl.de" or parsed.path != "/xaver/bgbl/start.xav":
        raise SourceIntegrityError("DIP did not supply the official legacy archive URL")
    query = parse_qs(parsed.query)
    start = query.get("start") or []
    if len(start) != 1:
        raise SourceIntegrityError("ambiguous legacy document selector")
    filename = f"bgbl1{int(year) % 100:02d}s{int(page):04d}.pdf"
    if not re.fullmatch(r"//\*\[@attr_id='" + re.escape(filename) + r"'\]", start[0]):
        raise SourceIntegrityError("DIP PDF identity disagrees with year/page citation")
    args = {"start": start[0], "startbk": "Bundesanzeiger_BGBl"}
    viewer_args = {"start": start[0], "bk": "Bundesanzeiger_BGBl", "skin": "pdf"}
    return {
        "document_id": f"bgbl-1-{year}-s{int(page)}", "year": year,
        "page": str(int(page)), "issue": str(int(issue)),
        "source_filename": filename, "dip_pdf_url": url,
        "official_html_url": "https://www.bgbl.de/xaver/bgbl/start.xav?" + urlencode(args),
        "official_pdf_url": "https://www.bgbl.de/xaver/bgbl/text.xav?" + urlencode(viewer_args),
    }


def select_legacy_documents(procedures, acts, start: str, end: str, *,
                            conflicts: dict | None = None) -> list[dict]:
    """Select exact identities; optionally quarantine and report conflicts.

    Without a report sink, retain fail-closed behavior. The CLI supplies a
    report so one inconsistent DIP citation cannot stop unrelated documents.
    A conflicting document is never reintroduced by a later matching row.
    """
    selected = {}
    conflicted = {}
    acts = list(acts)
    for procedure in procedures:
        matched = descriptor_matches(procedure, acts)
        for act in matched:
            act["aliases"] = sorted(act["aliases"])
        if not matched or procedure.get("beratungsstand") != "Verkündet":
            continue
        for promulgation in procedure.get("verkuendung") or []:
            if promulgation.get("verkuendungsblatt_kuerzel") != "BGBl I":
                continue
            try:
                published = _day(promulgation.get("verkuendungsdatum"))
                if not start <= published <= end:
                    continue
                identity = legacy_source(promulgation)
            except (ValueError, SourceIntegrityError):
                continue
            ref = {"procedure_id": str(procedure["id"]),
                   "wahlperiode": procedure.get("wahlperiode"),
                   "procedure_title": procedure.get("titel"),
                   "procedure_status": procedure.get("beratungsstand"),
                   "dip_entry_into_force": procedure.get("inkrafttreten") or [],
                   "matched_acts": matched}
            row = {**identity, "publication_date": published,
                   "execution_date": promulgation.get("ausfertigungsdatum"),
                   "title": procedure.get("titel"),
                   "fundstelle": promulgation.get("fundstelle"),
                   "retrospective_procedures": [ref]}
            key = identity["document_id"]
            if key in conflicted:
                conflicted[key].append(row)
                continue
            existing = selected.get(key)
            if existing:
                if any(existing[key] != row[key] for key in
                       ("publication_date", "execution_date", "issue")):
                    if conflicts is None:
                        raise SourceIntegrityError("conflicting legacy publication identity")
                    conflicted[key] = [selected.pop(key), row]
                    continue
                if ref not in existing["retrospective_procedures"]:
                    existing["retrospective_procedures"].append(ref)
            else:
                selected[key] = row

    if conflicts is not None:
        conflicts.clear()
        for key, rows in sorted(conflicted.items()):
            variants = {}
            for row in rows:
                identity = tuple(row[field] for field in
                                 ("publication_date", "execution_date", "issue"))
                variant = variants.setdefault(identity, {
                    **{field: row[field] for field in
                       ("publication_date", "execution_date", "issue", "official_pdf_url")},
                    "procedure_ids": [],
                })
                variant["procedure_ids"] = sorted(set(variant["procedure_ids"]) | {
                    proc["procedure_id"] for proc in row["retrospective_procedures"]})
            conflicts[key] = {
                "reason": "conflicting legacy publication identity",
                "variants": sorted(variants.values(), key=lambda item: json.dumps(item, sort_keys=True)),
            }

    def order(row):
        slugs = {act["slug"] for proc in row["retrospective_procedures"]
                 for act in proc["matched_acts"]}
        priority = any(s.startswith("sgb_") or s in PRIORITY_SLUGS for s in slugs)
        return (not priority, row["publication_date"], int(row["page"]))

    return sorted(selected.values(), key=order)


def _verify_publication_header(candidate: dict, text: str) -> None:
    first = " ".join(text.split("\f", 1)[0].split())
    published = date.fromisoformat(candidate["publication_date"])
    patterns = [
        rf"Bundesgesetzblatt\s+Jahrgang\s+{candidate['year']}\s+T\s*eil\s+I\b",
        rf"\bNr\.\s*{candidate['issue']}\b",
        rf"\b{published.day}\.\s*{MONTHS[published.month]}\s+{published.year}\b",
        rf"(?<!\d){candidate['page']}(?!\d)",
    ]
    if not all(re.search(pattern, first, re.IGNORECASE) for pattern in patterns):
        raise SourceIntegrityError("legacy PDF publication header disagrees with DIP")


def capture_legacy_bytes(candidate: dict, pdf: bytes, archive: LegacyArchive,
                         *, captured_at: str | None = None) -> dict:
    if not pdf.startswith(b"%PDF-"):
        raise SourceIntegrityError("legacy archive response is not PDF")
    try:
        text, pages = extract_pdf_text(pdf)
    except SourceIntegrityError as exc:
        if isinstance(exc.__cause__, DependencyError):
            raise exc.__cause__
        raise
    _verify_publication_header(candidate, text)
    encoded = text.encode("utf-8")
    archive.reserve(len(pdf) + len(encoded) + len(_json_bytes(candidate)))
    row = {**candidate, "sha256": _digest(pdf), "text_sha256": _digest(encoded),
           "pdf_bytes": len(pdf), "text_bytes": len(encoded), "page_count": pages,
           "pdf_object": archive.put("objects", pdf, ".pdf"),
           "text_object": archive.put("texts", encoded, ".txt"),
           "captured_at": captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "integrity_verified": True, "integrity_basis": INTEGRITY_BASIS,
           "publisher_checksum_available": False, "source": SOURCE}
    evidence = _json_bytes(row)
    row["legacy_document_sha256"] = _digest(evidence)
    row["legacy_document_object"] = archive.put("documents", evidence, ".json")
    return row


def capture_legacy_document(candidate: dict, archive: LegacyArchive,
                            http: BoundedHttp) -> dict:
    viewer = http.get_bytes(candidate["official_pdf_url"], max_bytes=2 * MIB)
    image = BeautifulSoup(viewer, "html.parser").select_one(".content span img")
    if image is None:
        raise SourceIntegrityError("legacy official viewer contains no PDF media link")
    url = urljoin(candidate["official_pdf_url"], str(image.get("src") or ""))
    _official_url(url)
    parsed = urlparse(url)
    expected = "/xaver/bgbl/media.xav/" + candidate["source_filename"]
    if parsed.netloc != "www.bgbl.de" or parsed.path != expected:
        raise SourceIntegrityError("legacy official viewer supplied a different PDF")
    pdf = http.get_bytes(url, max_bytes=16 * MIB)
    return capture_legacy_bytes(candidate, pdf, archive)


def legacy_article_sections(text: str) -> list[dict]:
    sections, _ = parse_retrospective_article_sections(text)
    for section in sections:
        lines = str(section["text"]).splitlines()[1:]
        heading = []
        for line in lines[:14]:
            clean = line.strip()
            if not clean:
                if heading:
                    break
                continue
            if re.match(r"^\([\d-]+\)", clean) or re.match(
                    r"^(?:Das|Die|Der|Dem|Den|Im|In|Nach|Vor|Bei)\s|^§|^\d+\.", clean):
                break
            # letter-spaced glyph runs of the first command ("I n § 1 A b s …")
            if re.match(r"^(?:I\s?n|I\s?m|D\s?e\s?m|N\s?a\s?c\s?h)\s?§", clean) or \
                    re.match(r"^\(\s?\d+\s?\)", clean) or re.match(
                        r"^Artikel\s+\d+[a-z]?\s+(?:Abs|Satz|wird|werden|des|der)",
                        clean):
                break
            heading.append(clean)
        if heading:
            joined = "\n".join(heading)
            joined = re.sub(r"(?<=\w)[-\u00ad]\n(?=\w)", "", joined)
            section["heading"] = " ".join(joined.split())
    return sections


def legacy_exact_target(section: dict, act: dict) -> bool:
    heading = normalize_legal_name(section.get("heading"))
    # Legacy PDFs insert spaces inside glyph runs. Require the complete same
    # character sequence, including every title qualifier, after whitespace.
    compact = re.sub(r"\s+", "", heading).rstrip("*")
    prefix = r"(?:(?:weitere|sonstige))?(?:änderung|aufhebung|neufassung)(?:des|der|von)"
    return any(re.fullmatch(prefix + re.escape(re.sub(r"\s+", "", form)), compact)
               for alias in act.get("aliases") or act_aliases(act)
               for form in legal_name_forms(alias))


def _verified_pinned(document: dict, archive: LegacyArchive) -> dict:
    """Verify the pinned evidence and that each source object is either
    present with its exact hash or receipted by the workstation archive.

    Used by the VPS capture-only mode: full text verification and candidate
    derivation happen on the workstation, which holds every source byte.
    """
    evidence = archive.read(document["legacy_document_object"],
                            document["legacy_document_sha256"])
    pinned = json.loads(evidence)
    expected = {key: value for key, value in document.items()
                if key not in {"legacy_document_object", "legacy_document_sha256"}}
    if pinned != expected:
        raise SourceIntegrityError("legacy document differs from pinned evidence")
    for obj, digest in ((pinned["pdf_object"], pinned["sha256"]),
                        (pinned["text_object"], pinned["text_sha256"])):
        if not archive.offloaded(obj, digest):
            archive.read(obj, digest)
    return pinned


def _verified_document(document: dict, archive: LegacyArchive) -> tuple[dict, str]:
    evidence = archive.read(document["legacy_document_object"],
                            document["legacy_document_sha256"])
    pinned = json.loads(evidence)
    expected = {key: value for key, value in document.items()
                if key not in {"legacy_document_object", "legacy_document_sha256"}}
    if pinned != expected:
        raise SourceIntegrityError("legacy document differs from pinned evidence")
    identity = legacy_source({"jahrgang": pinned["year"], "seite": pinned["page"],
                              "heftnummer": pinned["issue"], "pdf_url": pinned["dip_pdf_url"]})
    if any(pinned.get(key) != value for key, value in identity.items()):
        raise SourceIntegrityError("legacy source identity differs from DIP")
    archive.read(pinned["pdf_object"], pinned["sha256"])
    text = archive.read(pinned["text_object"], pinned["text_sha256"]).decode("utf-8")
    _verify_publication_header(pinned, text)
    sections = legacy_article_sections(text)
    return {**pinned, "articles": sections}, text


_LEGACY_PREFIX = re.compile(
    r"(?:(?:weitere|sonstige))?(?:änderung|aufhebung|neufassung)(?:des|der|von)")


SPACED_FORMS: dict[str, str] = {}


def heading_act_index(acts: list[dict]) -> dict[str, list[dict]]:
    """Compact exact legal-name forms → corpus acts (legacy_exact_target)."""
    index: dict[str, list[dict]] = {}
    for act in acts:
        forms = {}
        for alias in act.get("aliases") or act_aliases(act):
            for form in legal_name_forms(alias):
                forms[re.sub(r"\s+", "", form)] = form
        for compact, spaced in forms.items():
            if compact:
                index.setdefault(compact, []).append(act)
                SPACED_FORMS.setdefault(compact, spaced)
    return index


_COMMAND_START = re.compile(
    r"(?:(?:in|im|dem|nach|vor)?§|(?:in(?:der|den)?|die|der|das)"
    r"(?:inhaltsübersicht|überschrift|inhaltsverzeichnis))")
# "… Sozialgesetzbuch zum Jahr 2017", "… (Übergangsrecht)"
_HEADING_QUALIFIER = re.compile(r"(?:zum(?:jahr)?\d{4}|mitwirkungvom\S*|\(.{1,40}\))")
_COLLECTIVE = re.compile(
    r"^(?:(?:weitere|sonstige)\s+)?(?:folgeänderungen|änderungen?\s+"
    r"(?:weiterer|anderer|sonstiger|von)\s+(?:gesetze|vorschriften|"
    r"rechtsvorschriften|bundesrecht|verordnungen|gesetzen)|änderungen"
    r"(?!\s+(?:des|der|von)\s)|änderung\s+(?:kostenrechtlicher|"
    r"sozial|steuerlicher|verfahrensrechtlicher)\S*\s+(?:gesetze|"
    r"vorschriften))")


def heading_named_acts(section: dict, index: dict[str, list[dict]]
                       ) -> list[dict]:
    """Corpus acts an article heading names exactly ("Änderung des …").

    Legacy PDF text sometimes runs a single-command article's first command
    ("I n § 1 A b s a t z …") into the heading; the exact name must then be
    followed by a command start or by the act named again ("Das …gesetz").
    """
    heading = normalize_legal_name(section.get("heading"))
    compact = re.sub(r"\s+", "", heading).rstrip("*")
    match = _LEGACY_PREFIX.match(compact)
    if not match:
        return []
    rest = compact[match.end():]
    if rest in index:
        return index[rest]
    found = []
    for form, acts in index.items():
        if not rest.startswith(form) or len(form) < 8:
            continue
        tail = rest[len(form):]
        if _COMMAND_START.match(tail) or _HEADING_QUALIFIER.fullmatch(tail):
            found.extend(acts)
            continue
        for act in acts:
            forms = {re.sub(r"\s+", "", f)
                     for alias in act.get("aliases") or act_aliases(act)
                     for f in legal_name_forms(alias)}
            if any(tail.startswith(article + other) for article in
                   ("das", "die", "der") for other in forms if other):
                found.append(act)
    return found


def collective_subsections(section: dict, index: dict[str, list[dict]]
                           ) -> list[tuple[dict, dict]]:
    """(act, pseudo-section) for every subsection of a collective article
    ("Änderung weiterer Gesetze", "Folgeänderungen", "Weitere Änderungen …")
    whose opening clause names a corpus act exactly.  The pseudo-section
    carries the act's own heading and only that subsection's commands."""
    heading = normalize_legal_name(section.get("heading"))
    if not _COLLECTIVE.match(heading):
        return []
    raw = str(section.get("text") or "")
    marks = list(re.finditer(r"(?m)^\s*\(\s?(\d+[a-z]?)\s?\)", raw))
    out = []
    for position, mark in enumerate(marks):
        end = marks[position + 1].start() if position + 1 < len(marks) else \
            len(raw)
        body = raw[mark.start():end]
        clause = normalize_legal_name(body)
        verb = re.search(r"\s(?:wird|werden)\s", clause)
        compact = re.sub(r"\s+", "", clause[:verb.start()] if verb
                         else clause[:400])
        best = None
        for form, acts in index.items():
            if len(form) < 8:
                continue
            at = compact.find(form)
            if at < 0 or not (at <= 6 or compact[max(0, at - 3):at].endswith(
                    ("das", "die", "der", "des"))):
                continue
            if best is None or len(form) > len(best[0]):
                best = (form, acts)
        if best is None:
            continue
        form, acts = best
        pseudo = {**section,
                  "heading": f"Änderung des {SPACED_FORMS.get(form, form)}",
                  "text": (f"Artikel {section.get('article')}\n"
                           f"Änderung des {SPACED_FORMS.get(form, form)}\n"
                           + re.sub(r"^\s*\(\s?\d+[a-z]?\s?\)\s*", "", body)),
                  "legacy_collective_subsection": mark.group(1)}
        for act in acts:
            out.append((act, pseudo))
    return out


def collective_named_acts(section: dict, index: dict[str, list[dict]]
                          ) -> list[dict]:
    """Acts amended in a collective article ("Änderung weiterer Gesetze",
    "Folgeänderungen"): a subsection opening "(n) Das <exact name> …"."""
    heading = normalize_legal_name(section.get("heading"))
    if not _COLLECTIVE.match(heading):
        return []
    text = normalize_legal_name(section.get("text"))
    found = []
    # every subsection: its opening clause up to the operative verb names
    # the amended act ("(1) Das X …" or "(2) In § 5 des X vom … wird …")
    marks = list(re.finditer(r"\(\s?\d+\s?[a-z]?\s?\)", text))
    for position, mark in enumerate(marks):
        end = marks[position + 1].start() if position + 1 < len(marks) else \
            len(text)
        clause = text[mark.end():end]
        verb = re.search(r"\s(?:wird|werden)\s", clause)
        clause = clause[:verb.start()] if verb else clause[:400]
        compact = re.sub(r"\s+", "", clause)
        for form, acts in index.items():
            if len(form) < 8:
                continue
            at = compact.find(form)
            if at < 0:
                continue
            before = compact[max(0, at - 3):at]
            if at == 0 or before.endswith(("das", "die", "der", "des")):
                found.extend(acts)
    unique = {}
    for act in found:
        unique[str(act["slug"])] = act
    return list(unique.values())


def build_legacy_inventory(documents: list[dict], archive: LegacyArchive,
                           corpus_acts: list[dict] | None = None) -> list[dict]:
    """Candidates per article; an article whose heading names a corpus act
    exactly is matched to it even when DIP's descriptors did not list it."""
    inventory = []
    if corpus_acts is None:
        try:
            corpus_acts = load_curated_acts()
        except FileNotFoundError:
            corpus_acts = []
    index = heading_act_index(corpus_acts)
    for document in documents:
        verified, _ = _verified_document(document, archive)
        scoped_documents = []
        for section in verified["articles"]:
            procedures = [{**proc, "matched_acts": [
                act for act in proc["matched_acts"] if legacy_exact_target(section, act)]}
                for proc in verified["retrospective_procedures"]]
            known = {str(act["slug"]) for proc in procedures
                     for act in proc["matched_acts"]}
            def matched(act: dict, how: str) -> dict:
                return {"slug": act["slug"], "jurabk": act["jurabk"],
                        "long_title": act["long_title"],
                        "descriptor_aliases": [],
                        "aliases": sorted(act.get("aliases") or
                                          act_aliases(act)),
                        "discovery": how}
            extra = [matched(act, "article_heading")
                     for act in heading_named_acts(section, index)
                     if str(act["slug"]) not in known]
            extra = list({row["slug"]: row for row in extra}.values())
            if extra and procedures:
                procedures[0] = {**procedures[0], "matched_acts":
                                 [*procedures[0]["matched_acts"], *extra]}
            scoped_documents.append({**verified, "articles": [section],
                                     "retrospective_procedures": procedures})
            if not procedures:
                continue
            for act, pseudo in collective_subsections(section, index):
                if str(act["slug"]) in known or any(
                        row["slug"] == act["slug"] for row in extra):
                    continue
                known.add(str(act["slug"]))
                scoped_documents.append({
                    **verified, "articles": [pseudo],
                    "retrospective_procedures": [{
                        **procedures[0],
                        "matched_acts": [matched(act, "collective_subsection")]
                    }]})
        for row in build_inventory(scoped_documents):
            row.update({"source": SOURCE, "integrity_basis": INTEGRITY_BASIS,
                        "publisher_checksum_available": False,
                        "effective_at": None,
                        "effective_date_status": "unresolved_legacy_commencement_review",
                        "effective_date_basis": "unresolved_legacy_commencement_review",
                        "generated_at": verified["captured_at"],
                        "fundstelle": verified.get("fundstelle"),
                        "publication_page": verified["page"],
                        "publication_issue": verified["issue"],
                        "legacy_document_object": document["legacy_document_object"],
                        "legacy_document_sha256": document["legacy_document_sha256"]})
            inventory.append(row)
    return sorted(inventory, key=lambda row: (row["publication_date"], row["id"]))


# A real publication covers thousands of DIP procedures (2380-5521 so far).
# A snapshot below this floor is a test-sized or failed run; using it as the
# latest input would withdraw every legacy event from the public history.
MIN_DIP_PROCEDURES = 100


def usable_legacy_snapshot(path: Path) -> bool:
    """Whether a dated snapshot may be read as published legacy input.

    A signed workstation attestation admits the snapshot (its rows are
    verified separately by ``attested_rows``); an unattested snapshot must
    come from a real discovery run."""
    if (Path(path) / "attestation.json").is_file():
        return True
    try:
        summary = json.loads((Path(path) / "summary.json").read_text(
            encoding="utf-8"))
        procedures = int(summary.get("dip_procedures") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return procedures >= MIN_DIP_PROCEDURES


def latest_legacy_snapshot(root: Path | None = None) -> Path | None:
    """The newest usable snapshot; empty or test-sized runs are skipped."""
    root = Path(root or SNAPSHOT_ROOT)
    days = sorted(path for path in Path(root).iterdir()
                  if path.is_dir() and not path.name.startswith(".")) \
        if Path(root).is_dir() else []
    usable = [path for path in days if usable_legacy_snapshot(path)]
    return usable[-1] if usable else None


def attestation_notice(root: Path | None = None) -> str | None:
    """One line on which parser produced the attested rows, if any."""
    latest = latest_legacy_snapshot(root)
    if latest is None or not (latest / "attestation.json").is_file():
        return None
    try:
        _rows, summary = attested_rows(latest)
    except (ArchiveReceiptError, OSError, ValueError) as exc:
        return f"legacy attestation in {latest.name} is not valid: {exc}"
    current = parser_identity(ROOT)["sha256"]
    attested = (summary.get("parser") or {}).get("sha256")
    if attested == current:
        return (f"legacy candidates {latest.name}: workstation attestation, "
                f"parser {current[:12]}")
    return (f"legacy candidates {latest.name}: attested by parser "
            f"{str(attested)[:12]}, deployed parser {current[:12]}; run "
            "deploy/legacy-rebuild-push.sh on the workstation to rebuild")


def validate_legacy_candidate(candidate: dict, *, archive: LegacyArchive | None = None) -> None:
    # Production path: the workstation re-verified every source and signed
    # the exact candidate rows of the latest snapshot (archive_receipts).
    latest = latest_legacy_snapshot()
    if latest is not None and (latest / "attestation.json").is_file():
        try:
            rows, _summary = attested_rows(latest)
        except (ArchiveReceiptError, OSError, ValueError) as exc:
            raise SourceIntegrityError(
                f"legacy attestation failed verification: {exc}") from exc
        if row_digest(candidate) in rows:
            return
        raise SourceIntegrityError(
            "legacy candidate is not in the workstation attestation")
    archive = archive or LegacyArchive()
    try:
        evidence = archive.read(candidate["legacy_document_object"],
                                candidate["legacy_document_sha256"])
        document = json.loads(evidence)
        document.update({key: candidate[key] for key in
                         ("legacy_document_object", "legacy_document_sha256")})
        rows = build_legacy_inventory([document], archive)
    except (KeyError, OSError, ValueError) as exc:
        raise SourceIntegrityError("legacy candidate evidence is missing or invalid") from exc
    if not any(row == candidate for row in rows):
        raise SourceIntegrityError("legacy candidate differs from verified official source")


def capture_selected(selected: list[dict], state: dict, archive: LegacyArchive,
                     http: BoundedHttp, *, limit: int, retry_failures: bool = False,
                     capture_only: bool = False):
    stats = {"captured": 0, "reused": 0, "failed": 0, "skipped_failures": 0}
    attempted = 0
    for candidate in selected:
        key = candidate["document_id"]
        if key in state["documents"]:
            previous = state["documents"][key]
            if capture_only:
                _verified_pinned(previous, archive)
            else:
                _verified_document(previous, archive)
            if any(previous.get(field) != candidate.get(field) for field in
                   ("publication_date", "execution_date", "official_pdf_url", "issue")):
                raise SourceIntegrityError("resumed official document identity changed")
            if previous["retrospective_procedures"] != candidate["retrospective_procedures"]:
                refreshed = {**previous, **candidate}
                refreshed.pop("legacy_document_object")
                refreshed.pop("legacy_document_sha256")
                evidence = _json_bytes(refreshed)
                refreshed["legacy_document_sha256"] = _digest(evidence)
                refreshed["legacy_document_object"] = archive.put("documents", evidence, ".json")
                state["documents"][key] = refreshed
                save_checkpoint(archive, state)
            stats["reused"] += 1
            continue
        if key in state["failures"] and not retry_failures:
            stats["skipped_failures"] += 1
            continue
        if attempted >= limit:
            break
        attempted += 1
        try:
            row = capture_legacy_document(candidate, archive, http)
        except BudgetExceeded:
            save_checkpoint(archive, state)
            raise
        except (requests.RequestException, SourceIntegrityError, ValueError) as exc:
            state["failures"][key] = {"reason": type(exc).__name__ + ": " + str(exc),
                                      "source_url": candidate["official_pdf_url"]}
            stats["failed"] += 1
            print(f"Skipped {key}: {exc}", flush=True)
        else:
            state["documents"][key] = row
            state["failures"].pop(key, None)
            stats["captured"] += 1
            print(f"Captured {key}: {row['pdf_bytes']} PDF bytes", flush=True)
        save_checkpoint(archive, state)
    return list(state["documents"].values()), stats


def _publish(archive: LegacyArchive, state: dict, *, start: str, end: str,
             selected: list[dict], procedures: list[dict], http: BoundedHttp,
             stats: dict, stopped: str | None,
             selection_conflicts: dict | None = None,
             snapshot_root: Path | None = None) -> dict:
    # Resolve the root at call time: a default bound at import ignored a
    # patched SNAPSHOT_ROOT, so a unit test published an empty snapshot into
    # the real tree, which the next web build then read as its latest input.
    snapshot_root = Path(snapshot_root or SNAPSHOT_ROOT)
    selection_conflicts = selection_conflicts or {}
    # Keep immutable evidence in the checkpoint, but do not publish an identity
    # contradicted by the discovery inputs for this run.
    documents = [row for key, row in state["documents"].items()
                 if key not in selection_conflicts]
    candidates = build_legacy_inventory(documents, archive)
    years = sorted({row["publication_date"][:4] for row in candidates})
    summary = {"schema_version": 1, "source": SOURCE,
               "range": {"start": start, "end": end},
               "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "dip_procedures": len(procedures), "selected_documents": len(selected),
               "verified_documents": len(documents), "amendment_candidates": len(candidates),
               "acts_with_verified_candidates": len({row["act_id"] for row in candidates}),
               "publication_years": years,
               "earliest_publication": min((row["publication_date"] for row in candidates), default=None),
               "latest_publication": max((row["publication_date"] for row in candidates), default=None),
               "historical_texts_reconstructed": 0, "effective_dates_resolved": 0,
               "archive_bytes_before_publish": archive.size_bytes(),
               "archive_limit_bytes": archive.max_bytes,
               "http_requests_this_run": http.requests,
               "http_bytes_this_run": http.downloaded_bytes,
               "capture_this_run": stats, "stopped": stopped,
               "selection_conflicts": selection_conflicts,
               "selected_documents_conflicted": len(selection_conflicts),
               "quarantined_captured_documents": len(state["documents"]) - len(documents),
               "failed_documents": state["failures"],
               "selected_documents_pending": sum(
                   row["document_id"] not in state["documents"] and
                   row["document_id"] not in state["failures"] for row in selected),
               "selected_documents_failed": sum(
                   row["document_id"] in state["failures"] for row in selected),
               "selected_publication_year_counts": {
                   year: sum(row["publication_date"].startswith(year) for row in selected)
                   for year in sorted({row["publication_date"][:4] for row in selected})},
               "captured_publication_year_counts": {
                   year: sum(row["publication_date"].startswith(year) for row in documents)
                   for year in sorted({row["publication_date"][:4] for row in documents})},
               "discovery_checkpoints": {scope: {
                   "pages": len(progress["pages"]), "complete": progress["complete"]}
                   for scope, progress in state["discovery"].items()},
               "coverage_complete": False,
               "coverage_note": "Exact DIP descriptors plus final BGBl article matching; "
                                "unmatched, undownloaded, scanned and unresolved records remain gaps."}
    out = Path(snapshot_root) / datetime.now(timezone.utc).date().isoformat()
    payloads = {"documents.jsonl": b"".join(_json_bytes(row) for row in documents),
                "candidates.jsonl": b"".join(_json_bytes(row) for row in candidates),
                "summary.json": _json_bytes(summary)}
    archive.reserve(sum(len(payload) for payload in payloads.values()))
    out.mkdir(parents=True, exist_ok=True)
    for name, payload in payloads.items():
        fd, temporary = tempfile.mkstemp(prefix=".writing-", dir=out)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, out / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2006-01-01")
    parser.add_argument("--end", default="2022-12-31")
    parser.add_argument("--wahlperiode", type=int, action="append")
    parser.add_argument("--limit", type=int, default=100,
                        help="maximum previously uncaptured documents attempted this run")
    parser.add_argument("--max-requests", type=int, default=350)
    parser.add_argument("--max-download-mib", type=float, default=100)
    parser.add_argument("--max-archive-mib", type=float, default=400)
    parser.add_argument("--min-free-mib", type=float, default=128)
    parser.add_argument("--delay", type=float, default=0.8)
    parser.add_argument("--retry-failures", action="store_true")
    parser.add_argument("--inventory-only", action="store_true")
    parser.add_argument("--rebuild", action="store_true", help="offline verification and rebuild")
    parser.add_argument("--capture-only", action="store_true",
                        help="VPS mode: discover and capture new sources, keep the "
                             "checkpoint, but do not publish candidates (the "
                             "workstation rebuilds and attests them)")
    parser.add_argument("--archive-root", type=Path, default=ARCHIVE_ROOT,
                        help="legacy source store (workstation: the pulled archive)")
    parser.add_argument("--snapshot-root", type=Path, default=SNAPSHOT_ROOT,
                        help="where the rebuilt candidate snapshot is written")
    args = parser.parse_args()
    if args.capture_only and (args.rebuild or args.inventory_only):
        parser.error("--capture-only cannot be combined with --rebuild/--inventory-only")
    try:
        if not "1949-01-01" <= _day(args.start) <= _day(args.end) < "2023-01-01":
            raise ValueError("legacy dates must be within 1949-2022")
        if args.limit < 1 or args.delay < 0.2 or args.max_download_mib <= 0:
            raise ValueError("positive limits and --delay >= 0.2 are required")
        periods = args.wahlperiode or list(range(16, 21))
        if any(not 1 <= wp <= 20 for wp in periods):
            raise ValueError("legacy electoral periods must be between 1 and 20")
        # --max-archive-mib 0: the uncapped workstation archive.
        archive = LegacyArchive(args.archive_root,
                                max_bytes=(None if args.max_archive_mib == 0
                                           else int(args.max_archive_mib * MIB)),
                                min_free_bytes=int(args.min_free_mib * MIB),
                                accounting_roots=(args.snapshot_root,))
        http = BoundedHttp(max_requests=args.max_requests,
                           max_bytes=int(args.max_download_mib * MIB), delay=args.delay)
    except ValueError as exc:
        parser.error(str(exc))
    state = load_checkpoint(archive)
    if not args.rebuild and not args.inventory_only:
        try:
            check_pdf_dependencies()
        except DependencyError as exc:
            parser.error(str(exc))
    procedures, selected, stats, stopped = [], [], {}, None
    selection_conflicts = {}
    try:
        if not args.rebuild:
            spec = http.get_bytes(f"{DIP_BASE}/openapi.yaml", max_bytes=MIB).decode("utf-8")
            match = re.search(r"apikey=([A-Za-z0-9._\-]+)", spec)
            key = match.group(1) if match else PUBLIC_KEY
            procedures = discover_procedures(http, key, archive, state,
                start=args.start, end=args.end, wahlperioden=periods)
            selected = select_legacy_documents(procedures, load_curated_acts(),
                                                args.start, args.end,
                                                conflicts=selection_conflicts)
            print(f"Selected {len(selected)} official pre-2023 documents", flush=True)
            if not args.inventory_only:
                _, stats = capture_selected(selected, state, archive, http,
                                             limit=args.limit, retry_failures=args.retry_failures,
                                             capture_only=args.capture_only)
    except BudgetExceeded as exc:
        stopped = str(exc)
        print(f"Checkpoint retained: {stopped}", flush=True)
    if args.rebuild or not procedures:
        procedures = cached_procedures(archive, state, start=args.start,
                                       end=args.end, wahlperioden=periods)
        selected = select_legacy_documents(procedures, load_curated_acts(),
                                            args.start, args.end,
                                            conflicts=selection_conflicts)
    if args.capture_only:
        print(json.dumps({"mode": "capture_only", "capture_this_run": stats,
                          "stopped": stopped,
                          "captured_documents": len(state["documents"]),
                          "archive_bytes": archive.size_bytes(),
                          "publication": "workstation rebuild + attestation"},
                         ensure_ascii=False, sort_keys=True))
        return 0
    summary = _publish(archive, state, start=args.start, end=args.end,
                       selected=selected, procedures=procedures, http=http,
                       stats=stats, stopped=stopped,
                       selection_conflicts=selection_conflicts,
                       snapshot_root=args.snapshot_root)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
