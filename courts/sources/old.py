"""Open Legal Data (de.openlegaldata.io): an aggregator's copies of Länder decisions (gap filling).

Policy check 2026-09-28:

* Licence: the collection and metadata are ODbL v1.0; the decisions are amtliche Werke (§ 5 UrhG).
* The API (``/api/cases/``) is open but caps every listing at 10 pages and names the bulk channel itself:
  "For bulk access use https://huggingface.co/openlegaldata/datasets".  So the API is not used in bulk.
* The bulk channel is the Hugging Face dataset ``openlegaldata/court-decisions-germany`` (config
  ``dump-20260520``: 423,941 decisions, 54 parquet shards, 10.7 GB).  It is *gated* (auto-approval): the
  operator's HF account must request access on the dataset page; the token comes from ``HF_TOKEN``.  Without
  access the listing records a typed ``login`` failure (the gate is never worked around).
* robots.txt of de.openlegaldata.io disallows ``/api`` for every agent (generic; the operator's robots decision
  applies) and names no AI agents; huggingface.co allows all.

Listing (``sync --source old [--court by,ni,…]``): shard by shard - download (space guard first), read the
rows with pyarrow in batches, keep the rows of the wanted Länder that the corpus does not have yet (ECLI;
court + date + Az of a stored decision or of a primary source's listed item), append each kept row
zstd-compressed to ``<data home>/staging/old/rows-<shard>.zst``, delete the shard.  Fetch reads the row
back (no network).  Default Länder: Bayern and Niedersachsen first (the corpus has no primary source for
them), then every other Land; NRW, federal and EU rows are left to the primary sources.

Parse: the row's ``content`` is the original portal HTML: juris documents (``dl.RspDL``, the Länder portals) go
through ``courts.sources.juris.parse_body``; BAYERN.RECHT copies mark Randnummern as ``<rd nr="N"/>``;
VORIS copies (Niedersachsen) are ``div.tenor/.tatbestand/.gruende`` of ``<p>`` (no Randnummern).  Court,
date, Az, type and ECLI come from the row.  Every decision is a secondary copy (``source_kind:
aggregator``): any primary copy replaces it; ``decisions.metadata.aggregator`` records the OLD id, slug,
dump and shard.
"""
from __future__ import annotations

import io
import json
import os
import re
import sqlite3
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from ..casenumbers import case_key, split_case_numbers
from ..config import USER_AGENT, data_home, db_path
from ..courtids import priority, resolve as resolve_court
from ..errors import UpstreamFailure
from ..hostgov import Concurrency
from ..http import PoliteHttp
from ..model import Block, ParsedDecision, Section
from .base import Adapter, Capture, Listing, squash
from .htmlblocks import checked_ecli, inline_text, is_outline_marker

SOURCE_ID = "old"
DATASET = "openlegaldata/court-decisions-germany"
DUMP = "dump-20260520"
HF = "https://huggingface.co"
TREE_URL = f"{HF}/api/datasets/{DATASET}/tree/main/{DUMP}"
FILE_URL = f"{HF}/datasets/{DATASET}/resolve/main/{{path}}"
CASE_URL = "https://de.openlegaldata.io/case/{slug}"
STATES = {3: "bw", 4: "by", 5: "be", 6: "bb", 7: "hb", 8: "hh", 9: "he", 10: "mv", 11: "ni", 12: "nw", 13: "rp",
          14: "sl", 15: "sn", 16: "st", 17: "sh", 18: "th", 2: "de", 19: "eu", 1: None}
DEFAULT_LANDS = ("by", "ni", "bw", "be", "bb", "hb", "hh", "he", "mv", "rp", "sl", "sn", "st", "sh", "th")
COLUMNS = ["id", "slug", "court", "file_number", "date", "type", "ecli", "content"]
SHARD_MAX_BYTES = 600 * 1024 ** 2
HEADINGS = {"tenor": "tenor", "tatbestand": "tatbestand", "gründe": "gruende", "gruende": "gruende",
            "entscheidungsgründe": "entscheidungsgruende", "entscheidungsgruende": "entscheidungsgruende",
            "leitsatz": "leitsatz", "leitsätze": "leitsatz"}


def token() -> str | None:
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")


def staging(home: Path | None = None) -> Path:
    path = Path(home or data_home()) / "staging" / SOURCE_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- listing ------------------------------------------------------------------------------------------

def covered_keys(conn: sqlite3.Connection) -> tuple[set, set]:
    """(court, date, case key) and ECLIs the corpus has: stored decisions of any source, and items that a
    primary source has listed (the running backfill will store them)."""
    keys: set[tuple[str, str, str]] = set()
    eclis: set[str] = set()
    for slug, day, key in conn.execute("SELECT d.court_slug, d.decision_date, c.case_key FROM decisions d "
                                       "JOIN case_numbers c ON c.decision_id = d.id"):
        keys.add((slug, day, key))
    for (ecli,) in conn.execute("SELECT ecli FROM decisions WHERE ecli IS NOT NULL"):
        eclis.add(ecli.upper())
    for court, day, raw in conn.execute(
            "SELECT listed_court, listed_date, listed_case_numbers FROM source_items WHERE state <> 'failed' "
            "AND source_id NOT IN ('old', 'wayback') AND listed_court IS NOT NULL AND listed_date IS NOT NULL"):
        for number in split_case_numbers(raw or ""):
            keys.add((court, day, case_key(number)))
    return keys, eclis


def row_identity(row: dict) -> tuple[str | None, str | None, str | None, list[str]]:
    """(land, court slug, ISO date, case numbers) of an OLD row."""
    court = row.get("court") or {}
    land = STATES.get(court.get("state"))
    info = resolve_court(court.get("name") or "", default_land=land if land not in (None, "eu") else None)
    return land, info.court_id, row.get("date"), split_case_numbers(row.get("file_number") or "")


def wanted_row(row: dict, lands: set[str], keys: set, eclis: set) -> str | None:
    """None when the row should be fetched, else why not."""
    land, slug, day, numbers = row_identity(row)
    if land not in lands:
        return "land"
    if not row.get("content") or not day or not numbers:
        return "incomplete"
    if (row.get("ecli") or "").upper() in eclis:
        return "ecli"
    if any((slug, day, case_key(n)) in keys for n in numbers):
        return "court_date_az"
    return None


def _download(url: str, path: Path, tok: str) -> None:
    import requests
    with requests.get(url, headers={"User-Agent": USER_AGENT, "Authorization": f"Bearer {tok}"}, stream=True,
                      timeout=300) as response:
        if response.status_code in (401, 403):
            raise UpstreamFailure("login", f"{DATASET} is gated: the operator's Hugging Face account must "
                                  f"request access ({response.headers.get('x-error-code', response.status_code)})",
                                  url=url, status=response.status_code)
        if response.status_code != 200:
            raise UpstreamFailure("http_error", f"HTTP {response.status_code}", url=url, status=response.status_code)
        tmp = path.with_name(path.name + ".part")
        with open(tmp, "wb") as handle:
            for chunk in response.iter_content(1 << 20):
                handle.write(chunk)
        os.replace(tmp, path)


def shard_rows(path: Path):
    import pyarrow.parquet as pq
    parquet = pq.ParquetFile(path)
    names = [c for c in COLUMNS if c in parquet.schema_arrow.names]
    for batch in parquet.iter_batches(batch_size=500, columns=names):
        yield from batch.to_pylist()


def stage_rows(rows, out: Path, lands: set[str], keys: set, eclis: set, shard: str) -> tuple[list[dict], dict]:
    """Append the wanted rows (zstd frames) to ``out``; return their items and the skip reasons."""
    import zstandard
    compressor = zstandard.ZstdCompressor(level=10)
    items, skipped = [], {}
    with open(out, "ab") as handle:
        for row in rows:
            why = wanted_row(row, lands, keys, eclis)
            if why:
                skipped[why] = skipped.get(why, 0) + 1
                continue
            frame = compressor.compress(json.dumps(row, ensure_ascii=False).encode())
            offset = handle.tell()
            handle.write(frame)
            land, slug, day, numbers = row_identity(row)
            for n in numbers:          # a second copy of the same decision in the dump is not fetched twice
                keys.add((slug, day, case_key(n)))
            items.append({"source_doc_id": str(row["id"]), "url": CASE_URL.format(slug=row.get("slug") or row["id"]),
                          "listed_date": day, "listed_court": slug, "listed_case_numbers": row.get("file_number"),
                          "listed_modified": None, "priority": priority(resolve_court(
                              (row.get("court") or {}).get("name") or "", default_land=land).jurisdiction),
                          "extra": {"file": out.name, "offset": offset, "length": len(frame), "shard": shard,
                                    "land": land, "slug": row.get("slug"), "ecli": row.get("ecli")}})
    return items, skipped


# --- parsing --------------------------------------------------------------------------------------------

def _p_text(el: Tag) -> str:
    return inline_text([el]).replace("\n", " ").strip() if el.name == "p" else inline_text([el])


def parse_rd_markup(content: str) -> list[Section]:
    """BAYERN.RECHT copies: ``<h2>`` headings and ``<p><rd nr="N"/>text</p>`` paragraphs."""
    soup = BeautifulSoup(content, "html.parser")
    sections: list[Section] = []
    current: Section | None = None
    for el in soup.find_all(["h2", "h3", "p", "li"]):
        if el.name in ("h2", "h3"):
            heading = squash(el.get_text(" "))
            name = HEADINGS.get(heading.rstrip(": ").lower(), "sonstiger_langtext")
            current = Section(name, "court", f"Open Legal Data copy of BAYERN.RECHT: heading '{heading}'",
                              heading=heading)
            sections.append(current)
            continue
        if el.name == "li" and el.find("p"):
            continue
        rd = el.find("rd")
        label = rd.get("nr") if rd is not None else None
        if rd is not None:
            rd.extract()
        text = inline_text(list(el.children))
        if not text.strip():
            continue
        if current is None:
            current = Section("sonstiger_langtext", "court", "Open Legal Data copy: text before the first heading")
            sections.append(current)
        kind = "heading" if not label and is_outline_marker(text) else "text"
        current.blocks.append(Block(text=text, label=label or None, kind=kind, locator=f"p[{len(current.blocks) + 1}]"))
    return [s for s in sections if s.blocks]


def parse_voris_markup(content: str) -> list[Section]:
    """VORIS copies (Niedersachsen): ``div.tenor/.tatbestand/.gruende/.entscheidungsgruende`` of ``<p>``."""
    soup = BeautifulSoup(content, "html.parser")
    sections: list[Section] = []
    for div in soup.find_all("div", class_=re.compile(r"^(tenor|tatbestand|gruende|entscheidungsgruende|leitsatz)$")):
        name = HEADINGS.get((div.get("class") or [""])[0], "sonstiger_langtext")
        heading = div.find(["h2", "h3"])
        section = Section(name, "court", f"Open Legal Data copy of VORIS: div.{name}",
                          heading=squash(heading.get_text(" ")) if heading else None)
        for p in div.find_all(["p", "li"]):
            if p.name == "li" and p.find("p"):
                continue
            text = inline_text(list(p.children))
            if text.strip():
                section.blocks.append(Block(text=text, kind="heading" if is_outline_marker(text) else "text",
                                            locator=f"div.{name}/p[{len(section.blocks) + 1}]"))
        if section.blocks:
            sections.append(section)
    return sections


def content_origin(content: str) -> str:
    if "RspDL" in content or "docLayoutText" in content:
        return "juris"
    if re.search(r"<rd\s+nr=", content):
        return "bayern_recht"
    if re.search(r'<div class="(tenor|gruende|tatbestand)"', content):
        return "voris"
    return "unknown"


def parse_row(row: dict, *, shard: str | None = None) -> ParsedDecision:
    content = row.get("content") or ""
    origin = content_origin(content)
    land, slug, day, numbers = row_identity(row)
    court = row.get("court") or {}
    url = CASE_URL.format(slug=row.get("slug") or row.get("id"))
    procedural: list[dict] = []
    metadata: dict = {"source_kind": "aggregator", "content_origin": origin,
                      "aggregator": {"service": "Open Legal Data", "old_id": row.get("id"), "slug": row.get("slug"),
                                     "dataset": DATASET, "dump": DUMP, "shard": shard, "url": url,
                                     "licence": "ODbL v1.0 (collection); decisions § 5 UrhG"}}
    if origin == "juris":
        from .juris import parse_body, parse_verfahrensgang
        body = parse_body(content, str(row.get("id")))
        sections = body["sections"]
        procedural = parse_verfahrensgang("\n".join(body["procedural_text"]))
        if body["apparatus"]:
            metadata["apparatus"] = body["apparatus"]
    elif origin == "bayern_recht":
        sections = parse_rd_markup(content)
        metadata["numbering_source"] = "bayern_recht"
    elif origin == "voris":
        sections = parse_voris_markup(content)
    else:
        raise UpstreamFailure("unexpected_representation", f"OLD row {row.get('id')}: content of unknown origin",
                              url=url)
    if not day or not numbers:
        raise UpstreamFailure("parse_error", f"OLD row {row.get('id')}: no date or Aktenzeichen", url=url)
    info = resolve_court(court.get("name") or "", default_land=land if land not in (None, "eu") else None)
    if info.court_type == "unknown":
        raise UpstreamFailure("parse_error", f"OLD row {row.get('id')}: court {court.get('name')!r} not recognised",
                              url=url)
    return ParsedDecision(
        source_id=SOURCE_ID, source_doc_id=str(row.get("id")), source_url=url, court_name=court.get("name"),
        decision_date=day, case_numbers_raw=row.get("file_number"), case_numbers=numbers,
        decision_type=row.get("type") or None, sections=sections,
        ecli=checked_ecli(row.get("ecli"), day, metadata) if row.get("ecli") else None, court_id=info.court_id,
        court_type=info.court_type, jurisdiction=info.jurisdiction if info.jurisdiction != "unknown" else None,
        land=info.land or land, metadata=metadata, procedural=procedural)


class OpenLegalDataAdapter(Adapter):
    source_id = SOURCE_ID
    label = ("Open Legal Data (de.openlegaldata.io) via its Hugging Face dump: Länder decisions for gap filling "
             "(secondary copies; primary sources win)")
    base_url = HF
    land = None
    adapter_name = "openlegaldata-hf-dump"
    min_interval = 2.0
    concurrency = Concurrency(1, 1)
    robots_policy = "ignore_by_operator_decision"
    policy_decided_at = "2026-09-28"
    policy_basis = ("de.openlegaldata.io robots.txt disallows /api for every agent (generic; no AI agent named) and "
                    "the API caps listings at 10 pages, naming the Hugging Face dump as the bulk channel: only the "
                    "dump is used; it is gated (auto-approval) and read with the operator's HF_TOKEN once the "
                    "account has access; the gate is never worked around")
    source_kind = "aggregator"
    licence = "ODbL v1.0 (Open Legal Data collection and metadata); court decisions § 5 UrhG"

    def list_items(self, http: PoliteHttp, conn, *, since, limit, courts) -> Listing:
        tok = token()
        if not tok:
            raise UpstreamFailure("login", f"{DATASET} is gated: HF_TOKEN of an account with access is needed",
                                  url=f"{HF}/datasets/{DATASET}")
        lands = {c.lower() for c in courts} if courts else set(DEFAULT_LANDS)
        tree = json.loads(http.get(TREE_URL, timeout=120, headers={"Authorization": f"Bearer {tok}"}).body)
        shards = sorted(f["path"] for f in tree if f.get("path", "").endswith(".parquet"))
        if not shards:
            raise UpstreamFailure("unexpected_representation", f"{DATASET}: no parquet shards in {DUMP}", url=TREE_URL)
        reader = sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True, timeout=120)
        try:
            keys, eclis = covered_keys(reader)
        finally:
            reader.close()
        from ..space import check as check_space
        items: list[dict] = []
        skipped: dict[str, int] = {}
        for path in shards:
            name = path.rsplit("/", 1)[-1]
            out = staging() / f"rows-{name.removesuffix('.parquet')}.zst"
            done = out.with_suffix(".done")
            if done.exists():
                items.extend(json.loads(done.read_text(encoding="utf-8")))
                continue
            problems = check_space(data_home(), starting=True).problems
            if problems:
                raise UpstreamFailure("unexpected_representation", "space guard: " + "; ".join(problems))
            local = staging() / name
            _download(FILE_URL.format(path=path), local, tok)
            try:
                out.unlink(missing_ok=True)
                new, why = stage_rows(shard_rows(local), out, lands, keys, eclis, name)
            finally:
                local.unlink(missing_ok=True)
            done.write_text(json.dumps(new, ensure_ascii=False), encoding="utf-8")
            items.extend(new)
            for k, v in why.items():
                skipped[k] = skipped.get(k, 0) + v
            if limit and len(items) >= limit:
                break
        if since:
            items = [i for i in items if (i["listed_date"] or "") >= since]
        return Listing(items=items[:limit] if limit else items, upstream_total=None, complete=not limit)

    def fetch(self, http: PoliteHttp, item) -> Capture:
        extra = item["extra"] if isinstance(item["extra"], dict) else json.loads(item["extra"] or "{}")
        import zstandard
        path = staging() / extra["file"]
        try:
            with open(path, "rb") as handle:
                handle.seek(extra["offset"])
                frame = handle.read(extra["length"])
        except OSError as exc:
            raise UpstreamFailure("unexpected_representation", f"staged OLD row missing: {exc}", url=item["url"])
        body = zstandard.ZstdDecompressor().decompress(frame)
        return Capture(body=body, url=item["url"], media_type="application/json", extension="json",
                       headers={"x-old-shard": extra.get("shard") or ""})

    def parse(self, capture: Capture, item) -> ParsedDecision:
        row = json.loads(capture.body)
        if str(row.get("id")) != str(item["source_doc_id"]):
            raise UpstreamFailure("identity_mismatch", f"staged row {row.get('id')} for item {item['source_doc_id']}",
                                  url=capture.url)
        return parse_row(row, shard=capture.headers.get("x-old-shard") if capture.headers else None)


def adapters() -> dict[str, Adapter]:
    return {SOURCE_ID: OpenLegalDataAdapter()}
