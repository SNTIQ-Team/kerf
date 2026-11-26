"""Describe a locally verified federal case-law edition in the Kerf Hub card."""
import hashlib
import json
from datetime import datetime
from pathlib import Path


def add_to_card(card: str, directory: Path) -> str:
    manifest = json.loads((directory / "manifest.json").read_bytes())
    if manifest["format"] != "kerf-rii-publications/1":
        raise ValueError("unrecognised RII archive format")
    for name, entry in manifest["files"].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("RII file outside the archive")
        with path.open("rb") as stream:
            if (path.stat().st_size != entry["size"]
                    or hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]):
                raise ValueError(f"RII archive integrity check failed for {name}")
    head, sep, body = card.partition("\n---\n")
    if not sep or "\n## Reuse\n" not in body:
        raise ValueError("missing dataset front matter or reuse section")
    configs = "".join(
        f"\n  - config_name: rii_{name}\n    data_files: rii/{name}/*/*.parquet"
        for name in ("decisions", "paragraphs")
    )
    head = head.replace(configs, "") + configs
    heading = "## German federal court decisions"
    if "\n" + heading + "\n" in body:
        start, _, previous = body.partition("\n" + heading + "\n")
        _, section, following = previous.partition("\n## ")
        if not section:
            raise ValueError("missing section after the RII archive")
        body = start + section + following
    count = manifest["counts"]
    day = datetime.fromisoformat(manifest["source_catalogue"]["observed"]).strftime("%d.%m.%Y")
    section = (
        f"{heading}\n\n"
        f"On {day}, the federal catalogue listed {manifest['source_catalogue_count']:,} decisions. "
        f"This edition contains {count['documents']:,} of them and {count['paragraphs']:,} passages. "
        "Each decision carries the publisher’s document number and modification date. "
        "[Federal catalogue](./rii/catalogue.xml.gz) · [Edition manifest](./rii/manifest.json)\n\n"
        "The Parquet tables are grouped by court and year. Each passage records its position, "
        "the Randnummer where one is printed, and the XML paths of the contributing blocks. "
        "The archive preserves the original ZIP for single-file decisions. For decisions with attachments, "
        "it stores the original XML and links to the publisher’s complete ZIP with its SHA-256 digest. "
        "A document number and a pinned Hub revision are sufficient to retrieve one decision. "
        "[Reader](https://github.com/SNTIQ-Team/kerf/blob/main/tools/read_rii_archive.py)\n\n"
        "The Federal Office of Justice permits reuse of its published decisions in all formats it offers. "
        "[Publisher’s notice](https://www.rechtsprechung-im-internet.de/) · "
        "[GovData record](https://ckan.govdata.de/dataset/5c69f5a3-0707-4c7e-b062-bfc42c187f67)\n"
    )
    return head + sep + body.replace("\n## Reuse\n", "\n" + section + "\n## Reuse\n", 1)
