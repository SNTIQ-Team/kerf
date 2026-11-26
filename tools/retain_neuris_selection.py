"""Export explicitly selected, hash-pinned NeuRIS judgments from the local Kerf reader.

Run from the project root with python -m tools.retain_neuris_selection.
The selection is a JSON object mapping official document numbers to their XML SHA-256.
The output contains the normal official_decisions cache and a decisions.jsonl snapshot.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

from courts import read, store
from tools.neuris_passages import PROFILE, add_selected, extract, source_url


def encode(row):
    return (json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def export(corpus, selection, destination):
    if not selection or not isinstance(selection, dict):
        raise ValueError("supply an explicit selection of document numbers and SHA-256 hashes")
    database = (corpus / "courts.sqlite").resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError(destination)
    with tempfile.TemporaryDirectory(prefix=".writing-neuris-", dir=destination.parent.parent) as temporary:
        root = Path(temporary) / "release"
        cache = root / "official_decisions"
        cache.mkdir(parents=True)
        selected = []
        conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            with (root / "decisions.jsonl").open("wb") as output:
                for number, expected_sha in sorted(selection.items()):
                    result = read.get_decision(conn, "neuris:" + number, with_relations=False)
                    if result["status"] != "ok":
                        raise ValueError(f"missing or ambiguous publication {number}")
                    d = result["decision"]
                    if (d["source_id"], d["source_doc_id"], d["content_hash"]) != ("neuris", number, expected_sha):
                        raise ValueError(f"selection differs from retained source for {number}")
                    if d["metadata"].get("exhibits"):
                        raise ValueError(f"{number} requires additional source files")
                    blob = store.read_capture(conn, expected_sha, d["capture_path"], home=corpus)
                    if hashlib.sha256(blob).hexdigest() != expected_sha:
                        raise ValueError(f"capture checksum mismatch for {number}")
                    row = {"id": "neuris-" + number.lower(), "document_number": number, "court": d["court_name"],
                           "court_short": d["court_type"], "level": d["court_type"], "date": d["decision_date"],
                           "az": ", ".join(d["case_numbers"]), "case_numbers": d["case_numbers"], "ecli": d["ecli"],
                           "kind": d["decision_type"], "juris": "DE", "title": d["title"], "source": "NeuRIS",
                           "url": d["source_url"]}
                    if source_url(row) != row["url"]:
                        raise ValueError(f"retained URI differs from the official number {number}")
                    passages, _ = extract(blob, row)
                    published = [(p["paragraph_label"], p["text"]) for p in passages if p["paragraph_label"]]
                    native = [(p["rn"], p["text"]) for p in d["paragraphs"] if p["rn"]]
                    if published != native:
                        raise ValueError(f"source and local reader disagree about Rn. in {number}")
                    receipt = {"decision_id": row["id"], "profile": PROFILE, "url": row["url"],
                               "sha256": expected_sha, "media_type": "application/xml", "observed_at": d["observed_at"]}
                    (cache / (expected_sha + ".source")).write_bytes(blob)
                    (cache / (row["id"] + ".json")).write_bytes(encode(receipt))
                    selected.append(row)
                    output.write(encode(row))
            add_selected([], selected)
        finally:
            conn.close()
        (root / "selection.json").write_bytes(encode(selection))
        os.rename(root, destination)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--destination", required=True, type=Path)
    args = parser.parse_args()
    export(args.corpus, json.loads(args.selection.read_bytes()), args.destination)


if __name__ == "__main__":
    main()
