"""Read a decision, passage, or original from the public Kerf RII collection."""
from __future__ import annotations

import argparse
from http.client import HTTPException
import json
import os
from pathlib import Path
import sys
import zipfile
import zlib

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.rii_archive import EcliIndexUnavailable, MissingRecord, canonical_ecli, open_archive, passage_for, select_passages


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--ecli", help="look up all documents matching an exact official ECLI")
    target.add_argument("document", nargs="?", help="official RII document number, with optional jb- prefix")
    parser.add_argument("--revision", default=os.environ.get("KERF_RII_REVISION", ""),
                        help="immutable Hugging Face dataset commit")
    parser.add_argument("--manifest-sha256", default=os.environ.get("KERF_RII_MANIFEST_SHA256", ""),
                        help="SHA-256 of rii/manifest.json at that commit")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--anchor", help="exact published passage anchor")
    modes.add_argument("--source", action="store_true", help="write the original ZIP or XML bytes to stdout")
    modes.add_argument("--passages", action="store_true", help="list decision passages")
    parser.add_argument("--rn", help="match this printed paragraph number")
    parser.add_argument("--query", help="match text within the decision")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    if (args.rn is not None or args.query is not None or args.limit != 50 or args.offset) and not args.passages:
        parser.error("filters and pagination require --passages")
    if not 1 <= args.limit <= 250 or args.offset < 0:
        parser.error("limit must be 1–250 and offset must be nonnegative")
    if args.ecli is not None:
        if args.source or args.passages or args.anchor is not None:
            parser.error("passage and source options require a document number")
        try:
            identifier = canonical_ecli(args.ecli)
        except ValueError as exc:
            parser.error(str(exc))
    try:
        archive = open_archive(args.revision, args.manifest_sha256)
        if args.ecli is not None:
            result = archive.decisions_by_ecli(identifier)
            print(json.dumps({"revision": archive.revision, **result}, ensure_ascii=False))
            return 0
        if args.source:
            sys.stdout.buffer.write(archive.source(args.document)[0])
            return 0
        result = archive.decision(args.document)
        if args.anchor is not None:
            response = {"passage": passage_for(result, args.anchor)}
        elif args.passages:
            response = select_passages(result, args.rn, args.query, args.offset, args.limit)
        else:
            response = {"metadata": result["metadata"]}
        print(json.dumps({"revision": archive.revision, **response}, ensure_ascii=False))
    except EcliIndexUnavailable:
        print("The pinned RII release has no ECLI index", file=sys.stderr)
        return 2
    except MissingRecord as exc:
        print(f"No such document or passage in the pinned RII release: {exc}", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, AttributeError, HTTPException, zipfile.BadZipFile, zlib.error) as exc:
        print(f"Could not verify the pinned RII release: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
