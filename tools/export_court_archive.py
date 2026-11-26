"""Export a verified public passage database as Parquet and independently readable gzip members.

Run with an empty output directory. Each compressed member contains one court
document. The catalogue records its offset and digest for a bounded HTTP Range
read. An immutable Hub revision identifies the catalogue and its packs together.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sqlite3
import tempfile
from collections import defaultdict
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encoded(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def add_frame(stream, data):
    offset = stream.tell()
    frame = gzip.compress(data, compresslevel=6, mtime=0)
    stream.write(frame)
    return {
        "offset": offset,
        "length": len(frame),
        "compressed_sha256": digest(frame),
        "bytes": len(data),
        "sha256": digest(data),
    }


def passage_location(passage):
    if passage.get("source_locator"):
        return passage["locator_type"], passage["source_locator"]
    if passage.get("xml_path"):
        return "xpath", passage["xml_path"]
    if passage.get("source_anchor"):
        return "source_anchor", passage["source_anchor"]
    return None, None


def build(web: Path, output: Path) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    web = web.resolve()
    policy = json.loads((web / "data_policy.json").read_bytes())
    if (
        policy.get("public_build") is not True
        or policy.get("includes_quarantined_sources") is not False
    ):
        raise ValueError(
            "court archive requires a public generation with reviewed sources"
        )
    manifest = json.loads((web / "decision_passages.json").read_bytes())
    db_path = web / "decision_passages.sqlite"
    with db_path.open("rb") as f:
        database_hash = hashlib.file_digest(f, "sha256").hexdigest()
    if (
        manifest.get("profile") != "official-passages/2"
        or manifest.get("database_sha256") != database_hash
    ):
        raise ValueError("the public court database does not match its source manifest")
    source = sqlite3.connect(db_path.as_uri() + "?mode=ro&immutable=1", uri=True)
    source.row_factory = sqlite3.Row
    source.execute("begin")
    catalog, partitions = [], defaultdict(list)
    for row in source.execute(
        "select id,metadata from decisions where status='indexed' order by id"
    ):
        meta = json.loads(row["metadata"])
        name = re.sub(r"[^a-z0-9]+", "-", meta["court_short"].casefold()).strip("-")
        if not name or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta["date"]):
            raise ValueError(f"invalid court or decision date in {row['id']}")
        partitions[name, meta["date"][:4]].append(row["id"])
    if sum(map(len, partitions.values())) != manifest["indexed_decisions"]:
        raise ValueError("decision count differs from the public release")
    passage_count = mention_count = 0
    passage_schema = pa.schema(
        [
            (n, pa.string())
            for n in (
                "decision_id",
                "passage_id",
                "court",
                "date",
                "section",
                "source_role",
                "published_label",
                "locator_type",
                "source_locator",
                "text",
                "text_sha256",
            )
        ]
        + [("position", pa.int64())]
    )
    decision_schema = pa.schema(
        [
            (n, pa.string())
            for n in (
                "decision_id",
                "court",
                "date",
                "docket",
                "document_number",
                "metadata_source",
                "ecli",
                "title",
                "source_url",
                "source_media_type",
                "source_sha256",
            )
        ]
        + [("passages", pa.int64())]
    )
    receipts = {}
    for (court, year), ids in sorted(partitions.items()):
        prefix = f"{court}/{year}"
        pack_path = f"packs/{prefix}.gz"
        target = output / pack_path
        target.parent.mkdir(parents=True, exist_ok=True)
        passages, decisions = [], []
        with target.open("xb") as stream:
            for key in ids:
                row = source.execute(
                    "select * from decisions where id=?", (key,)
                ).fetchone()
                meta, descriptor = (
                    json.loads(row["metadata"]),
                    json.loads(row["source_data"]),
                )
                original = row["source_zip"]
                source_sha = digest(original)
                if (
                    row["content_sha256"] != source_sha
                    or descriptor["content_sha256"] != source_sha
                ):
                    raise ValueError(f"original checksum differs for {key}")
                documents = []
                for entry in source.execute(
                    "select passage_id,text,data from passages where decision_id=?",
                    (key,),
                ):
                    p = json.loads(entry["data"])
                    if (
                        p["decision_id"] != key
                        or entry["passage_id"] != p["passage_id"]
                        or p["text"] != entry["text"]
                        or digest(p["text"].encode()) != p["text_sha256"]
                    ):
                        raise ValueError(
                            f"passage verification failed for {key} {entry['passage_id']}"
                        )
                    documents.append(p)
                documents.sort(key=lambda p: (p["document_position"], p["passage_id"]))
                for p in documents:
                    locator_type, locator = passage_location(p)
                    passages.append(
                        dict(
                            decision_id=key,
                            passage_id=p["passage_id"],
                            court=meta["court_short"],
                            date=meta["date"],
                            section=p["section"],
                            source_role=p.get("source_role"),
                            published_label=p.get("paragraph_label"),
                            source_locator=locator,
                            locator_type=locator_type,
                            text=p["text"],
                            text_sha256=p["text_sha256"],
                            position=p["document_position"],
                        )
                    )
                citations = [
                    json.loads(r[0])
                    for r in source.execute(
                        "select data from citations where source_id=? order by passage_id,position,target_id",
                        (key,),
                    )
                ]
                result = dict(
                    metadata=meta,
                    ecli=row["ecli"],
                    source=descriptor,
                    passages=documents,
                    citations=citations,
                )
                index = dict(
                    id=key,
                    court=meta["court_short"],
                    date=meta["date"],
                    docket=meta["az"],
                    document_number=meta.get("document_number"),
                    ecli=row["ecli"],
                    pack=pack_path,
                    document=add_frame(stream, encoded(result)),
                    original=add_frame(stream, original),
                )
                catalog.append(index)
                decisions.append(
                    dict(
                        decision_id=key,
                        court=meta["court_short"],
                        date=meta["date"],
                        docket=meta["az"],
                        document_number=meta.get("document_number"),
                        metadata_source=meta.get("source"),
                        ecli=row["ecli"],
                        title=meta["title"],
                        source_url=descriptor["url"],
                        source_media_type=descriptor["media_type"],
                        source_sha256=source_sha,
                        passages=len(documents),
                    )
                )
                passage_count += len(documents)
                mention_count += len(citations)
        for group, rows, schema in [
            ("decisions", decisions, decision_schema),
            ("passages", passages, passage_schema),
        ]:
            path = output / group / (prefix + ".parquet")
            path.parent.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_pylist(rows, schema=schema)
            pq.write_table(
                table, path, compression="zstd", use_dictionary=True, row_group_size=512
            )
            if not pq.read_table(path).equals(table):
                raise ValueError(f"Parquet round trip failed for {path.name}")
    source.close()
    with db_path.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != database_hash:
            raise ValueError("court database changed during export")
    if (passage_count, mention_count) != (
        manifest["passages"],
        manifest["citation_mentions"],
    ):
        raise ValueError("archive counts differ from the verified passage release")
    info = {
        "schema": "kerf-court-archive/1",
        "public_database_sha256": database_hash,
        "decisions": len(catalog),
        "passages": passage_count,
        "citation_mentions": mention_count,
    }
    (output / "catalog.json.gz").write_bytes(
        gzip.compress(
            encoded(dict(**info, records=sorted(catalog, key=lambda v: v["id"]))),
            mtime=0,
        )
    )
    for path in sorted(output.rglob("*")):
        if path.is_file():
            with path.open("rb") as f:
                receipts[path.relative_to(output).as_posix()] = {
                    "size": path.stat().st_size,
                    "sha256": hashlib.file_digest(f, "sha256").hexdigest(),
                }
    (output / "manifest.json").write_bytes(
        encoded(dict(**info, files=receipts)) + b"\n"
    )
    return info


def add_to_card(card: str, directory: Path) -> str:
    """Describe the independently generated court mirror in a Hub data card."""
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if manifest.get("schema") != "kerf-court-archive/1":
        raise ValueError("unrecognised court archive")
    for name, entry in manifest["files"].items():
        path = directory / name
        if not path.resolve().is_relative_to(directory.resolve()):
            raise ValueError("court archive file outside its directory")
        with path.open("rb") as stream:
            if (
                path.stat().st_size != entry["size"]
                or hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]
            ):
                raise ValueError(f"court archive integrity check failed for {name}")
    configs = "".join(
        f"  - config_name: court_{group}\n    data_files: court_archive/{group}/*/*.parquet\n"
        for group in ("decisions", "passages")
    )
    marker = "\n---\n"
    head, marker, body = card.partition(marker)
    if not marker or "## Reuse" not in body:
        raise ValueError("missing dataset card header or rights section")
    head = head.removesuffix("\n" + configs.rstrip("\n"))
    previous_title = "\n## Court decisions and original files\n"
    if previous_title in body:
        left, _, previous = body.partition(previous_title)
        _, heading_marker, rest = previous.partition("\n## ")
        if not heading_marker:
            raise ValueError("missing section after court archive")
        body = left + heading_marker + rest
    description = (
        "## Court decisions and original files\n\n"
        f"The court archive mirrors {manifest['decisions']:,} decisions and {manifest['passages']:,} passages "
        "from the public Kerf reader. The two Parquet tables are grouped by court and year. "
        "The passage table records a Randnummer when the source prints one. The position field follows the order in that edition.\n\n"
        "Pin a dataset revision and look up the decision ID in the catalogue. The reader fetches one decision "
        "or its original file in a single byte-range request and checks both SHA-256 digests. "
        "[Archive manifest](./court_archive/manifest.json) · "
        "[Download tool](https://github.com/SNTIQ-Team/kerf/blob/main/tools/read_court_archive.py)\n\n"
    )
    return (
        head
        + "\n"
        + configs.rstrip("\n")
        + marker
        + body.replace("## Reuse", description + "## Reuse", 1)
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("web_dir", type=Path)
    ap.add_argument("output", type=Path)
    args = ap.parse_args()
    if args.output.exists():
        ap.error("choose a new directory for this immutable export")
    args.output.parent.mkdir(exist_ok=True, parents=True)
    with tempfile.TemporaryDirectory(dir=args.output.parent) as temp:
        staging = Path(temp) / "archive"
        staging.mkdir()
        info = build(args.web_dir, staging)
        staging.rename(args.output)
    print(json.dumps(info))


if __name__ == "__main__":
    main()
