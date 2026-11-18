"""Release-local metadata for the public observations route, separate from passage coverage."""
from datetime import date, datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

FILENAME = "court_observations.json"
PROFILE = "official-court-observation-metadata/1"
MAX_ROWS = 10_000
MAX_BYTES = 16 * 1024 ** 2
HEX = re.compile(r"[0-9a-f]{64}")
RULES = {
    "rii-court-metadata/1": ("www.rechtsprechung-im-internet.de", "application/zip", r"/jportal/docs/bsjrs/jb-[A-Za-z0-9]+\.zip", ""),
    "hessen-court-metadata/1": ("www.lareda.hessenrecht.hessen.de", "application/json", r"/perma", r"d=[A-Za-z0-9_-]+"),
    "nrwe-court-metadata/1": ("nrwe.justiz.nrw.de", "text/html", r"/[\w%/.,()+~ -]+\.html", ""),
}
FIELDS = {"id","reference","court","court_short","event_date","document_type","published_at","ecli","source","norm_mentions"}
SOURCE_FIELDS = {"profile","url","media_type","content_sha256","observed_at","lineage_key","parser_version","canonical_decision_id"}


def validate_metadata(row):
    source = row["source"]
    host, media, path_pattern, query_pattern = RULES[source["profile"]]
    url = urlsplit(source["url"])
    if set(row) != FIELDS or set(source) != SOURCE_FIELDS:
        raise ValueError("invalid observation fields")
    limits = ((row["id"],200), (row["reference"],512), (row["court"],512), (row["court_short"],80),
              (source["lineage_key"],512), (source["canonical_decision_id"],200), (source["parser_version"],80))
    if not all(isinstance(value,str) and 0 < len(value) <= limit for value,limit in limits):
        raise ValueError("invalid court observation identity")
    if row["document_type"] is not None and not (isinstance(row["document_type"],str) and 0 < len(row["document_type"]) <= 100):
        raise ValueError("invalid official decision type")
    if row["ecli"] is not None and not (isinstance(row["ecli"],str) and row["ecli"].startswith("ECLI:") and len(row["ecli"]) <= 200):
        raise ValueError("invalid ECLI")
    if (url.scheme != "https" or url.netloc != host or not re.fullmatch(path_pattern,url.path) or ".." in url.path or
        not re.fullmatch(query_pattern,url.query) or url.fragment or source["media_type"] != media or not HEX.fullmatch(source["content_sha256"])):
        raise ValueError("invalid court observation source")
    if row["published_at"] is not None or row["norm_mentions"] != []:
        raise ValueError("metadata-only observation cannot assert publication or norm evidence")
    if date.fromisoformat(row["event_date"]).isoformat() != row["event_date"]:
        raise ValueError("invalid decision date")
    observed = datetime.fromisoformat(source["observed_at"].replace("Z","+00:00"))
    if observed.tzinfo is None or observed > datetime.now(timezone.utc):
        raise ValueError("invalid observation time")


def identity_keys(row):
    keys = {("id",row["id"]),("url",row["source"]["url"]),("lineage",row["source"]["lineage_key"]),
            ("hash",row["source"]["content_sha256"])}
    if row.get("ecli"):
        keys.add(("ecli",row["ecli"].casefold()))
    return keys


@lru_cache(maxsize=4)
def load_release(root, file_stamp, digest, base_digest):
    path = Path(root) / FILENAME
    if file_stamp[1] > MAX_BYTES:
        raise ValueError("observation projection exceeds bounded size")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("court observation projection hash mismatch")
    bundle = json.loads(data)
    rows = bundle["observations"]
    if bundle["profile"] != PROFILE or bundle["passage_database_sha256"] != base_digest or len(rows) > MAX_ROWS:
        raise ValueError("court observation projection does not match passage release")
    ids = [r["id"] for r in rows]
    if ids != sorted(set(ids)):
        raise ValueError("unordered or duplicate observations")
    for row in rows:
        if row["source"]["profile"] in RULES:
            validate_metadata(row)
    return rows
