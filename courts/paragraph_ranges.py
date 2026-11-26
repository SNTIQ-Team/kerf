"""Exact ranges retained when a source corrects the boundary of a former paragraph."""
import hashlib
import re

FIELD = "former_paragraph_range_v1"


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def valid_range(record, row, previous_anchor):
    if (not isinstance(record, dict) or row is None
            or set(record) != {"anchor", "start", "end", "source_sha256", "paragraph_sha256", "fragment_sha256"}
            or re.search(r"(?:^|[-:])rn-", previous_anchor, re.IGNORECASE)):
        return False
    target = record.get("anchor")
    start, end = record.get("start"), record.get("end")
    if not isinstance(target, str) or not target or "#" in target or target == previous_anchor:
        return False
    if target != row["anchor"] or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(row["text"]):
        return False
    if any(not isinstance(record.get(k), str) or not re.fullmatch(r"[0-9a-f]{64}", record[k])
           for k in ("source_sha256", "paragraph_sha256", "fragment_sha256")):
        return False
    return (record["source_sha256"] == row["content_hash"]
            and record["paragraph_sha256"] == row["text_hash"] == text_hash(row["text"])
            and record["fragment_sha256"] == text_hash(row["text"][start:end]))
